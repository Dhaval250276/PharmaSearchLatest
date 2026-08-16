"""Fills blank columns from what other registries published about the molecule.

A registry answers only for its own market, so a row is missing fields that a
different regulator states plainly about the same molecule. CDSCO never gives
an ATC code; EMA always does. Completion closes that gap by grouping every
harvested row by molecule and lending values across the group.

What may be lent depends on what the field describes:

* ATC code and therapeutic category are properties of the *molecule*. Metformin
  is A10BA02 in every country, so lending it is stating a fact, not inventing
  one, and it is written into the row's own column.
* SmPC, PIL and assessment reports are properties of a *specific marketing
  authorisation*. A Spanish product's SmPC is not the Indian product's label,
  and writing it into smpc_url would present another country's document as this
  product's own. Those are attached as clearly separate reference_* fields, with
  the regulator that issued them recorded alongside.

Every lent value records where it came from, so nothing inherited is ever
mistaken for something the local regulator published.
"""

from collections import Counter
from typing import Any

from core.logging_config import get_logger
from repository import get_connection, initialize_database
from services.field_availability import DOCUMENT_CAPABLE_SOURCES
from services.harvest_vocabulary import normalize_molecule
from services.therapeutic_category import short_therapeutic_category


logger = get_logger(__name__)

# Molecule-level facts, safe to write into the row's own column. The ATC code
# is the only one that can be lent verbatim: it is a code, so it means the same
# thing whichever registry states it.
#
# Therapeutic category is deliberately not lent. Registries write it in their
# own language and register -- Romania stores the ATC class in Romanian, the
# FDA stores label prose -- so a majority vote across a molecule spreads
# "Hipocolesterolemiante Si Hipotrigliceridemiante" or "Calpol products are
# indicated for..." over every other country's rows. It is derived from the
# agreed ATC code instead, which yields the same fact in one language.
MOLECULE_FIELDS = ("atc_code",)

# Authorisation-level documents, attached only as a molecule reference.
DOCUMENT_FIELDS = ("smpc_url", "pil_url", "assessment_report_url")


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def _molecule_key(row: dict[str, Any]) -> str:
    """Group on the normalized molecule, so "ATORVASTATIN CALCIUM" from one
    registry and "Atorvastatin" from another are the same group."""
    return normalize_molecule(row.get("substance")) or _clean(row.get("substance")).lower()


def _agreed_value(rows: list[dict[str, Any]], field: str) -> tuple[str, str]:
    """The value most registries state for a molecule-level field.

    A majority is used rather than the first value found: a single row with a
    mis-parsed ATC code should not be lent to the whole molecule.
    """
    votes: Counter[str] = Counter()
    sources: dict[str, str] = {}
    for row in rows:
        value = _clean(row.get(field))
        if not value:
            continue
        votes[value] += 1
        sources.setdefault(value, _clean(row.get("source")))
    if not votes:
        return "", ""
    value, _count = votes.most_common(1)[0]
    return value, sources.get(value, "")


def _document_donor(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The row whose documents best represent the molecule.

    Regulators that publish full document sets are preferred; among them the
    row carrying the most documents wins, so a reference points at a complete
    label rather than a stray PIL.
    """
    candidates = [
        row
        for row in rows
        if any(_clean(row.get(field)) for field in DOCUMENT_FIELDS)
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda row: (
            _clean(row.get("source")) in DOCUMENT_CAPABLE_SOURCES,
            sum(1 for field in DOCUMENT_FIELDS if _clean(row.get(field))),
        ),
    )


def _completions_for_group(rows: list[dict[str, Any]]) -> list[tuple[int, dict[str, str]]]:
    """The column updates each row in a molecule group needs."""
    agreed = {field: _agreed_value(rows, field) for field in MOLECULE_FIELDS}
    donor = _document_donor(rows)
    updates: list[tuple[int, dict[str, str]]] = []

    for row in rows:
        changes: dict[str, str] = {}
        lenders = []

        for field in MOLECULE_FIELDS:
            value, lender = agreed[field]
            if value and not _clean(row.get(field)):
                changes[field] = value
                if lender and lender not in lenders:
                    lenders.append(lender)

        # Derived from whichever code the row will end up carrying, so a row
        # that just inherited an ATC code gets the category that goes with it.
        atc_code = changes.get("atc_code") or _clean(row.get("atc_code"))
        category = short_therapeutic_category("", row.get("substance"), atc_code)
        if category and category != _clean(row.get("therapeutic_category")):
            changes["therapeutic_category"] = category

        if changes:
            changes["completion_source"] = ", ".join(lenders) or "derived from ATC code"

        if donor is not None and donor.get("id") != row.get("id"):
            reference_changes = {}
            for field in DOCUMENT_FIELDS:
                value = _clean(donor.get(field))
                # Only offer a reference where the row has no document of its own.
                if value and not _clean(row.get(field)):
                    reference_changes[f"reference_{field}"] = value
            if reference_changes:
                changes.update(reference_changes)
                changes["reference_source"] = _clean(donor.get("source"))
                changes["reference_product"] = _clean(donor.get("product"))

        if changes:
            updates.append((int(row["id"]), changes))

    return updates


def _load_rows() -> list[dict[str, Any]]:
    with get_connection() as connection:
        rows = connection.execute("SELECT * FROM product_details").fetchall()
    return [dict(row) for row in rows]


def _apply(updates: list[tuple[int, dict[str, str]]]) -> int:
    applied = 0
    with get_connection() as connection:
        for row_id, changes in updates:
            assignments = ", ".join(f"{column} = ?" for column in changes)
            connection.execute(
                f"UPDATE product_details SET {assignments} WHERE id = ?",
                (*changes.values(), row_id),
            )
            applied += 1
    return applied


def complete_fields(dry_run: bool = False) -> dict[str, Any]:
    """Lend molecule-level values across every harvested row.

    Returns what was filled, per field, so the gain is measured rather than
    assumed.
    """
    initialize_database()
    rows = _load_rows()

    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        key = _molecule_key(row)
        if key:
            groups.setdefault(key, []).append(row)

    updates: list[tuple[int, dict[str, str]]] = []
    for group in groups.values():
        # A molecule seen once has nobody to borrow from.
        if len(group) > 1:
            updates.extend(_completions_for_group(group))

    filled: Counter[str] = Counter()
    for _row_id, changes in updates:
        for column in changes:
            if not column.endswith("_source") and column != "reference_product":
                filled[column] += 1

    applied = 0 if dry_run else _apply(updates)
    logger.info(
        "Field completion %s %s rows across %s molecules",
        "would update" if dry_run else "updated",
        len(updates),
        len(groups),
    )
    return {
        "molecules": len(groups),
        "rows_examined": len(rows),
        "rows_updated": len(updates),
        "rows_written": applied,
        "filled": dict(filled.most_common()),
        "dry_run": dry_run,
    }


def repair_lent_categories() -> dict[str, Any]:
    """Re-derive therapeutic category on every row completion has touched.

    An earlier version of this pass lent the category verbatim, which spread one
    registry's wording -- in its own language -- across every other country's
    rows for that molecule. Only rows completion wrote are repaired: a category
    a regulator published for its own product is that regulator's text and stays
    exactly as it published it.
    """
    initialize_database()
    with get_connection() as connection:
        rows = [
            dict(row)
            for row in connection.execute(
                "SELECT id, substance, atc_code, therapeutic_category FROM product_details"
                " WHERE completion_source <> ''"
            ).fetchall()
        ]

    repaired = 0
    cleared = 0
    with get_connection() as connection:
        for row in rows:
            category = short_therapeutic_category("", row.get("substance"), row.get("atc_code"))
            if category == _clean(row.get("therapeutic_category")):
                continue
            connection.execute(
                "UPDATE product_details SET therapeutic_category = ? WHERE id = ?",
                (category, row["id"]),
            )
            repaired += 1
            if not category:
                cleared += 1

    logger.info("Repaired the therapeutic category on %s completed rows", repaired)
    return {"rows_examined": len(rows), "rows_repaired": repaired, "rows_cleared": cleared}


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Fill blank fields from other registries")
    parser.add_argument("--dry-run", action="store_true", help="Report what would be filled")
    parser.add_argument(
        "--repair-categories",
        action="store_true",
        help="Re-derive the therapeutic category on rows completion has written",
    )
    args = parser.parse_args()
    if args.repair_categories:
        print(json.dumps(repair_lent_categories(), indent=1))
    else:
        print(json.dumps(complete_fields(dry_run=args.dry_run), indent=1))
