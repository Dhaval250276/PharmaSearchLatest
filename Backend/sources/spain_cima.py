from datetime import datetime, timezone
import re
from urllib.parse import quote

import requests

from core.logging_config import get_logger
from sources.parser import extract_dosage_form, extract_pack_size, extract_strength


CIMA_SEARCH_URL = "https://cima.aemps.es/cima/rest/medicamentos"
CIMA_DETAIL_URL = "https://cima.aemps.es/cima/rest/medicamento"
CIMA_BASE_URL = "https://cima.aemps.es/cima"
CIMA_PAGE_SIZE = 200
CIMA_MAX_RESULTS = 1000
DETAIL_TIMEOUT = 10
PACK_SIZE_PATTERN = re.compile(
    r",\s*\d+(?:[.,]\d+)?\s*(?:x\s*\d+\s*)?"
    r"(?:comprimidos?|c[aá]psulas?|sobres?|viales?|ampollas?|jeringas?|parches?"
    r"|frascos?|bolsas?|envases?|unidades?|ml|mg|g)\b[^,]*",
    flags=re.IGNORECASE,
)
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


def _epoch_date(value):
    """CIMA reports dates as epoch milliseconds."""
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return ""
    if timestamp <= 0:
        return ""
    return datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _authorisation_date(row):
    estado = row.get("estado") or {}
    if not isinstance(estado, dict):
        return ""
    return _epoch_date(estado.get("aut"))


def _named_value(row, field):
    value = row.get(field)
    if isinstance(value, dict):
        return str(value.get("nombre") or "").strip()
    return str(value or "").strip()


def _extract_record(row, substance):
    docs = _document_urls(row.get("docs"))
    registration_number = str(row.get("nregistro") or "")
    product_url = f"{CIMA_BASE_URL}/dochtml/ft/{registration_number}/FT_{registration_number}.html"
    if not docs.get("smpc_url"):
        product_url = f"{CIMA_BASE_URL}/medicamento/{registration_number}"
    product = row.get("nombre", "")
    holder = str(row.get("labtitular") or "").strip()
    marketer = str(row.get("labcomercializador") or "").strip()
    return {
        "substance": substance,
        "product": product,
        "company": holder or marketer,
        "commercial_company": marketer,
        "country": "Spain",
        "region": "EU",
        "status": _status(row),
        "strength": str(row.get("dosis") or "").strip() or extract_strength(product),
        "dosage_form": _named_value(row, "formaFarmaceutica")
        or _named_value(row, "formaFarmaceuticaSimplificada")
        or extract_dosage_form(product),
        "pack_size": extract_pack_size(product),
        "route": _first_named(row.get("viasAdministracion")),
        "registration_number": registration_number,
        "registration_date": _authorisation_date(row),
        "source": "Spain CIMA",
        "source_url": f"{CIMA_SEARCH_URL}?practiv1={quote(substance)}",
        "product_url": product_url,
        "url": product_url,
        "smpc_url": docs.get("smpc_url", ""),
        "pil_url": docs.get("pil_url", ""),
    }


def _first_named(values):
    for value in values or []:
        name = str((value or {}).get("nombre") or "").strip()
        if name:
            return name
    return ""


def _best_atc_code(atcs):
    """CIMA returns the ATC hierarchy; the deepest level is the product ATC."""
    best_code = ""
    best_level = -1
    for entry in atcs or []:
        code = str((entry or {}).get("codigo") or "").strip()
        try:
            level = int((entry or {}).get("nivel") or 0)
        except (TypeError, ValueError):
            level = len(code)
        if code and level > best_level:
            best_code = code
            best_level = level
    return best_code


def _spanish_pack_size(name):
    """CIMA presentation names end with the pack quantity, e.g. ", 50 comprimidos"."""
    match = PACK_SIZE_PATTERN.search(str(name or ""))
    if match:
        return " ".join(match.group(0).strip(" ,").split())
    return extract_pack_size(name)


def _detail_pack_size(presentaciones):
    sizes = []
    for entry in presentaciones or []:
        name = str((entry or {}).get("nombre") or "").strip()
        pack = _spanish_pack_size(name)
        if pack and pack not in sizes:
            sizes.append(pack)
    return "; ".join(sizes[:3])


def fetch_cima_detail(registration_number):
    """Fetch the per-product record that carries ATC codes and pack sizes."""
    if not registration_number:
        return {}
    try:
        response = requests.get(
            CIMA_DETAIL_URL,
            params={"nregistro": registration_number},
            timeout=DETAIL_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.info("Spain CIMA detail lookup failed for %s: %s", registration_number, exc)
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        "atc_code": _best_atc_code(payload.get("atcs")),
        "pack_size": _detail_pack_size(payload.get("presentaciones")),
        "registration_date": _authorisation_date(payload),
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
