"""adtini · Pacing — Flask entrypoint.

Mirrors the quote builder's shape (`gunicorn app:app`, Jinja templates, the
shared adtini stylesheet) so the two tools read as one product.
"""
from __future__ import annotations

import datetime as dt
import functools
import hmac
import io
import logging
import math
import os
import threading
import tempfile
import time
from pathlib import Path

from flask import (
    Flask,
    Response,
    abort,
    flash,
    make_response,
    redirect,
    render_template,
    request,
    session as flask_session,
    url_for,
)
from sqlalchemy import func, or_, select
from sqlalchemy.exc import OperationalError
from werkzeug.exceptions import HTTPException

import details
import exports
import pagecache
import products
import schedule
import sheets
import views
from config import get_settings
from db import engine, session_scope
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
    SaAlert,
    StrategyTerms,
    WeeklyCheck,
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


app.jinja_env.globals["alert_labels"] = views.SA_ALERT_LABELS


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
def entry_filter(value, whole: bool = False):
    """A sold figure as it should sit in an input box.

    The engine works in floats, so 700000.0 comes back where a buyer typed
    700,000. Whole numbers lose the decimal; the rest keep two places, and an
    unset term stays an empty box rather than a 0 that reads as sold.
    """
    value = _real(value)
    if not value:
        return ""
    if whole:  # impressions: no fractions of one
        return f"{round(value):,.0f}"
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
                 max_mb: float | None = None, kind: str | None = None) -> str:
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
    if kind:
        command += ["--kind", kind]
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


def cached_page(view):
    """Serve a GET page from memory until the data under it changes.

    See `pagecache`. Off under tests, which change the database directly
    between requests and share one process across many databases.
    """
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if app.config.get("TESTING") or flask_session.get("_flashes"):
            return view(*args, **kwargs)
        with session_scope() as db:
            stamp = pagecache.data_stamp(db)
        page_key = pagecache.key(request.full_path, stamp)
        body = pagecache.get(page_key)
        if body is not None:
            return _timed(Response(body, mimetype="text/html"), "hit")
        # New data landed since this page was built (the nightly load drops
        # files one at a time for most of an hour). Show the last copy now
        # and build the new one behind it.
        if _warmer_started and not request.headers.get(pagecache.REFRESH_HEADER):
            body = pagecache.stale(request.full_path)
            if body is not None:
                pagecache.prefetch([request.full_path])
                return _timed(Response(body, mimetype="text/html"), "stale")
        # A page is built once at a time. Two pacing tables at once (a
        # visitor and the warmer) were enough for the database to drop the
        # connection.
        with pagecache.render_lock(request.path):
            body = pagecache.get(page_key)
            if body is not None:
                return _timed(Response(body, mimetype="text/html"), "hit")
            started = time.monotonic()
            try:
                response = make_response(view(*args, **kwargs))
            except OperationalError:
                # A dropped connection: start the pool again and try once more.
                app.logger.warning("database connection dropped, retrying %s", request.path)
                engine.dispose()
                response = make_response(view(*args, **kwargs))
        if response.status_code == 200 and response.mimetype == "text/html":
            pagecache.put(page_key, response.get_data())
        elapsed = time.monotonic() - started
        app.logger.info("rendered %s in %.2fs", request.full_path, elapsed)
        return _timed(response, "miss", elapsed)

    return wrapped


def _timed(response, how: str, seconds: float = 0.0):
    """Say in the response how the page was served, so a slow load can be
    told apart in the browser's network panel: from memory or built."""
    response.headers["Server-Timing"] = f'page;desc="{how}";dur={seconds * 1000:.0f}'
    return response


@app.after_request
def _drop_cache_on_write(response):
    if request.method == "POST" and response.status_code < 500:
        order_id = (request.view_args or {}).get("order_id")
        pagecache.edited(order_id)
        if order_id and _warmer_started:
            # The redirect back renders the order page itself (uncached, it
            # carries the flash); its Budgeting grids are fetched right after.
            pagecache.prefetch([url_for("order_budgeting", order_id=order_id)])
    return response


_warmer_started = False


@app.before_request
def _start_warmer():
    global _warmer_started
    if _warmer_started or app.config.get("TESTING"):
        return
    _warmer_started = True
    pagecache.start_warmer(app, paths=_warm_paths)


def _warm_paths(full: bool = True) -> list[str]:
    """What the warmer builds whenever data lands or the day turns: the
    pacing table, Home, and the pilot buyers' My Day for every weekday."""
    paths = ["/", url_for("home")] + [
        url_for("my_day", buyer=b, day=d)
        for b in settings.pilot_buyers for d in range(5)
    ]
    # And the pilot buyers' running orders, page and Budgeting both, so a
    # refresh after a deploy or the morning load is served from memory.
    # Only when data or the day turned (or after a deploy): an edit already
    # re-warms the order it was made on, and hundreds of rebuilds after every
    # save would compete with the buyer making them.
    if full and settings.pilot_buyers:
        from models import Order

        today = dt.date.today()
        with session_scope() as db:
            orders = db.execute(
                select(Order.id).where(
                    Order.active.is_(True),
                    or_(Order.end_date.is_(None), Order.end_date >= today),
                    or_(*[
                        func.lower(Order.buyer).like(f"{b.lower()}%")
                        for b in settings.pilot_buyers
                    ]),
                ).order_by(Order.id)
            ).scalars().all()
        for order_id in orders:
            paths.append(url_for("order_detail", order_id=order_id))
            paths.append(url_for("order_budgeting", order_id=order_id))
    return paths


def _back(order_id: int, anchor: str = ""):
    """Back to the page the form was posted from.

    The split and the day log are edited from the order page and from a
    product's details grid both; a form posted from the grid says which
    product, and lands back on it.
    """
    line_item_id = request.form.get("back_li", type=int)
    if line_item_id:
        return redirect(
            url_for("details_page", order_id=order_id, line_item_id=line_item_id)
            + anchor
        )
    return redirect(url_for("order_detail", order_id=order_id) + anchor)


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------
# Rows per page. Fifty is about a screen and a half of scrolling, and the
# page a buyer actually works is the top of a sorted list rather than all
# six hundred of it. Bigger pages are a click away.
PAGE_SIZE = 50
PAGE_SIZES = (25, 50, 100, 250)


@app.route("/")
@login_required
@cached_page
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
        try:
            per_page = int(request.args.get("rows", PAGE_SIZE))
        except ValueError:
            per_page = PAGE_SIZE
        if per_page not in PAGE_SIZES:
            per_page = PAGE_SIZE
        total_rows = len(rows)
        pages = max(1, -(-total_rows // per_page))
        page = min(page, pages)
        window = rows[(page - 1) * per_page : page * per_page]

        # A search that matched orders the filters then dropped says so.
        # Silently returning fewer rows than exist is how an order somebody
        # worked on all week disappears with nothing saying where it went.
        hidden = views.hidden_matches(
            db,
            request.args.get("q") or "",
            shown={r.order.id for r in rows},
            as_of=as_of,
            include_ended=request.args.get("ended") == "1",
            include_non_io=request.args.get("allorders") == "1",
        )

        return render_template(
            "overview.html",
            rows=window,
            hidden=hidden,
            page_total=views.page_total(window),
            total_rows=total_rows,
            page=page,
            pages=pages,
            per_page=per_page,
            page_sizes=PAGE_SIZES,
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


# The goal boxes on the order page for each pacing type: monthly, total, rate.
GOAL_FIELDS = {
    "impression": ("monthly_impressions", "total_impressions", "goal_cpm"),
    "click": ("monthly_spend", "total_spend", "goal_cpc"),
    "event": ("client_monthly_budget", "client_total_budget", "goal_cpe"),
}


def _chosen_pacing_type(chosen: str, current: str | None) -> str | None:
    """Map the two choices onto the three the engine works in."""
    if chosen == "impression":
        return "impression"
    if chosen == "click":
        return "event" if current == "event" else "click"
    return None


@app.route("/orders/<int:order_id>")
@login_required
@cached_page
def order_detail(order_id: int):
    with session_scope() as db:
        grid_range = request.args.get("grid") or "month"
        if grid_range not in dict(views.GRID_RANGES):
            grid_range = "month"
        view = views.order_view(
            db, order_id, as_of=_as_of(), grid_range=grid_range
        )
        if view is None:
            # People type and share the order number (56935), not our row id.
            # Row ids win when both exist, so existing links keep working.
            match = db.execute(
                select(Order.id)
                .where(Order.external_order_id == str(order_id))
                .order_by(Order.id.desc())
            ).scalars().first()
            if match is not None:
                return redirect(url_for("order_detail", order_id=match, **request.args))
            abort(404)
        if _warmer_started:
            paced = [li for li in view.order.line_items if products.is_paced(li.product)]
            # The Budgeting expand first: it is what gets opened next.
            pagecache.prefetch(
                [url_for("order_budgeting", order_id=order_id)]
                + [url_for("details_page", order_id=order_id, line_item_id=li.id)
                   for li in paced[:5]]
            )
        return render_template(
            "order.html",
            sa_alerts=views.sa_alerts(db, [li.external_id for li in view.order.line_items]),
            v=view,
            t=view.total,
            pacing_choices=PACING_CHOICES,
            notes=views.day_log(db, order_id),
            linking=views.linking_view(
                db, view.order, as_of=view.as_of,
                daily=view.daily_by_line_item,
            ),
            lineitem_names={li.id: li.name for li in view.order.line_items},
            pacing_types=PACING_TYPES,
        )


@app.route("/orders/<int:order_id>/budgeting")
@login_required
@cached_page
def order_budgeting(order_id: int):
    """The Budgeting expand: the details sheet for each product still running.

    Fetched when the expand opens, so the order page itself never waits on
    a grid per product.
    """
    with session_scope() as db:
        view = views.order_view(db, order_id, as_of=_as_of())
        if view is None:
            abort(404)
        grids = [
            g for g in (
                details.details_view(db, order_id, r.line_item_id, as_of=view.as_of)
                for r in (view.open_rows or view.rows)
                if r.line_item_id and r.line_item
                and products.is_paced(r.line_item.product)
            ) if g is not None
        ][:20]
        show_all, month = _grid_window()
        months = sorted({m for g in grids for m in g.months_in_flight})
        if months and month not in months:
            current = view.as_of.replace(day=1)
            month = current if current in months else (months[0] if current < months[0] else months[-1])
        for g in grids:
            g.show_all, g.month = show_all, month
        i = months.index(month) if month in months else -1
        nav = {
            "show_all": show_all,
            "month": month,
            "multi": len(months) > 1,
            "prev": months[i - 1].strftime("%Y-%m") if i > 0 else None,
            "next": months[i + 1].strftime("%Y-%m") if 0 <= i < len(months) - 1 else None,
        }
        return render_template("_budgeting.html", o=view.order, grids=grids, nav=nav)


def _grid_window() -> tuple[bool, dt.date | None]:
    """The ?days=all and ?month=YYYY-MM a details grid is shown with."""
    try:
        month = dt.datetime.strptime(request.args.get("month", ""), "%Y-%m").date()
    except ValueError:
        month = None
    return request.args.get("days") == "all", month


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
        # The monthly note and the adjustment moved to the day log, which
        # dates and attributes them and keeps more than one. The columns stay
        # for what they already hold; nothing writes them any more.
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
            # Only what the form carries. The goal boxes are named for the
            # pacing type the page was drawn with, so the fields of every
            # other type are absent - and setting them from the form blanked
            # them. Switching a line to Ad spend wiped its sold spend.
            for field_name in (
                "monthly_impressions", "total_impressions", "goal_cpm",
                "monthly_spend", "total_spend", "goal_cpc",
                "client_monthly_budget", "client_total_budget",
                "google_monthly_spend", "google_total_spend", "goal_cpe",
                "monthly_events", "total_events",
            ):
                if prefix + field_name in form:
                    setattr(item, field_name, as_float(prefix + field_name))
            # Switched type in the same save: what was typed in the goal boxes
            # is the new type's goal. A blank box leaves the stored goal alone.
            wanted = GOAL_FIELDS.get(kind or order.pacing_type or "impression")
            drawn = next(
                (f for f in GOAL_FIELDS.values() if prefix + f[0] in form), None
            )
            if wanted and drawn and drawn != wanted:
                for src, dst in zip(drawn[:2], wanted[:2]):
                    value = as_float(prefix + src)
                    if value is not None:
                        setattr(item, dst, value)
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
# Delivery for one order
# --------------------------------------------------------------------------
@app.route("/orders/<int:order_id>/delivery", methods=["POST"])
@login_required
def order_delivery_upload(order_id: int):
    """Load a day of delivery for this order, without waiting for a sweep.

    A buyer chasing a number today should not have to wait for tonight's
    drop, or load the whole book to see one order.

    Only this order's rows are kept. A drop that happens to carry the rest
    of the book must not arrive through a page that says it is about one -
    nothing on the page would say it had.
    """
    upload = request.files.get("file")
    if not upload or not upload.filename:
        flash("Pick a file first.")
        return redirect(url_for("order_detail", order_id=order_id))

    with session_scope() as db:
        order = db.get(Order, order_id)
        if order is None:
            abort(404)
        external_id = order.external_order_id

    if not external_id:
        flash("This order has no order number, so its rows cannot be told apart.")
        return redirect(url_for("order_detail", order_id=order_id))

    path = _spool(upload)
    try:
        with session_scope() as db:
            written, summary = loader.load_delivery_file(
                db, path, source_label=f"upload:{upload.filename}",
                only_order=external_id,
            )
        if written:
            covers = ""
            if summary.get("min_date") and summary.get("max_date"):
                covers = (
                    f" covering {summary['min_date']:%-d %b}"
                    f" to {summary['max_date']:%-d %b}"
                )
            flash(f"Loaded {written:,} days for this order{covers}.")
        else:
            flash(
                f"Nothing in that file carries order {external_id}. "
                "It may be a drop for other clients."
            )
    except Exception as exc:  # the file is whatever someone had to hand
        app.logger.exception("order delivery upload failed")
        flash(f"That file could not be read: {exc}")
    finally:
        if os.path.exists(path):
            os.unlink(path)

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
        return _back(order_id)

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
    return _back(order_id, "#daylog")


@app.route("/orders/<int:order_id>/notes/<int:note_id>/delete", methods=["POST"])
@login_required
def note_delete(order_id: int, note_id: int):
    with session_scope() as db:
        note = db.get(DayNote, note_id)
        if note and note.order_id == order_id:
            db.delete(note)
    return _back(order_id, "#daylog")


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
            if prefix + "targets" in form:
                term.targets = (form.get(prefix + "targets") or "").strip() or None

    flash("Saved.")
    return _back(order_id)


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
            return _back(order_id)

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
        if clash is not None and request.form.get("split"):
            # A second line of the same targeting: its own numbered row.
            taken = set(db.execute(
                select(StrategyTerms.label).where(StrategyTerms.line_item_id == item.id)
            ).scalars())
            n = 2
            while f"{label} {n}" in taken:
                n += 1
            label, clash = f"{label} {n}", None
        if clash is not None:
            flash(f"{label} is already on this order.")
            return _back(order_id)

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
    return _back(order_id)


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
    return _back(order_id)


# --------------------------------------------------------------------------
# The details grid, one product at a time
# --------------------------------------------------------------------------
@app.route("/orders/<int:order_id>/details/<int:line_item_id>")
@login_required
@cached_page
def details_page(order_id: int, line_item_id: int):
    with session_scope() as db:
        view = details.details_view(db, order_id, line_item_id, as_of=_as_of())
        if view is None:
            abort(404)
        view.show_all, view.month = _grid_window()
        return render_template(
            "details.html",
            sa_alerts=views.sa_alerts(db, [view.item.external_id]),
            d=view,
            r=view.row,
            siblings=[
                (
                    li.id,
                    " - ".join(
                        part for part in (
                            li.external_id if li.external_id
                            and not li.external_id.startswith("name:") else None,
                            li.product or li.name,
                            li.status,
                        ) if part
                    ),
                )
                for li in sorted(
                    view.order.line_items, key=lambda li: (li.sort_order, li.id)
                )
                if products.is_paced(li.product)
            ],
        )


@app.route("/orders/<int:order_id>/details/<int:line_item_id>/build", methods=["POST"])
@login_required
def details_build(order_id: int, line_item_id: int):
    """Draft the split the guide has a buyer type by hand."""
    with session_scope() as db:
        item = db.get(LineItem, line_item_id)
        if item is None or item.order_id != order_id:
            abort(404)
        kind = item.pacing_type or item.order.pacing_type or "impression"
        added, source = details.build_split(db, item, money=kind != "impression")

    if source == "existing":
        flash("Split already exists.")
    elif not added:
        flash("No strategies on the order and nothing running.")
    elif source == "order":
        flash(f"Built {added} rows from the order.")
    else:
        flash(f"Built {added} rows from actual serve.")
    return redirect(
        url_for("details_page", order_id=order_id, line_item_id=line_item_id)
    )


@app.route("/orders/<int:order_id>/details/<int:line_item_id>/link", methods=["POST"])
@login_required
def strategy_link(order_id: int, line_item_id: int):
    """Pair a delivery group with a strategy row when the names did not."""
    form = request.form
    with session_scope() as db:
        item = db.get(LineItem, line_item_id)
        if item is None or item.order_id != order_id:
            abort(404)
        terms = list(
            db.execute(
                select(StrategyTerms).where(StrategyTerms.line_item_id == item.id)
            ).scalars()
        )

        def keys_of(term):
            return [k for k in (term.linked_keys or "").splitlines() if k.strip()]

        if form.get("unlink"):
            term = next((t for t in terms if str(t.id) == form["unlink"]), None)
            key = (form.get("key") or "").strip()
            if term is not None:
                term.linked_keys = "\n".join(
                    k for k in keys_of(term) if k != key
                ) or None
        else:
            key = (form.get("key") or "").strip()
            term = next((t for t in terms if str(t.id) == form.get("term")), None)
            if key and term is not None:
                for other in terms:
                    if key in keys_of(other):
                        other.linked_keys = "\n".join(
                            k for k in keys_of(other) if k != key
                        ) or None
                term.linked_keys = "\n".join(keys_of(term) + [key])
        item.terms_locked = True

    back = request.referrer or ""
    if not back.startswith(request.host_url):
        back = url_for("details_page", order_id=order_id, line_item_id=line_item_id)
    return redirect(back)


@app.route("/orders/<int:order_id>/details/<int:line_item_id>/check", methods=["POST"])
@login_required
def details_check(order_id: int, line_item_id: int):
    """Record the weekly check - the Updates sheet row."""
    form = request.form
    raw_date = (form.get("checked_on") or "").strip()
    try:
        checked_on = dt.date.fromisoformat(raw_date) if raw_date else dt.date.today()
    except ValueError:
        checked_on = dt.date.today()
    tracking = {"yes": True, "no": False}.get(form.get("tracking_conversions") or "")

    with session_scope() as db:
        view = details.details_view(db, order_id, line_item_id)
        if view is None:
            abort(404)
        details.record_check(
            db,
            view,
            checked_on=checked_on,
            author=(form.get("author") or "").strip() or None,
            tracking_conversions=tracking,
            search_terms_checked=bool(form.get("search_terms_checked")),
            optimizations=(form.get("optimizations") or "").strip() or None,
            notes=(form.get("notes") or "").strip() or None,
        )

    flash(f"Weekly check saved for {checked_on:%-d %b}.")
    return redirect(
        url_for("details_page", order_id=order_id, line_item_id=line_item_id)
        + "#checks"
    )


@app.route(
    "/orders/<int:order_id>/details/<int:line_item_id>/check/<int:check_id>/delete",
    methods=["POST"],
)
@login_required
def details_check_delete(order_id: int, line_item_id: int, check_id: int):
    with session_scope() as db:
        check = db.get(WeeklyCheck, check_id)
        if check and check.order_id == order_id:
            db.delete(check)
    return redirect(
        url_for("details_page", order_id=order_id, line_item_id=line_item_id)
        + "#checks"
    )


# --------------------------------------------------------------------------
# Campaign linking
# --------------------------------------------------------------------------
# One link save at a time: a double click sent two, and both inserted.
_link_lock = threading.Lock()


@app.route("/orders/<int:order_id>/link", methods=["POST"])
@login_required
def campaign_link(order_id: int):
    """Set which DSP campaigns a line item was bought on.

    The whole set arrives at once, so unticking one removes it. A line item
    can carry several: a Meta line split into an impressions campaign and a
    leads campaign is ordinary, and reading only one of them understates the
    line by whatever the other ran.

    A campaign already on another line item is shared, not moved: older
    setups ran one campaign across several line items, and each day of it
    goes to the line item whose dates it falls in, so nothing counts twice.
    """
    line_item_id = request.form.get("line_item_id", type=int)
    picked = [
        raw.strip() for raw in request.form.getlist("campaign") if raw.strip()
    ]
    verified = request.form.get("ops_verified") == "on"
    who = (request.form.get("linked_by") or "").strip() or None

    with _link_lock, session_scope() as db:
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
# Home and My Day
# --------------------------------------------------------------------------
@app.route("/home")
@login_required
def home():
    weekday = schedule.today_weekday()
    with session_scope() as db:
        schedule.seed_if_empty(db)
        today = schedule.by_day(db, weekday)
        as_of = views.latest_delivery_date(db)
    if _warmer_started:
        # Each name here is a click away; build their pages behind this one.
        pagecache.prefetch([url_for("my_day", buyer=b, day=weekday) for b in today])
    return render_template(
        "home.html",
        nav="home",
        weekday=weekday,
        day_name=schedule.WEEKDAYS[weekday],
        today=today,
        as_of=as_of,
    )


MY_DAY_COOKIE = "my_day_buyer"
MY_DAY_DAY_COOKIE = "my_day_day"


@app.route("/my-day")
@login_required
def my_day():
    buyer = request.args.get("buyer")
    day = request.args.get("day")
    # Coming back from an order: the buyer always, and the day picked
    # earlier today. A day picked yesterday falls back to today.
    remembered_day = None
    stamp, _, saved = (request.cookies.get(MY_DAY_DAY_COOKIE) or "").partition(":")
    if stamp == dt.date.today().isoformat() and saved.isdigit():
        remembered_day = saved
    if (not buyer and request.cookies.get(MY_DAY_COOKIE)) or (day is None and remembered_day):
        args = {k: v for k, v in request.args.items()}
        if not buyer and request.cookies.get(MY_DAY_COOKIE):
            args["buyer"] = request.cookies[MY_DAY_COOKIE]
        if day is None and remembered_day:
            args["day"] = remembered_day
        return redirect(url_for("my_day", **args))
    response = make_response(_my_day_page())
    if buyer:
        response.set_cookie(MY_DAY_COOKIE, buyer, max_age=60 * 60 * 24 * 365, samesite="Lax")
    if day is not None and day.isdigit():
        response.set_cookie(MY_DAY_DAY_COOKIE, f"{dt.date.today().isoformat()}:{day}",
                            max_age=60 * 60 * 24, samesite="Lax")
    return response


def _my_day_weekday() -> int:
    try:
        weekday = int(request.args.get("day", schedule.today_weekday()))
    except ValueError:
        return schedule.today_weekday()
    return weekday if 0 <= weekday <= 4 else schedule.today_weekday()


@cached_page
def _my_day_page():
    buyer = request.args.get("buyer")
    weekday = _my_day_weekday()
    with session_scope() as db:
        schedule.seed_if_empty(db)
        buyers = schedule.buyers(db)
        markets_all = schedule.all_markets(db)
        rows, partners, missing = [], [], []
        if buyer:
            # A buyer's day is the partners the sheet gives them that day,
            # plus their own orders under any partner updated that day: the
            # sheet assigns partners, the orders drop assigns orders, and
            # the two disagree (Laura's Lotus Las Vegas orders sit on
            # Olivia's Thursday). Only those partners are paced, so the page
            # works out tens of orders, not the whole book.
            partners = schedule.day_markets(db, buyer, weekday)
            mine, missing = schedule.matching_markets(db, partners)
            full_name = schedule.order_buyer(db, buyer)
            theirs = []
            if full_name:
                day = set(schedule.day_market_values(db, weekday))
                theirs = [
                    m for m in db.execute(
                        select(Client.market)
                        .join(Order, Order.client_id == Client.id)
                        .where(Order.buyer == full_name, Client.market.isnot(None))
                        .distinct()
                    ).scalars() if m in day
                ]
            mine_keys = {schedule.key(m) for m in mine}
            markets = sorted(set(mine) | set(theirs))
            rows = [
                r for r in (views.overview(db, as_of=_as_of(), markets=markets) if markets else [])
                if schedule.key(r.client.market) in mine_keys
                or (full_name and r.order.buyer == full_name)
            ]
            shown = {r.client.market for r in rows if r.client.market}
            partners = sorted(set(partners) | shown, key=str.lower)
        as_of = rows[0].as_of if rows else views.latest_delivery_date(db)
        return render_template(
            "my_day.html",
            nav="myday",
            buyer=buyer,
            buyers=buyers,
            markets_all=markets_all,
            weekday=weekday,
            weekdays=schedule.WEEKDAYS,
            partners=partners,
            missing=missing,
            rows=rows,
            page_total=views.page_total(rows),
            as_of=as_of,
        )


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
    elif action == "reread-orders":
        # The sold side only. It is minutes, where a forced sweep of
        # everything is the whole delivery history - gigabytes that say
        # nothing new about what was sold.
        message = _start_sweep(force=True, kind=loader.ORDERS)
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
    elif action == "schedule":
        upload = request.files.get("file")
        if upload and upload.filename:
            try:
                with session_scope() as db:
                    count = schedule.load(db, upload.read().decode("utf-8-sig"))
                pagecache.edited()
                message = f"Schedule loaded: {count} partners."
            except (ValueError, UnicodeDecodeError) as exc:
                message = f"Schedule not loaded: {exc}"
    elif action == "draft-splits":
        with session_scope() as db:
            message = views.draft_missing_splits(db, as_of=_as_of()).summary()
            extra = views.add_extra_lines(db, as_of=_as_of())
            if extra:
                message += f"; {extra} second lines added"

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


@app.route("/api/sa-alerts", methods=["POST"])
def sa_alerts_post():
    """The launcher's nightly StackAdapt alerts. Replaces that run date's rows.

    Token auth (X-Alerts-Token = SA_ALERTS_TOKEN), not the login: a cron posts it.
    """
    token = os.getenv("SA_ALERTS_TOKEN", "")
    sent = request.headers.get("X-Alerts-Token", "")
    if not token or not hmac.compare_digest(token, sent):
        abort(403)
    body = request.get_json(silent=True) or {}
    try:
        run_date = dt.date.fromisoformat(body.get("run_date") or "")
    except ValueError:
        abort(400)
    rows = body.get("alerts") or []
    with session_scope() as db:
        db.query(SaAlert).filter(SaAlert.run_date == run_date).delete()
        for a in rows:
            db.add(SaAlert(
                run_date=run_date,
                external_order_id=str(a.get("order_id") or "")[:64] or None,
                external_line_item_id=str(a.get("line_item_id") or "")[:64] or None,
                group_id=str(a.get("group_id") or "")[:64] or None,
                group_name=(a.get("group") or "")[:400] or None,
                campaign_id=str(a.get("campaign_id") or "")[:64] or None,
                campaign_name=(a.get("campaign") or "")[:400] or None,
                kind=(a.get("kind") or "")[:40],
                alert=a.get("alert"),
                adjustment=a.get("adjustment"),
                status=(a.get("status") or "")[:300] or None,
            ))
    return {"ok": True, "run_date": run_date.isoformat(), "alerts": len(rows)}


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
