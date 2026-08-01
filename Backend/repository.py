import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from collections.abc import Iterator
from typing import Any
import re
import json

from config import DB_PATH


PRODUCT_DETAIL_COLUMNS = {
    "id",
    "substance",
    "product",
    "company",
    "country",
    "region",
    "status",
    "strength",
    "dosage_form",
    "pack_size",
    "atc_code",
    "therapeutic_category",
    "registration_number",
    "registration_date",
    "expiry_date",
    "manufacturer_name",
    "manufacturer_country",
    "manufacturer_source",
    "manufacturer_website",
    "smpc_url",
    "pil_url",
    "assessment_report_url",
    "product_url",
    "source",
    "source_url",
    "document_type",
    "last_checked",
}


def company_identity_key(value: object) -> str:
    text = " ".join(str(value or "").strip().lower().split())
    text = re.sub(r"\b(?:limited|ltd|plc|inc|llc|gmbh|bv|b v|b\.v|sa|s a|s\.a|ag|kft|pty|pvt|private|company|co)\b", "", text)
    return re.sub(r"[^a-z0-9]+", "", text)


def same_company_identity(left: object, right: object) -> bool:
    left_key = company_identity_key(left)
    right_key = company_identity_key(right)
    if not left_key or not right_key:
        return False
    return left_key == right_key or left_key.startswith(right_key) or right_key.startswith(left_key)


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def initialize_database():
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS medicines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                substance TEXT,
                product TEXT,
                company TEXT,
                country TEXT,
                status TEXT,
                source TEXT
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS product_details (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                substance TEXT,
                product TEXT,
                company TEXT,
                country TEXT,
                region TEXT,
                status TEXT,
                strength TEXT,
                dosage_form TEXT,
                pack_size TEXT,
                atc_code TEXT,
                therapeutic_category TEXT,
                registration_number TEXT,
                registration_date TEXT,
                expiry_date TEXT,
                manufacturer_name TEXT,
                manufacturer_country TEXT,
                manufacturer_source TEXT,
                manufacturer_website TEXT,
                smpc_url TEXT,
                pil_url TEXT,
                assessment_report_url TEXT,
                product_url TEXT,
                source TEXT,
                source_url TEXT,
                document_type TEXT,
                last_checked TEXT
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS search_jobs (
                job_id TEXT PRIMARY KEY,
                substance TEXT,
                sources_json TEXT,
                mode TEXT,
                status TEXT,
                created_at TEXT,
                started_at TEXT,
                finished_at TEXT
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS search_job_progress (
                job_id TEXT,
                source TEXT,
                status TEXT,
                records INTEGER DEFAULT 0,
                error TEXT,
                started_at TEXT,
                finished_at TEXT,
                PRIMARY KEY (job_id, source)
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS search_job_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT,
                source TEXT,
                product TEXT,
                country TEXT,
                registration_number TEXT,
                url TEXT,
                row_json TEXT
            )
            """
        )
        existing_columns = {
            row["name"]
            for row in cursor.execute("PRAGMA table_info(product_details)")
        }
        for column in PRODUCT_DETAIL_COLUMNS - existing_columns - {"id"}:
            cursor.execute(f"ALTER TABLE product_details ADD COLUMN {column} TEXT")
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_product_details_substance
            ON product_details (substance)
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_product_details_country_source
            ON product_details (country, source)
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_search_jobs_created_at
            ON search_jobs (created_at)
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_search_job_results_job_id
            ON search_job_results (job_id)
            """
        )
        rows = cursor.execute(
            """
            SELECT id, manufacturer_name, company
            FROM product_details
            WHERE TRIM(COALESCE(manufacturer_name, '')) != ''
            """
        ).fetchall()
        for row in rows:
            manufacturer_parts = [
                part.strip()
                for part in str(row["manufacturer_name"] or "").split(";")
                if part.strip()
            ]
            filtered_parts = [
                part
                for part in manufacturer_parts
                if not same_company_identity(part, row["company"])
            ]
            if manufacturer_parts and filtered_parts != manufacturer_parts:
                cursor.execute(
                    """
                    UPDATE product_details
                    SET manufacturer_name=?,
                        manufacturer_country=CASE WHEN ?='' THEN '' ELSE manufacturer_country END,
                        manufacturer_source=CASE WHEN ?='' THEN '' ELSE manufacturer_source END
                    WHERE id=?
                    """,
                    ("; ".join(filtered_parts), "; ".join(filtered_parts), "; ".join(filtered_parts), row["id"]),
                )
        cursor.execute(
            """
            DELETE FROM product_details
            WHERE
                LOWER(COALESCE(document_type, '')) LIKE '%lookup fallback%'
                OR LOWER(COALESCE(product, '')) LIKE '%regulator lookup%'
            """
        )


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def save_search_job(job: dict[str, Any]) -> None:
    initialize_database()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO search_jobs (
                job_id, substance, sources_json, mode, status, created_at, started_at, finished_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                substance=excluded.substance,
                sources_json=excluded.sources_json,
                mode=excluded.mode,
                status=excluded.status,
                created_at=excluded.created_at,
                started_at=excluded.started_at,
                finished_at=excluded.finished_at
            """,
            (
                job.get("job_id", ""),
                job.get("substance", ""),
                json.dumps(job.get("sources", [])),
                job.get("mode", ""),
                job.get("status", ""),
                job.get("created_at", ""),
                job.get("started_at", ""),
                job.get("finished_at", ""),
            ),
        )


def save_search_job_progress(job_id: str, progress: dict[str, Any]) -> None:
    initialize_database()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO search_job_progress (
                job_id, source, status, records, error, started_at, finished_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id, source) DO UPDATE SET
                status=excluded.status,
                records=excluded.records,
                error=excluded.error,
                started_at=excluded.started_at,
                finished_at=excluded.finished_at
            """,
            (
                job_id,
                progress.get("source", ""),
                progress.get("status", ""),
                int(progress.get("records") or 0),
                progress.get("error", ""),
                progress.get("started_at", ""),
                progress.get("finished_at", ""),
            ),
        )


def save_search_job_results(job_id: str, rows: list[dict[str, Any]]) -> None:
    initialize_database()
    with get_connection() as conn:
        seen = {
            (
                row["source"],
                row["product"],
                row["country"],
                row["registration_number"],
                row["url"],
            )
            for row in conn.execute(
                """
                SELECT source, product, country, registration_number, url
                FROM search_job_results
                WHERE job_id=?
                """,
                (job_id,),
            ).fetchall()
        }
        for row in rows:
            key = (
                str(row.get("source", "")),
                str(row.get("product", "")),
                str(row.get("country", "")),
                str(row.get("registration_number", "")),
                str(row.get("url", "")),
            )
            if key in seen:
                continue
            seen.add(key)
            conn.execute(
                """
                INSERT INTO search_job_results (
                    job_id, source, product, country, registration_number, url, row_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    key[0],
                    key[1],
                    key[2],
                    key[3],
                    key[4],
                    json.dumps(row),
                ),
            )


def get_persisted_search_job(job_id: str) -> dict[str, Any] | None:
    initialize_database()
    with get_connection() as conn:
        job = conn.execute("SELECT * FROM search_jobs WHERE job_id=?", (job_id,)).fetchone()
        if not job:
            return None
        progress = conn.execute(
            """
            SELECT source, status, records, error, started_at, finished_at
            FROM search_job_progress
            WHERE job_id=?
            ORDER BY source
            """,
            (job_id,),
        ).fetchall()
        record_count = conn.execute(
            "SELECT COUNT(*) AS count FROM search_job_results WHERE job_id=?",
            (job_id,),
        ).fetchone()["count"]
    job_dict = dict(job)
    job_dict["sources"] = json.loads(job_dict.pop("sources_json") or "[]")
    job_dict["record_count"] = record_count
    job_dict["progress"] = rows_to_dicts(progress)
    return job_dict


def get_persisted_search_job_results(job_id: str) -> list[dict[str, Any]]:
    initialize_database()
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT row_json
            FROM search_job_results
            WHERE job_id=?
            ORDER BY id
            """,
            (job_id,),
        ).fetchall()
    results = []
    for row in rows:
        try:
            results.append(json.loads(row["row_json"]))
        except json.JSONDecodeError:
            continue
    return results


def list_search_jobs(limit: int = 25) -> list[dict[str, Any]]:
    initialize_database()
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                job_id,
                substance,
                mode,
                status,
                created_at,
                started_at,
                finished_at,
                (
                    SELECT COUNT(*)
                    FROM search_job_results
                    WHERE search_job_results.job_id = search_jobs.job_id
                ) AS record_count
            FROM search_jobs
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return rows_to_dicts(rows)


def region_for_country(country: str | None) -> str:
    normalized = (country or "").strip().lower()
    if normalized in {"united kingdom", "uk", "great britain"}:
        return "UK"
    if normalized in {"united states", "usa", "us"}:
        return "US"
    if normalized in {"canada"}:
        return "CA"
    if normalized in {"european union", "eu"}:
        return "EU"
    return ""


def search_product_details(substance: str) -> list[dict[str, Any]]:
    initialize_database()
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM product_details
            WHERE substance LIKE ? OR product LIKE ?
            ORDER BY country, product
            """,
            (f"%{substance}%", f"%{substance}%"),
        ).fetchall()
    return rows_to_dicts(rows)


def search_medicines(substance: str) -> list[dict[str, Any]]:
    initialize_database()
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT substance, product, company, country, status, source
            FROM medicines
            WHERE substance LIKE ? OR product LIKE ?
            ORDER BY country, product
            """,
            (f"%{substance}%", f"%{substance}%"),
        ).fetchall()
    return rows_to_dicts(rows)


def list_product_details() -> list[dict[str, Any]]:
    initialize_database()
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM product_details
            ORDER BY product
            """
        ).fetchall()
    return rows_to_dicts(rows)


def save_product_detail(record: dict[str, Any]) -> dict[str, Any]:
    initialize_database()
    now = datetime.now(timezone.utc).isoformat()
    manufacturer_name = record.get("manufacturer_name", "")
    manufacturer_country = record.get("manufacturer_country", "")
    manufacturer_source = record.get("manufacturer_source", "")
    manufacturer_parts = [
        part.strip()
        for part in str(manufacturer_name or "").split(";")
        if part.strip()
    ]
    filtered_parts = [
        part
        for part in manufacturer_parts
        if not same_company_identity(part, record.get("company", ""))
    ]
    if manufacturer_parts and filtered_parts != manufacturer_parts:
        manufacturer_name = "; ".join(filtered_parts)
        if not manufacturer_name:
            manufacturer_country = ""
            manufacturer_source = ""
    data = {
        "substance": record.get("substance", ""),
        "product": record.get("product", ""),
        "company": record.get("company", ""),
        "country": record.get("country", ""),
        "region": record.get("region") or region_for_country(record.get("country", "")),
        "status": record.get("status", ""),
        "strength": record.get("strength", ""),
        "dosage_form": record.get("dosage_form", ""),
        "pack_size": record.get("pack_size", ""),
        "atc_code": record.get("atc_code", ""),
        "therapeutic_category": record.get("therapeutic_category", ""),
        "registration_number": record.get("registration_number", ""),
        "registration_date": record.get("registration_date", ""),
        "expiry_date": record.get("expiry_date", ""),
        "manufacturer_name": manufacturer_name,
        "manufacturer_country": manufacturer_country,
        "manufacturer_source": manufacturer_source,
        "manufacturer_website": record.get("manufacturer_website", ""),
        "smpc_url": record.get("smpc_url", ""),
        "pil_url": record.get("pil_url", ""),
        "assessment_report_url": record.get("assessment_report_url", ""),
        "product_url": record.get("product_url") or record.get("url", ""),
        "source": record.get("source", ""),
        "source_url": record.get("source_url") or record.get("url", ""),
        "document_type": record.get("document_type", ""),
        "last_checked": record.get("last_checked", now),
    }
    columns = list(data.keys())
    placeholders = ", ".join(["?"] * len(columns))
    with get_connection() as conn:
        existing = conn.execute(
            """
            SELECT id
            FROM product_details
            WHERE
                COALESCE(source, '') = COALESCE(?, '')
                AND COALESCE(product, '') = COALESCE(?, '')
                AND COALESCE(country, '') = COALESCE(?, '')
                AND (
                    (? != '' AND COALESCE(registration_number, '') = ?)
                    OR (? != '' AND COALESCE(product_url, '') = ?)
                    OR (? = '' AND ? = '')
                )
            ORDER BY id
            LIMIT 1
            """,
            [
                data["source"],
                data["product"],
                data["country"],
                data["registration_number"],
                data["registration_number"],
                data["product_url"],
                data["product_url"],
                data["registration_number"],
                data["product_url"],
            ],
        ).fetchone()
        if existing:
            assignments = ", ".join(f"{column}=?" for column in columns)
            conn.execute(
                f"UPDATE product_details SET {assignments} WHERE id=?",
                [data[column] for column in columns] + [existing["id"]],
            )
        else:
            conn.execute(
                f"""
                INSERT INTO product_details ({", ".join(columns)})
                VALUES ({placeholders})
                """,
                [data[column] for column in columns],
            )
    return data


def reset_database() -> None:
    initialize_database()
    with get_connection() as conn:
        conn.execute("DELETE FROM medicines")
        conn.execute("DELETE FROM product_details")
