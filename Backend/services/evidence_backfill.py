"""Give the rows harvested before the evidence table existed their provenance.

Evidence is only written where a connector attaches assertions to a record it
is saving (see repository.save_product_details), so every row harvested before
those connectors learned to do it carries none -- 50,000 of them, against two
dozen assertions.

Their provenance was never lost, though. Each row already records the regulator
that supplied it and the URL it was read from, and where a value came from
somewhere other than that register -- a document the parser opened, or another
regulator's record for the same molecule -- the row says so in
``manufacturer_source`` and ``completion_source``. This module turns those
columns back into the per-field assertions the evidence table was built to
hold, and it claims nothing the columns do not support: a value is only called
a verified register record where the row names the register it was read from.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from repository import get_connection
from services.field_availability import (
    DOCUMENT_PARSED_SOURCES,
    NOT_COLLECTED,
    NOT_SUPPLIED,
    PENDING_ENRICHMENT,
)


REGISTER_METHOD = "REGISTER_RECORD_BACKFILL"
DOCUMENT_METHOD = "DOCUMENT_FIELD_BACKFILL"
COMPLETION_METHOD = "CROSS_SOURCE_COMPLETION_BACKFILL"

REGISTER_SECTION = "Regulator register entry"
COMPLETION_SECTION = "Agreed across regulators for the same molecule"

VERIFIED_RECORD = "VERIFIED_REGULATOR_RECORD"
VERIFIED_DOCUMENT = "VERIFIED_OFFICIAL_DOCUMENT"
UNVERIFIED = "UNVERIFIED"

# Read straight off the regulator's own record by the connector that harvested
# the row. Nothing else fills them, so the register entry is their evidence.
REGISTER_FIELDS = (
    "product",
    "company",
    "applicant_sponsor",
    "status",
    "strength",
    "dosage_form",
    "pack_size",
    "registration_number",
    "registration_date",
    "expiry_date",
    "authorisation_scope",
)

# Listed by the register, which is what the assertion rests on -- the value is
# the document's address, not something read out of the document.
DOCUMENT_URL_FIELDS = ("smpc_url", "pil_url", "assessment_report_url")

# field_completion may agree these across a molecule group rather than read
# them from this row's own register, and completion_source says when it did.
COMPLETABLE_FIELDS = ("atc_code", "therapeutic_category")

# Filled either by a register field or by opening a document; the row's
# manufacturer_source is its own record of which.
MANUFACTURER_FIELDS = (
    "manufacturer_name",
    "manufacturer_country",
    "manufacturer_address",
    "manufacturer_role",
    "batch_release_manufacturer",
)

MANUFACTURER_ROLE = "MANUFACTURER_OR_BATCH_RELEASE_SITE"

_SELECT_COLUMNS = (
    "id", "source", "source_url", "product_url", "last_checked", "document_type",
    "manufacturer_source", "completion_source", "smpc_url", "pil_url",
    "assessment_report_url", "evidence_url", "verification_status", "missing_reason",
    *REGISTER_FIELDS,
    *COMPLETABLE_FIELDS,
    *MANUFACTURER_FIELDS,
)

_INSERT_SQL = """
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
        source_regulator=excluded.source_regulator,
        document_type=excluded.document_type,
        extraction_method=excluded.extraction_method,
        verification_status=excluded.verification_status
"""


def _text(value: object) -> str:
    return str(value or "").strip()


def _register_url(row: dict[str, Any]) -> str:
    """Where the row was read from, most specific address first."""
    return _text(row.get("product_url")) or _text(row.get("source_url"))


def _document_url(row: dict[str, Any]) -> str:
    """The document a parser would have opened for this row."""
    for field in ("smpc_url", "pil_url", "assessment_report_url", "product_url"):
        url = _text(row.get(field))
        if url:
            return url
    return ""


def missing_reason_for_row(row: dict[str, Any]) -> str:
    """Why this row's document-derived fields are still blank.

    The reasoning field_availability applies at display time, over the columns
    a stored row actually has. It matters most for the manufacturer fields,
    which only a document parser fills and only for the registries that have
    one -- calling the rest "pending" would promise a job that will never run.
    """
    if all(_text(row.get(field)) for field in MANUFACTURER_FIELDS):
        return ""
    if not _document_url(row):
        return NOT_SUPPLIED
    if _text(row.get("source")) in DOCUMENT_PARSED_SOURCES:
        return PENDING_ENRICHMENT
    return NOT_COLLECTED


def assertions_for_row(row: dict[str, Any]) -> list[tuple]:
    """The per-field assertions the row's own provenance columns support."""
    product_detail_id = row["id"]
    regulator = _text(row.get("source"))
    register_url = _register_url(row)
    document_type = _text(row.get("document_type"))
    extracted_at = _text(row.get("last_checked")) or datetime.now(timezone.utc).isoformat()
    completion_source = _text(row.get("completion_source"))
    manufacturer_source = _text(row.get("manufacturer_source"))

    # Without an address to point at there is nothing to assert: an assertion
    # carrying no evidence_url is the unsourced value we are replacing.
    if not register_url:
        return []

    assertions: list[tuple] = []

    def add(
        field: str,
        value: str,
        *,
        url: str,
        section: str,
        method: str,
        status: str,
        source_regulator: str = "",
        role: str = "",
    ) -> None:
        assertions.append((
            product_detail_id, None, field, value, role,
            source_regulator or regulator, document_type, url, "",
            section, method, extracted_at, status, "",
        ))

    for field in (*REGISTER_FIELDS, *DOCUMENT_URL_FIELDS):
        value = _text(row.get(field))
        if value:
            add(
                field, value, url=register_url, section=REGISTER_SECTION,
                method=REGISTER_METHOD, status=VERIFIED_RECORD,
            )

    for field in COMPLETABLE_FIELDS:
        value = _text(row.get(field))
        if not value:
            continue
        if completion_source:
            # Agreed across the molecule group, so it rests on those
            # regulators' records rather than on this row's register entry.
            add(
                field, value, url=register_url, section=COMPLETION_SECTION,
                method=COMPLETION_METHOD, status=UNVERIFIED,
                source_regulator=completion_source,
            )
        else:
            add(
                field, value, url=register_url, section=REGISTER_SECTION,
                method=REGISTER_METHOD, status=VERIFIED_RECORD,
            )

    from_document = "document" in manufacturer_source.lower()
    for field in MANUFACTURER_FIELDS:
        value = _text(row.get(field))
        if not value:
            continue
        if from_document:
            add(
                field, value, url=_document_url(row) or register_url,
                section=manufacturer_source, method=DOCUMENT_METHOD,
                status=VERIFIED_DOCUMENT, role=MANUFACTURER_ROLE,
            )
        else:
            add(
                field, value, url=register_url,
                section=manufacturer_source or REGISTER_SECTION,
                method=REGISTER_METHOD, status=VERIFIED_RECORD,
                role=MANUFACTURER_ROLE,
            )

    return assertions


def backfill_evidence(
    *,
    limit: int | None = None,
    batch_size: int = 1000,
    dry_run: bool = False,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """Reconstruct evidence for stored rows that were saved without it.

    Idempotent: the unique index on the evidence table means a second run
    refreshes the same assertions rather than duplicating them, and rows
    already stamped are skipped, so an interrupted pass resumes where it
    stopped.
    """
    columns = ", ".join(f'"{name}"' for name in _SELECT_COLUMNS)
    summary = {
        "rows_scanned": 0,
        "rows_with_evidence": 0,
        "assertions_written": 0,
        "rows_stamped": 0,
        "reasons_filled": 0,
        "rows_without_source_url": 0,
        "dry_run": dry_run,
    }

    with get_connection() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM product_details WHERE COALESCE(evidence_url,'') = ''"
        ).fetchone()[0]
        if limit is not None:
            total = min(total, limit)

        last_id = 0
        while True:
            remaining = None if limit is None else limit - summary["rows_scanned"]
            if remaining is not None and remaining <= 0:
                break
            take = batch_size if remaining is None else min(batch_size, remaining)
            batch = [
                dict(item) for item in conn.execute(
                    f"""SELECT {columns} FROM product_details
                        WHERE id > ? AND COALESCE(evidence_url,'') = ''
                        ORDER BY id LIMIT ?""",
                    (last_id, take),
                )
            ]
            if not batch:
                break
            last_id = batch[-1]["id"]
            summary["rows_scanned"] += len(batch)

            assertions: list[tuple] = []
            stamps: list[tuple] = []
            for row in batch:
                register_url = _register_url(row)
                if not register_url:
                    summary["rows_without_source_url"] += 1
                    continue
                row_assertions = assertions_for_row(row)
                if row_assertions:
                    assertions.extend(row_assertions)
                    summary["rows_with_evidence"] += 1
                reason = missing_reason_for_row(row)
                if reason:
                    summary["reasons_filled"] += 1
                stamps.append((
                    register_url, REGISTER_SECTION,
                    _text(row.get("verification_status")) or VERIFIED_RECORD,
                    reason, row["id"],
                ))

            summary["assertions_written"] += len(assertions)
            summary["rows_stamped"] += len(stamps)

            if not dry_run:
                if assertions:
                    conn.executemany(_INSERT_SQL, assertions)
                if stamps:
                    conn.executemany(
                        """UPDATE product_details
                           SET evidence_url = ?, evidence_section = ?,
                               verification_status = ?, missing_reason = ?
                           WHERE id = ?""",
                        stamps,
                    )
                conn.commit()

            if progress:
                progress(summary["rows_scanned"], total)

    return summary


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Backfill evidence for stored rows.")
    parser.add_argument("--limit", type=int, default=None, help="stop after this many rows")
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--dry-run", action="store_true", help="count without writing")
    args = parser.parse_args()

    def report(scanned: int, total: int) -> None:
        print(f"  {scanned}/{total} rows", flush=True)

    print(json.dumps(
        backfill_evidence(
            limit=args.limit,
            batch_size=args.batch_size,
            dry_run=args.dry_run,
            progress=report,
        ),
        indent=2,
    ))
