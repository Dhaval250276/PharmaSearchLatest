from __future__ import annotations

from typing import Any


NOT_SUPPLIED = "Not supplied by regulator"
PENDING_ENRICHMENT = "Pending document enrichment"
NOT_APPLICABLE = "Not applicable for this source"

DOCUMENT_FIELDS = {"smpc_url", "pil_url", "assessment_report_url"}
DOCUMENT_ENRICHMENT_FIELDS = {
    "pack_size",
    "manufacturer_name",
    "manufacturer_country",
}
DOCUMENT_CAPABLE_SOURCES = {
    "MHRA",
    "EMA",
    "EU MRI Product Index",
    "Belgium FAMHP",
    "Ireland medicines.ie",
    "Spain CIMA",
    "ANMDMR Romania",
}


def missing_field_value(item: dict[str, Any], field: str) -> str:
    """Explain why a normalized regulatory field has no value."""
    source = str(item.get("source") or "").strip()
    if str(item.get("connector_mode") or "").strip() == "manual_registry":
        return "Product record not extracted"
    if field in DOCUMENT_FIELDS and source and source not in DOCUMENT_CAPABLE_SOURCES:
        return NOT_APPLICABLE
    if (
        field in DOCUMENT_ENRICHMENT_FIELDS
        and source
        and source not in DOCUMENT_CAPABLE_SOURCES
    ):
        return NOT_SUPPLIED
    if (
        field in DOCUMENT_FIELDS | DOCUMENT_ENRICHMENT_FIELDS
        and item.get("document_enrichment_attempted")
    ):
        return NOT_SUPPLIED
    if field in DOCUMENT_FIELDS | DOCUMENT_ENRICHMENT_FIELDS and any(
        item.get(name)
        for name in ("product_url", "url", "source_url", "smpc_url", "pil_url", "assessment_report_url")
    ):
        return PENDING_ENRICHMENT
    return NOT_SUPPLIED


def field_value(item: dict[str, Any], field: str, *values: object) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return missing_field_value(item, field)
