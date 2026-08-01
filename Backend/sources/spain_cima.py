from urllib.parse import quote

import requests

from core.logging_config import get_logger
from sources.parser import extract_dosage_form, extract_pack_size, extract_strength


CIMA_SEARCH_URL = "https://cima.aemps.es/cima/rest/medicamentos"
CIMA_BASE_URL = "https://cima.aemps.es/cima"
CIMA_PAGE_SIZE = 200
CIMA_MAX_RESULTS = 1000
logger = get_logger(__name__)


def _document_urls(docs):
    urls = {"smpc_url": "", "pil_url": ""}
    for doc in docs or []:
        doc_type = doc.get("tipo")
        url = doc.get("url") or doc.get("urlHtml") or ""
        if doc_type == 1:
            urls["smpc_url"] = url
        elif doc_type == 2:
            urls["pil_url"] = url
    return urls


def _status(row):
    if row.get("comerc") is True:
        return "Marketed"
    if row.get("estado"):
        return "Authorised"
    return ""


def _extract_record(row, substance):
    docs = _document_urls(row.get("docs"))
    registration_number = str(row.get("nregistro") or "")
    product_url = f"{CIMA_BASE_URL}/dochtml/ft/{registration_number}/FT_{registration_number}.html"
    if not docs.get("smpc_url"):
        product_url = f"{CIMA_BASE_URL}/medicamento/{registration_number}"
    return {
        "substance": substance,
        "product": row.get("nombre", ""),
        "company": row.get("labtitular", "") or row.get("labcomercializador", ""),
        "country": "Spain",
        "region": "EU",
        "status": _status(row),
        "strength": extract_strength(row.get("nombre", "")),
        "dosage_form": extract_dosage_form(row.get("nombre", "")),
        "pack_size": extract_pack_size(row.get("nombre", "")),
        "registration_number": registration_number,
        "source": "Spain CIMA",
        "source_url": f"{CIMA_SEARCH_URL}?practiv1={quote(substance)}",
        "product_url": product_url,
        "url": product_url,
        "smpc_url": docs.get("smpc_url", ""),
        "pil_url": docs.get("pil_url", ""),
    }


def run_spain_cima_search(substance, limit=CIMA_MAX_RESULTS):
    results = []
    page = 1
    try:
        while len(results) < limit:
            response = requests.get(
                CIMA_SEARCH_URL,
                params={"practiv1": substance, "pagina": page},
                timeout=30,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            response.raise_for_status()
            payload = response.json()
            records = payload.get("resultados", [])
            if not records:
                break
            for row in records:
                product = row.get("nombre", "")
                if not product:
                    continue
                results.append(_extract_record(row, substance))
                if len(results) >= limit:
                    break
            total = int(payload.get("totalFilas") or 0)
            if page * CIMA_PAGE_SIZE >= total:
                break
            page += 1
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Spain CIMA request failed: %s", exc)
        return []
    return results
