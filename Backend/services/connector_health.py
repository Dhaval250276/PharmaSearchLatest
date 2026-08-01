from __future__ import annotations

from datetime import datetime
from threading import Lock
from typing import Any

from services.connector_status import connector_status_rows


_lock = Lock()
_health: dict[str, dict[str, Any]] = {}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def record_source_health(
    source: str,
    status: str,
    records: int = 0,
    elapsed_seconds: float | None = None,
    error: str = "",
) -> None:
    if not source:
        return
    with _lock:
        _health[source] = {
            "source": source,
            "last_status": status,
            "last_records": records,
            "last_elapsed_seconds": round(elapsed_seconds or 0, 2),
            "last_error": error,
            "last_checked": _now(),
        }


def connector_health_rows() -> list[dict[str, Any]]:
    static_rows = connector_status_rows()
    with _lock:
        live_health = dict(_health)
    rows = []
    for row in static_rows:
        source = row["name"]
        health = live_health.get(source, {})
        rows.append(
            {
                **row,
                "last_status": health.get("last_status", "not checked"),
                "last_records": health.get("last_records", ""),
                "last_elapsed_seconds": health.get("last_elapsed_seconds", ""),
                "last_error": health.get("last_error", ""),
                "last_checked": health.get("last_checked", ""),
            }
        )
    return rows
