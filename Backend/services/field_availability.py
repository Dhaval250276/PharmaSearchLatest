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
NON_EU_DOCUMENT_SOURCES = {
    "FDA",
    "FDA Orange Book",
    "FDA Purple Book",
    "Health Canada",
    "TGA Australia",
    "Medsafe New Zealand",
}


def missing_field_value(item: dict[str, Any], field: str) -> str:
    """Explain why a normalized regulatory field has no value."""
    source = str(item.get("source") or "").strip()
    if str(item.get("connector_mode") or "").strip() == "manual_registry":
        return "Product record not extracted"
    if field in DOCUMENT_FIELDS and source in NON_EU_DOCUMENT_SOURCES:
        return NOT_APPLICABLE
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
