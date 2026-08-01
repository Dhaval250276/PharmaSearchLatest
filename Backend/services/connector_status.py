from __future__ import annotations

from typing import Any

from sources.source_registry import connector_metadata


CORE_CONNECTOR_STATUS: dict[str, dict[str, str]] = {
    "FDA": {
        "tier": "core",
        "mode": "live_api",
        "status": "ready",
        "notes": "OpenFDA label/NDC APIs. Returns product records when OpenFDA has matching substance metadata.",
    },
    "MHRA": {
        "tier": "core",
        "mode": "live_api_pdf",
        "status": "ready",
        "notes": "MHRA API plus SmPC/PIL/PAR URL merge and PDF metadata extraction.",
    },
    "EMA": {
        "tier": "core",
        "mode": "dataset_html_pdf",
        "status": "ready",
        "notes": "EMA medicine data plus product page/document enrichment.",
    },
    "EU MRI Product Index": {
        "tier": "eu_national",
        "mode": "live_html_or_registry",
        "status": "partial",
        "notes": "HMA MRI Product Index for MRP/DCP products. Current official service may return 503; connector parses table rows when available and otherwise provides official registry handoff.",
    },
    "Health Canada": {
        "tier": "core",
        "mode": "live_api",
        "status": "ready",
        "notes": "Health Canada DPD API. Some document fields may require later deep extraction.",
    },
    "TGA Australia": {
        "tier": "core",
        "mode": "live_html_or_registry",
        "status": "partial",
        "notes": "ARTG page is slow/intermittent from this environment. No hardcoded product fallback; manual registry row when blocked.",
    },
    "Medsafe New Zealand": {
        "tier": "core",
        "mode": "form_or_registry",
        "status": "partial",
        "notes": "Medsafe app shell is reachable, but direct result extraction is unstable. Manual registry row when no product rows are parsed.",
    },
    "Hong Kong Drug Office": {
        "tier": "core",
        "mode": "live_form_detail",
        "status": "ready",
        "notes": "Official form search and product detail extraction for product, holder, registration number/date.",
    },
    "NMPA China": {
        "tier": "asia_national",
        "mode": "official_registry_handoff",
        "status": "manual_parser_needed",
        "notes": "English NMPA database page is a registry landing page, not a direct active-substance product search API. Needs Chinese NMPA database/browser connector before live product rows can be trusted.",
    },
    "GRLS Russia": {
        "tier": "core",
        "mode": "browser_registry",
        "status": "ready_with_browser_permission",
        "notes": "Official GRLS browser flow returns real product rows. FastAPI must run with browser-launch permission.",
    },
    "Belgium FAMHP": {
        "tier": "eu_national",
        "mode": "live_html",
        "status": "partial",
        "notes": "EU national connector; coverage depends on regulator page search behavior.",
    },
    "France BDPM": {
        "tier": "eu_national",
        "mode": "live_html",
        "status": "partial",
        "notes": "EU national connector; coverage depends on regulator page search behavior.",
    },
    "Ireland medicines.ie": {
        "tier": "eu_national",
        "mode": "live_html_documents",
        "status": "partial",
        "notes": "EU national connector with document links where available.",
    },
    "Spain CIMA": {
        "tier": "eu_national",
        "mode": "live_api",
        "status": "ready",
        "notes": "CIMA API connector for Spain.",
    },
}


def connector_status_rows() -> list[dict[str, Any]]:
    metadata_by_name = {item["name"]: item for item in connector_metadata()}
    rows: list[dict[str, Any]] = []
    for name, status in CORE_CONNECTOR_STATUS.items():
        metadata = metadata_by_name.get(name, {})
        rows.append(
            {
                "name": name,
                "region": metadata.get("region", ""),
                "countries": metadata.get("countries", []),
                "enabled": metadata.get("enabled", False),
                "supports_live_search": metadata.get("supports_live_search", False),
                "supports_documents": metadata.get("supports_documents", False),
                **status,
            }
        )
    return rows
