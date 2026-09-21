"""adtini · Pacing — Flask entrypoint.

Mirrors the quote builder's shape (`gunicorn app:app`, Jinja templates, the
shared adtini stylesheet) so the two tools read as one product.
"""
from __future__ import annotations

import datetime as dt
import functools
import io
import logging

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
from sqlalchemy import select

import exports
import views
from config import get_settings
from db import init_db, session_scope
from ingest import loader
from models import PACING_TYPES, Client, IngestedFile, LineItem, Order
from orderbook import adopt_unmatched_delivery

logging.basicConfig(level=logging.INFO)

settings = get_settings()
app = Flask(__name__)
app.secret_key = settings.session_secret
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024 * 1024  # the drops are ~70MB

init_db()


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
def _num(value, places=0):
    if value is None:
        return "—"
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "—"
    if value < 0:
        return f"({abs(value):,.{places}f})"
    return f"{value:,.{places}f}"


@app.template_filter("num")
def num_filter(value, places=0):
    return _num(value, places)


@app.template_filter("money")
def money_filter(value, places=2):
    if value is None:
        return "—"
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "—"
    if value < 0:
        return f"(${abs(value):,.{places}f})"
    return f"${value:,.{places}f}"


@app.template_filter("pct")
def pct_filter(value, places=2):
    if value is None:
        return "—"
    return f"{float(value) * 100:,.{places}f}%"


@app.template_filter("entry")
def entry_filter(value):
    """A sold figure as it should sit in an input box.

    The engine works in floats, so 700000.0 comes back where a buyer typed
    700,000. Whole numbers lose the decimal; the rest keep two places, and an
    unset term stays an empty box rather than a 0 that reads as sold.
    """
    if value in (None, 0, 0.0):
        return ""
    try:
        value = float(value)
    except (TypeError, ValueError):
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


def _unwrap(raw: bytes, filename: str) -> bytes:
    """Take a CSV out of its wrapper, if it arrived in one."""
    lowered = filename.lower()
    if lowered.endswith(".gz"):
        import gzip

        return gzip.decompress(raw)
    if lowered.endswith(".zip"):
        import zipfile

        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = [
                n for n in archive.namelist()
                if n.lower().endswith(".csv") and not n.startswith("__MACOSX/")
            ]
            return archive.read(names[0]) if names else b""
    return raw


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
            total_rows=total_rows,
            page=page,
            pages=pages,
            options=options,
            as_of=as_of,
            args=request.args,
            pacing_types=PACING_TYPES,
        )


@app.route("/orders/<int:order_id>")
@login_required
def order_detail(order_id: int):
    with session_scope() as db:
        view = views.order_view(db, order_id, as_of=_as_of())
        if view is None:
            abort(404)
        return render_template(
            "order.html",
            v=view,
            t=view.total,
            chart=views.chart_series(view),
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
# Data
# --------------------------------------------------------------------------
@app.route("/data", methods=["GET", "POST"])
@login_required
def data_page():
    message = None
    if request.method == "POST":
        action = request.form.get("action")
        if action == "ingest":
            result = loader.run(force=request.form.get("force") == "on")
            message = f"S3 sweep: {result.summary()}"
            if result.errors:
                message += " — " + "; ".join(result.errors[:3])
        elif action == "upload":
            upload = request.files.get("file")
            if upload and upload.filename:
                raw = _unwrap(upload.read(), upload.filename)
                # The same filename rule the S3 sweep uses, so an uploaded
                # file behaves exactly as it would from the bucket.
                kind = loader.classify(upload.filename)
                with session_scope() as db:
                    if kind == loader.ORDERS:
                        imported, frame = loader.load_orders_bytes(db, raw, upload.filename)
                        message = f"{upload.filename}: {imported.summary()}."
                        if frame.unmapped_note:
                            message += f" Unrecognised columns: {frame.unmapped_note}"
                    elif kind == loader.DELIVERY:
                        written, _ = loader.load_bytes(db, raw, upload.filename)
                        message = f"Loaded {written:,} delivery rows from {upload.filename}."
                    else:
                        message = (
                            f"{upload.filename} is not recognised. Delivery files "
                            "start with 'client-serve' and order files with 'orders'."
                        )
            else:
                message = "Pick a file first."
        elif action == "adopt":
            with session_scope() as db:
                message = adopt_unmatched_delivery(db).summary()

    with session_scope() as db:
        files = list(
            db.execute(
                select(IngestedFile).order_by(IngestedFile.ingested_at.desc()).limit(25)
            ).scalars()
        )
        latest = views.latest_delivery_date(db)
        counts = {
            "clients": db.execute(select(Client)).scalars().all().__len__(),
            "orders": db.execute(select(Order)).scalars().all().__len__(),
        }
    return render_template(
        "data.html",
        files=files,
        latest=latest,
        counts=counts,
        message=message,
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
        view = views.order_view(db, order_id, as_of=_as_of())
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


@app.route("/healthz")
def healthz():
    return {"ok": True}


if __name__ == "__main__":
    app.run(debug=True, port=5000)
