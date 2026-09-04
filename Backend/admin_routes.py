"""The admin section: a signed-in dashboard over the harvest's own records."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.templating import Jinja2Templates

from config import BASE_DIR
from core.logging_config import get_logger
from repository import list_search_jobs, reset_database
from services import admin_data, admin_metrics, admin_records, admin_tasks
from services.search_jobs import create_search_job
from sources.source_registry import connector_metadata
from services.admin_auth import (
    SESSION_COOKIE_NAME,
    SESSION_COOKIE_PATH,
    SESSION_TTL_SECONDS,
    admin_is_configured,
    authenticate,
    clear_failures,
    issue_session,
    read_session,
    record_failure,
    seconds_until_unlocked,
)


logger = get_logger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

RESET_CONFIRMATION = "DELETE EVERYTHING"


async def _form(request: Request) -> dict[str, str]:
    """Parse a urlencoded form body without depending on python-multipart."""
    body = (await request.body()).decode("utf-8", "replace")
    return {key: values[0] for key, values in parse_qs(body, keep_blank_values=True).items()}


async def _form_multi(request: Request) -> dict[str, list[str]]:
    """The same, keeping every value for fields that repeat (a multi-select)."""
    body = (await request.body()).decode("utf-8", "replace")
    return parse_qs(body, keep_blank_values=True)


def _notice(kind: str, text: str) -> list[dict[str, str]]:
    return [{"kind": kind, "text": text}]


def _int_or_none(value: str, low: int, high: int) -> int | None:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if low <= number <= high else None


def _safe_next(value: object) -> str:
    """Where to land after signing in, refusing anything off this site.

    Only a single-slash relative path is accepted, so a crafted ?next= cannot
    bounce someone to another host once they have signed in.
    """
    path = str(value or "").strip()
    if not path.startswith("/") or path.startswith("//") or "\\" in path:
        return "/admin"
    return path or "/admin"


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _signed_in_user(request: Request) -> str | None:
    return read_session(request.cookies.get(SESSION_COOKIE_NAME))


ADMIN_STYLESHEET = BASE_DIR / "static" / "admin.css"


def _asset_version() -> str:
    """Stamp the stylesheet URL with its own mtime.

    Without it a browser keeps serving the cached copy, and a change to the
    admin's styling simply never reaches the person looking at the page.
    """
    try:
        return str(int(ADMIN_STYLESHEET.stat().st_mtime))
    except OSError:
        return "0"


def _render(request: Request, template: str, **context: Any) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        f"admin/{template}",
        {
            "user": _signed_in_user(request),
            "asset_version": _asset_version(),
            **context,
        },
    )


def _login_required(request: Request) -> RedirectResponse | None:
    """Send anyone without a valid session back to the sign-in page."""
    if _signed_in_user(request):
        return None
    return RedirectResponse("/admin/login", status_code=303)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if _signed_in_user(request):
        return RedirectResponse("/admin", status_code=303)
    return _render(
        request, "login.html", configured=admin_is_configured(), error="", username="",
        next_path=_safe_next(request.query_params.get("next")),
    )


@router.post("/login")
async def login(request: Request):
    form = await _form(request)
    username = (form.get("username") or "").strip()
    password = form.get("password") or ""
    client_ip = _client_ip(request)
    next_path = _safe_next(form.get("next"))

    if not admin_is_configured():
        return _render(
            request, "login.html", configured=False, error="", username=username,
            next_path=next_path,
        )

    wait = seconds_until_unlocked(username, client_ip)
    if wait:
        logger.warning("Admin sign-in locked for %s from %s", username or "(blank)", client_ip)
        return _render(
            request, "login.html", configured=True, username=username,
            next_path=next_path,
            error=f"Too many attempts. Try again in {max(1, wait // 60)} minute(s).",
        )

    if not authenticate(username, password):
        record_failure(username, client_ip)
        logger.warning("Admin sign-in refused for %s from %s", username or "(blank)", client_ip)
        # Never say which half was wrong.
        return _render(
            request, "login.html", configured=True, username=username,
            next_path=next_path,
            error="That user name and password did not match.",
        )

    clear_failures(username, client_ip)
    logger.info("Admin %s signed in from %s", username, client_ip)
    response = RedirectResponse(next_path, status_code=303)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        issue_session(username),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path=SESSION_COOKIE_PATH,
    )
    return response


@router.post("/logout")
def logout(request: Request):
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME, path=SESSION_COOKIE_PATH)
    # Sessions issued while the cookie was scoped to /admin are a different
    # cookie to the browser, and clearing one does not clear the other.
    response.delete_cookie(SESSION_COOKIE_NAME, path="/admin")
    return response


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    redirect = _login_required(request)
    if redirect:
        return redirect
    reset = request.query_params.get("reset")
    notices = []
    if reset == "done":
        notices = _notice("ok", "The stored records were cleared.")
    elif reset == "unconfirmed":
        notices = _notice("warn", "Nothing was cleared — the confirmation phrase did not match.")
    return _render(
        request, "dashboard.html", active="dashboard", notices=notices,
        confirmation=RESET_CONFIRMATION, **admin_metrics.dashboard()
    )


@router.get("/api/dashboard")
def dashboard_json(request: Request):
    if not _signed_in_user(request):
        return JSONResponse({"detail": "Sign in required"}, status_code=401)
    return admin_metrics.dashboard()


@router.post("/reset-database")
async def reset_database_route(request: Request) -> Response:
    """Clear the stored records. Replaces the old unauthenticated GET /reset_db."""
    redirect = _login_required(request)
    if redirect:
        return redirect
    form = await _form(request)
    if (form.get("confirmation") or "").strip() != RESET_CONFIRMATION:
        return RedirectResponse("/admin?reset=unconfirmed", status_code=303)
    reset_database()
    logger.warning("Admin %s cleared the database", _signed_in_user(request))
    return RedirectResponse("/admin?reset=done", status_code=303)


# ---------------------------------------------------------------- operations

@router.get("/operations", response_class=HTMLResponse)
def operations_page(request: Request):
    redirect = _login_required(request)
    if redirect:
        return redirect
    started = request.query_params.get("started")
    notices: list[dict[str, str]] = []
    if started == "harvest":
        notices = _notice("ok", "Harvest started. Progress appears in the task list below.")
    elif started == "cancelled":
        notices = _notice("warn", "Cancellation requested. The molecule in flight finishes first.")
    elif started == "busy":
        notices = _notice("warn", "A harvest is already running. Cancel it before starting another.")
    elif started == "backfill":
        notices = _notice("ok", "Rebuilding evidence. Progress appears in the task list below.")
    return _render(
        request, "operations.html", active="operations", notices=notices,
        connectors=connector_metadata(), tasks=admin_tasks.list_tasks(),
        jobs=list_search_jobs(limit=10),
    )


@router.post("/operations/harvest")
async def start_harvest_route(request: Request) -> Response:
    redirect = _login_required(request)
    if redirect:
        return redirect
    # One harvest at a time. Two would race each other for the same rate limit
    # and the same rows, and neither would finish sooner.
    if admin_tasks.running_count():
        return RedirectResponse("/admin/operations?started=busy", status_code=303)

    form = await _form_multi(request)
    known = {item["name"] for item in connector_metadata()}
    sources = [name for name in form.get("sources", []) if name in known] or None
    molecules = [
        part.strip()
        for part in (form.get("molecules", [""])[0]).replace("\n", ",").split(",")
        if part.strip()
    ] or None
    admin_tasks.start_harvest(
        sources=sources,
        molecules=molecules,
        molecule_limit=None if molecules else _int_or_none(form.get("molecule_limit", [""])[0], 1, 5000),
        # An unticked checkbox is absent, not empty, so read the value itself.
        resume=bool((form.get("resume") or [""])[0]),
        started_by=_signed_in_user(request) or "unknown",
    )
    return RedirectResponse("/admin/operations?started=harvest", status_code=303)


@router.post("/operations/search")
async def start_search_route(request: Request) -> Response:
    redirect = _login_required(request)
    if redirect:
        return redirect
    form = await _form(request)
    substance = (form.get("substance") or "").strip()
    if not substance:
        return RedirectResponse("/admin/operations", status_code=303)
    job_id = create_search_job(substance, None, mode=("full" if form.get("mode") == "full" else "fast"))
    logger.info("Admin %s started search job %s for %s", _signed_in_user(request), job_id, substance)
    return RedirectResponse(f"/search_jobs/{job_id}", status_code=303)


@router.post("/operations/evidence-backfill")
async def start_evidence_backfill_route(request: Request) -> Response:
    redirect = _login_required(request)
    if redirect:
        return redirect
    # It walks every stored row, so let it have the machine to itself rather
    # than compete with a harvest writing the rows it is reading.
    if admin_tasks.running_count():
        return RedirectResponse("/admin/operations?started=busy", status_code=303)
    admin_tasks.start_evidence_backfill(started_by=_signed_in_user(request) or "unknown")
    return RedirectResponse("/admin/operations?started=backfill", status_code=303)


@router.post("/operations/cancel")
async def cancel_task_route(request: Request) -> Response:
    redirect = _login_required(request)
    if redirect:
        return redirect
    form = await _form(request)
    admin_tasks.cancel_task((form.get("task_id") or "").strip())
    return RedirectResponse("/admin/operations?started=cancelled", status_code=303)


@router.get("/api/tasks")
def tasks_json(request: Request):
    if not _signed_in_user(request):
        return JSONResponse({"detail": "Sign in required"}, status_code=401)
    return {"tasks": admin_tasks.list_tasks()}


# ------------------------------------------------------------------- records

RECORDS_PAGE_SIZE = 50


@router.get("/records", response_class=HTMLResponse)
def records_page(request: Request):
    redirect = _login_required(request)
    if redirect:
        return redirect
    params = request.query_params
    offset = _int_or_none(params.get("offset", "0"), 0, 10_000_000) or 0
    query = (params.get("q") or "").strip()
    source = (params.get("source") or "").strip()
    country = (params.get("country") or "").strip()
    records, total = admin_records.find_records(
        query=query, source=source, country=country, limit=RECORDS_PAGE_SIZE, offset=offset
    )
    notices: list[dict[str, str]] = []
    if params.get("saved") == "none":
        notices = _notice("warn", "Nothing was changed — every field matched what was stored.")
    return _render(
        request, "records.html", active="records", notices=notices,
        records=records, total=total, query=query, source=source, country=country,
        limit=RECORDS_PAGE_SIZE, offset=offset,
        options=admin_records.filter_options(), edits=admin_records.recent_edits(20),
    )


@router.get("/records/{record_id}", response_class=HTMLResponse)
def record_page(request: Request, record_id: int):
    redirect = _login_required(request)
    if redirect:
        return redirect
    record = admin_records.get_record(record_id)
    if not record:
        return RedirectResponse("/admin/records", status_code=303)
    changed = request.query_params.get("changed")
    notices = _notice("ok", "Saved: " + changed.replace(",", ", ") + ".") if changed else []
    return _render(
        request, "record_edit.html", active="records", notices=notices,
        record=record, editable=admin_records.EDITABLE_FIELDS,
    )


@router.post("/records/{record_id}")
async def save_record_route(request: Request, record_id: int) -> Response:
    redirect = _login_required(request)
    if redirect:
        return redirect
    form = await _form(request)
    result = admin_records.update_record(
        record_id, form, editor=_signed_in_user(request) or "unknown"
    )
    if not result["found"]:
        return RedirectResponse("/admin/records", status_code=303)
    if not result["changed"]:
        return RedirectResponse("/admin/records/" + str(record_id) + "?saved=none", status_code=303)
    return RedirectResponse(
        "/admin/records/" + str(record_id) + "?changed=" + ",".join(result["changed"]),
        status_code=303,
    )


# ---------------------------------------------------------------------- data

PRUNE_CONFIRMATION = "PRUNE"


def _data_context(request: Request, notices: list[dict[str, str]] | None = None) -> dict[str, Any]:
    params = request.query_params
    purge_name = (params.get("purge_source") or "").strip()
    prune_days = _int_or_none(params.get("prune_days", ""), 30, 3650)
    return {
        "active": "data",
        "notices": notices or [],
        "options": admin_records.filter_options(),
        "source_counts": admin_data.source_row_counts(),
        "export_limit": admin_data.EXPORT_ROW_LIMIT,
        "purge_source_name": purge_name,
        "purge_preview": admin_data.purge_preview(purge_name) if purge_name else None,
        "prune_days": prune_days,
        "prune_preview": admin_data.prune_preview(prune_days) if prune_days else None,
    }


@router.get("/data", response_class=HTMLResponse)
def data_page(request: Request):
    redirect = _login_required(request)
    if redirect:
        return redirect
    done = request.query_params.get("done")
    rows = request.query_params.get("rows", "0")
    notices: list[dict[str, str]] = []
    if done == "purged":
        notices = _notice("ok", "Purged " + rows + " registrations.")
    elif done == "pruned":
        notices = _notice("ok", "Pruned " + rows + " registrations.")
    elif done == "unconfirmed":
        notices = _notice("warn", "Nothing was removed — the confirmation did not match.")
    elif done == "empty":
        notices = _notice("warn", "No rows matched those filters, so no workbook was written.")
    return _render(request, "data.html", **_data_context(request, notices))


@router.post("/data/purge")
async def purge_route(request: Request) -> Response:
    redirect = _login_required(request)
    if redirect:
        return redirect
    form = await _form(request)
    source = (form.get("source") or "").strip()
    # The confirmation is the source's own name, so a mistyped or stale form
    # cannot delete a different source than the one that was previewed.
    if not source or (form.get("confirmation") or "").strip() != source:
        return RedirectResponse("/admin/data?done=unconfirmed", status_code=303)
    counts = admin_data.purge_source(source, _signed_in_user(request) or "unknown")
    return RedirectResponse(
        "/admin/data?done=purged&rows=" + str(counts["products"]), status_code=303
    )


@router.post("/data/prune")
async def prune_route(request: Request) -> Response:
    redirect = _login_required(request)
    if redirect:
        return redirect
    form = await _form(request)
    days = _int_or_none(form.get("days", ""), 30, 3650)
    if not days or (form.get("confirmation") or "").strip() != PRUNE_CONFIRMATION:
        return RedirectResponse("/admin/data?done=unconfirmed", status_code=303)
    counts = admin_data.prune_stale(days, _signed_in_user(request) or "unknown")
    return RedirectResponse(
        "/admin/data?done=pruned&rows=" + str(counts["products"]), status_code=303
    )


@router.post("/data/export")
async def export_route(request: Request) -> Response:
    redirect = _login_required(request)
    if redirect:
        return redirect
    form = await _form(request)
    path, count = admin_data.export_slice(
        substance=form.get("substance", ""),
        source=form.get("source", ""),
        country=form.get("country", ""),
        actor=_signed_in_user(request) or "unknown",
    )
    if not path or not count:
        return RedirectResponse("/admin/data?done=empty", status_code=303)
    return FileResponse(
        path,
        filename=path.name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
