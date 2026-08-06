from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock
import time
from typing import Any
from uuid import uuid4

from core.logging_config import get_logger
from repository import (
    get_persisted_search_job,
    get_persisted_search_job_results,
    save_search_job,
    save_search_job_progress,
    save_search_job_results,
)
from services.search_pipeline import (
    parse_sources,
)
from services.connector_health import record_source_health
from sources.search_engine import SINGLE_TERM_SOURCES
from sources.source_registry import connector_by_name
from sources.synonyms import get_substance_search_terms


logger = get_logger(__name__)
JOB_WORKERS = 4
JOB_FAST_WORKERS = 10
JOB_SLOW_WORKERS = 3
JOB_SOURCE_TIMEOUT_SECONDS = 10
JOB_SLOW_SOURCE_TIMEOUT_SECONDS = 40
FAST_BACKGROUND_SOURCES = {
    "FDA",
    "FDA Orange Book",
    "FDA Purple Book",
    "Spain CIMA",
}
SLOW_SOURCES = {
    "GRLS Russia",
    "TGA Australia",
    "Medsafe New Zealand",
    "MHRA",
    "EMA",
    "EU MRI Product Index",
    "Belgium FAMHP",
    "Ireland medicines.ie",
}
_executor = ThreadPoolExecutor(max_workers=JOB_WORKERS)
_lock = Lock()
_jobs: dict[str, "SearchJob"] = {}


@dataclass
class SourceProgress:
    source: str
    status: str = "queued"
    records: int = 0
    error: str = ""
    started_at: str = ""
    finished_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "status": self.status,
            "records": self.records,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


@dataclass
class SearchJob:
    job_id: str
    substance: str
    sources: list[str]
    mode: str = "fast"
    status: str = "queued"
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    started_at: str = ""
    finished_at: str = ""
    results: list[dict[str, Any]] = field(default_factory=list)
    progress: dict[str, SourceProgress] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "substance": self.substance,
            "sources": self.sources,
            "mode": self.mode,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "record_count": len(self.results),
            "progress": [item.to_dict() for item in self.progress.values()],
        }


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def order_sources_for_job(sources: list[str]) -> list[str]:
    return sorted(
        sources,
        key=lambda source: (source in SLOW_SOURCES, source.lower()),
    )


def source_skipped_in_mode(source: str, mode: str) -> bool:
    return mode != "full" and source not in FAST_BACKGROUND_SOURCES


def create_search_job(substance: str, sources: list[str] | str | None, mode: str = "fast") -> str:
    selected_sources = parse_sources(sources)
    selected_sources = order_sources_for_job(selected_sources)
    normalized_mode = "full" if mode == "full" else "fast"
    job_id = uuid4().hex[:12]
    progress = {}
    for source in selected_sources:
        if source_skipped_in_mode(source, normalized_mode):
            progress[source] = SourceProgress(
                source=source,
                status="skipped",
                error="Skipped in fast mode. Use Full Background Search for this source.",
                finished_at=_now(),
            )
        else:
            progress[source] = SourceProgress(source=source)
    job = SearchJob(
        job_id=job_id,
        substance=substance.strip(),
        sources=selected_sources,
        mode=normalized_mode,
        progress=progress,
    )
    with _lock:
        _jobs[job_id] = job
    save_search_job(job.to_dict())
    for progress_item in job.progress.values():
        save_search_job_progress(job_id, progress_item.to_dict())
    _executor.submit(_run_job, job_id)
    return job_id


def get_search_job(job_id: str) -> dict[str, Any] | None:
    with _lock:
        job = _jobs.get(job_id)
        if job:
            return job.to_dict()
    return get_persisted_search_job(job_id)


def get_search_job_results(job_id: str) -> list[dict[str, Any]]:
    with _lock:
        job = _jobs.get(job_id)
        if job:
            return [dict(item) for item in job.results]
    return get_persisted_search_job_results(job_id)


def _set_job_status(job_id: str, status: str, finished: bool = False) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return
        job.status = status
        if status == "running" and not job.started_at:
            job.started_at = _now()
        if finished:
            job.finished_at = _now()
        job_snapshot = job.to_dict()
    save_search_job(job_snapshot)


def _set_source_progress(
    job_id: str,
    source: str,
    status: str,
    records: int = 0,
    error: str = "",
) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if not job or source not in job.progress:
            return
        progress = job.progress[source]
        progress.status = status
        progress.records = records
        progress.error = error
        if status == "running" and not progress.started_at:
            progress.started_at = _now()
        if status in {"done", "failed", "timeout", "skipped"}:
            progress.finished_at = _now()
        progress_snapshot = progress.to_dict()
    save_search_job_progress(job_id, progress_snapshot)


def _append_results(job_id: str, rows: list[dict[str, Any]]) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return
        seen = {
            (
                item.get("source", "").strip().lower(),
                item.get("product", "").strip().lower(),
                item.get("country", "").strip().lower(),
                item.get("registration_number", "").strip().lower(),
                item.get("url", "").strip().lower(),
            )
            for item in job.results
        }
        new_rows = []
        for row in rows:
            key = (
                row.get("source", "").strip().lower(),
                row.get("product", "").strip().lower(),
                row.get("country", "").strip().lower(),
                row.get("registration_number", "").strip().lower(),
                row.get("url", "").strip().lower(),
            )
            if key in seen:
                continue
            seen.add(key)
            clean_row = dict(row)
            job.results.append(clean_row)
            new_rows.append(clean_row)
    save_search_job_results(job_id, new_rows)


def _source_timeout(source: str) -> int:
    return JOB_SLOW_SOURCE_TIMEOUT_SECONDS if source in SLOW_SOURCES else JOB_SOURCE_TIMEOUT_SECONDS


def _run_connector_once(source: str, search_term: str, substance: str) -> list[dict[str, Any]]:
    connector = connector_by_name(source)
    if not connector:
        return []
    rows = []
    for item in connector.search(search_term) or []:
        row = dict(item)
        row["source_substance"] = row.get("substance", "")
        row["searched_substance"] = substance
        row["substance"] = substance
        rows.append(row)
    return rows


def _dedupe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique = []
    seen = set()
    for item in rows:
        if not item.get("product"):
            continue
        key = (
            item.get("source", "").strip().lower(),
            item.get("product", "").strip().lower(),
            item.get("country", "").strip().lower(),
            item.get("registration_number", "").strip().lower(),
            item.get("url", "").strip().lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _run_source_for_job(job_id: str, substance: str, source: str) -> tuple[str, list[dict[str, Any]], str]:
    started = time.perf_counter()
    _set_source_progress(job_id, source, "running")
    try:
        source_key = source.strip().lower()
        search_terms = [substance] if source_key in SINGLE_TERM_SOURCES else get_substance_search_terms(substance)
        rows = []
        executor = ThreadPoolExecutor(max_workers=min(4, len(search_terms)))
        futures = [
            executor.submit(_run_connector_once, source, search_term, substance)
            for search_term in search_terms
        ]
        try:
            for future in as_completed(futures, timeout=_source_timeout(source)):
                rows.extend(future.result())
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        unique_rows = _dedupe_rows(rows)
        record_source_health(source, "done", len(unique_rows), time.perf_counter() - started)
        return source, unique_rows, ""
    except TimeoutError:
        logger.warning("Search job %s source %s timed out", job_id, source)
        record_source_health(source, "timeout", 0, time.perf_counter() - started, "timeout")
        return source, [], "timeout"
    except Exception as exc:
        logger.exception("Search job %s source %s failed", job_id, source)
        record_source_health(source, "failed", 0, time.perf_counter() - started, str(exc))
        return source, [], str(exc)


def _run_job(job_id: str) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return
        substance = job.substance
        sources = [
            source
            for source in job.sources
            if job.progress.get(source) and job.progress[source].status == "queued"
        ]
    _set_job_status(job_id, "running")
    try:
        if not sources:
            _set_job_status(job_id, "done", finished=True)
            return
        max_workers = min(
            JOB_FAST_WORKERS if any(source not in SLOW_SOURCES for source in sources) else JOB_SLOW_WORKERS,
            max(1, len(sources)),
        )
        with ThreadPoolExecutor(max_workers=max_workers) as source_executor:
            futures = {
                source_executor.submit(_run_source_for_job, job_id, substance, source): source
                for source in sources
            }
            for future in as_completed(futures):
                source = futures[future]
                try:
                    source_name, rows, error = future.result()
                except Exception as exc:
                    _set_source_progress(job_id, source, "failed", error=str(exc))
                    continue
                if error:
                    status = "timeout" if error == "timeout" else "failed"
                    _set_source_progress(job_id, source_name, status, error=error)
                    continue
                _append_results(job_id, rows)
                _set_source_progress(job_id, source_name, "done", records=len(rows))
        _set_job_status(job_id, "done", finished=True)
    except Exception as exc:
        logger.exception("Search job %s failed", job_id)
        _set_job_status(job_id, "failed", finished=True)
        with _lock:
            job = _jobs.get(job_id)
            if job:
                for progress in job.progress.values():
                    if progress.status in {"queued", "running"}:
                        progress.status = "failed"
                        progress.error = str(exc)
                        progress.finished_at = _now()
