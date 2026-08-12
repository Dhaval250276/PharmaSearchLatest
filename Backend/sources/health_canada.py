from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

import requests

from core.logging_config import get_logger
from sources.parser import extract_dosage_form, extract_pack_size, extract_strength


BASE_URL = "https://health-products.canada.ca/api/drug"
DPD_PRODUCT_URL = "https://health-products.canada.ca/dpd-bdpp/info"
REQUEST_TIMEOUT = 15
DETAIL_WORKERS = 12
logger = get_logger(__name__)


def _get_field(row, *names):
    lowered = {str(key).lower(): value for key, value in row.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value not in (None, ""):
            return value
    return ""


def _fetch(dataset, **params):
    """Call one Drug Product Database dataset with server-side filtering."""
    url = f"{BASE_URL}/{dataset}/"
    response = requests.get(
        url,
        params={"lang": "en", "type": "json", **params},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, dict):
        return [payload]
    return payload or []


def _fetch_quietly(dataset, drug_code):
    try:
        return _fetch(dataset, id=drug_code)
    except (requests.RequestException, ValueError) as exc:
        logger.info("Health Canada %s lookup failed for %s: %s", dataset, drug_code, exc)
        return []


@lru_cache(maxsize=128)
def _ingredient_rows(substance):
    return _fetch("activeingredient", ingredientname=substance)


def _ingredient_strength(rows):
    strengths = []
    for row in rows:
        value = str(_get_field(row, "strength")).strip()
        unit = str(_get_field(row, "strength_unit", "strengthUnit")).strip()
        text = f"{value} {unit}".strip()
        if text and text not in strengths:
            strengths.append(text)
    return "; ".join(strengths[:3])


def _pack_size(packaging_rows):
    sizes = []
    for row in packaging_rows:
        size = str(_get_field(row, "package_size", "packageSize")).strip()
        unit = str(_get_field(row, "package_size_unit", "packageSizeUnit")).strip()
        package_type = str(_get_field(row, "package_type", "packageType")).strip()
        text = " ".join(part for part in (size, unit, package_type) if part).strip()
        if not text:
            text = str(_get_field(row, "product_information", "productInformation")).strip()
        if text and text not in sizes:
            sizes.append(text)
    return "; ".join(sizes[:3])


def _status_details(status_rows):
    """Latest status plus the original market date used as the registration date."""
    status = ""
    market_date = ""
    for row in status_rows:
        status = str(_get_field(row, "status", "status_name", "statusName")).strip() or status
        market_date = (
            str(_get_field(row, "original_market_date", "originalMarketDate")).strip()
            or market_date
        )
    return status, market_date


def _drug_record(substance, drug_code, ingredient_rows):
    products = _fetch_quietly("drugproduct", drug_code)
    if not products:
        return None
    product_row = products[0]
    product = _get_field(product_row, "brand_name", "brandName")
    if not product:
        return None

    forms = _fetch_quietly("form", drug_code)
    packaging = _fetch_quietly("packaging", drug_code)
    statuses = _fetch_quietly("status", drug_code)
    routes = _fetch_quietly("route", drug_code)

    status, market_date = _status_details(statuses)
    dosage_form = ""
    if forms:
        dosage_form = str(
            _get_field(forms[0], "pharmaceutical_form_name", "pharmaceuticalFormName")
        ).strip()
    route = ""
    if routes:
        route = str(
            _get_field(routes[0], "route_of_administration_name", "routeOfAdministrationName")
        ).strip()

    din = str(_get_field(product_row, "drug_identification_number", "drugIdentificationNumber")).strip()
    company = str(_get_field(product_row, "company_name", "companyName")).strip()
    product_url = f"{DPD_PRODUCT_URL}.do?lang=en&code={drug_code}"
    return {
        "substance": substance,
        "product": product,
        "company": company,
        "country": "Canada",
        "region": "CA",
        "status": status,
        "strength": _ingredient_strength(ingredient_rows) or extract_strength(product),
        "dosage_form": dosage_form or extract_dosage_form(product),
        "pack_size": _pack_size(packaging) or extract_pack_size(product),
        "route": route,
        "registration_number": din or str(drug_code),
        "registration_date": market_date,
        "source": "Health Canada",
        "source_url": f"{BASE_URL}/activeingredient/?lang=en&type=json&ingredientname={substance}",
        "product_url": product_url,
        "url": product_url,
    }


def run_health_canada_search(substance, limit=100):
    query = substance.strip()
    if not query:
        return []
    try:
        ingredients = _ingredient_rows(query)
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Health Canada request failed: %s", exc)
        return []

    rows_by_code = {}
    for row in ingredients:
        drug_code = str(_get_field(row, "drug_code", "drugCode")).strip()
        if drug_code:
            rows_by_code.setdefault(drug_code, []).append(row)
    if not rows_by_code:
        return []

    codes = list(rows_by_code)[:limit]
    with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as executor:
        records = executor.map(
            lambda code: _drug_record(query, code, rows_by_code[code]), codes
        )
        return [record for record in records if record]
