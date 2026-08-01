from functools import lru_cache

import requests

from core.logging_config import get_logger
from sources.parser import extract_dosage_form, extract_pack_size, extract_strength


BASE_URL = "https://health-products.canada.ca/api/drug"
logger = get_logger(__name__)


def _get_field(row, *names):
    lowered = {str(key).lower(): value for key, value in row.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value not in (None, ""):
            return value
    return ""


@lru_cache(maxsize=8)
def _fetch_dataset(name):
    url = f"{BASE_URL}/{name}/?lang=en&type=json"
    response = requests.get(url, timeout=45)
    response.raise_for_status()
    return response.json()


def run_health_canada_search(substance, limit=100):
    try:
        ingredients = _fetch_dataset("activeingredient")
        products = _fetch_dataset("drugproduct")
        companies = _fetch_dataset("company")
        statuses = _fetch_dataset("status")
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Health Canada request failed: %s", exc)
        return []

    query = substance.strip().lower()
    matching_codes = {
        str(_get_field(row, "drug_code", "drugCode"))
        for row in ingredients
        if query in str(_get_field(row, "ingredient_name", "ingredientName")).lower()
    }
    if not matching_codes:
        return []

    company_by_code = {
        str(_get_field(row, "drug_code", "drugCode")): _get_field(row, "company_name", "companyName")
        for row in companies
    }
    status_by_code = {
        str(_get_field(row, "drug_code", "drugCode")): _get_field(row, "status", "status_name", "statusName")
        for row in statuses
    }

    results = []
    for row in products:
        drug_code = str(_get_field(row, "drug_code", "drugCode"))
        if drug_code not in matching_codes:
            continue
        product = _get_field(row, "brand_name", "brandName")
        if not product:
            continue
        results.append(
            {
                "substance": substance,
                "product": product,
                "company": company_by_code.get(drug_code, ""),
                "country": "Canada",
                "region": "CA",
                "status": status_by_code.get(drug_code, ""),
                "strength": extract_strength(product),
                "dosage_form": extract_dosage_form(product),
                "pack_size": extract_pack_size(product),
                "registration_number": drug_code,
                "source": "Health Canada",
                "source_url": f"{BASE_URL}/drugproduct/?lang=en&type=json",
                "product_url": "https://health-products.canada.ca/dpd-bdpp/index-eng.jsp",
                "url": "https://health-products.canada.ca/dpd-bdpp/index-eng.jsp",
            }
        )
        if len(results) >= limit:
            break
    return results
