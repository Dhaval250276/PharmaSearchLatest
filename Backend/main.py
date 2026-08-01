from html import escape
from pathlib import Path
from math import ceil
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from config import BASE_DIR, DB_PATH, EXPORT_DIR
from core.logging_config import configure_logging, get_logger
from export_service import write_excel_export
from repository import (
    PRODUCT_DETAILS_SEED_PATH,
    initialize_database,
    list_search_jobs,
    list_product_details,
    reset_database,
    save_product_detail,
    seed_product_details_if_needed,
)
from sources.ema import EU_COUNTRIES, find_product_url, run_ema_search
from sources.ema_product_parser import extract_product_page
from sources.mhra import run_mhra_search
from sources.mhra_product_parser import extract_mhra_product_page
from sources.search_engine import COMPLETE_SEARCH_TIMEOUT_SECONDS, search_substance
from sources.source_registry import connector_metadata
from services.search_pipeline import (
    COUNTRY_SOURCE_DEFAULTS,
    COUNTRY_OPTIONS,
    DEFAULT_SOURCES,
    REGION_OPTIONS,
    combined_search,
    enriched_cached_results,
    filtered_search_results,
    parse_sources,
)
from services.ai_client import ai_status as current_ai_status
from services.connector_health import connector_health_rows
from services.connector_status import connector_status_rows
from services.english_normalizer import english_text
from services.result_formatter import formatted_result_row
from services.search_jobs import FAST_BACKGROUND_SOURCES, create_search_job, get_search_job, get_search_job_results


configure_logging()
logger = get_logger(__name__)
app = FastAPI(title="PharmaSearch", version="0.2.0")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
SEARCH_RESULT_CACHE: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
APP_BUILD = "UAT-2026-08-01-platform-core"


def result_cache_key(
    substance: str,
    live: bool,
    sources: list[str],
    country: str = "",
    region: str = "",
    source: str = "",
    status: str = "",
    table_search: str = "",
    substance_filter: str = "",
    product_filter: str = "",
    company_filter: str = "",
    strength_filter: str = "",
    dosage_form_filter: str = "",
    pack_size_filter: str = "",
    atc_code_filter: str = "",
    therapeutic_category_filter: str = "",
    ma_holder_filter: str = "",
    manufacturer_name_filter: str = "",
    manufacturer_country_filter: str = "",
    registration_number_filter: str = "",
    registration_date_filter: str = "",
    sort_by: str = "",
    sort_dir: str = "asc",
) -> tuple[Any, ...]:
    return (
        substance.strip().lower(),
        bool(live),
        tuple(sources),
        country,
        region,
        source,
        status,
        table_search,
        substance_filter,
        product_filter,
        company_filter,
        strength_filter,
        dosage_form_filter,
        pack_size_filter,
        atc_code_filter,
        therapeutic_category_filter,
        ma_holder_filter,
        manufacturer_name_filter,
        manufacturer_country_filter,
        registration_number_filter,
        registration_date_filter,
        sort_by,
        "desc" if sort_dir == "desc" else "asc",
    )


def h(value: object) -> str:
    return escape("" if value is None else str(value))


def link_or_unavailable(url: str, label: str) -> str:
    if not url:
        return '<span class="text-muted">Not available</span>'
    return f'<a href="{h(url)}" target="_blank" rel="noopener noreferrer">{h(label)}</a>'


def unique_values(rows: list[dict[str, Any]], field: str) -> list[str]:
    values = {
        str(row.get(field) or "").strip()
        for row in rows
        if str(row.get(field) or "").strip()
    }
    return sorted(values, key=str.lower)


def option_tags(values: list[str], selected_value: str, all_label: str = "All") -> str:
    tags = [f'<option value="">{h(all_label)}</option>']
    for value in values:
        selected = " selected" if value == selected_value else ""
        tags.append(f'<option value="{h(value)}"{selected}>{h(value)}</option>')
    return "".join(tags)


def merged_options(primary_values: list[str], fixed_values: list[str]) -> list[str]:
    seen = set()
    merged = []
    for value in list(fixed_values) + list(primary_values):
        clean_value = str(value or "").strip()
        if not clean_value:
            continue
        key = clean_value.lower()
        if key in seen:
            continue
        seen.add(key)
        merged.append(clean_value)
    return merged


@app.on_event("startup")
def startup() -> None:
    logger.info("Starting PharmaSearch backend")
    initialize_database()


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "sources": connector_metadata(),
            "fast_background_sources": FAST_BACKGROUND_SOURCES,
            "country_options": COUNTRY_OPTIONS,
            "region_options": REGION_OPTIONS,
        },
    )


@app.get("/global_search/{substance}")
def global_search(
    substance: str,
    sources: list[str] | None = Query(default=None),
):
    results = search_substance(substance, source_names=parse_sources(sources))
    return {"substance": substance, "count": len(results), "results": results}


@app.get("/api/sources")
def api_sources():
    return {"sources": connector_metadata()}


@app.get("/api/version")
def api_version():
    return {"app": "PharmaSearch", "build": APP_BUILD}


@app.get("/api/data_health")
def api_data_health():
    inserted = seed_product_details_if_needed()
    rows = list_product_details()
    sample_substances = [
        "metformin",
        "paracetamol",
        "dapagliflozin",
        "quetiapine",
        "mirabegron",
    ]
    sample_counts = {
        substance: sum(
            1
            for row in rows
            if substance in str(row.get("substance") or "").lower()
            or substance in str(row.get("product") or "").lower()
        )
        for substance in sample_substances
    }
    return {
        "app": "PharmaSearch",
        "build": APP_BUILD,
        "database_path": str(DB_PATH),
        "seed_file": str(PRODUCT_DETAILS_SEED_PATH),
        "seed_file_exists": PRODUCT_DETAILS_SEED_PATH.exists(),
        "seed_file_size": PRODUCT_DETAILS_SEED_PATH.stat().st_size
        if PRODUCT_DETAILS_SEED_PATH.exists()
        else 0,
        "seed_rows_inserted_now": inserted,
        "product_details_count": len(rows),
        "sample_counts": sample_counts,
    }


@app.get("/api/export_directory")
def api_export_directory():
    return {"export_directory": str(EXPORT_DIR)}


@app.get("/api/search")
def api_search(
    substance: str,
    live: bool = True,
    sources: list[str] | None = Query(default=None),
    country: str = "",
    region: str = "",
    source: str = "",
):
    if not substance.strip():
        return {
            "substance": substance,
            "count": 0,
            "results": [],
            "message": "Please enter the active substance name.",
        }
    results, _, _ = filtered_search_results(
        substance,
        live=live,
        sources=sources,
        country=country,
        region=region,
        source=source,
    )
    return {"substance": substance, "count": len(results), "results": results}


@app.get("/search/{substance}")
def search_saved(
    substance: str,
    live: bool = True,
    sources: list[str] = Query(default=DEFAULT_SOURCES),
):
    return combined_search(substance, include_live=live, sources=sources)


@app.get("/search_jobs/start")
def start_search_job(
    substance: str,
    sources: list[str] = Query(default=DEFAULT_SOURCES),
    mode: str = "fast",
):
    if not substance.strip():
        return RedirectResponse(url="/", status_code=303)
    job_id = create_search_job(substance, sources, mode=mode)
    return RedirectResponse(url=f"/search_jobs/{job_id}", status_code=303)


@app.get("/api/search_jobs/{job_id}")
def search_job_status_api(job_id: str):
    job = get_search_job(job_id)
    if not job:
        return {"error": "Search job not found"}
    return job


@app.get("/search_jobs", response_class=HTMLResponse)
def recent_search_jobs_page():
    jobs = list_search_jobs(limit=50)
    rows = []
    for job in jobs:
        rows.append(
            f"""
            <tr>
                <td>{h(job["created_at"])}</td>
                <td>{h(job["substance"])}</td>
                <td>{h(job["mode"])}</td>
                <td>{h(job["status"])}</td>
                <td>{h(job["record_count"])}</td>
                <td>
                    <a class="btn btn-sm btn-outline-primary" href="/search_jobs/{h(job["job_id"])}">Progress</a>
                    <a class="btn btn-sm btn-outline-success" href="/search_jobs/{h(job["job_id"])}/results">Results</a>
                    <a class="btn btn-sm btn-success" href="/search_jobs/{h(job["job_id"])}/export">Export</a>
                </td>
            </tr>
            """
        )
    if not rows:
        rows.append('<tr><td colspan="6" class="text-center text-muted">No saved search jobs yet.</td></tr>')
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Recent Searches</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    </head>
    <body>
    <div class="container-fluid px-4 mt-4">
        <div class="d-flex justify-content-between align-items-center mb-3">
            <h2>Recent Searches</h2>
            <a class="btn btn-secondary" href="/">Back</a>
        </div>
        <table class="table table-striped table-bordered align-middle">
            <thead class="table-dark">
                <tr>
                    <th>Created</th>
                    <th>Substance</th>
                    <th>Mode</th>
                    <th>Status</th>
                    <th>Records</th>
                    <th>Actions</th>
                </tr>
            </thead>
            <tbody>{"".join(rows)}</tbody>
        </table>
    </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html)


@app.get("/search_jobs/{job_id}", response_class=HTMLResponse)
def search_job_page(job_id: str):
    job = get_search_job(job_id)
    if not job:
        return HTMLResponse(
            content="""
            <!DOCTYPE html>
            <html><head><title>Search Job</title>
            <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
            </head><body><div class="container mt-5">
            <div class="alert alert-warning">Search job not found.</div>
            <a class="btn btn-secondary" href="/">Back</a>
            </div></body></html>
            """,
            status_code=404,
        )
    rows = []
    for progress in job["progress"]:
        badge = {
            "done": "success",
            "running": "primary",
            "failed": "danger",
            "timeout": "warning",
            "queued": "secondary",
            "skipped": "secondary",
        }.get(progress["status"], "secondary")
        rows.append(
            f"""
            <tr>
                <td>{h(progress["source"])}</td>
                <td><span class="badge text-bg-{badge}">{h(progress["status"])}</span></td>
                <td>{h(progress["records"])}</td>
                <td>{h(progress["error"])}</td>
            </tr>
            """
        )
    refresh_meta = '<meta http-equiv="refresh" content="5">' if job["status"] in {"queued", "running"} else ""
    results_button = (
        f'<a class="btn btn-success" href="/search_jobs/{h(job_id)}/results">Open Results</a>'
        if job["record_count"]
        else '<button class="btn btn-outline-secondary" disabled>No results yet</button>'
    )
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Search Job</title>
        {refresh_meta}
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    </head>
    <body>
    <div class="container-fluid px-4 mt-4">
        <div class="d-flex justify-content-between align-items-center mb-3">
            <div>
                <h2>Search Job Progress</h2>
                <p class="mb-0">Active Substance: <strong>{h(job["substance"])}</strong></p>
                <p class="mb-0">Mode: <strong>{h(job["mode"])}</strong></p>
                <p class="text-muted small mb-0">Job ID: {h(job_id)}</p>
            </div>
            <div class="d-flex gap-2">
                <a class="btn btn-secondary" href="/">Back</a>
                <a class="btn btn-outline-secondary" href="/search_jobs">Recent Searches</a>
                {results_button}
            </div>
        </div>
        <div class="alert alert-info">
            Status: <strong>{h(job["status"])}</strong>. Records found so far: <strong>{h(job["record_count"])}</strong>.
            This page refreshes every 5 seconds while the job is running.
            Fast mode skips heavy sources; use Full Background Search when you need every selected source.
        </div>
        <table class="table table-striped table-bordered align-middle">
            <thead class="table-dark">
                <tr>
                    <th>Source</th>
                    <th>Status</th>
                    <th>Records</th>
                    <th>Error</th>
                </tr>
            </thead>
            <tbody>{"".join(rows)}</tbody>
        </table>
    </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html)


@app.get("/search_jobs/{job_id}/results", response_class=HTMLResponse)
def search_job_results_page(job_id: str):
    job = get_search_job(job_id)
    if not job:
        return HTMLResponse("<h2>Search job not found</h2>", status_code=404)
    rows = enriched_cached_results(get_search_job_results(job_id))
    body_rows = []
    for row in rows[:200]:
        display_row = formatted_result_row(row, searched_substance=job["substance"])
        body_rows.append(
            f"""
            <tr>
                <td>{h(display_row["molecule"])}</td>
                <td>{h(display_row["product"])}</td>
                <td>{h(display_row["country"])}</td>
                <td>{h(display_row["source"])}</td>
                <td>{h(display_row["registration_status"])}</td>
                <td>{h(row.get("data_confidence", ""))}</td>
                <td>{link_or_unavailable(display_row["product_details_url"], "Open Product")}</td>
            </tr>
            """
        )
    if not body_rows:
        body_rows.append('<tr><td colspan="7" class="text-center text-muted">No records found yet.</td></tr>')
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Search Job Results</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    </head>
    <body>
    <div class="container-fluid px-4 mt-4">
        <div class="d-flex justify-content-between align-items-center mb-3">
            <div>
                <h2>Search Job Results</h2>
                <p class="mb-0">Active Substance: <strong>{h(job["substance"])}</strong></p>
                <p class="text-muted small mb-0">Showing up to 200 records from this background job.</p>
            </div>
            <div class="d-flex gap-2">
                <a class="btn btn-secondary" href="/search_jobs/{h(job_id)}">Progress</a>
                <a class="btn btn-success" href="/search_jobs/{h(job_id)}/export">Export Excel</a>
                <a class="btn btn-secondary" href="/">Back</a>
            </div>
        </div>
        <table class="table table-striped table-bordered align-middle">
            <thead class="table-dark">
                <tr>
                    <th>Molecule</th>
                    <th>Product</th>
                    <th>Country</th>
                    <th>Source</th>
                    <th>Status</th>
                    <th>Confidence</th>
                    <th>Product Details</th>
                </tr>
            </thead>
            <tbody>{"".join(body_rows)}</tbody>
        </table>
    </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html)


@app.get("/search_jobs/{job_id}/export")
def export_search_job_results(job_id: str):
    job = get_search_job(job_id)
    if not job:
        return HTMLResponse("<h2>Search job not found</h2>", status_code=404)
    rows = enriched_cached_results(get_search_job_results(job_id))
    path = write_excel_export(job["substance"], rows, deep=False)
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=Path(path).name,
    )


@app.get("/search_page", response_class=HTMLResponse)
def search_page(
    substance: str,
    live: bool = True,
    sources: list[str] | None = Query(default=None),
    page: int = 1,
    page_size: int = 50,
    country: str = "",
    region: str = "",
    source: str = "",
    status: str = "",
    table_search: str = "",
    substance_filter: str = "",
    product_filter: str = "",
    company_filter: str = "",
    strength_filter: str = "",
    dosage_form_filter: str = "",
    pack_size_filter: str = "",
    atc_code_filter: str = "",
    therapeutic_category_filter: str = "",
    ma_holder_filter: str = "",
    manufacturer_name_filter: str = "",
    manufacturer_country_filter: str = "",
    registration_number_filter: str = "",
    registration_date_filter: str = "",
    sort_by: str = "",
    sort_dir: str = "asc",
    search_mode: str = "fast",
    export_all: bool = False,
):
    if not substance.strip():
        return HTMLResponse(
            content="""
            <!DOCTYPE html>
            <html>
            <head>
                <title>PharmaSearch Results</title>
                <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
            </head>
            <body>
            <div class="container mt-5">
                <h2>PharmaSearch Results</h2>
                <div class="alert alert-warning">
                    <strong>Please enter the active substance name.</strong>
                </div>
                <a class="btn btn-primary" href="/">Back to search</a>
            </div>
            </body>
            </html>
            """,
            status_code=200,
        )
    sort_dir = "desc" if sort_dir == "desc" else "asc"
    status = english_text(status)
    requested_sources = sources if isinstance(sources, list) else [sources] if sources else []
    if not requested_sources:
        sources = sorted(FAST_BACKGROUND_SOURCES)
        requested_sources = list(sources)
    requested_source_names = {str(item).strip().lower() for item in requested_sources if str(item).strip()}
    normalized_search_mode = "full" if search_mode == "full" else "fast"
    slow_live_sources = {
        "cdsco india",
        "grls russia",
        "mhra",
        "ema",
        "tga australia",
        "medsafe new zealand",
        "eu mri product index",
        "belgium famhp",
        "ireland medicines.ie",
    }
    country_default_source = COUNTRY_SOURCE_DEFAULTS.get(country, "").strip().lower()
    auto_slow_country_source = (
        normalized_search_mode == "fast"
        and country_default_source in slow_live_sources
        and country_default_source not in requested_source_names
    )
    effective_live = bool(live and not auto_slow_country_source)
    live_timeout = 45 if normalized_search_mode == "full" else 5
    rows, selected_sources, all_rows = filtered_search_results(
        substance,
        live=effective_live,
        sources=sources,
        country=country,
        region=region,
        source=source,
        status=status,
        table_search=table_search,
        substance_filter=substance_filter,
        product_filter=product_filter,
        company_filter=company_filter,
        strength_filter=strength_filter,
        dosage_form_filter=dosage_form_filter,
        pack_size_filter=pack_size_filter,
        atc_code_filter=atc_code_filter,
        therapeutic_category_filter=therapeutic_category_filter,
        ma_holder_filter=ma_holder_filter,
        manufacturer_name_filter=manufacturer_name_filter,
        manufacturer_country_filter=manufacturer_country_filter,
        registration_number_filter=registration_number_filter,
        registration_date_filter=registration_date_filter,
        sort_by=sort_by,
        sort_dir=sort_dir,
        live_timeout=live_timeout,
        include_lookup_rows=True,
    )
    SEARCH_RESULT_CACHE[
        result_cache_key(
            substance,
            effective_live,
            selected_sources,
            country=country,
            region=region,
            source=source,
            status=status,
            table_search=table_search,
            substance_filter=substance_filter,
            product_filter=product_filter,
            company_filter=company_filter,
            strength_filter=strength_filter,
            dosage_form_filter=dosage_form_filter,
            pack_size_filter=pack_size_filter,
            atc_code_filter=atc_code_filter,
            therapeutic_category_filter=therapeutic_category_filter,
            ma_holder_filter=ma_holder_filter,
            manufacturer_name_filter=manufacturer_name_filter,
            manufacturer_country_filter=manufacturer_country_filter,
            registration_number_filter=registration_number_filter,
            registration_date_filter=registration_date_filter,
            sort_by=sort_by,
            sort_dir=sort_dir,
        )
    ] = [dict(row) for row in rows]
    page = max(page, 1)
    page_size = min(max(page_size, 10), 200)
    total_pages = max(1, ceil(len(rows) / page_size))
    page = min(page, total_pages)
    start = (page - 1) * page_size
    visible_rows = rows[start : start + page_size]
    if effective_live and visible_rows:
        visible_rows = enriched_cached_results(visible_rows)

    def is_manual_registry_row(row):
        return row.get("connector_mode") == "manual_registry"

    product_rows = [row for row in rows if not is_manual_registry_row(row)]
    registry_rows = [row for row in rows if is_manual_registry_row(row)]
    product_all_rows = [row for row in all_rows if not is_manual_registry_row(row)]
    registry_all_rows = [row for row in all_rows if is_manual_registry_row(row)]

    source_counts = {}
    country_counts = {}
    for row in rows:
        source_counts[row.get("source") or "Unknown"] = source_counts.get(row.get("source") or "Unknown", 0) + 1
        country_counts[row.get("country") or "Unknown"] = country_counts.get(row.get("country") or "Unknown", 0) + 1

    common_query = [("substance", substance), ("live", str(live).lower())]
    common_query.extend(("sources", selected_source) for selected_source in selected_sources)
    common_query.extend(
        (key, value)
        for key, value in [
            ("country", country),
            ("region", region),
            ("source", source),
            ("status", status),
            ("table_search", table_search),
            ("substance_filter", substance_filter),
            ("product_filter", product_filter),
            ("company_filter", company_filter),
            ("strength_filter", strength_filter),
            ("dosage_form_filter", dosage_form_filter),
            ("pack_size_filter", pack_size_filter),
            ("atc_code_filter", atc_code_filter),
            ("therapeutic_category_filter", therapeutic_category_filter),
            ("ma_holder_filter", ma_holder_filter),
            ("manufacturer_name_filter", manufacturer_name_filter),
            ("manufacturer_country_filter", manufacturer_country_filter),
            ("registration_number_filter", registration_number_filter),
            ("registration_date_filter", registration_date_filter),
            ("sort_by", sort_by),
            ("sort_dir", sort_dir),
            ("search_mode", normalized_search_mode),
        ]
        if value
    )

    export_query = urlencode(
        [("sources", selected_source) for selected_source in selected_sources]
        + [
            (key, value)
            for key, value in [
                ("live", str(live).lower()),
                ("country", country),
                ("region", region),
                ("source", source),
                ("status", status),
                ("table_search", table_search),
                ("substance_filter", substance_filter),
                ("product_filter", product_filter),
                ("company_filter", company_filter),
                ("strength_filter", strength_filter),
                ("dosage_form_filter", dosage_form_filter),
                ("pack_size_filter", pack_size_filter),
                ("atc_code_filter", atc_code_filter),
                ("therapeutic_category_filter", therapeutic_category_filter),
                ("ma_holder_filter", ma_holder_filter),
                ("manufacturer_name_filter", manufacturer_name_filter),
                ("manufacturer_country_filter", manufacturer_country_filter),
                ("registration_number_filter", registration_number_filter),
                ("registration_date_filter", registration_date_filter),
                ("sort_by", sort_by),
                ("sort_dir", sort_dir),
                ("search_mode", normalized_search_mode),
                ("page", str(page)),
                ("page_size", str(page_size)),
            ]
            if value
        ]
    )
    export_href = f"/export/{quote(substance)}"
    if export_query:
        export_href = f"{export_href}?{export_query}"
    export_all_query = urlencode(
        [("sources", selected_source) for selected_source in selected_sources]
        + [
            (key, value)
            for key, value in [
                ("live", str(live).lower()),
                ("country", country),
                ("region", region),
                ("source", source),
                ("status", status),
                ("table_search", table_search),
                ("substance_filter", substance_filter),
                ("product_filter", product_filter),
                ("company_filter", company_filter),
                ("strength_filter", strength_filter),
                ("dosage_form_filter", dosage_form_filter),
                ("pack_size_filter", pack_size_filter),
                ("atc_code_filter", atc_code_filter),
                ("therapeutic_category_filter", therapeutic_category_filter),
                ("ma_holder_filter", ma_holder_filter),
                ("manufacturer_name_filter", manufacturer_name_filter),
                ("manufacturer_country_filter", manufacturer_country_filter),
                ("registration_number_filter", registration_number_filter),
                ("registration_date_filter", registration_date_filter),
                ("sort_by", sort_by),
                ("sort_dir", sort_dir),
                ("search_mode", normalized_search_mode),
                ("export_all", "true"),
            ]
            if value
        ]
    )
    export_all_href = f"/export/{quote(substance)}"
    if export_all_query:
        export_all_href = f"{export_all_href}?{export_all_query}"

    def page_href(page_number):
        query = list(common_query)
        query.extend([("page", str(page_number)), ("page_size", str(page_size))])
        return f"/search_page?{urlencode(query)}"

    def sort_href(column):
        next_dir = "desc" if sort_by == column and sort_dir == "asc" else "asc"
        query = list(common_query)
        query.extend(
            [
                ("page", "1"),
                ("page_size", str(page_size)),
                ("sort_by", column),
                ("sort_dir", next_dir),
            ]
        )
        return f"/search_page?{urlencode(query)}"

    def sort_label(column, label):
        marker = ""
        if sort_by == column:
            marker = " v" if sort_dir == "asc" else " ^"
        return f'<a class="link-light" href="{h(sort_href(column))}">{h(label)}{marker}</a>'

    visible_product_rows = [row for row in visible_rows if not is_manual_registry_row(row)]
    visible_registry_rows = [row for row in visible_rows if is_manual_registry_row(row)]

    body_rows = []
    for row in visible_product_rows:
        display_row = formatted_result_row(row, searched_substance=substance)
        product_link = link_or_unavailable(display_row["product_details_url"], "Open Product")
        smpc_link = link_or_unavailable(display_row["smpc_url"], "Open SmPC")
        pil_link = link_or_unavailable(display_row["pil_url"], "Open PIL")
        assessment_link = link_or_unavailable(
            display_row["assessment_report_url"],
            "Open Assessment",
        )
        body_rows.append(
            f"""
            <tr>
                <td>{h(display_row["molecule"])}</td>
                <td>{h(display_row["product"])}</td>
                <td>{h(display_row["company"])}</td>
                <td>{h(display_row["country"])}</td>
                <td>{h(display_row["region"])}</td>
                <td>{h(display_row["registration_status"])}</td>
                <td>{h(display_row["source"])}</td>
                <td>{product_link}</td>
                <td>{h(display_row["strength"])}</td>
                <td>{h(display_row["dosage_form"])}</td>
                <td>{h(display_row["pack_size"])}</td>
                <td>{h(display_row["atc_code"])}</td>
                <td>{h(display_row["therapeutic_category"])}</td>
                <td>{h(display_row["ma_holder"])}</td>
                <td>{h(display_row["manufacturer_name"])}</td>
                <td>{h(display_row["manufacturer_country"])}</td>
                <td>{h(display_row["registration_status"])}</td>
                <td>{h(display_row["registration_number"])}</td>
                <td>{h(display_row["registration_date"])}</td>
                <td>{smpc_link}</td>
                <td>{pil_link}</td>
                <td>{assessment_link}</td>
                <td>{h(row.get("data_confidence", ""))}</td>
                <td>{h(row.get("enrichment_status", ""))}</td>
                <td>{h(row.get("missing_fields", ""))}</td>
            </tr>
            """
        )
    if not body_rows:
        body_rows.append(
            """
            <tr>
                <td colspan="25" class="text-center text-muted py-4">
                    No direct product records on this page. Use the official source links above for manual verification.
                </td>
            </tr>
            """
        )

    registry_link_rows = []
    for row in visible_registry_rows:
        display_row = formatted_result_row(row, searched_substance=substance)
        registry_link_rows.append(
            f"""
            <tr>
                <td>{h(display_row["country"])}</td>
                <td>{h(display_row["source"])}</td>
                <td>{h(display_row["registration_status"])}</td>
                <td>{link_or_unavailable(display_row["product_details_url"], "Open Official Registry")}</td>
            </tr>
            """
        )
    registry_links_section = ""
    if registry_link_rows:
        registry_links_section = f"""
        <div class="alert alert-info border mt-3">
            <strong>Official source links:</strong> Product data was not extracted from these countries yet. Use these regulator links for manual verification.
            <div class="table-responsive mt-2">
                <table class="table table-sm table-bordered bg-white mb-0">
                    <thead>
                        <tr>
                            <th>Country</th>
                            <th>Source</th>
                            <th>Status</th>
                            <th>Official Link</th>
                        </tr>
                    </thead>
                    <tbody>{"".join(registry_link_rows)}</tbody>
                </table>
            </div>
        </div>
        """

    previous_button = (
        f'<a class="btn btn-outline-primary" href="{h(page_href(page - 1))}">Previous</a>'
        if page > 1
        else '<button class="btn btn-outline-secondary" disabled>Previous</button>'
    )
    next_button = (
        f'<a class="btn btn-outline-primary" href="{h(page_href(page + 1))}">Next</a>'
        if page < total_pages
        else '<button class="btn btn-outline-secondary" disabled>Next</button>'
    )
    source_summary = ", ".join(
        f"{h(source)}: {count}" for source, count in sorted(source_counts.items())
    )
    country_summary = ", ".join(
        f"{h(country)}: {count}" for country, count in sorted(country_counts.items())
    )
    country_options = option_tags(
        merged_options(unique_values(all_rows, "country"), COUNTRY_OPTIONS),
        country,
        "All countries",
    )
    region_options = option_tags(
        merged_options(unique_values(all_rows, "region"), REGION_OPTIONS),
        region,
        "All regions",
    )
    source_options = option_tags(unique_values(all_rows, "source"), source, "All sources")
    formatted_all_rows = [formatted_result_row(row, searched_substance=substance) for row in all_rows]
    status_options = option_tags(
        unique_values(formatted_all_rows, "registration_status"),
        status,
        "All statuses",
    )
    source_inputs = "".join(
        f'<input type="hidden" name="sources" value="{h(selected_source)}">'
        for selected_source in selected_sources
    )
    active_filter_parts = []
    for label, value in [
        ("Country", country),
        ("Region", region),
        ("Source", source),
        ("Status", status),
        ("Substance", substance_filter),
        ("Product", product_filter),
        ("Company", company_filter),
        ("Strength", strength_filter),
        ("Dosage Form", dosage_form_filter),
        ("Pack Size", pack_size_filter),
        ("ATC Code", atc_code_filter),
        ("Therapeutic Category", therapeutic_category_filter),
        ("MA Holder", ma_holder_filter),
        ("Manufacturer Name", manufacturer_name_filter),
        ("Manufacturer Country", manufacturer_country_filter),
        ("Registration Number", registration_number_filter),
        ("Registration Date", registration_date_filter),
    ]:
        if value:
            active_filter_parts.append(f"{h(label)}: <strong>{h(value)}</strong>")
    active_filter_summary = (
        " | ".join(active_filter_parts)
        if active_filter_parts
        else "No column filters active"
    )
    if product_rows:
        result_alert_class = "success"
        result_alert_title = "Results found"
        result_alert_detail = (
            f"Showing {len(product_rows)} product records"
            f"{f' and {len(registry_rows)} official registry links' if registry_rows else ''} "
            f"from {len(product_all_rows)} selected-source product records."
        )
    elif registry_rows:
        result_alert_class = "info"
        result_alert_title = "Official source links"
        result_alert_detail = (
            f"No direct product records were extracted. Showing {len(registry_rows)} official regulator "
            f"links at the top for manual verification."
        )
    else:
        result_alert_class = "warning"
        result_alert_title = "No results found"
        result_alert_detail = (
            f"No records matched the active substance and filters. "
            f"The selected sources returned {len(all_rows)} records before column filters."
        )
    no_results_help = ""
    if not rows:
        no_results_help = (
            '<div class="alert alert-light border">'
            '<strong>Try this:</strong> clear Country/Region/Source/Status filters, '
            'check spelling of the active substance, or search with all sources selected.'
            '</div>'
        )
    eu_countries_in_results = {
        row.get("country")
        for row in rows
        if row.get("region") == "EU" and row.get("country") in EU_COUNTRIES
    }
    eu_coverage_note = ""
    if region == "EU":
        note_class = "success" if len(eu_countries_in_results) == len(EU_COUNTRIES) else "warning"
        eu_coverage_note = (
            f'<p class="alert alert-{note_class} py-2">'
            f'EU country coverage: <strong>{len(eu_countries_in_results)}/{len(EU_COUNTRIES)}</strong> '
            f'EU countries shown for this substance from the currently available EU sources.'
            f'</p>'
        )
    clear_href = "/search_page?" + urlencode(
        [("substance", substance), ("live", str(live).lower())]
        + [("sources", selected_source) for selected_source in selected_sources]
        + [("page_size", str(page_size)), ("search_mode", normalized_search_mode)]
    )
    has_active_filters = any(
        [
            country,
            region,
            source,
            status,
            table_search,
            substance_filter,
            product_filter,
            company_filter,
            strength_filter,
            dosage_form_filter,
            pack_size_filter,
            atc_code_filter,
            therapeutic_category_filter,
            ma_holder_filter,
            manufacturer_name_filter,
            manufacturer_country_filter,
            registration_number_filter,
            registration_date_filter,
        ]
    )
    show_all_button = (
        f'<a class="btn btn-warning" href="{h(clear_href)}">Show all results</a>'
        if has_active_filters
        else ""
    )

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>PharmaSearch Results</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
        <style>
            .results-table {{
                font-size: 0.84rem;
                min-width: 3350px;
                table-layout: auto;
            }}
            .results-table th,
            .results-table td {{
                vertical-align: top;
                white-space: normal;
                overflow-wrap: anywhere;
            }}
            .results-table th {{
                min-width: 130px;
            }}
            .results-table th:nth-child(2),
            .results-table td:nth-child(2) {{
                min-width: 280px;
            }}
            .results-table th:nth-child(8),
            .results-table td:nth-child(8),
            .results-table th:nth-child(18),
            .results-table td:nth-child(18),
            .results-table th:nth-child(19),
            .results-table td:nth-child(19),
            .results-table th:nth-child(20),
            .results-table td:nth-child(20) {{
                min-width: 230px;
            }}
            .results-table th:nth-child(12),
            .results-table td:nth-child(12),
            .results-table th:nth-child(13),
            .results-table td:nth-child(13) {{
                min-width: 220px;
            }}
            .results-scroll {{
                overflow-x: auto;
                padding-bottom: 0.5rem;
            }}
        </style>
    </head>
    <body>
    <div class="container-fluid px-4 mt-4">
        <h2>PharmaSearch Results</h2>
        <p>Active Substance: <strong>{h(substance)}</strong></p>
        <div class="alert alert-{result_alert_class}">
            <strong>{result_alert_title}:</strong> {h(result_alert_detail)}
        </div>
        {no_results_help}
        <p class="small text-muted mb-1">By source: {source_summary}</p>
        <p class="small text-muted">By country: {country_summary}</p>
        <p class="small">Active filters: {active_filter_summary}</p>
        {eu_coverage_note}
        {registry_links_section}
        <div class="d-flex justify-content-between align-items-center mb-3">
            <div>Page <strong>{page}</strong> of <strong>{total_pages}</strong> ({start + 1 if rows else 0}-{min(start + page_size, len(rows))} shown)</div>
            <div class="d-flex gap-2">
                {show_all_button}
                <a class="btn btn-secondary" href="/">Back</a>
                <a class="btn btn-outline-success" href="{h(export_all_href)}">Export All Excel</a>
                <a class="btn btn-success" href="{h(export_href)}">Export Current Page Excel</a>
                <div class="btn-group">{previous_button}{next_button}</div>
            </div>
        </div>
        <form id="result-filter-form" method="get" action="/search_page">
            <input type="hidden" name="substance" value="{h(substance)}">
            <input type="hidden" name="live" value="{h(str(live).lower())}">
            <input type="hidden" name="page" value="1">
            <input type="hidden" name="page_size" value="{page_size}">
            <input type="hidden" name="table_search" value="{h(table_search)}">
            <input type="hidden" name="sort_by" value="{h(sort_by)}">
            <input type="hidden" name="sort_dir" value="{h(sort_dir)}">
            <input type="hidden" name="search_mode" value="{h(normalized_search_mode)}">
            {source_inputs}
        </form>
        <div class="results-scroll">
        <table class="table table-striped table-bordered align-middle results-table">
            <thead>
                <tr class="table-dark">
                    <th>{sort_label("substance", "Molecule")}</th>
                    <th>{sort_label("product", "Product Name")}</th>
                    <th>{sort_label("company", "Company")}</th>
                    <th>{sort_label("country", "Country")}</th>
                    <th>{sort_label("region", "Region")}</th>
                    <th>{sort_label("status", "Status")}</th>
                    <th>{sort_label("source", "Source")}</th>
                    <th>Product Details</th>
                    <th>{sort_label("strength", "Strength")}</th>
                    <th>{sort_label("dosage_form", "Dosage Form")}</th>
                    <th>{sort_label("pack_size", "Pack Size")}</th>
                    <th>{sort_label("atc_code", "ATC Code")}</th>
                    <th>{sort_label("therapeutic_category", "Therapeutic Category")}</th>
                    <th>{sort_label("ma_holder", "MA Holder Name")}</th>
                    <th>{sort_label("manufacturer_name", "Manufacturer Name")}</th>
                    <th>{sort_label("manufacturer_country", "Manufacturer Country")}</th>
                    <th>Registration Status</th>
                    <th>{sort_label("registration_number", "Registration Number")}</th>
                    <th>{sort_label("registration_date", "Registration Date")}</th>
                    <th>SMPC URL</th>
                    <th>PIL URL</th>
                    <th>Assessment Report URL</th>
                    <th>Data Confidence</th>
                    <th>Enrichment Status</th>
                    <th>Missing Fields</th>
                </tr>
                <tr class="table-secondary">
                    <th><input form="result-filter-form" class="form-control form-control-sm" name="substance_filter" value="{h(substance_filter)}" placeholder="Filter"></th>
                    <th><input form="result-filter-form" class="form-control form-control-sm" name="product_filter" value="{h(product_filter)}" placeholder="Filter"></th>
                    <th><input form="result-filter-form" class="form-control form-control-sm" name="company_filter" value="{h(company_filter)}" placeholder="Filter"></th>
                    <th><select id="country-filter" form="result-filter-form" class="form-select form-select-sm" name="country">{country_options}</select></th>
                    <th><select id="region-filter" form="result-filter-form" class="form-select form-select-sm" name="region">{region_options}</select></th>
                    <th><select form="result-filter-form" class="form-select form-select-sm" name="status" onchange="this.form.submit()">{status_options}</select></th>
                    <th><select form="result-filter-form" class="form-select form-select-sm" name="source" onchange="this.form.submit()">{source_options}</select></th>
                    <th><a class="btn btn-sm btn-outline-secondary w-100" href="{h(clear_href)}">Clear</a></th>
                    <th><input form="result-filter-form" class="form-control form-control-sm" name="strength_filter" value="{h(strength_filter)}" placeholder="Filter"></th>
                    <th><input form="result-filter-form" class="form-control form-control-sm" name="dosage_form_filter" value="{h(dosage_form_filter)}" placeholder="Filter"></th>
                    <th><input form="result-filter-form" class="form-control form-control-sm" name="pack_size_filter" value="{h(pack_size_filter)}" placeholder="Filter"></th>
                    <th><input form="result-filter-form" class="form-control form-control-sm" name="atc_code_filter" value="{h(atc_code_filter)}" placeholder="Filter"></th>
                    <th><input form="result-filter-form" class="form-control form-control-sm" name="therapeutic_category_filter" value="{h(therapeutic_category_filter)}" placeholder="Filter"></th>
                    <th><input form="result-filter-form" class="form-control form-control-sm" name="ma_holder_filter" value="{h(ma_holder_filter)}" placeholder="Filter"></th>
                    <th><input form="result-filter-form" class="form-control form-control-sm" name="manufacturer_name_filter" value="{h(manufacturer_name_filter)}" placeholder="Filter"></th>
                    <th><input form="result-filter-form" class="form-control form-control-sm" name="manufacturer_country_filter" value="{h(manufacturer_country_filter)}" placeholder="Filter"></th>
                    <th></th>
                    <th><input form="result-filter-form" class="form-control form-control-sm" name="registration_number_filter" value="{h(registration_number_filter)}" placeholder="Filter"></th>
                    <th><input form="result-filter-form" class="form-control form-control-sm" name="registration_date_filter" value="{h(registration_date_filter)}" placeholder="Filter"></th>
                    <th></th>
                    <th></th>
                    <th><button form="result-filter-form" class="btn btn-sm btn-primary w-100">Apply</button></th>
                    <th></th>
                    <th></th>
                    <th></th>
                </tr>
            </thead>
            <tbody>{"".join(body_rows)}</tbody>
        </table>
        </div>
        <div class="d-flex justify-content-between align-items-center mb-3">
            <div>Page <strong>{page}</strong> of <strong>{total_pages}</strong></div>
            <div class="btn-group">{previous_button}{next_button}</div>
        </div>
        <a class="btn btn-secondary" href="/">Back</a>
    </div>
    <script>
    const resultFilterForm = document.getElementById("result-filter-form");
    const countryFilter = document.getElementById("country-filter");
    const regionFilter = document.getElementById("region-filter");
    function submitFilters() {{
        resultFilterForm.submit();
    }}
    countryFilter.addEventListener("change", function () {{
        if (countryFilter.value) {{
            regionFilter.value = "";
        }}
        submitFilters();
    }});
    regionFilter.addEventListener("change", function () {{
        if (regionFilter.value) {{
            countryFilter.value = "";
        }}
        submitFilters();
    }});
    document.querySelectorAll('select[form="result-filter-form"]:not(#country-filter):not(#region-filter)').forEach(function (control) {{
        control.addEventListener("change", submitFilters);
    }});
    </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html)


@app.get("/export/{substance}")
def export_live(
    substance: str,
    live: bool = True,
    sources: list[str] = Query(default=DEFAULT_SOURCES),
    page: int = 1,
    page_size: int = 50,
    country: str = "",
    region: str = "",
    source: str = "",
    status: str = "",
    table_search: str = "",
    substance_filter: str = "",
    product_filter: str = "",
    company_filter: str = "",
    strength_filter: str = "",
    dosage_form_filter: str = "",
    pack_size_filter: str = "",
    atc_code_filter: str = "",
    therapeutic_category_filter: str = "",
    ma_holder_filter: str = "",
    manufacturer_name_filter: str = "",
    manufacturer_country_filter: str = "",
    registration_number_filter: str = "",
    registration_date_filter: str = "",
    sort_by: str = "",
    sort_dir: str = "asc",
    export_all: bool = False,
):
    status = english_text(status)
    cache_key = result_cache_key(
        substance,
        live,
        sources,
        country=country,
        region=region,
        source=source,
        status=status,
        table_search=table_search,
        substance_filter=substance_filter,
        product_filter=product_filter,
        company_filter=company_filter,
        strength_filter=strength_filter,
        dosage_form_filter=dosage_form_filter,
        pack_size_filter=pack_size_filter,
        atc_code_filter=atc_code_filter,
        therapeutic_category_filter=therapeutic_category_filter,
        ma_holder_filter=ma_holder_filter,
        manufacturer_name_filter=manufacturer_name_filter,
        manufacturer_country_filter=manufacturer_country_filter,
        registration_number_filter=registration_number_filter,
        registration_date_filter=registration_date_filter,
        sort_by=sort_by,
        sort_dir=sort_dir,
    )
    results = SEARCH_RESULT_CACHE.get(cache_key)
    if results is None:
        results, _, _ = filtered_search_results(
            substance,
            live=live,
            sources=sources,
            country=country,
            region=region,
            source=source,
            status=status,
            table_search=table_search,
            substance_filter=substance_filter,
            product_filter=product_filter,
            company_filter=company_filter,
            strength_filter=strength_filter,
            dosage_form_filter=dosage_form_filter,
            pack_size_filter=pack_size_filter,
            atc_code_filter=atc_code_filter,
            therapeutic_category_filter=therapeutic_category_filter,
            ma_holder_filter=ma_holder_filter,
            manufacturer_name_filter=manufacturer_name_filter,
            manufacturer_country_filter=manufacturer_country_filter,
            registration_number_filter=registration_number_filter,
            registration_date_filter=registration_date_filter,
            sort_by=sort_by,
            sort_dir=sort_dir,
            live_timeout=COMPLETE_SEARCH_TIMEOUT_SECONDS,
            include_lookup_rows=False,
        )
    if not export_all:
        page = max(page, 1)
        page_size = min(max(page_size, 10), 200)
        start = (page - 1) * page_size
        results = results[start : start + page_size]
    if not export_all:
        results = enriched_cached_results(results)
    path = write_excel_export(substance, results, deep=False)
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=Path(path).name,
    )


@app.get("/deep_export/{substance}")
def deep_export_live(
    substance: str,
    live: bool = True,
    sources: list[str] = Query(default=DEFAULT_SOURCES),
    country: str = "",
    region: str = "",
    source: str = "",
    status: str = "",
    table_search: str = "",
    substance_filter: str = "",
    product_filter: str = "",
    company_filter: str = "",
    strength_filter: str = "",
    dosage_form_filter: str = "",
    pack_size_filter: str = "",
    atc_code_filter: str = "",
    therapeutic_category_filter: str = "",
    ma_holder_filter: str = "",
    manufacturer_name_filter: str = "",
    manufacturer_country_filter: str = "",
    registration_number_filter: str = "",
    registration_date_filter: str = "",
    sort_by: str = "",
    sort_dir: str = "asc",
):
    query = urlencode(
        [("sources", selected_source) for selected_source in parse_sources(sources)]
        + [
            (key, value)
            for key, value in [
                ("live", str(live).lower()),
                ("country", country),
                ("region", region),
                ("source", source),
                ("status", status),
                ("table_search", table_search),
                ("substance_filter", substance_filter),
                ("product_filter", product_filter),
                ("company_filter", company_filter),
                ("strength_filter", strength_filter),
                ("dosage_form_filter", dosage_form_filter),
                ("pack_size_filter", pack_size_filter),
                ("atc_code_filter", atc_code_filter),
                ("therapeutic_category_filter", therapeutic_category_filter),
                ("ma_holder_filter", ma_holder_filter),
                ("manufacturer_name_filter", manufacturer_name_filter),
                ("manufacturer_country_filter", manufacturer_country_filter),
                ("registration_number_filter", registration_number_filter),
                ("registration_date_filter", registration_date_filter),
                ("sort_by", sort_by),
                ("sort_dir", sort_dir),
            ]
            if value
        ]
    )
    export_url = f"/export/{quote(substance)}"
    if query:
        export_url = f"{export_url}?{query}"
    return RedirectResponse(export_url, status_code=307)


@app.get("/products", response_class=HTMLResponse)
def products():
    rows = list_product_details()
    body_rows = []
    for row in rows:
        body_rows.append(
            f"""
            <tr>
                <td>{h(row.get("product", ""))}</td>
                <td>{h(row.get("substance", ""))}</td>
                <td>{h(row.get("company", ""))}</td>
                <td>{h(row.get("country", ""))}</td>
                <td>{h(row.get("region", ""))}</td>
                <td>{h(row.get("status", ""))}</td>
                <td>{h(row.get("source", ""))}</td>
                <td>{h(row.get("registration_date", ""))}</td>
            </tr>
            """
        )
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Products</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    </head>
    <body>
    <div class="container mt-5">
        <h2>Saved Products</h2>
        <p class="alert alert-info">Total Products: <strong>{len(rows)}</strong></p>
        <table class="table table-striped table-bordered">
            <thead class="table-dark">
                <tr>
                    <th>Product</th>
                    <th>Substance</th>
                    <th>Company</th>
                    <th>Country</th>
                    <th>Region</th>
                    <th>Status</th>
                    <th>Source</th>
                    <th>Registration Date</th>
                </tr>
            </thead>
            <tbody>{"".join(body_rows)}</tbody>
        </table>
    </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html)


@app.get("/reset_db")
def reset_db():
    reset_database()
    return {"message": "Database cleared"}


@app.get("/connector_status")
def connector_status():
    return {"connectors": connector_status_rows()}


@app.get("/connector_health")
def connector_health():
    return {"connectors": connector_health_rows()}


@app.get("/ai_status")
def ai_status():
    return current_ai_status()


@app.get("/connector_status_page")
def connector_status_page():
    rows = connector_status_rows()
    body_rows = []
    for row in rows:
        countries = ", ".join(row.get("countries") or [])
        body_rows.append(
            f"""
            <tr>
                <td>{h(row["name"])}</td>
                <td>{h(row["region"])}</td>
                <td>{h(countries)}</td>
                <td>{h(row["tier"])}</td>
                <td>{h(row["mode"])}</td>
                <td>{h(row["status"])}</td>
                <td>{h("Yes" if row["enabled"] else "No")}</td>
                <td>{h(row["notes"])}</td>
            </tr>
            """
        )
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Connector Status</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    </head>
    <body>
    <div class="container-fluid px-4 mt-4">
        <div class="d-flex justify-content-between align-items-center mb-3">
            <h2>Core Connector Status</h2>
            <a class="btn btn-secondary" href="/">Back</a>
        </div>
        <div class="alert alert-info">
            Step 1 baseline: core sources are classified as ready, partial, or browser-permission required.
        </div>
        <table class="table table-striped table-bordered align-middle">
            <thead class="table-dark">
                <tr>
                    <th>Source</th>
                    <th>Region</th>
                    <th>Countries</th>
                    <th>Tier</th>
                    <th>Mode</th>
                    <th>Status</th>
                    <th>Enabled</th>
                    <th>Notes</th>
                </tr>
            </thead>
            <tbody>{"".join(body_rows)}</tbody>
        </table>
    </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html)


@app.get("/connector_health_page")
def connector_health_page():
    rows = connector_health_rows()
    body_rows = []
    for row in rows:
        countries = ", ".join(row.get("countries") or [])
        body_rows.append(
            f"""
            <tr>
                <td>{h(row["name"])}</td>
                <td>{h(row["region"])}</td>
                <td>{h(countries)}</td>
                <td>{h(row["tier"])}</td>
                <td>{h(row["mode"])}</td>
                <td>{h(row["status"])}</td>
                <td>{h(row["last_status"])}</td>
                <td>{h(row["last_records"])}</td>
                <td>{h(row["last_elapsed_seconds"])}</td>
                <td>{h(row["last_checked"])}</td>
                <td>{h(row["last_error"])}</td>
                <td>{h(row["notes"])}</td>
            </tr>
            """
        )
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Connector Health</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    </head>
    <body>
    <div class="container-fluid px-4 mt-4">
        <div class="d-flex justify-content-between align-items-center mb-3">
            <h2>Connector Health</h2>
            <a class="btn btn-secondary" href="/">Back</a>
        </div>
        <div class="alert alert-info">
            Health shows the static connector readiness plus the last background-search result recorded during this server session.
        </div>
        <div class="table-responsive">
            <table class="table table-striped table-bordered align-middle">
                <thead class="table-dark">
                    <tr>
                        <th>Source</th>
                        <th>Region</th>
                        <th>Countries</th>
                        <th>Tier</th>
                        <th>Mode</th>
                        <th>Static Status</th>
                        <th>Last Status</th>
                        <th>Records</th>
                        <th>Seconds</th>
                        <th>Last Checked</th>
                        <th>Error</th>
                        <th>Notes</th>
                    </tr>
                </thead>
                <tbody>{"".join(body_rows)}</tbody>
            </table>
        </div>
    </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html)


@app.get("/load_demo_data")
def load_demo_data():
    reset_database()
    demo_data = [
        ("Dapagliflozin", "Forxiga", "AstraZeneca AB", "Sweden", "Authorised", "Demo"),
        (
            "Dapagliflozin",
            "FORXIGA 10MG FILM COATED TABLETS",
            "AstraZeneca UK Limited",
            "United Kingdom",
            "Authorised",
            "Demo",
        ),
        ("Dapagliflozin", "Dapagliflozin Viatris", "Viatris", "France", "Authorised", "Demo"),
        ("Empagliflozin", "Jardiance", "Boehringer Ingelheim International GmbH", "Germany", "Authorised", "Demo"),
        ("Semaglutide", "Ozempic", "Novo Nordisk A/S", "Denmark", "Authorised", "Demo"),
        ("Metformin", "Glucophage", "Merck", "Spain", "Authorised", "Demo"),
    ]
    for substance, product, company, country, status, source in demo_data:
        save_product_detail(
            {
                "substance": substance,
                "product": product,
                "company": company,
                "country": country,
                "status": status,
                "source": source,
            }
        )
    return {"message": "Demo data loaded", "count": len(demo_data)}


@app.get("/crawl/{substance}")
def crawl_ema_search(substance: str):
    return run_ema_search(substance)


@app.get("/crawl_mhra")
def crawl_mhra(substance: str):
    url = f"https://products.mhra.gov.uk/search/?search={quote(substance)}&page=1"
    result = extract_mhra_product_page(url)
    save_product_detail(
        {
            "substance": result.get("active_substance", substance),
            "product": result.get("product_name", ""),
            "country": "United Kingdom",
            "status": "Authorised",
            "product_url": result.get("product_url", ""),
            "source": "MHRA",
        }
    )
    return result


@app.get("/mhra_full/{substance}")
def mhra_full(substance: str):
    return {"substance": substance, "results": run_mhra_search(substance)}


@app.get("/crawl_substance/{substance}")
def crawl_substance(substance: str):
    product_url = find_product_url(substance)
    if not product_url:
        return {"error": f"No product found for {substance}"}
    return crawl_url(product_url)


@app.get("/crawl_url")
def crawl_url(url: str):
    result = extract_product_page(url)
    saved = save_product_detail(
        {
            "substance": result.get("active_substance", ""),
            "product": result.get("product_name", ""),
            "company": result.get("mah", ""),
            "country": "European Union",
            "region": "EU",
            "status": result.get("status", ""),
            "product_url": result.get("product_url", url),
            "atc_code": result.get("atc_code", ""),
            "registration_date": result.get("authorisation_date", ""),
            "smpc_url": result.get("smpc_url", ""),
            "pil_url": result.get("pil_url", ""),
            "assessment_report_url": result.get("assessment_report_url", ""),
            "source": "EMA",
            "source_url": url,
        }
    )
    return {"message": "Product saved", "product": saved.get("product"), "data": result}


@app.get("/crawl_all_products/{substance}")
def crawl_all_products(substance: str):
    saved_products = []
    for product_data in run_ema_search(substance):
        try:
            result = crawl_url(product_data["url"])
            saved_products.append(result["product"])
        except Exception:
            logger.exception("EMA product crawl failed")
    return {
        "substance": substance,
        "products_saved": len(saved_products),
        "products": saved_products,
    }
