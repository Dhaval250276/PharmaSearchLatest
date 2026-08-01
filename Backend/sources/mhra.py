import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup

from core.logging_config import get_logger
from sources.mhra_document_parser import enrich_mhra_document_metadata
from sources.parser import (
    clean_product_name,
    extract_dosage_form,
    extract_pack_size,
    extract_registration_number,
    extract_strength,
)


MHRA_BASE_URL = "https://products.mhra.gov.uk"
MHRA_SEARCH_URL = (
    "https://mhraproducts4853.search.windows.net/indexes/products-index/docs"
)
MHRA_SEARCH_API_KEY = "17CCFC430C1A78A169B392A35A99C49D"
MHRA_PAGE_SIZE = 100
MHRA_MAX_RESULTS = 1000
MHRA_MAX_WORKERS = 6
logger = get_logger(__name__)


def _finalize_mhra_results(results, enrich_documents=False):
    merged_results = _sort_document_results(_merge_related_document_urls(results))
    if enrich_documents:
        return enrich_mhra_document_metadata(merged_results)
    return merged_results


def _sort_document_results(results):
    document_rank = {"SPC": 0, "PIL": 1, "PAR": 2}
    return sorted(
        results,
        key=lambda item: (
            0 if item.get("smpc_url") or item.get("pil_url") else 1,
            document_rank.get(str(item.get("document_type") or "").upper(), 9),
            item.get("product") or "",
            item.get("registration_number") or "",
        ),
    )


def _format_pl_number(value):
    if not value:
        return ""
    text = str(value).upper().replace(" ", "").replace("_", "").replace("-", "")
    match = re.match(r"^(PLGB|PLNI|PLPI|PL|THRGB|THRNI|THR|NRGB|NRNI|NR)(\d{5})(\d{4})$", text)
    if match:
        return f"{match.group(1)} {match.group(2)}/{match.group(3)}"
    return text


def _build_search_query(substance):
    tokens = re.split(r"[,+\-!(){}\[\]^~*?:%/\s]+", substance.strip())
    tokens = [token for token in tokens if token]
    if not tokens:
        return substance
    return " ".join(f"({token}~1 || {token}^{4})" for token in tokens)


def _active_substances(record):
    values = record.get("substance_name") or []
    if isinstance(values, str):
        values = [values]
    return [str(value).strip() for value in values if str(value).strip()]


def _normalized_tokens(value):
    return [
        token
        for token in re.split(r"[^a-z0-9]+", str(value or "").lower())
        if token
    ]


def _record_matches_substance(record, substance):
    query_tokens = _normalized_tokens(substance)
    if not query_tokens:
        return True
    active_text = " ".join(_active_substances(record))
    searchable_text = " ".join(
        [
            active_text,
            str(record.get("title") or ""),
            str(record.get("product_name") or ""),
        ]
    ).lower()
    searchable_tokens = set(_normalized_tokens(searchable_text))
    return all(token in searchable_tokens for token in query_tokens)


def _highlight_text(record):
    highlights = record.get("@search.highlights") or {}
    values = highlights.get("content") or []
    if isinstance(values, str):
        values = [values]
    text = "\n".join(str(value) for value in values)
    text = re.sub(r"<[^>]+>", "", text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def _company_from_highlights(record, registration_number):
    text = _highlight_text(record)
    if not text:
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    ignore_prefixes = (
        "public assessment report",
        "national procedure",
        "lay summary",
        "summary of product characteristics",
        "patient information leaflet",
        "package leaflet",
        "par ",
        "pl ",
    )
    cleaned_registration = re.sub(r"\s+", "", registration_number or "").upper()
    for index, line in enumerate(lines):
        normalized_line = re.sub(r"\s+", "", line).upper()
        if cleaned_registration and cleaned_registration in normalized_line:
            for candidate in lines[index + 1 : index + 5]:
                lower_candidate = candidate.lower()
                if lower_candidate.startswith(ignore_prefixes):
                    continue
                if re.search(r"\b(?:ltd|limited|plc|gmbh|s\.?a\.?|b\.?v\.?|inc|llc|company|pharma|pharmaceuticals?)\b", candidate, re.IGNORECASE):
                    return candidate.strip(" .")
    for line in lines:
        if re.search(r"\b(?:ltd|limited|plc|gmbh|s\.?a\.?|b\.?v\.?|inc|llc|pharma|pharmaceuticals?)\b", line, re.IGNORECASE):
            return line.strip(" .")
    return ""


def _extract_mhra_json_record(record, substance):
    active_substances = _active_substances(record)
    product = clean_product_name(record.get("product_name") or record.get("title") or "")
    pl_numbers = record.get("pl_number") or []
    if isinstance(pl_numbers, str):
        pl_numbers = [pl_numbers]
    registration_number = _format_pl_number(pl_numbers[0]) if pl_numbers else ""
    if not registration_number:
        registration_number = extract_registration_number(product)
    company = _company_from_highlights(record, registration_number)

    document_url = record.get("metadata_storage_path") or ""
    document_type = str(record.get("doc_type") or "").upper()
    if document_type == "SPC":
        document_type = "SPC"
    elif document_type == "PIL":
        document_type = "PIL"
    elif document_type == "PAR":
        document_type = "PAR"

    item = {
        "substance": ", ".join(active_substances) or substance,
        "product": product,
        "company": company,
        "country": "United Kingdom",
        "region": "UK",
        "status": "Authorised",
        "strength": extract_strength(product),
        "dosage_form": extract_dosage_form(product),
        "pack_size": extract_pack_size(product),
        "registration_number": registration_number,
        "registration_date": record.get("created", ""),
        "document_type": document_type,
        "source": "MHRA",
        "source_url": f"{MHRA_BASE_URL}/search/?search={quote(substance)}&page=1",
        "product_url": document_url,
        "url": document_url,
        "last_checked": record.get("created", ""),
    }
    if document_type == "SPC":
        item["smpc_url"] = document_url
    elif document_type == "PIL":
        item["pil_url"] = document_url
    elif document_type == "PAR":
        item["assessment_report_url"] = document_url
    return item


def _fetch_mhra_page(substance, skip):
    params = {
        "api-key": MHRA_SEARCH_API_KEY,
        "api-version": "2017-11-11",
        "highlight": "content",
        "queryType": "full",
        "$count": "true",
        "$top": str(MHRA_PAGE_SIZE),
        "$skip": str(skip),
        "search": _build_search_query(substance),
        "scoringProfile": "preferKeywords",
        "searchMode": "all",
    }
    response = requests.get(
        MHRA_SEARCH_URL,
        params=params,
        timeout=45,
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
    )
    response.raise_for_status()
    return response.json()


def _run_mhra_api_search(substance, limit=MHRA_MAX_RESULTS, enrich_documents=False):
    results = []
    seen = set()
    max_results = min(limit, MHRA_MAX_RESULTS)

    first_payload = _fetch_mhra_page(substance, 0)
    total = int(first_payload.get("@odata.count") or 0)
    pages_to_fetch = min(total, max_results)
    payloads = [(0, first_payload)]
    skips = list(range(MHRA_PAGE_SIZE, pages_to_fetch, MHRA_PAGE_SIZE))

    if skips:
        with ThreadPoolExecutor(max_workers=min(MHRA_MAX_WORKERS, len(skips))) as executor:
            jobs = {
                executor.submit(_fetch_mhra_page, substance, skip): skip
                for skip in skips
            }
            for job in as_completed(jobs):
                payloads.append((jobs[job], job.result()))

    for _, payload in sorted(payloads, key=lambda item: item[0]):
        records = payload.get("value", [])
        if not records:
            continue
        for record in records:
            if not _record_matches_substance(record, substance):
                continue
            item = _extract_mhra_json_record(record, substance)
            key = (
                item.get("registration_number"),
                item.get("document_type"),
                item.get("url"),
            )
            if key in seen:
                continue
            seen.add(key)
            results.append(item)
            if len(results) >= max_results:
                return _finalize_mhra_results(results, enrich_documents=enrich_documents)
    return _finalize_mhra_results(results, enrich_documents=enrich_documents)


def _merge_related_document_urls(results):
    related = {}
    for item in results:
        registration_number = item.get("registration_number", "")
        if not registration_number:
            continue
        bucket = related.setdefault(registration_number, {})
        for field in ["smpc_url", "pil_url", "assessment_report_url"]:
            if item.get(field) and not bucket.get(field):
                bucket[field] = item[field]
        if item.get("company") and not bucket.get("company"):
            bucket["company"] = item["company"]

    for item in results:
        registration_number = item.get("registration_number", "")
        bucket = related.get(registration_number, {})
        for field in ["smpc_url", "pil_url", "assessment_report_url"]:
            if bucket.get(field) and not item.get(field):
                item[field] = bucket[field]
        if bucket.get("company") and not item.get("company"):
            item["company"] = bucket["company"]
    return results


def _document_type(text, classes):
    combined = f"{text} {' '.join(classes or [])}".lower()
    if "spc" in combined or "summary of product characteristics" in combined:
        return "SPC"
    if "pil" in combined or "patient information leaflet" in combined:
        return "PIL"
    if "par" in combined or "public assessment report" in combined:
        return "PAR"
    return ""


def _extract_mhra_results(html, substance, page_url):
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for link in soup.select("a[href]"):
        href = urljoin(MHRA_BASE_URL, link.get("href", ""))
        text = clean_product_name(link.get_text(" ", strip=True))
        if not text:
            continue
        registration_number = extract_registration_number(text)
        if not registration_number:
            continue
        metadata_text = ""
        parent = link.find_parent(["div", "dd", "li", "article"])
        if parent:
            metadata_text = parent.get_text(" ", strip=True)
        active_substance = substance
        marker = "Active substances:"
        if marker in metadata_text:
            active_substance = metadata_text.split(marker, 1)[1].split(" ", 1)[0]
        results.append(
            {
                "substance": active_substance or substance,
                "product": text,
                "company": "",
                "country": "United Kingdom",
                "region": "UK",
                "status": "Authorised",
                "registration_number": registration_number,
                "document_type": _document_type(text, link.get("class")),
                "source": "MHRA",
                "source_url": page_url,
                "product_url": href,
                "url": href,
            }
        )
    return results


def run_mhra_search(substance, limit=MHRA_MAX_RESULTS, enrich_documents=False):
    try:
        return _run_mhra_api_search(substance, limit=limit, enrich_documents=enrich_documents)
    except (requests.RequestException, ValueError) as e:
        logger.warning("MHRA API search failed: %s", e)

    url = f"{MHRA_BASE_URL}/search/?search={quote(substance)}&page=1"
    try:
        response = requests.get(
            url,
            timeout=45,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/137.0.0.0 Safari/537.36"
                )
            },
        )
        response.raise_for_status()
        return _finalize_mhra_results(
            _extract_mhra_results(response.text, substance, url),
            enrich_documents=enrich_documents,
        )
    except requests.RequestException as e:
        logger.warning("MHRA fallback search failed: %s", e)
        return []
