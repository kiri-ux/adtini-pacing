"""adtini · Pacing — Flask entrypoint.

Mirrors the quote builder's shape (`gunicorn app:app`, Jinja templates, the
shared adtini stylesheet) so the two tools read as one product.
"""
from __future__ import annotations

import datetime as dt
import functools
import io
import logging
import math
import os
import tempfile
import time
from pathlib import Path

from flask import (
    Flask,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    session as flask_session,
    url_for,
)
from sqlalchemy import func, select
from werkzeug.exceptions import HTTPException

import exports
import products
import sheets
import views
from config import get_settings
from db import session_scope
from ingest import loader
from models import (
    PACING_TYPES,
    Base,
    CampaignLink,
    Client,
    DailyDelivery,
    DayNote,
    IngestedFile,
    LineItem,
    Order,
    StrategyTerms,
)
from orderbook import adopt_unmatched_delivery, recompute_terms

logging.basicConfig(level=logging.INFO)

settings = get_settings()
app = Flask(__name__)
app.secret_key = settings.session_secret
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024 * 1024  # the drops are ~70MB

# The schema is owned by Alembic, applied by the deploy's pre-deploy command.
# Nothing here creates or alters tables: `create_all` only ever creates
# missing tables, so running it on a live database silently leaves new
# columns off and every page then fails on the missing column.


# --------------------------------------------------------------------------
# Login gate. One shared password, the same as the quote builder's.
# --------------------------------------------------------------------------
def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if settings.app_password and not flask_session.get("ok"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    if not settings.app_password:
        return redirect(url_for("overview"))
    error = None
    if request.method == "POST":
        if request.form.get("password") == settings.app_password:
            flask_session["ok"] = True
            return redirect(request.args.get("next") or url_for("overview"))
        error = "That password is not right."
    return render_template("login.html", error=error, build=settings.build)


@app.route("/logout")
def logout():
    flask_session.clear()
    return redirect(url_for("login"))


# --------------------------------------------------------------------------
# Template helpers
# --------------------------------------------------------------------------
def _real(value) -> float | None:
    """A value as a number, or None for anything that is not one.

    NaN counts as not one. A NaN reaching a template used to render as the
    literal "nan" in every money and count column, and `entry` went further
    and raised on `int(nan)`, which took the whole order page down with
    "cannot convert float NaN to integer". Formatting is the last place that
    should decide a page cannot be shown, so a value it cannot make sense of
    renders as a dash and the rest of the page still loads.
    """
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(value) or math.isinf(value) else value


def _num(value, places=0):
    value = _real(value)
    if value is None:
        return "—"
    if value < 0:
        return f"({abs(value):,.{places}f})"
    return f"{value:,.{places}f}"


@app.template_filter("num")
def num_filter(value, places=0):
    return _num(value, places)


@app.template_filter("money")
def money_filter(value, places=2):
    value = _real(value)
    if value is None:
        return "—"
    if value < 0:
        return f"(${abs(value):,.{places}f})"
    return f"${value:,.{places}f}"


@app.template_filter("pct")
def pct_filter(value, places=2):
    value = _real(value)
    if value is None:
        return "—"
    return f"{value * 100:,.{places}f}%"


@app.template_filter("entry")
def entry_filter(value):
    """A sold figure as it should sit in an input box.

    The engine works in floats, so 700000.0 comes back where a buyer typed
    700,000. Whole numbers lose the decimal; the rest keep two places, and an
    unset term stays an empty box rather than a 0 that reads as sold.
    """
    value = _real(value)
    if not value:
        return ""
    return f"{value:,.0f}" if value == int(value) else f"{value:,.2f}"


@app.template_filter("paceclass")
def paceclass_filter(value):
    """Bucket a pacing percent for colouring - see `pacing.engine.health`."""
    from pacing.engine import health

    return health(value)


@app.template_filter("metric")
def metric_filter(value, row):
    """Format by what the row paces on: impressions count, spend is money."""
    return money_filter(value) if getattr(row, "is_money", False) else _num(value)


@app.context_processor
def inject_globals():
    return {"build": settings.build, "today": dt.date.today()}


SWEEP_LOCK = Path(tempfile.gettempdir()) / "adtini-pacing-sweep.pid"
# A sweep is minutes of work. A lock older than this is wreckage from a
# restart, and the pid written in it may belong to something else by now.
SWEEP_LOCK_MAX_AGE = dt.timedelta(hours=6)


def _clear_sweep_lock() -> None:
    SWEEP_LOCK.unlink(missing_ok=True)


def _reap() -> None:
    """Clear finished children, so they stop looking like running ones.

    A subprocess nothing ever waits on becomes a zombie, and a zombie keeps
    its pid: `os.kill(pid, 0)` reports it alive for as long as the worker
    lives. That is how the lock below wedged the Data page - the sweep
    finished normally, the zombie stayed, `_sweep_running` answered True
    forever, and every Load button sat disabled with nothing on the page
    saying why. Clicking them did nothing at all.
    """
    try:
        while os.waitpid(-1, os.WNOHANG)[0] != 0:
            pass
    except (ChildProcessError, OSError):
        return


def _is_zombie(pid: int) -> bool:
    """Whether the pid is a finished child that has not been cleared yet.

    `_reap` handles the ordinary case. This covers the pid being reaped
    elsewhere - the worker runs threads, and only the thread that called
    `waitpid` sees the result.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    # The command sits in brackets and may itself contain spaces, so the
    # state is the first field after the closing bracket - not the third
    # whitespace-separated token.
    tail = stat.rsplit(") ", 1)[-1].split(maxsplit=1)
    return bool(tail) and tail[0] == "Z"


def _sweep_running() -> bool:
    """Whether a sweep started from here is still going.

    Answering True when nothing is running is not a harmless error: it
    disables the sweep and every per-file Load button, so the page offers no
    way to load anything and no way to say so. Every branch that cannot
    prove a sweep is alive clears the lock and answers False.
    """
    try:
        pid = int(SWEEP_LOCK.read_text().strip())
    except (OSError, ValueError):
        return False

    try:
        started = dt.datetime.fromtimestamp(SWEEP_LOCK.stat().st_mtime)
    except OSError:
        started = dt.datetime.now()
    if dt.datetime.now() - started > SWEEP_LOCK_MAX_AGE:
        _clear_sweep_lock()
        return False

    _reap()
    try:
        os.kill(pid, 0)          # signal 0 only tests that it exists
    except OSError:
        _clear_sweep_lock()
        return False
    if _is_zombie(pid):
        _clear_sweep_lock()
        return False
    return True


def _start_sweep(force: bool = False, only: str | None = None,
                 max_mb: float | None = None) -> str:
    """Kick off an S3 sweep outside this process, and return immediately.

    A sweep is minutes of work and hundreds of megabytes. Run inside the web
    worker it took the whole service down: the instance exceeded its memory
    limit, Render restarted it, and every open page got a 502. A separate
    process keeps that cost off the worker serving pages, and the worker is
    free again the moment this returns.

    The sweep is resumable, so a container restart mid-run loses nothing -
    files already loaded are skipped by their ETag on the next run.
    """
    import subprocess
    import sys

    if _sweep_running():
        return "A sweep is already running. Refresh to see files arrive below."

    command = [sys.executable, str(Path(__file__).parent / "scripts" / "ingest.py")]
    if force:
        command.append("--force")
    if only:
        command += ["--only", only]
    elif max_mb:
        command += ["--max-mb", str(max_mb)]
    try:
        # Output is inherited, not discarded, so the sweep's own logging lands
        # in the service log. A sweep that cannot reach the bucket writes no
        # file rows at all, so with its output thrown away the page would sit
        # empty with nothing anywhere saying why.
        process = subprocess.Popen(
            command, cwd=str(Path(__file__).parent), start_new_session=True
        )
        SWEEP_LOCK.write_text(str(process.pid))
    except Exception as exc:
        app.logger.exception("could not start the sweep")
        return f"Could not start the sweep: {exc}"
    if only:
        return f"Loading {only.rsplit('/', 1)[-1]}. Refresh to see it arrive below."
    return (
        "Sweep started. It runs in the background - refresh this page to see "
        "files arrive below. A delivery file takes about a minute."
    )


def _spool(upload) -> str:
    """Write an upload to a temp file, unwrapping it if it arrived zipped.

    The caller deletes the file.
    """
    import gzip
    import shutil
    import tempfile
    import zipfile

    raw = tempfile.NamedTemporaryFile(suffix=".raw", delete=False)
    upload.save(raw)
    raw.close()

    lowered = upload.filename.lower()
    if not lowered.endswith((".gz", ".zip")):
        return raw.name

    out = tempfile.NamedTemporaryFile(suffix=".csv", delete=False)
    try:
        if lowered.endswith(".gz"):
            with gzip.open(raw.name, "rb") as source:
                shutil.copyfileobj(source, out)
        else:
            with zipfile.ZipFile(raw.name) as archive:
                names = [
                    n for n in archive.namelist()
                    if n.lower().endswith(".csv") and not n.startswith("__MACOSX/")
                ]
                if not names:
                    raise ValueError(f"{upload.filename} contains no CSV")
                with archive.open(names[0]) as source:
                    shutil.copyfileobj(source, out)
        out.close()
        return out.name
    finally:
        os.unlink(raw.name)


def _as_of():
    raw = request.args.get("as_of")
    if not raw:
        return None
    try:
        return dt.date.fromisoformat(raw)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------
PAGE_SIZE = 150


@app.route("/")
@login_required
def overview():
    needs = request.args.get("needs")
    with session_scope() as db:
        rows = views.overview(
            db,
            as_of=_as_of(),
            buyer=request.args.get("buyer") or None,
            market=request.args.get("market") or None,
            pacing_type=request.args.get("pacing_type") or None,
            query=request.args.get("q") or None,
            include_ended=request.args.get("ended") == "1",
            needs_terms={"1": True, "0": False}.get(needs),
            include_non_io=request.args.get("allorders") == "1",
        )
        options = views.filter_options(db)
        as_of = rows[0].as_of if rows else (views.latest_delivery_date(db) or dt.date.today())

        # 2,800 orders is a 2MB page, so the table is paged. Filters apply
        # first, which is how a buyer actually gets to their own list.
        try:
            page = max(1, int(request.args.get("page", 1)))
        except ValueError:
            page = 1
        total_rows = len(rows)
        pages = max(1, -(-total_rows // PAGE_SIZE))
        page = min(page, pages)
        window = rows[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]

        return render_template(
            "overview.html",
            rows=window,
            page_total=views.page_total(window),
            total_rows=total_rows,
            page=page,
            pages=pages,
            options=options,
            as_of=as_of,
            args=request.args,
            pacing_types=PACING_TYPES,
        )


# The order page's views. The template's tab bar is built from this, so a
# tab cannot be added to one and forgotten in the other - which is exactly
# how renaming Campaign to Product left the new link falling back to Order.
# What a product can be sold on, as the page offers it. Performance Max
# paces the client's budget against the client's cost, which is a kind of
# spend - it does not need a third name in the picker, and a buyer choosing
# "Ad spend" on a Performance Max line must not quietly turn it into a
# platform-spend line and lose the client-cost gross.
PACING_CHOICES = (
    ("impression", "Impressions"),
    ("click", "Ad spend"),
)


def _chosen_pacing_type(chosen: str, current: str | None) -> str | None:
    """Map the two choices onto the three the engine works in."""
    if chosen == "impression":
        return "impression"
    if chosen == "click":
        return "event" if current == "event" else "click"
    return None


@app.route("/orders/<int:order_id>")
@login_required
def order_detail(order_id: int):
    with session_scope() as db:
        grid_range = request.args.get("grid") or "month"
        if grid_range not in dict(views.GRID_RANGES):
            grid_range = "month"
        view = views.order_view(
            db, order_id, as_of=_as_of(), grid_range=grid_range
        )
        if view is None:
            abort(404)
        return render_template(
            "order.html",
            v=view,
            t=view.total,
            chart=views.chart_series(view),
            product_charts=views.product_charts(view),
            blocks=views.strategy_blocks(db, view),
            pacing_choices=PACING_CHOICES,
            notes=views.day_log(db, order_id),
            grid_range=grid_range,
            grid_ranges=views.GRID_RANGES,
            linking=views.linking_view(
                db, view.order, as_of=view.as_of,
                daily=view.daily_by_line_item,
            ),
            lineitem_names={li.id: li.name for li in view.order.line_items},
            pacing_types=PACING_TYPES,
        )


@app.route("/orders/<int:order_id>/save", methods=["POST"])
@login_required
def order_save(order_id: int):
    """Save the sold terms a buyer typed into the order's sheet."""
    form = request.form

    def as_date(key):
        raw = (form.get(key) or "").strip()
        try:
            return dt.date.fromisoformat(raw) if raw else None
        except ValueError:
            return None

    def as_float(key):
        raw = (form.get(key) or "").strip().replace(",", "").replace("$", "")
        try:
            return float(raw) if raw else None
        except ValueError:
            return None

    with session_scope() as db:
        order = db.get(Order, order_id)
        if order is None:
            abort(404)

        order.pacing_type = form.get("pacing_type") or order.pacing_type
        order.start_date = as_date("start_date")
        order.end_date = as_date("end_date")
        order.buyer = (form.get("buyer") or "").strip() or None
        order.notes = (form.get("notes") or "").strip() or None
        order.paused = form.get("paused") == "on"
        order.last_adjusted_on = as_date("last_adjusted_on")
        order.adjustment_note = (form.get("adjustment_note") or "").strip() or None
        # Saving by hand means these are the buyer's now: the next orders
        # import leaves them alone. Budgets get adjusted mid-flight and the
        # adjustment has to survive.
        order.terms_locked = True

        for item in order.line_items:
            prefix = f"li-{item.id}-"
            if prefix + "name" not in form:
                continue
            item.name = (form.get(prefix + "name") or item.name).strip()
            kind = _chosen_pacing_type(
                (form.get(prefix + "pacing_type") or "").strip(), item.pacing_type
            )
            # Stored only when it differs from the order's, so a line item
            # keeps following the order when the order changes.
            item.pacing_type = kind if kind and kind != order.pacing_type else None
            item.start_date = as_date(prefix + "start_date")
            item.end_date = as_date(prefix + "end_date")
            for field_name in (
                "monthly_impressions", "total_impressions", "goal_cpm",
                "monthly_spend", "total_spend", "goal_cpc",
                "client_monthly_budget", "client_total_budget",
                "google_monthly_spend", "google_total_spend", "goal_cpe",
                "monthly_events", "total_events",
            ):
                setattr(item, field_name, as_float(prefix + field_name))
            item.terms_locked = True

    flash("Saved.")
    return redirect(url_for("order_detail", order_id=order_id))


@app.route("/orders/<int:order_id>/unlock", methods=["POST"])
@login_required
def order_unlock(order_id: int):
    """Hand an order back to the orders file.

    The next import overwrites its terms with whatever the file says.
    """
    with session_scope() as db:
        order = db.get(Order, order_id)
        if order is None:
            abort(404)
        order.terms_locked = False
        for item in order.line_items:
            item.terms_locked = False
    flash("Unlocked. The next orders import will overwrite these terms.")
    return redirect(url_for("order_detail", order_id=order_id))


@app.route("/orders/<int:order_id>/line-items", methods=["POST"])
@login_required
def line_item_add(order_id: int):
    with session_scope() as db:
        order = db.get(Order, order_id)
        if order is None:
            abort(404)
        db.add(
            LineItem(
                order_id=order.id,
                name=(request.form.get("name") or "New line item").strip(),
                sort_order=len(order.line_items),
            )
        )
    return redirect(url_for("order_detail", order_id=order_id))


@app.route("/orders/<int:order_id>/line-items/<int:line_item_id>/delete", methods=["POST"])
@login_required
def line_item_delete(order_id: int, line_item_id: int):
    with session_scope() as db:
        item = db.get(LineItem, line_item_id)
        if item and item.order_id == order_id:
            db.delete(item)
    return redirect(url_for("order_detail", order_id=order_id))


# --------------------------------------------------------------------------
# The day log
# --------------------------------------------------------------------------
@app.route("/orders/<int:order_id>/notes", methods=["POST"])
@login_required
def note_add(order_id: int):
    """Write down what happened on a day.

    A dip in the grid is unexplainable a month later without it, and the
    hand-kept sheets have always carried one.
    """
    raw_date = (request.form.get("date") or "").strip()
    body = (request.form.get("body") or "").strip()
    author = (request.form.get("author") or "").strip() or None
    line_item_id = request.form.get("line_item_id", type=int)

    if not body:
        flash("Write something first.")
        return redirect(url_for("order_detail", order_id=order_id))

    try:
        on = dt.date.fromisoformat(raw_date) if raw_date else dt.date.today()
    except ValueError:
        on = dt.date.today()

    with session_scope() as db:
        if db.get(Order, order_id) is None:
            abort(404)
        if line_item_id is not None:
            item = db.get(LineItem, line_item_id)
            if item is None or item.order_id != order_id:
                line_item_id = None
        db.add(
            DayNote(
                order_id=order_id,
                line_item_id=line_item_id,
                date=on,
                body=body,
                author=author,
            )
        )

    flash(f"Note saved for {on:%-d %b}.")
    return redirect(url_for("order_detail", order_id=order_id) + "#daylog")


@app.route("/orders/<int:order_id>/notes/<int:note_id>/delete", methods=["POST"])
@login_required
def note_delete(order_id: int, note_id: int):
    with session_scope() as db:
        note = db.get(DayNote, note_id)
        if note and note.order_id == order_id:
            db.delete(note)
    return redirect(url_for("order_detail", order_id=order_id) + "#daylog")


# --------------------------------------------------------------------------
# Strategies
# --------------------------------------------------------------------------
@app.route("/orders/<int:order_id>/strategies/save", methods=["POST"])
@login_required
def strategies_save(order_id: int):
    """Save the sold split across targeting.

    This is the only place the split exists. The orders export stops at the
    product line item and the delivery feed only says what ran, so nothing
    else can say that Meta's 128,000 monthly was 10,000 of retargeting and
    118,000 of categories.
    """
    form = request.form

    def as_float(key):
        raw = (form.get(key) or "").strip().replace(",", "").replace("$", "")
        try:
            return float(raw) if raw else None
        except ValueError:
            return None

    with session_scope() as db:
        order = db.get(Order, order_id)
        if order is None:
            abort(404)
        for term in db.execute(
            select(StrategyTerms).where(StrategyTerms.order_id == order_id)
        ).scalars():
            prefix = f"st-{term.id}-"
            if prefix + "label" not in form:
                continue
            label = (form.get(prefix + "label") or "").strip()
            if label and label != term.label:
                term.label = label
                term.match_key = sheets.match_key(label)
            term.monthly_target = as_float(prefix + "monthly")
            term.total_target = as_float(prefix + "total")
            term.rate = as_float(prefix + "rate")

    flash("Saved.")
    return redirect(url_for("order_detail", order_id=order_id))


@app.route("/orders/<int:order_id>/strategies", methods=["POST"])
@login_required
def strategy_add(order_id: int):
    """Add a strategy a buyer bought that no sheet or export mentions.

    Extra targeting gets added mid-flight, and the order data will never
    catch up with it.
    """
    line_item_id = request.form.get("line_item_id", type=int)
    label = (request.form.get("label") or "").strip()

    with session_scope() as db:
        item = db.get(LineItem, line_item_id) if line_item_id else None
        if item is None or item.order_id != order_id:
            abort(404)
        if not label:
            flash("Give the strategy a name.")
            return redirect(url_for("order_detail", order_id=order_id))

        # Prefixed with the product's own code, so it reads like the rest and
        # finds its product again on the next page load.
        code = products.abbreviation(item.product)
        if not label.lower().startswith(code.lower()):
            label = f"{code} - {label}"

        clash = db.execute(
            select(StrategyTerms).where(
                StrategyTerms.line_item_id == item.id,
                StrategyTerms.label == label,
            )
        ).scalar_one_or_none()
        if clash is not None:
            flash(f"{label} is already on this order.")
            return redirect(url_for("order_detail", order_id=order_id))

        count = db.execute(
            select(func.count())
            .select_from(StrategyTerms)
            .where(StrategyTerms.order_id == order_id)
        ).scalar()
        db.add(
            StrategyTerms(
                order_id=order_id,
                line_item_id=item.id,
                label=label,
                match_key=sheets.match_key(label),
                sort_order=count or 0,
                source="added by hand",
                added_by_hand=True,
            )
        )

    flash(f"Added {label}.")
    return redirect(url_for("order_detail", order_id=order_id))


@app.route("/orders/<int:order_id>/strategies/seed", methods=["POST"])
@login_required
def strategy_seed(order_id: int):
    """Draft the sold split from the shares that are actually running.

    What is running is knowable without anyone typing it; what is missing is
    the *sold* split - how much of the product each targeting was bought for.
    Nothing can derive that, but the running shares are the obvious first
    draft of it, and a buyer correcting three numbers beats a buyer entering
    ten from nothing.

    Drafted, not decided: the rows land editable and a buyer is expected to
    fix them. Existing rows are left alone, so this fills gaps rather than
    overwriting anybody's work.
    """
    line_item_id = request.form.get("line_item_id", type=int)

    with session_scope() as db:
        item = db.get(LineItem, line_item_id) if line_item_id else None
        if item is None or item.order_id != order_id:
            abort(404)

        grid_range = request.args.get("grid") or "month"
        if grid_range not in dict(views.GRID_RANGES):
            grid_range = "month"
        view = views.order_view(
            db, order_id, as_of=_as_of(), grid_range=grid_range
        )
        if view is None:
            abort(404)
        block = next(
            (
                b
                for b in views.strategy_blocks(db, view)
                if b.line_item.id == item.id
            ),
            None,
        )
        running = [o for o in (block.observed if block else []) if o.delivered > 0]
        if not running:
            flash("Nothing is running under this product yet to build a split from.")
            return redirect(url_for("order_detail", order_id=order_id))

        taken = {
            term.label
            for term in db.execute(
                select(StrategyTerms).where(StrategyTerms.line_item_id == item.id)
            ).scalars()
        }
        count = db.execute(
            select(func.count())
            .select_from(StrategyTerms)
            .where(StrategyTerms.order_id == order_id)
        ).scalar() or 0

        monthly = item.monthly_impressions or item.monthly_spend or item.client_monthly_budget
        total = item.total_impressions or item.total_spend or item.client_total_budget
        rate = item.goal_cpm or item.goal_cpc or item.goal_cpe

        added = 0
        for observed in running:
            if observed.label in taken:
                continue
            db.add(
                StrategyTerms(
                    order_id=order_id,
                    line_item_id=item.id,
                    label=observed.label,
                    match_key=observed.match_key,
                    # The product's sold figures, apportioned by what each
                    # targeting is actually taking.
                    monthly_target=(monthly * observed.share) if monthly else None,
                    total_target=(total * observed.share) if total else None,
                    rate=rate,
                    sort_order=count + added,
                    source="drafted from delivery",
                    added_by_hand=True,
                )
            )
            added += 1

    if added:
        flash(f"Drafted {added} strategies from what is running. Check the numbers.")
    else:
        flash("Every running strategy already has a row.")
    return redirect(url_for("order_detail", order_id=order_id))


@app.route(
    "/orders/<int:order_id>/strategies/<int:strategy_id>/delete", methods=["POST"]
)
@login_required
def strategy_delete(order_id: int, strategy_id: int):
    with session_scope() as db:
        term = db.get(StrategyTerms, strategy_id)
        if term and term.order_id == order_id:
            db.delete(term)
    return redirect(url_for("order_detail", order_id=order_id))


# --------------------------------------------------------------------------
# Campaign linking
# --------------------------------------------------------------------------
@app.route("/orders/<int:order_id>/link", methods=["POST"])
@login_required
def campaign_link(order_id: int):
    """Set which DSP campaigns a line item was bought on.

    The whole set arrives at once, so unticking one removes it. A line item
    can carry several: a Meta line split into an impressions campaign and a
    leads campaign is ordinary, and reading only one of them understates the
    line by whatever the other ran.

    The other direction stays one-to-one. A campaign already on another line
    item is moved here rather than shared, because two line items reading one
    campaign would count its delivery twice on the same order - which is a
    worse answer than the zero it started with.
    """
    line_item_id = request.form.get("line_item_id", type=int)
    picked = [
        raw.strip() for raw in request.form.getlist("campaign") if raw.strip()
    ]
    verified = request.form.get("ops_verified") == "on"
    who = (request.form.get("linked_by") or "").strip() or None

    with session_scope() as db:
        item = db.get(LineItem, line_item_id) if line_item_id else None
        if item is None or item.order_id != order_id:
            abort(404)

        existing = {
            (link.data_source, link.campaign_id): link
            for link in db.execute(
                select(CampaignLink).where(CampaignLink.line_item_id == item.id)
            ).scalars()
        }

        wanted: set[tuple[str, str]] = set()
        for raw in picked:
            source, _, campaign_id = raw.partition("\u241f")
            if campaign_id:
                wanted.add((source, campaign_id))

        for key, link in existing.items():
            if key not in wanted:
                db.delete(link)

        for source, campaign_id in sorted(wanted):
            link = existing.get((source, campaign_id))
            if link is not None:
                link.ops_verified = verified
                link.linked_by = who or link.linked_by
                continue

            # It may be on another line item. Move it rather than copy it.
            claimed = db.execute(
                select(CampaignLink).where(
                    CampaignLink.data_source == source,
                    CampaignLink.campaign_id == campaign_id,
                )
            ).scalar_one_or_none()
            if claimed is not None:
                db.delete(claimed)
                db.flush()

            name = db.execute(
                select(func.max(DailyDelivery.campaign_name)).where(
                    DailyDelivery.data_source == source,
                    DailyDelivery.campaign_id == campaign_id,
                )
            ).scalar()
            db.add(
                CampaignLink(
                    line_item_id=item.id,
                    data_source=source,
                    campaign_id=campaign_id,
                    campaign_name=name,
                    ops_verified=verified,
                    linked_by=who,
                )
            )

        removed = len(set(existing) - wanted)
        added = len(wanted - set(existing))

    if not wanted and not removed:
        flash("Nothing picked.")
    elif not wanted:
        flash("Link removed.")
    else:
        parts = []
        if added:
            parts.append(f"{added} campaign{'' if added == 1 else 's'} linked")
        if removed:
            parts.append(f"{removed} removed")
        flash(", ".join(parts) + "." if parts else "Updated.")
    return redirect(url_for("order_detail", order_id=order_id))


@app.route("/orders/<int:order_id>/link/<int:line_item_id>/clear", methods=["POST"])
@login_required
def campaign_unlink(order_id: int, line_item_id: int):
    with session_scope() as db:
        item = db.get(LineItem, line_item_id)
        if item is None or item.order_id != order_id:
            abort(404)
        for link in db.execute(
            select(CampaignLink).where(CampaignLink.line_item_id == item.id)
        ).scalars():
            db.delete(link)
    flash("Link removed.")
    return redirect(url_for("order_detail", order_id=order_id))


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------
@app.route("/data", methods=["POST"])
@login_required
def data_action():
    """Do the thing, then redirect.

    Rendering the page straight from the POST left the browser offering to
    resubmit on every refresh - and the page tells you to refresh. Accepting
    that offer would start a second sweep on top of the first, which is two
    ingests plus the worker and back over the memory limit.
    """
    message = None
    action = request.form.get("action")
    if action == "ingest":
        message = _start_sweep(
            force=request.form.get("force") == "on",
            max_mb=float(request.form["max_mb"]) if request.form.get("max_mb") else None,
        )
    elif action == "load-one":
        message = _start_sweep(only=request.form.get("key"), force=True)
    elif action == "upload":
        upload = request.files.get("file")
        if upload and upload.filename:
            # The same filename rule the S3 sweep uses, so an uploaded
            # file behaves exactly as it would from the bucket.
            kind = loader.classify(upload.filename)
            if kind is None:
                message = (
                    f"{upload.filename} is not recognised. Delivery files "
                    "start with 'client-serve' and order files with 'orders'."
                )
            else:
                # To disk, not through memory: a 69MB drop read whole
                # costs more than a worker has.
                path = _spool(upload)
                try:
                    with session_scope() as db:
                        if kind == loader.ORDERS:
                            with open(path, "rb") as handle:
                                imported, frame = loader.load_orders_bytes(
                                    db, handle.read(), upload.filename
                                )
                            message = f"{upload.filename}: {imported.summary()}."
                            if frame.unmapped_note:
                                message += (
                                    f" Unrecognised columns: {frame.unmapped_note}"
                                )
                        else:
                            written, _ = loader.load_delivery_file(
                                db, path, upload.filename
                            )
                            message = (
                                f"Loaded {written:,} delivery rows "
                                f"from {upload.filename}."
                            )
                finally:
                    if os.path.exists(path):
                        os.unlink(path)
        else:
            message = "Pick a file first."
    elif action == "adopt":
        with session_scope() as db:
            message = adopt_unmatched_delivery(db).summary()
    elif action == "recompute":
        with session_scope() as db:
            message = recompute_terms(db).summary()
    elif action == "draft-splits":
        with session_scope() as db:
            message = views.draft_missing_splits(db, as_of=_as_of()).summary()

    if message:
        flash(message)
    return redirect(url_for("data_page"))


@app.route("/data")
@login_required
def data_page():
    with session_scope() as db:
        files = list(
            db.execute(
                select(IngestedFile).order_by(IngestedFile.ingested_at.desc()).limit(25)
            ).scalars()
        )
        latest = views.latest_delivery_date(db)
        loaded = {row.s3_key: row for row in db.execute(select(IngestedFile)).scalars()}
        counts = {
            "clients": db.execute(select(func.count()).select_from(Client)).scalar(),
            "orders": db.execute(select(func.count()).select_from(Order)).scalar(),
        }
    bucket_files = []
    try:
        from ingest import s3

        for obj in s3.list_objects():
            record = loaded.get(obj.key)
            bucket_files.append({
                "key": obj.key,
                "name": obj.key.rsplit("/", 1)[-1],
                "kind": loader.classify(obj.key) or "unknown",
                "mb": obj.size / 1e6,
                "status": (record.status if record and record.etag == obj.etag
                           else ("stale" if record else "")),
                "rows": record.rows_written if record else None,
            })
    except Exception as exc:
        app.logger.warning("could not list the bucket: %s", exc)
        bucket_files = []

    return render_template(
        "data.html",
        files=files,
        bucket_files=bucket_files,
        latest=latest,
        counts=counts,
        sweep_running=_sweep_running(),
        bucket=settings.s3_bucket,
        prefix=settings.s3_prefix,
    )


# --------------------------------------------------------------------------
# Exports
# --------------------------------------------------------------------------
@app.route("/export/overview.xlsx")
@login_required
def export_overview():
    with session_scope() as db:
        rows = views.overview(
            db,
            as_of=_as_of(),
            include_ended=request.args.get("ended") == "1",
            include_non_io=request.args.get("allorders") == "1",
        )
        book = exports.overview_workbook(rows)
    return _xlsx(book, "adtini-pacing-overview")


@app.route("/export/orders/<int:order_id>.xlsx")
@login_required
def export_order(order_id: int):
    with session_scope() as db:
        grid_range = request.args.get("grid") or "month"
        if grid_range not in dict(views.GRID_RANGES):
            grid_range = "month"
        view = views.order_view(
            db, order_id, as_of=_as_of(), grid_range=grid_range
        )
        if view is None:
            abort(404)
        book = exports.order_workbook(view)
        name = f"{view.client.name}-{view.order.name}"
    return _xlsx(book, name)


def _xlsx(book, stem: str) -> Response:
    buffer = io.BytesIO()
    book.save(buffer)
    buffer.seek(0)
    safe = "".join(c if c.isalnum() or c in "-_ " else "-" for c in stem)[:80].strip()
    return Response(
        buffer.read(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{safe}.xlsx"'},
    )


@app.errorhandler(500)
@app.errorhandler(Exception)
def on_error(error):
    """Log the traceback, and say something a person can act on.

    Flask's default 500 page says only that something went wrong, which is
    nothing to go on when the app is on Render and the reader is not.
    """
    if isinstance(error, HTTPException):
        return error
    app.logger.exception("unhandled error on %s", request.path)
    return render_template("error.html", detail=str(error)), 500


@app.before_request
def _mark_start():
    request._started = time.perf_counter()


@app.after_request
def _server_timing(response):
    """How long the server actually took, in the browser's own timing panel.

    A page can feel slow for reasons the server never sees - a cold
    instance, half a CPU, the network - and guessing between those wastes
    more time than measuring them.
    """
    started = getattr(request, "_started", None)
    if started is not None:
        response.headers["Server-Timing"] = (
            f"app;dur={(time.perf_counter() - started) * 1000:.0f}"
        )
    return response


@app.route("/healthz")
def healthz():
    """Liveness, plus whether the schema is actually current.

    A health check that only proves the process is up would have reported
    green through the outage that made every page 500.
    """
    from sqlalchemy import inspect

    from db import engine

    try:
        tables = set(inspect(engine).get_table_names())
    except Exception as exc:
        return {"ok": False, "database": f"unreachable: {exc}"}, 503

    expected = {t.name for t in Base.metadata.sorted_tables}
    missing = sorted(expected - tables)
    if missing:
        return {
            "ok": False,
            "database": "reachable",
            "missing_tables": missing,
            "hint": "run `alembic upgrade head`",
        }, 503

    try:
        with session_scope() as db:
            db.execute(select(Order).limit(1)).first()
    except Exception as exc:
        # Almost always a column the models have and the database does not.
        return {
            "ok": False,
            "database": "reachable",
            "schema": f"out of date: {exc}",
            "hint": "run `alembic upgrade head`",
        }, 503

    return {"ok": True}


if __name__ == "__main__":
    # Local convenience: bring the schema up before serving, which the deploy
    # does with its own pre-deploy step.
    import subprocess

    subprocess.run(["alembic", "upgrade", "head"], check=False)
    app.run(debug=True, port=5000)
