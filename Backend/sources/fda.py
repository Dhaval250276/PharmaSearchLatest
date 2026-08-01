from functools import lru_cache
import re
from urllib.parse import quote

import requests

from core.logging_config import get_logger
from sources.parser import extract_dosage_form, extract_strength


OPENFDA_LABEL_URL = "https://api.fda.gov/drug/label.json"
OPENFDA_NDC_URL = "https://api.fda.gov/drug/ndc.json"
logger = get_logger(__name__)


def _first(values):
    if isinstance(values, list) and values:
        return values[0]
    if isinstance(values, str):
        return values
    return ""


def _join_values(values):
    if isinstance(values, list):
        return " ".join(str(value) for value in values if str(value).strip())
    return str(values or "")


def _label_text(item, *fields):
    parts = []
    for field in fields:
        value = _join_values(item.get(field))
        if value:
            parts.append(value)
    return " ".join(parts)


def _fda_strength(item, product):
    text = _label_text(
        item,
        "active_ingredient",
        "spl_product_data_elements",
        "description",
    )
    return extract_strength(text) or extract_strength(product)


def _fda_dosage_form(openfda, item, product):
    dosage_form = _first(openfda.get("dosage_form"))
    if dosage_form:
        return dosage_form.title()
    text = _label_text(
        item,
        "spl_product_data_elements",
        "dosage_and_administration",
        "description",
    )
    return extract_dosage_form(text) or extract_dosage_form(product)


def _fda_therapeutic_category(item):
    purpose = _first(item.get("purpose"))
    if purpose:
        return " ".join(purpose.split())
    pharmacologic_class = _first(item.get("openfda", {}).get("pharm_class_epc"))
    return pharmacologic_class


def _normalize(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


@lru_cache(maxsize=128)
def _fetch_ndc_records(substance):
    query = f'active_ingredients.name:"{substance.upper()}"'
    try:
        response = requests.get(
            OPENFDA_NDC_URL,
            params={"search": query, "limit": 1000},
            timeout=5,
        )
        if response.status_code == 404:
            return []
        response.raise_for_status()
        return response.json().get("results", [])
    except (requests.RequestException, ValueError) as exc:
        logger.warning("FDA NDC request failed: %s", exc)
        return []


def _pack_size_from_ndc(record):
    packages = record.get("packaging") or []
    descriptions = [
        package.get("description", "")
        for package in packages
        if package.get("description")
    ]
    return "; ".join(descriptions[:3])


def _strength_from_ndc(record):
    ingredients = record.get("active_ingredients") or []
    strengths = [
        ingredient.get("strength", "")
        for ingredient in ingredients
        if ingredient.get("strength")
    ]
    return "; ".join(strengths)


def _best_ndc_record(product, company, ndc_records):
    product_key = _normalize(product)
    company_key = _normalize(company)
    best_record = None
    best_score = 0
    for record in ndc_records:
        names = [
            record.get("brand_name", ""),
            record.get("brand_name_base", ""),
            record.get("generic_name", ""),
        ]
        name_keys = [_normalize(name) for name in names if name]
        if not name_keys:
            continue
        score = 0
        if product_key in name_keys:
            score += 100
        elif any(product_key and (product_key in key or key in product_key) for key in name_keys):
            score += 70
        labeler_key = _normalize(record.get("labeler_name", ""))
        if company_key and labeler_key and (company_key in labeler_key or labeler_key in company_key):
            score += 25
        if score > best_score:
            best_record = record
            best_score = score
    return best_record


def run_fda_search(substance, limit=100):
    encoded = quote(f'openfda.substance_name:"{substance}"')
    url = f"{OPENFDA_LABEL_URL}?search={encoded}&limit={limit}"
    try:
        response = requests.get(url, timeout=8)
        if response.status_code == 404:
            return []
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        logger.warning("FDA request failed: %s", exc)
        return []
    except ValueError as exc:
        logger.warning("FDA returned invalid JSON: %s", exc)
        return []

    ndc_records = _fetch_ndc_records(substance)
    results = []
    for item in data.get("results", []):
        openfda = item.get("openfda", {})
        product = _first(openfda.get("brand_name")) or _first(openfda.get("generic_name"))
        application_number = _first(openfda.get("application_number"))
        route = _first(openfda.get("route"))
        if not product:
            continue
        company = _first(openfda.get("manufacturer_name"))
        ndc_record = _best_ndc_record(product, company, ndc_records)
        product_query_url = url
        if application_number:
            product_query = quote(f'openfda.application_number:"{application_number}"')
            product_query_url = f"{OPENFDA_LABEL_URL}?search={product_query}&limit=10"
        results.append(
            {
                "substance": substance,
                "product": product,
                "company": company or (ndc_record or {}).get("labeler_name", ""),
                "country": "United States",
                "region": "US",
                "status": "Label available",
                "strength": _fda_strength(item, product) or _strength_from_ndc(ndc_record or {}),
                "dosage_form": _fda_dosage_form(openfda, item, product)
                or str((ndc_record or {}).get("dosage_form", "")).title(),
                "pack_size": _pack_size_from_ndc(ndc_record or {}),
                "expiry_date": (ndc_record or {}).get("listing_expiration_date", ""),
                "route": route,
                "therapeutic_category": _fda_therapeutic_category(item),
                "registration_number": application_number,
                "source": "FDA",
                "source_url": url,
                "product_url": product_query_url,
                "url": product_query_url,
            }
        )
    return results
