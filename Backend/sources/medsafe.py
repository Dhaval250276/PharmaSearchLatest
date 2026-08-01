from typing import Any
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup

from core.logging_config import get_logger
from sources.parser import extract_dosage_form, extract_pack_size, extract_strength


MEDSAFE_BASE_URL = "https://www.medsafe.govt.nz"
PRODUCT_SEARCH_URL = f"{MEDSAFE_BASE_URL}/DbSearch/"
INFO_SEARCH_URL = f"{MEDSAFE_BASE_URL}/DbSearch/InfoSearch"
REQUEST_TIMEOUT = 8
MAX_RESULTS = 100
logger = get_logger(__name__)


def _clean_text(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def _verification_token(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    token = soup.select_one('input[name="__RequestVerificationToken"]')
    return token.get("value", "") if token else ""


def _lookup_url(substance: str) -> str:
    return f"{PRODUCT_SEARCH_URL}?ingredient={quote(substance.strip())}"


def _fallback_rows(substance: str, limit: int = MAX_RESULTS) -> list[dict[str, Any]]:
    display_substance = substance.strip()
    if not display_substance:
        return []
    return [
        {
            "substance": display_substance,
            "product": f"{display_substance} official New Zealand registry search",
            "company": "",
            "country": "New Zealand",
            "region": "NZ",
            "status": "Open official Medsafe registry - direct live parser unavailable or no exact match",
            "strength": extract_strength(display_substance),
            "dosage_form": extract_dosage_form(display_substance),
            "pack_size": extract_pack_size(display_substance),
            "registration_number": "",
            "document_type": "Official registry search handoff",
            "source": "Medsafe New Zealand",
            "source_url": _lookup_url(display_substance),
            "product_url": _lookup_url(display_substance),
            "url": _lookup_url(display_substance),
            "connector_mode": "manual_registry",
        }
    ][:limit]


def _header_index(headers: list[str], *needles: str) -> int | None:
    normalized_needles = [needle.lower() for needle in needles]
    for index, header in enumerate(headers):
        normalized_header = header.lower()
        if any(needle in normalized_header for needle in normalized_needles):
            return index
    return None


def _cell(cells: list[Any], index: int | None) -> str:
    if index is None or index >= len(cells):
        return ""
    return _clean_text(cells[index].get_text(" ", strip=True))


def _cell_link(cells: list[Any], index: int | None) -> str:
    if index is None or index >= len(cells):
        return ""
    link = cells[index].select_one("a[href]")
    if not link:
        return ""
    return urljoin(PRODUCT_SEARCH_URL, link.get("href", ""))


def _parse_product_search_results(html: str, substance: str, source_url: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    results = []
    seen = set()

    for table in soup.select("table"):
        header_cells = table.select("tr th")
        if not header_cells:
            first_row = table.select_one("tr")
            header_cells = first_row.select("td") if first_row else []
        headers = [_clean_text(cell.get_text(" ", strip=True)) for cell in header_cells]
        if not headers:
            continue

        ingredient_index = _header_index(headers, "ingredient", "active")
        product_index = _header_index(headers, "trade", "product", "medicine")
        sponsor_index = _header_index(headers, "sponsor", "applicant", "company")
        status_index = _header_index(headers, "status", "situation")
        classification_index = _header_index(headers, "classification")
        approval_index = _header_index(headers, "approval", "consent")

        if product_index is None and ingredient_index is None:
            continue

        for row in table.select("tr")[1:]:
            cells = row.select("td")
            if not cells:
                continue
            product = _cell(cells, product_index) or _cell(cells, ingredient_index)
            ingredient = _cell(cells, ingredient_index) or substance
            if not product:
                continue
            haystack = f"{product} {ingredient}".lower()
            if substance.strip().lower() not in haystack:
                continue

            product_url = _cell_link(cells, product_index) or source_url
            key = (product.lower(), _cell(cells, sponsor_index).lower(), product_url.lower())
            if key in seen:
                continue
            seen.add(key)

            status = _cell(cells, status_index) or "Listed by Medsafe"
            classification = _cell(cells, classification_index)
            if classification:
                status = f"{status} - {classification}"

            results.append(
                {
                    "substance": ingredient,
                    "product": product,
                    "company": _cell(cells, sponsor_index),
                    "country": "New Zealand",
                    "region": "NZ",
                    "status": status,
                    "strength": extract_strength(product),
                    "dosage_form": extract_dosage_form(product),
                    "pack_size": extract_pack_size(product),
                    "registration_number": "",
                    "registration_date": _cell(cells, approval_index),
                    "source": "Medsafe New Zealand",
                    "source_url": source_url,
                    "product_url": product_url,
                    "url": product_url,
                }
            )
    return results


def _document_links(html: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    links = {}
    for anchor in soup.select("a[href]"):
        href = urljoin(INFO_SEARCH_URL, anchor.get("href", ""))
        text = anchor.get_text(" ", strip=True).lower()
        combined = f"{text} {href.lower()}"
        if not links.get("smpc_url") and ("data sheet" in combined or "/datasheet/" in combined):
            links["smpc_url"] = href
        elif not links.get("pil_url") and (
            "consumer medicine information" in combined
            or "cmi" in combined
            or "/consumers/cmi/" in combined
        ):
            links["pil_url"] = href
    return links


def _post_product_search(session: requests.Session, substance: str) -> tuple[str, str]:
    response = session.get(
        PRODUCT_SEARCH_URL,
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    response.raise_for_status()
    token = _verification_token(response.text)
    payload = {
        "_handler": "SearchForm",
        "__RequestVerificationToken": token,
        "query.SearchType": "Product",
        "query.Ingredient": substance,
        "query.TradeName": "",
        "query.Sponsor": "",
        "query.Classification": "",
        "query.ProductType": "",
        "query.RegSituation": "",
    }
    result = session.post(
        PRODUCT_SEARCH_URL,
        data=payload,
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": "Mozilla/5.0", "Referer": PRODUCT_SEARCH_URL},
    )
    result.raise_for_status()
    return result.text, result.url


def _fetch_document_links(session: requests.Session, substance: str) -> dict[str, str]:
    try:
        response = session.get(
            INFO_SEARCH_URL,
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        response.raise_for_status()
        token = _verification_token(response.text)
        result = session.post(
            INFO_SEARCH_URL,
            data={
                "_handler": "SearchForm",
                "__RequestVerificationToken": token,
                "reportQuery.Medicine": substance,
                "reportQuery.Sponsor": "",
            },
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0", "Referer": INFO_SEARCH_URL},
        )
        result.raise_for_status()
        return _document_links(result.text)
    except requests.RequestException as exc:
        logger.warning("Medsafe information search failed for %s: %s", substance, exc)
        return {}


def run_medsafe_search(substance: str, limit: int = MAX_RESULTS) -> list[dict[str, Any]]:
    clean_substance = substance.strip()
    if not clean_substance:
        return []

    session = requests.Session()
    try:
        html, source_url = _post_product_search(session, clean_substance)
    except requests.RequestException as exc:
        logger.warning("Medsafe product search unavailable: %s", exc)
        return _fallback_rows(clean_substance, limit)

    rows = _parse_product_search_results(html, clean_substance, source_url)[:limit]
    if not rows:
        return _fallback_rows(clean_substance, limit)

    links = _fetch_document_links(session, clean_substance)
    if links:
        for row in rows:
            for field, value in links.items():
                row.setdefault(field, value)
    return rows
