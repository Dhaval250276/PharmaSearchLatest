import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from collections.abc import Iterator
from typing import Any
import re
import json

from config import BASE_DIR, DB_PATH
from services.harvest_vocabulary import molecule_group_key


PRODUCT_DETAILS_SEED_PATH = BASE_DIR / "data" / "product_details_seed.jsonl"


# Tables whose rows belong to one registration and cannot outlive it.
PRODUCT_DETAIL_CHILD_TABLES = (
    "evidence",
    "regulatory_documents",
    "registration_organization_roles",
)

PRODUCT_DETAIL_COLUMNS = {
    "id",
    "substance",
    "product",
    "company",
    "applicant_sponsor",
    "country",
    "region",
    "authorisation_scope",
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
    "manufacturer_address",
    "manufacturer_role",
    "batch_release_manufacturer",
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
    "verification_status",
    "evidence_url",
    "evidence_page",
    "evidence_section",
    "missing_reason",
    # The molecule this row is about, normalized across languages and registers
    # so a search for "ibuprofen" also reaches France's IBUPROFENE rows.
    "substance_key",
    # The substance exactly as the registry stated it, which is where the salt
    # or ester is named when the product is a brand.
    "source_substance",
    # Values lent by another regulator that published them for the same
    # molecule. Kept apart from the row's own columns so an inherited document
    # is never read as this authorisation's own label.
    "completion_source",
    "reference_smpc_url",
    "reference_pil_url",
    "reference_assessment_report_url",
    "reference_source",
    "reference_product",
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
                applicant_sponsor TEXT,
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
                authorisation_scope TEXT,
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
                manufacturer_address TEXT,
                manufacturer_role TEXT,
                batch_release_manufacturer TEXT,
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
                , verification_status TEXT
                , evidence_url TEXT
                , evidence_page TEXT
                , evidence_section TEXT
                , missing_reason TEXT
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
        cursor.executescript(
            """
            CREATE TABLE IF NOT EXISTS organizations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                normalized_name TEXT NOT NULL,
                display_name TEXT NOT NULL,
                country TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE(normalized_name, country)
            );

            CREATE TABLE IF NOT EXISTS manufacturing_sites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                organization_id INTEGER,
                site_name TEXT NOT NULL DEFAULT '',
                address TEXT NOT NULL DEFAULT '',
                country TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(organization_id) REFERENCES organizations(id),
                UNIQUE(organization_id, site_name, address, country)
            );

            CREATE TABLE IF NOT EXISTS registration_organization_roles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_detail_id INTEGER NOT NULL,
                organization_id INTEGER,
                site_id INTEGER,
                role TEXT NOT NULL,
                scope TEXT NOT NULL DEFAULT '',
                evidence_id INTEGER,
                verification_status TEXT NOT NULL DEFAULT 'UNVERIFIED',
                created_at TEXT NOT NULL,
                FOREIGN KEY(product_detail_id) REFERENCES product_details(id),
                FOREIGN KEY(organization_id) REFERENCES organizations(id),
                FOREIGN KEY(site_id) REFERENCES manufacturing_sites(id),
                UNIQUE(product_detail_id, organization_id, site_id, role, scope)
            );

            CREATE TABLE IF NOT EXISTS regulatory_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_detail_id INTEGER NOT NULL,
                document_type TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL,
                language TEXT NOT NULL DEFAULT '',
                effective_date TEXT NOT NULL DEFAULT '',
                source_regulator TEXT NOT NULL DEFAULT '',
                last_checked TEXT NOT NULL DEFAULT '',
                verification_status TEXT NOT NULL DEFAULT 'UNVERIFIED',
                FOREIGN KEY(product_detail_id) REFERENCES product_details(id),
                UNIQUE(product_detail_id, document_type, url)
            );

            CREATE TABLE IF NOT EXISTS evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_detail_id INTEGER NOT NULL,
                document_id INTEGER,
                field_name TEXT NOT NULL,
                value TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL DEFAULT '',
                source_regulator TEXT NOT NULL DEFAULT '',
                document_type TEXT NOT NULL DEFAULT '',
                evidence_url TEXT NOT NULL DEFAULT '',
                evidence_page TEXT NOT NULL DEFAULT '',
                evidence_section TEXT NOT NULL DEFAULT '',
                extraction_method TEXT NOT NULL DEFAULT '',
                extracted_at TEXT NOT NULL,
                verification_status TEXT NOT NULL DEFAULT 'UNVERIFIED',
                missing_reason TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(product_detail_id) REFERENCES product_details(id),
                FOREIGN KEY(document_id) REFERENCES regulatory_documents(id)
            );

            CREATE TABLE IF NOT EXISTS source_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                query TEXT NOT NULL,
                status TEXT NOT NULL,
                records_found INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                evidence_url TEXT NOT NULL DEFAULT '',
                checked_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_roles_product_detail
            ON registration_organization_roles(product_detail_id);
            CREATE INDEX IF NOT EXISTS idx_documents_product_detail
            ON regulatory_documents(product_detail_id);
            CREATE INDEX IF NOT EXISTS idx_evidence_product_field
            ON evidence(product_detail_id, field_name);
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
        applied_migrations = {
            row["name"] for row in cursor.execute("SELECT name FROM schema_migrations")
        }

        def _migrate(name: str) -> bool:
            """True while a one-time repair still has to run on this database.

            These repairs rewrite rows the connectors have since stopped
            producing. Left unguarded they re-run on every call and erase
            whatever a refreshed connector has just written.
            """
            if name in applied_migrations:
                return False
            cursor.execute(
                "INSERT INTO schema_migrations(name, applied_at) VALUES (?, ?)",
                (name, datetime.now(timezone.utc).isoformat()),
            )
            applied_migrations.add(name)
            return True

        if _migrate("remove_orphaned_registration_children"):
            # Earlier deletes took the registration and left its rows behind,
            # where nothing can reach them but every total still counts them.
            for table in PRODUCT_DETAIL_CHILD_TABLES:
                cursor.execute(
                    f"""DELETE FROM {table}
                        WHERE product_detail_id NOT IN (SELECT id FROM product_details)"""
                )
        if _migrate("dedupe_evidence_assertions"):
            cursor.execute(
                """
                DELETE FROM evidence WHERE id NOT IN (
                    SELECT MIN(id) FROM evidence
                    GROUP BY product_detail_id, field_name, role, value,
                             evidence_url, evidence_page, evidence_section
                )
                """
            )
        cursor.executescript(
            """
            -- An assertion is identified by what it claims and where it was
            -- read, so re-harvesting a product refreshes its evidence rather
            -- than stacking another copy of it.
            CREATE UNIQUE INDEX IF NOT EXISTS idx_evidence_identity
            ON evidence(product_detail_id, field_name, role, value,
                        evidence_url, evidence_page, evidence_section);

            -- SQLite counts NULLs as distinct from one another, so a role or
            -- site with no resolved organization slips past a plain UNIQUE
            -- constraint. Folding the absent reference to 0 catches those too.
            CREATE UNIQUE INDEX IF NOT EXISTS idx_sites_identity
            ON manufacturing_sites(
                COALESCE(organization_id, 0), site_name, address, country
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_roles_identity
            ON registration_organization_roles(
                product_detail_id, COALESCE(organization_id, 0),
                COALESCE(site_id, 0), role, scope
            );
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
        if _migrate("strip_company_echo_from_manufacturer_name"):
            rows = cursor.execute(
                """
                SELECT id, manufacturer_name, company, manufacturer_source
                FROM product_details
                WHERE TRIM(COALESCE(manufacturer_name, '')) != ''
                """
            ).fetchall()
            for row in rows:
                if str(row["manufacturer_source"] or "").strip():
                    continue
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
        lookup_fallback_scope = """
            SELECT id FROM product_details
            WHERE
                LOWER(COALESCE(document_type, '')) LIKE '%lookup fallback%'
                OR LOWER(COALESCE(product, '')) LIKE '%regulator lookup%'
        """
        for table in PRODUCT_DETAIL_CHILD_TABLES:
            cursor.execute(
                f"DELETE FROM {table} WHERE product_detail_id IN ({lookup_fallback_scope})"
            )
        cursor.execute(
            """
            DELETE FROM product_details
            WHERE
                LOWER(COALESCE(document_type, '')) LIKE '%lookup fallback%'
                OR LOWER(COALESCE(product, '')) LIKE '%regulator lookup%'
            """
        )
        if _migrate("move_registry_handoffs_to_source_runs"):
            handoff_rows = cursor.execute(
                """
                SELECT source, substance, source_url, last_checked
                FROM product_details
                WHERE LOWER(COALESCE(document_type, '')) LIKE '%registry search handoff%'
                """
            ).fetchall()
            for row in handoff_rows:
                cursor.execute(
                    """
                    INSERT INTO source_runs(
                        source, query, status, records_found, error, evidence_url, checked_at
                    ) VALUES (?, ?, 'SOURCE_UNSUPPORTED', 0, '', ?, ?)
                    """,
                    (
                        row["source"] or "", row["substance"] or "",
                        row["source_url"] or "", row["last_checked"] or datetime.now(timezone.utc).isoformat(),
                    ),
                )
            if handoff_rows:
                cursor.execute(
                    """DELETE FROM product_details
                       WHERE LOWER(COALESCE(document_type, '')) LIKE '%registry search handoff%'"""
                )
        if _migrate("clear_legacy_manufacturer_role_assumptions"):
            # Remove legacy role assumptions. These values can be repopulated only
            # by refreshed connectors that emit explicit organization roles/sites.
            cursor.execute(
                """UPDATE product_details
                   SET manufacturer_name='', manufacturer_country='', manufacturer_source=''
                   WHERE source='FDA' AND (
                       manufacturer_source LIKE 'FDA label manufacturer_name%'
                       OR manufacturer_source LIKE 'FDA NDC labeler_name%'
                   )"""
            )
            cursor.execute(
                """UPDATE product_details
                   SET manufacturer_name='', manufacturer_country='', manufacturer_source=''
                   WHERE source='CDSCO India'
                     AND manufacturer_source='CDSCO SUGAM approval record'"""
            )
            cursor.execute(
                """UPDATE product_details SET manufacturer_country=''
                   WHERE source='BPOM Indonesia' AND manufacturer_source='BPOM JSON'"""
            )
            cursor.execute(
                """UPDATE product_details SET manufacturer_country=''
                   WHERE source='ANMDMR Romania'
                     AND manufacturer_source='ANMDMR nomenclator producer field'"""
            )
        _seed_product_details_if_sparse(cursor)


def _seed_product_details_if_sparse(cursor: sqlite3.Cursor, minimum_rows: int = 1000) -> int:
    if not PRODUCT_DETAILS_SEED_PATH.exists():
        return 0
    existing_count = cursor.execute("SELECT COUNT(*) FROM product_details").fetchone()[0]
    if existing_count >= minimum_rows:
        return 0

    columns = [
        column
        for column in PRODUCT_DETAIL_COLUMNS
        if column != "id"
    ]
    placeholders = ", ".join(["?"] * len(columns))
    inserted = 0
    with PRODUCT_DETAILS_SEED_PATH.open("r", encoding="utf-8") as seed_file:
        for line in seed_file:
            if not line.strip():
                continue
            row = json.loads(line)
            existing = cursor.execute(
                """
                SELECT 1
                FROM product_details
                WHERE
                    COALESCE(source, '') = COALESCE(?, '')
                    AND COALESCE(product, '') = COALESCE(?, '')
                    AND COALESCE(country, '') = COALESCE(?, '')
                    AND COALESCE(registration_number, '') = COALESCE(?, '')
                LIMIT 1
                """,
                (
                    row.get("source", ""),
                    row.get("product", ""),
                    row.get("country", ""),
                    row.get("registration_number", ""),
                ),
            ).fetchone()
            if existing:
                continue
            cursor.execute(
                f"""
                INSERT INTO product_details ({", ".join(columns)})
                VALUES ({placeholders})
                """,
                [row.get(column, "") for column in columns],
            )
            inserted += 1
    if inserted:
        cursor.execute(
            """
            DELETE FROM product_details
            WHERE
                LOWER(COALESCE(document_type, '')) LIKE '%lookup fallback%'
                OR LOWER(COALESCE(product, '')) LIKE '%regulator lookup%'
            """
        )
    return inserted


def seed_product_details_if_needed(minimum_rows: int = 1000) -> int:
    initialize_database()
    with get_connection() as conn:
        return _seed_product_details_if_sparse(conn.cursor(), minimum_rows=minimum_rows)


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def _attach_structured_data(
    conn: sqlite3.Connection, rows: list[sqlite3.Row]
) -> list[dict[str, Any]]:
    results = rows_to_dicts(rows)
    by_id = {result["id"]: result for result in results if result.get("id")}
    for result in results:
        result["manufacturers"] = []
        result["documents"] = []
        result["evidence"] = []
    ids = list(by_id)
    for offset in range(0, len(ids), 500):
        batch = ids[offset : offset + 500]
        placeholders = ",".join("?" for _ in batch)
        for row in conn.execute(
                """
                SELECT r.product_detail_id, o.display_name AS name, s.site_name, s.address,
                       COALESCE(s.country, o.country, '') AS country,
                       r.role, r.scope, r.verification_status
                FROM registration_organization_roles r
                LEFT JOIN organizations o ON o.id=r.organization_id
                LEFT JOIN manufacturing_sites s ON s.id=r.site_id
                WHERE r.product_detail_id IN (""" + placeholders + ") ORDER BY r.id",
                batch,
            ).fetchall():
            item = dict(row)
            by_id[item.pop("product_detail_id")]["manufacturers"].append(item)
        for row in conn.execute(
                """SELECT product_detail_id, document_type, title, url, language, effective_date,
                          source_regulator, last_checked, verification_status
                   FROM regulatory_documents WHERE product_detail_id IN (""" + placeholders + ") ORDER BY id",
                batch,
            ).fetchall():
            item = dict(row)
            by_id[item.pop("product_detail_id")]["documents"].append(item)
        for row in conn.execute(
                """SELECT product_detail_id, field_name, value, role, source_regulator, document_type,
                          evidence_url, evidence_page, evidence_section,
                          extraction_method, extracted_at, verification_status, missing_reason
                   FROM evidence WHERE product_detail_id IN (""" + placeholders + ") ORDER BY id",
                batch,
            ).fetchall():
            item = dict(row)
            by_id[item.pop("product_detail_id")]["evidence"].append(item)
    return results


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
    """Rows for a molecule, whatever language the registry filed it in.

    Matching on the text alone strands the rows a registry wrote in its own
    register: "ibuprofen" never matches France's IBUPROFENE, because the accent
    breaks the substring. The normalized molecule key catches those, and the
    text match stays for brand names and for rows stored before the key existed.
    """
    initialize_database()
    key = molecule_group_key(substance)
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM product_details
            WHERE substance LIKE ? OR product LIKE ? OR (substance_key <> '' AND substance_key = ?)
            ORDER BY country, product
            """,
            (f"%{substance}%", f"%{substance}%", key),
        ).fetchall()
        return _attach_structured_data(conn, rows)


def backfill_substance_keys() -> int:
    """Populate the molecule key on rows stored before the column existed."""
    initialize_database()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, substance FROM product_details"
            " WHERE substance_key IS NULL OR substance_key = ''"
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE product_details SET substance_key = ? WHERE id = ?",
                (molecule_group_key(row["substance"]), row["id"]),
            )
    return len(rows)


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
        return _attach_structured_data(conn, rows)


def save_product_detail(record: dict[str, Any]) -> dict[str, Any]:
    initialize_database()
    if (
        str(record.get("connector_mode") or "").strip().lower() == "manual_registry"
        or "registry search handoff" in str(record.get("document_type") or "").strip().lower()
    ):
        # A search handoff is a source-run outcome, not a pharmaceutical product.
        return {**record, "persistence_status": "SOURCE_RUN_ONLY"}
    now = datetime.now(timezone.utc).isoformat()
    manufacturer_name = record.get("manufacturer_name", "")
    manufacturer_country = record.get("manufacturer_country", "")
    manufacturer_source = record.get("manufacturer_source", "")
    manufacturer_parts = [
        part.strip()
        for part in str(manufacturer_name or "").split(";")
        if part.strip()
    ]
    filtered_parts = (
        manufacturer_parts
        if manufacturer_source
        else [
            part
            for part in manufacturer_parts
            if not same_company_identity(part, record.get("company", ""))
        ]
    )
    if manufacturer_parts and filtered_parts != manufacturer_parts:
        manufacturer_name = "; ".join(filtered_parts)
        if not manufacturer_name:
            manufacturer_country = ""
            manufacturer_source = ""
    data = {
        "substance": record.get("substance", ""),
        "substance_key": molecule_group_key(record.get("substance", "")),
        "source_substance": record.get("source_substance", ""),
        "product": record.get("product", ""),
        "company": record.get("company", ""),
        "applicant_sponsor": record.get("applicant_sponsor", ""),
        "country": record.get("country", ""),
        "region": record.get("region") or region_for_country(record.get("country", "")),
        "authorisation_scope": record.get("authorisation_scope", ""),
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
        "manufacturer_address": record.get("manufacturer_address", ""),
        "manufacturer_role": record.get("manufacturer_role", ""),
        "batch_release_manufacturer": record.get("batch_release_manufacturer", ""),
        "manufacturer_source": manufacturer_source,
        "manufacturer_website": record.get("manufacturer_website", ""),
        "smpc_url": record.get("smpc_url", ""),
        "pil_url": record.get("pil_url", ""),
        "assessment_report_url": record.get("assessment_report_url", ""),
        "product_url": record.get("product_url") or record.get("url", ""),
        "source": record.get("source", ""),
        "source_url": record.get("source_url") or record.get("url", ""),
        "document_type": record.get("document_type", ""),
        "last_checked": record.get("last_checked") or now,
        "verification_status": record.get("verification_status", "UNVERIFIED"),
        "evidence_url": record.get("evidence_url", ""),
        "evidence_page": record.get("evidence_page", ""),
        "evidence_section": record.get("evidence_section", ""),
        "missing_reason": record.get("missing_reason", ""),
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
            product_detail_id = existing["id"]
            assignments = ", ".join(f"{column}=?" for column in columns)
            conn.execute(
                f"UPDATE product_details SET {assignments} WHERE id=?",
                [data[column] for column in columns] + [existing["id"]],
            )
        else:
            product_detail_id = conn.execute(
                f"""
                INSERT INTO product_details ({", ".join(columns)})
                VALUES ({placeholders})
                """,
                [data[column] for column in columns],
            ).lastrowid
        _save_structured_regulatory_data(conn, product_detail_id, record, now)
    return data


def save_source_run(
    source: str,
    query: str,
    status: str,
    records_found: int = 0,
    error: str = "",
    evidence_url: str = "",
) -> None:
    initialize_database()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO source_runs(
                source, query, status, records_found, error, evidence_url, checked_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source, query, status, records_found, error[:1000], evidence_url,
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def _text(value: object) -> str:
    """Evidence key columns are NOT NULL, so an absent field reads as empty."""
    return "" if value is None else str(value)


def _organization_id(
    conn: sqlite3.Connection, name: object, country: object, now: str
) -> int | None:
    display_name = " ".join(str(name or "").split())
    if not display_name:
        return None
    normalized_name = company_identity_key(display_name) or display_name.lower()
    normalized_country = " ".join(str(country or "").split())
    conn.execute(
        """
        INSERT INTO organizations(normalized_name, display_name, country, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(normalized_name, country) DO UPDATE SET
            display_name=excluded.display_name
        """,
        (normalized_name, display_name, normalized_country, now),
    )
    row = conn.execute(
        "SELECT id FROM organizations WHERE normalized_name=? AND country=?",
        (normalized_name, normalized_country),
    ).fetchone()
    return row["id"] if row else None


def _save_structured_regulatory_data(
    conn: sqlite3.Connection,
    product_detail_id: int,
    record: dict[str, Any],
    now: str,
) -> None:
    document_ids: dict[str, int] = {}
    documents = list(record.get("documents") or [])
    for field_name, document_type in (
        ("smpc_url", "SMPC"),
        ("pil_url", "PIL"),
        ("assessment_report_url", "ASSESSMENT_REPORT"),
        ("label_url", "LABEL"),
    ):
        url = str(record.get(field_name) or "").strip()
        if url:
            documents.append({"document_type": document_type, "url": url})
    for document in documents:
        url = str(document.get("url") or "").strip()
        document_type = str(document.get("document_type") or "OTHER").strip().upper()
        if not url:
            continue
        conn.execute(
            """
            INSERT INTO regulatory_documents(
                product_detail_id, document_type, title, url, language,
                effective_date, source_regulator, last_checked, verification_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(product_detail_id, document_type, url) DO UPDATE SET
                title=excluded.title, effective_date=excluded.effective_date,
                last_checked=excluded.last_checked,
                verification_status=excluded.verification_status
            """,
            (
                product_detail_id, document_type, document.get("title", ""), url,
                document.get("language", ""), document.get("effective_date", ""),
                record.get("source", ""), record.get("last_checked") or now,
                document.get("verification_status", "VERIFIED_REGULATOR_RECORD"),
            ),
        )
        row = conn.execute(
            "SELECT id FROM regulatory_documents WHERE product_detail_id=? AND document_type=? AND url=?",
            (product_detail_id, document_type, url),
        ).fetchone()
        if row:
            document_ids[url] = row["id"]

    for assertion in record.get("evidence", []) or []:
        evidence_url = str(assertion.get("evidence_url") or assertion.get("url") or "").strip()
        conn.execute(
            """
            INSERT INTO evidence(
                product_detail_id, document_id, field_name, value, role,
                source_regulator, document_type, evidence_url, evidence_page,
                evidence_section, extraction_method, extracted_at,
                verification_status, missing_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(
                product_detail_id, field_name, role, value,
                evidence_url, evidence_page, evidence_section
            ) DO UPDATE SET
                document_id=excluded.document_id,
                source_regulator=excluded.source_regulator,
                document_type=excluded.document_type,
                extraction_method=excluded.extraction_method,
                extracted_at=excluded.extracted_at,
                verification_status=excluded.verification_status,
                missing_reason=excluded.missing_reason
            """,
            (
                product_detail_id, document_ids.get(evidence_url), _text(assertion.get("field_name")),
                _text(assertion.get("value")), _text(assertion.get("role")),
                assertion.get("source_regulator") or record.get("source", ""),
                assertion.get("document_type", ""), evidence_url,
                _text(assertion.get("evidence_page")), _text(assertion.get("evidence_section")),
                assertion.get("extraction_method", ""), assertion.get("extracted_at") or now,
                assertion.get("verification_status", "UNVERIFIED"),
                assertion.get("missing_reason", ""),
            ),
        )

    manufacturers = list(record.get("manufacturers") or [])
    for manufacturer in manufacturers:
        organization_id = _organization_id(
            conn, manufacturer.get("name"), manufacturer.get("country"), now
        )
        address = " ".join(str(manufacturer.get("address") or "").split())
        site_name = " ".join(str(manufacturer.get("site_name") or "").split())
        country = " ".join(str(manufacturer.get("country") or "").split())
        site_id = None
        if address or site_name:
            conn.execute(
                """
                INSERT OR IGNORE INTO manufacturing_sites(
                    organization_id, site_name, address, country, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (organization_id, site_name, address, country, now),
            )
            site = conn.execute(
                """SELECT id FROM manufacturing_sites
                   WHERE organization_id IS ? AND site_name=? AND address=? AND country=?""",
                (organization_id, site_name, address, country),
            ).fetchone()
            site_id = site["id"] if site else None
        conn.execute(
            """
            INSERT OR IGNORE INTO registration_organization_roles(
                product_detail_id, organization_id, site_id, role, scope,
                verification_status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                product_detail_id, organization_id, site_id,
                manufacturer.get("role", "MANUFACTURER_UNKNOWN_ROLE"),
                manufacturer.get("scope", ""),
                manufacturer.get("verification_status", "UNVERIFIED"), now,
            ),
        )


def reset_database() -> None:
    initialize_database()
    with get_connection() as conn:
        conn.execute("DELETE FROM medicines")
        # The rows hanging off a registration go with it. Left behind they are
        # unreachable, yet they still count towards every total reported.
        for table in PRODUCT_DETAIL_CHILD_TABLES:
            conn.execute(f"DELETE FROM {table}")
        conn.execute("DELETE FROM manufacturing_sites")
        conn.execute("DELETE FROM organizations")
        conn.execute("DELETE FROM product_details")
        # schema_migrations deliberately survives. Replaying the one-time
        # repairs would undo what the refreshed connectors write next.
