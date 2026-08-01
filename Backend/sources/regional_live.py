from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urljoin
import json
import re
import subprocess
import sys

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

from core.logging_config import get_logger
from sources.parser import extract_dosage_form, extract_pack_size, extract_strength


REQUEST_TIMEOUT = 5
MAX_RESULTS = 50
MAX_CDSCO_PDFS = 1
MAX_CDSCO_PAGES_PER_PDF = 4
MAX_CDSCO_LIVE_PDF_KB = 600
logger = get_logger(__name__)

RUSSIAN_INN_TERMS = {
    "atorvastatin": "Аторвастатин",
    "enalapril": "Эналаприл",
    "ibuprofen": "Ибупрофен",
    "lidocaine": "Лидокаин",
    "metformin": "Метформин",
    "mirabegron": "Мирабегрон",
    "paracetamol": "Парацетамол",
    "prilocaine": "Прилокаин",
    "quetiapine": "Кветиапин",
    "solifenacin": "Солифенацин",
    "tafluprost": "Тафлупрост",
}


@dataclass(frozen=True, slots=True)
class RegionalSourceConfig:
    source: str
    country: str
    region: str
    search_url: str
    query_params: tuple[tuple[str, str], ...] = ()


REGIONAL_SOURCES = {
    "SAHPRA South Africa": RegionalSourceConfig(
        source="SAHPRA South Africa",
        country="South Africa",
        region="AF",
        search_url="https://www.sahpra.org.za/registered-health-products/",
    ),
    "SFDA Saudi Arabia": RegionalSourceConfig(
        source="SFDA Saudi Arabia",
        country="Saudi Arabia",
        region="ME",
        search_url="https://www.sfda.gov.sa/en/drugs-list",
    ),
    "FDA Ghana": RegionalSourceConfig(
        source="FDA Ghana",
        country="Ghana",
        region="AF",
        search_url="https://fdaghana.gov.gh/product-register/",
    ),
    "CDSCO India": RegionalSourceConfig(
        source="CDSCO India",
        country="India",
        region="AS",
        search_url="https://cdsco.gov.in/opencms/opencms/en/Approval_new/FDC-New-Drugs-Marketing/",
    ),
    "NMPA China": RegionalSourceConfig(
        source="NMPA China",
        country="China",
        region="AS",
        search_url="https://english.nmpa.gov.cn/database.html",
    ),
    "BPOM Indonesia": RegionalSourceConfig(
        source="BPOM Indonesia",
        country="Indonesia",
        region="AS",
        search_url="https://cekbpom.pom.go.id/",
    ),
    "NPRA Malaysia": RegionalSourceConfig(
        source="NPRA Malaysia",
        country="Malaysia",
        region="AS",
        search_url="https://www.npra.gov.my/index.php/my/consumers-2/maklumat/carian-produk-berdaftar-bernotifikasi.html",
    ),
    "FDA Philippines": RegionalSourceConfig(
        source="FDA Philippines",
        country="Philippines",
        region="AS",
        search_url="https://verification.fda.gov.ph/drug_productslist.php",
    ),
    "HSA Singapore": RegionalSourceConfig(
        source="HSA Singapore",
        country="Singapore",
        region="AS",
        search_url="https://eservice.hsa.gov.sg/prism/common/enquirepublic/SearchDRBProduct.do?action=load",
    ),
    "MFDS South Korea": RegionalSourceConfig(
        source="MFDS South Korea",
        country="South Korea",
        region="AS",
        search_url="https://nedrug.mfds.go.kr/",
    ),
    "Thai FDA": RegionalSourceConfig(
        source="Thai FDA",
        country="Thailand",
        region="AS",
        search_url="https://pertento.fda.moph.go.th/FDA_SEARCH_DRUG/SEARCH_DRUG/FRM_SEARCH_DRUG.aspx",
    ),
    "DAV Vietnam": RegionalSourceConfig(
        source="DAV Vietnam",
        country="Vietnam",
        region="AS",
        search_url="https://dichvucong.dav.gov.vn/congbothuoc/index",
    ),
    "Israel Drug Registry": RegionalSourceConfig(
        source="Israel Drug Registry",
        country="Israel",
        region="ME",
        search_url="https://israeldrugs.health.gov.il/",
    ),
    "PMDA Japan": RegionalSourceConfig(
        source="PMDA Japan",
        country="Japan",
        region="JP",
        search_url="https://www.pmda.go.jp/files/000278243.pdf",
    ),
    "Ukraine DRLZ": RegionalSourceConfig(
        source="Ukraine DRLZ",
        country="Ukraine",
        region="EU",
        search_url="http://www.drlz.com.ua/ibp/ddsite.nsf/all/shlist?opendocument",
    ),
    "GRLS Russia": RegionalSourceConfig(
        source="GRLS Russia",
        country="Russia",
        region="RU",
        search_url="https://grls.rosminzdrav.ru/grls.aspx",
    ),
    "Cyprus Pharmaceutical Services": RegionalSourceConfig(
        source="Cyprus Pharmaceutical Services",
        country="Cyprus",
        region="EU",
        search_url="https://www.phs.moh.gov.cy/human-search/home.xhtml?lang=en",
    ),
    "Hong Kong Drug Office": RegionalSourceConfig(
        source="Hong Kong Drug Office",
        country="Hong Kong",
        region="AS",
        search_url="https://www.drugoffice.gov.hk/eps/do/en/consumer/search_drug_database2.html",
    ),
    "FDA Orange Book": RegionalSourceConfig(
        source="FDA Orange Book",
        country="United States",
        region="US",
        search_url="https://www.accessdata.fda.gov/scripts/cder/ob/index.cfm",
    ),
    "FDA Purple Book": RegionalSourceConfig(
        source="FDA Purple Book",
        country="United States",
        region="US",
        search_url="https://purplebooksearch.fda.gov/",
    ),
}


def _clean_text(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def _source_url(config: RegionalSourceConfig, substance: str) -> str:
    if not config.query_params:
        return config.search_url
    query = "&".join(
        f"{quote(key)}={quote(value.format(substance=substance))}"
        for key, value in config.query_params
    )
    separator = "&" if "?" in config.search_url else "?"
    return f"{config.search_url}{separator}{query}"


def _fallback_rows(config: RegionalSourceConfig, substance: str, limit: int = MAX_RESULTS) -> list[dict[str, Any]]:
    display_substance = substance.strip()
    if not display_substance:
        return []
    source_url = _source_url(config, display_substance)
    return [
        {
            "substance": display_substance,
            "product": f"{display_substance} official {config.country} registry search",
            "company": "",
            "country": config.country,
            "region": config.region,
            "status": f"Open official {config.source} registry - direct live parser unavailable or no exact match",
            "strength": extract_strength(display_substance),
            "dosage_form": extract_dosage_form(display_substance),
            "pack_size": extract_pack_size(display_substance),
            "registration_number": "",
            "document_type": "Official registry search handoff",
            "source": config.source,
            "source_url": source_url,
            "product_url": source_url,
            "url": source_url,
            "connector_mode": "manual_registry",
        }
    ][:limit]


def _clean_company_country(value: object) -> tuple[str, str]:
    text = _clean_text(value)
    if " - " not in text:
        return text, ""
    name, country = text.rsplit(" - ", 1)
    return _clean_text(name), _clean_text(country)


def _cdsco_pdf_url(wrapper_html: str, wrapper_url: str) -> str:
    match = re.search(r"iframe\s+src=['\"]([^'\"]+)", wrapper_html, flags=re.IGNORECASE)
    if match:
        return urljoin("https://cdsco.gov.in", match.group(1))
    comment_match = re.search(r"<!--\s*([^>]+?\.pdf)\s*-->", wrapper_html, flags=re.IGNORECASE)
    if comment_match:
        return urljoin("https://cdsco.gov.in", comment_match.group(1).strip())
    return wrapper_url


def _text_windows_for_query(text: str, query: str, radius: int = 420) -> list[str]:
    windows = []
    seen = set()
    for match in re.finditer(re.escape(query), text or "", flags=re.IGNORECASE):
        start = max(0, match.start() - radius)
        end = min(len(text), match.end() + radius)
        snippet = _clean_text(text[start:end])
        key = snippet.lower()
        if snippet and key not in seen:
            seen.add(key)
            windows.append(snippet)
    return windows


def _size_kb(value: str) -> int:
    match = re.search(r"(\d+)\s*KB", value or "", flags=re.IGNORECASE)
    return int(match.group(1)) if match else 0


@lru_cache(maxsize=64)
def _cdsco_pdf_text(pdf_url: str) -> str:
    response = requests.get(pdf_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
    response.raise_for_status()
    reader = PdfReader(BytesIO(response.content), strict=False)
    return "\n".join((page.extract_text() or "") for page in reader.pages[:MAX_CDSCO_PAGES_PER_PDF])


def _run_cdsco_india_pdf_search(substance: str, limit: int = MAX_RESULTS) -> list[dict[str, Any]]:
    config = REGIONAL_SOURCES["CDSCO India"]
    clean_substance = substance.strip()
    if not clean_substance:
        return []

    try:
        response = requests.get(config.search_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("%s PDF list unavailable: %s", config.source, exc)
        return _fallback_rows(config, clean_substance, limit)

    soup = BeautifulSoup(response.text, "html.parser")
    pdf_entries = []
    for table_row in soup.select("table tr")[1:]:
        cells = [_clean_text(cell.get_text(" ", strip=True)) for cell in table_row.select("td")]
        link = table_row.select_one("a[href]")
        if len(cells) < 5 or not link:
            continue
        title = cells[1]
        release_date = cells[2]
        pdf_size_kb = _size_kb(cells[4])
        if pdf_size_kb and pdf_size_kb > MAX_CDSCO_LIVE_PDF_KB:
            continue
        wrapper_url = urljoin(config.search_url, link.get("href", ""))
        pdf_entries.append((title, release_date, wrapper_url))
    pdf_entries.sort(key=lambda item: "since 1961" in item[0].lower())

    rows = []
    seen = set()
    for title, release_date, wrapper_url in pdf_entries[:MAX_CDSCO_PDFS]:
        if len(rows) >= limit:
            break
        try:
            wrapper = requests.get(wrapper_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=REQUEST_TIMEOUT)
            wrapper.raise_for_status()
            pdf_url = _cdsco_pdf_url(wrapper.text, wrapper_url)
            text = _cdsco_pdf_text(pdf_url)
        except Exception as exc:
            logger.warning("%s PDF scan skipped %s: %s", config.source, title, exc)
            continue
        for snippet in _text_windows_for_query(text, clean_substance):
            key = (title.lower(), snippet.lower()[:160])
            if key in seen:
                continue
            seen.add(key)
            product = snippet[:240]
            rows.append(
                {
                    "substance": clean_substance,
                    "product": product,
                    "company": "",
                    "country": config.country,
                    "region": config.region,
                    "status": "Listed in CDSCO approval PDF",
                    "strength": extract_strength(snippet),
                    "dosage_form": extract_dosage_form(snippet),
                    "pack_size": extract_pack_size(snippet),
                    "registration_number": "",
                    "registration_date": release_date,
                    "document_type": "CDSCO approval PDF",
                    "source": config.source,
                    "source_url": config.search_url,
                    "product_url": pdf_url,
                    "url": pdf_url,
                }
            )
            if len(rows) >= limit:
                break
    return rows or _fallback_rows(config, clean_substance, limit)


def _bpom_detail_url(row: dict[str, Any]) -> str:
    product_id = row.get("PRODUCT_ID") or ""
    application_id = row.get("APPLICATION_ID") or ""
    if product_id and application_id:
        return f"https://cekbpom.pom.go.id/produk/{product_id}/{application_id}/detail"
    return "https://cekbpom.pom.go.id/all-produk"


def _run_bpom_indonesia_json_search(substance: str, limit: int = MAX_RESULTS) -> list[dict[str, Any]]:
    config = REGIONAL_SOURCES["BPOM Indonesia"]
    clean_substance = substance.strip()
    if not clean_substance:
        return []

    session = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        page = session.get(
            f"https://cekbpom.pom.go.id/all-produk?query={quote(clean_substance)}",
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        page.raise_for_status()
        token_match = re.search(r'csrf-token"\s+content="([^"]+)', page.text)
        token = token_match.group(1) if token_match else ""
        data = {
            "draw": "1",
            "start": "0",
            "length": str(min(max(limit, 10), 100)),
            "search[value]": clean_substance,
            "search[regex]": "false",
            "product_register": "",
            "product_name": clean_substance,
            "registrar": "",
            "manufacturer": "",
            "product_status": "",
            "published_date": "",
            "expired_date": "",
            "sort": "",
        }
        for index, name in enumerate(
            ["PRODUCT_REGISTER", "PRODUCT_NAME", "REGISTRAR", "MANUFACTURER", "STATUS"]
        ):
            data[f"columns[{index}][data]"] = name
            data[f"columns[{index}][name]"] = ""
            data[f"columns[{index}][searchable]"] = "true"
            data[f"columns[{index}][orderable]"] = "true"
            data[f"columns[{index}][search][value]"] = ""
            data[f"columns[{index}][search][regex]"] = "false"
        response = session.post(
            "https://cekbpom.pom.go.id/produk-dt/all",
            data=data,
            headers={
                **headers,
                "X-CSRF-TOKEN": token,
                "Referer": page.url,
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("%s live JSON search unavailable: %s", config.source, exc)
        return _fallback_rows(config, clean_substance, limit)

    rows = []
    for item in payload.get("data", [])[:limit]:
        product = _clean_text(item.get("PRODUCT_NAME"))
        ingredients = _clean_text(item.get("INGREDIENTS"))
        if clean_substance.lower() not in f"{product} {ingredients}".lower():
            continue
        company, _ = _clean_company_country(item.get("REGISTRAR"))
        manufacturer, manufacturer_country = _clean_company_country(item.get("MANUFACTURER_NAME"))
        product_url = _bpom_detail_url(item)
        rows.append(
            {
                "substance": ingredients or clean_substance,
                "product": product,
                "company": company,
                "country": config.country,
                "region": config.region,
                "status": _clean_text(item.get("STATUS")) or "Registered",
                "strength": extract_strength(product),
                "dosage_form": _clean_text(item.get("PRODUCT_FORM")) or extract_dosage_form(product),
                "pack_size": _clean_text(item.get("PRODUCT_PACKAGE")) or extract_pack_size(product),
                "atc_code": _clean_text(item.get("PRODUCT_ATC")),
                "registration_number": _clean_text(item.get("PRODUCT_REGISTER")),
                "registration_date": _clean_text(item.get("PRODUCT_DATE")),
                "expiry_date": _clean_text(item.get("PRODUCT_EXPIRED")),
                "manufacturer_name": manufacturer,
                "manufacturer_country": manufacturer_country or "Indonesia",
                "manufacturer_source": "BPOM JSON",
                "source": config.source,
                "source_url": page.url,
                "product_url": product_url,
                "url": product_url,
            }
        )
    return rows or _fallback_rows(config, clean_substance, limit)


def _run_npra_malaysia_search(substance: str, limit: int = MAX_RESULTS) -> list[dict[str, Any]]:
    config = REGIONAL_SOURCES["NPRA Malaysia"]
    clean_substance = substance.strip()
    if not clean_substance:
        return []

    session = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        session.get("https://quest3plus.bpfk.gov.my/pmo2/index.php", headers=headers, timeout=REQUEST_TIMEOUT)
        response = session.post(
            "https://quest3plus.bpfk.gov.my/pmo2/content.php",
            data={
                "func": "search",
                "searchBy": "6",
                "searchTxt": clean_substance,
                "cat": "1",
            },
            headers={**headers, "Referer": "https://quest3plus.bpfk.gov.my/pmo2/index.php"},
            timeout=20,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("%s live QUEST3+ search unavailable: %s", config.source, exc)
        return _fallback_rows(config, clean_substance, limit)

    soup = BeautifulSoup(response.text, "html.parser")
    table = soup.select_one("table#searchTable") or soup.select_one("table")
    if not table:
        return _fallback_rows(config, clean_substance, limit)

    rows = []
    seen = set()
    for table_row in table.select("tbody tr, tr")[1:]:
        cells = [_clean_text(cell.get_text(" ", strip=True)) for cell in table_row.select("td")]
        if len(cells) < 4 or not cells[0].isdigit():
            continue
        registration_number = cells[1]
        product = cells[2]
        holder = cells[3]
        if not product or not registration_number:
            continue
        key = (registration_number.lower(), product.lower())
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "substance": clean_substance,
                "product": product,
                "company": holder,
                "country": config.country,
                "region": config.region,
                "status": "Registered",
                "strength": extract_strength(product),
                "dosage_form": extract_dosage_form(product),
                "pack_size": extract_pack_size(product),
                "registration_number": registration_number,
                "source": config.source,
                "source_url": "https://quest3plus.bpfk.gov.my/pmo2/index.php",
                "product_url": "https://quest3plus.bpfk.gov.my/pmo2/index.php",
                "url": "https://quest3plus.bpfk.gov.my/pmo2/index.php",
            }
        )
        if len(rows) >= limit:
            break
    return rows or _fallback_rows(config, clean_substance, limit)


def _iso_date(value: object) -> str:
    text = _clean_text(value)
    if "T" in text:
        return text.split("T", 1)[0]
    return text


def _first_attachment_url(value: object) -> str:
    text = _clean_text(value)
    if not text:
        return ""
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return ""
    if not isinstance(payload, list):
        return ""
    for item in payload:
        if not isinstance(item, dict):
            continue
        path = _clean_text(item.get("duongDanTep"))
        if path:
            return "https://dichvucong.dav.gov.vn/File/GoToViewTaiLieu?url=" + quote(path)
    return ""


def _run_dav_vietnam_api_search(substance: str, limit: int = MAX_RESULTS) -> list[dict[str, Any]]:
    config = REGIONAL_SOURCES["DAV Vietnam"]
    clean_substance = substance.strip()
    if not clean_substance:
        return []

    session = requests.Session()
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        session.get(config.search_url, headers=headers, timeout=REQUEST_TIMEOUT)
        payload = {
            "filterText": "",
            "SoDangKyThuoc": {"HoatChatChinh": clean_substance},
            "KichHoat": True,
            "skipCount": 0,
            "maxResultCount": min(max(limit, 10), 100),
            "sorting": None,
        }
        response = session.post(
            "https://dichvucong.dav.gov.vn/api/services/app/soDangKy/GetAllPublicServerPaging",
            data=json.dumps(payload),
            headers={
                **headers,
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json; charset=UTF-8",
                "Referer": config.search_url,
                "X-Requested-With": "XMLHttpRequest",
                "X-XSRF-TOKEN": session.cookies.get("XSRF-TOKEN", ""),
            },
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("%s live API search unavailable: %s", config.source, exc)
        return _fallback_rows(config, clean_substance, limit)

    rows = []
    for item in payload.get("result", {}).get("items", [])[:limit]:
        basics = item.get("thongTinThuocCoBan") or {}
        registration = item.get("thongTinDangKyThuoc") or {}
        documents = item.get("thongTinTaiLieu") or {}
        manufacturer = item.get("congTySanXuat") or {}
        holder = item.get("congTyDangKy") or {}
        active = _clean_text(basics.get("hoatChatChinh"))
        product = _clean_text(item.get("tenThuoc"))
        if clean_substance.lower() not in f"{active} {product}".lower():
            continue
        pil_url = _first_attachment_url(documents.get("urlHuongDanSuDung"))
        label_url = _first_attachment_url(documents.get("urlNhan")) or _first_attachment_url(
            documents.get("urlNhanVaHDSD")
        )
        product_url = f"{config.search_url}#registration-{_clean_text(item.get('id'))}"
        rows.append(
            {
                "substance": active or clean_substance,
                "product": product,
                "company": _clean_text(holder.get("tenCongTyDangKy")),
                "country": config.country,
                "region": config.region,
                "status": "Withdrawn" if item.get("isDaRutSoDangKy") else "Registered",
                "strength": _clean_text(basics.get("hamLuong")) or extract_strength(product),
                "dosage_form": _clean_text(basics.get("dangBaoChe")) or extract_dosage_form(product),
                "pack_size": _clean_text(basics.get("dongGoi")) or extract_pack_size(product),
                "registration_number": _clean_text(item.get("soDangKy")),
                "registration_date": _iso_date(registration.get("ngayCapSoDangKy")),
                "expiry_date": _iso_date(registration.get("ngayHetHanSoDangKy")),
                "manufacturer_name": _clean_text(manufacturer.get("tenCongTySanXuat")),
                "manufacturer_country": _clean_text(manufacturer.get("nuocSanXuat")),
                "pil_url": pil_url,
                "label_url": label_url,
                "source": config.source,
                "source_url": config.search_url,
                "product_url": product_url,
                "url": product_url,
            }
        )
    return rows or _fallback_rows(config, clean_substance, limit)


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


def _cell_link(cells: list[Any], index: int | None, base_url: str) -> str:
    if index is None or index >= len(cells):
        return ""
    link = cells[index].select_one("a[href]")
    if not link:
        return ""
    return urljoin(base_url, link.get("href", ""))


def _parse_table_results(
    html: str,
    substance: str,
    config: RegionalSourceConfig,
    source_url: str,
) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    seen = set()
    query = substance.strip().lower()

    for table in soup.select("table"):
        if table.select("form, input, textarea"):
            continue
        header_cells = table.select("tr th")
        if not header_cells:
            first_row = table.select_one("tr")
            header_cells = first_row.select("td") if first_row else []
        headers = [_clean_text(cell.get_text(" ", strip=True)) for cell in header_cells]
        if not headers:
            continue

        product_index = _header_index(headers, "product", "trade", "brand", "medicine", "drug", "name")
        substance_index = _header_index(headers, "substance", "ingredient", "generic", "scientific")
        company_index = _header_index(headers, "company", "holder", "sponsor", "manufacturer", "applicant")
        status_index = _header_index(headers, "status", "legal", "registration")
        registration_index = _header_index(headers, "registration", "reg no", "certificate", "license")
        dosage_index = _header_index(headers, "dosage", "form")

        if product_index is None and substance_index is None:
            continue

        for row in table.select("tr")[1:]:
            cells = row.select("td")
            if not cells:
                continue
            product = _cell(cells, product_index) or _cell(cells, substance_index)
            active = _cell(cells, substance_index)
            if not product or len(product) > 240:
                continue
            haystack = f"{product} {active}".lower()
            if query not in haystack:
                continue
            product_url = _cell_link(cells, product_index, config.search_url) or source_url
            key = (product.lower(), _cell(cells, registration_index).lower(), product_url.lower())
            if key in seen:
                continue
            seen.add(key)
            dosage_form = _cell(cells, dosage_index) or extract_dosage_form(product)
            rows.append(
                {
                    "substance": active,
                    "product": product,
                    "company": _cell(cells, company_index),
                    "country": config.country,
                    "region": config.region,
                    "status": _cell(cells, status_index) or "Listed by regulator source",
                    "strength": extract_strength(product),
                    "dosage_form": dosage_form,
                    "pack_size": extract_pack_size(product),
                    "registration_number": _cell(cells, registration_index),
                    "source": config.source,
                    "source_url": source_url,
                    "product_url": product_url,
                    "url": product_url,
                }
            )
    return rows


def _run_hong_kong_search(substance: str, limit: int = MAX_RESULTS) -> list[dict[str, Any]]:
    config = REGIONAL_SOURCES["Hong Kong Drug Office"]
    clean_substance = substance.strip()
    if not clean_substance:
        return []

    headers = {"User-Agent": "Mozilla/5.0"}
    per_page = next((value for value in [20, 40, 60, 80, 100, 500, 1000] if value >= limit), 1000)
    response = None
    last_error: Exception | None = None
    for _ in range(2):
        session = requests.Session()
        try:
            session.get(config.search_url, headers=headers, timeout=REQUEST_TIMEOUT)
            search_url = "https://www.drugoffice.gov.hk/eps/drug/productSearchOneFieldAction2"
            response = session.post(
                search_url,
                data={
                    "hkNoFrom": "",
                    "hkNoTo": "",
                    "productName": "",
                    "activeIngTextSearchType": "A",
                    "activeIngTexts[0]": clean_substance,
                    "activeIngTexts[1]": "",
                    "activeIngTexts[2]": "",
                    "certHolder": "",
                    "perPage": str(per_page),
                    "searchType": "A",
                    "pageNoRequested": "1",
                    "userType": "E",
                    "fromLang": "en",
                    "fromSection": "consumer",
                    "btn_01": "Search",
                },
                headers={**headers, "Referer": config.search_url, "Origin": "https://www.drugoffice.gov.hk"},
                timeout=20,
            )
            response.raise_for_status()
            break
        except requests.RequestException as exc:
            last_error = exc
            response = None
    if response is None:
        logger.warning("%s live search unavailable: %s", config.source, last_error)
        return _fallback_rows(config, clean_substance, limit)

    soup = BeautifulSoup(response.text, "html.parser")
    rows = []
    seen = set()
    query = clean_substance.lower()
    for table_row in soup.select("tr.tablerow1, tr.tablerow2"):
        cells = [_clean_text(cell.get_text(" ", strip=True)) for cell in table_row.find_all("td", recursive=False)]
        if len(cells) < 5 or not cells[0].isdigit():
            continue
        product, company, registration_number, ingredients = cells[1], cells[2], cells[3], cells[4]
        if query not in f"{product} {ingredients}".lower():
            continue
        key = (product.lower(), registration_number.lower())
        if key in seen:
            continue
        seen.add(key)
        detail_link = table_row.select_one("a[href]")
        product_url = urljoin(response.url, detail_link.get("href", "")) if detail_link else response.url
        detail_metadata = _hong_kong_detail_metadata(session, product_url, config.search_url)
        rows.append(
            {
                "substance": ingredients or clean_substance,
                "product": product,
                "company": company,
                "country": config.country,
                "region": config.region,
                "status": detail_metadata.get("status") or "Registered",
                "strength": extract_strength(product),
                "dosage_form": extract_dosage_form(product),
                "pack_size": extract_pack_size(product),
                "registration_number": registration_number,
                "registration_date": detail_metadata.get("registration_date", ""),
                "source": config.source,
                "source_url": response.url,
                "product_url": product_url,
                "url": product_url,
            }
        )
        if len(rows) >= limit:
            break
    return rows or _fallback_rows(config, clean_substance, limit)


def _hong_kong_detail_metadata(session: requests.Session, product_url: str, referer: str) -> dict[str, str]:
    try:
        response = session.get(
            product_url,
            headers={"User-Agent": "Mozilla/5.0", "Referer": referer},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
    except requests.RequestException:
        return {}

    text = BeautifulSoup(response.text, "html.parser").get_text("\n", strip=True)
    metadata: dict[str, str] = {}
    date_match = re.search(r"Date of Registration[#\s:]*\n?\s*([^\n]+)", text, flags=re.IGNORECASE)
    if date_match:
        metadata["registration_date"] = _clean_text(date_match.group(1))
    classification_match = re.search(r"Legal Classification\s*:\s*\n?\s*([^\n]+)", text, flags=re.IGNORECASE)
    if classification_match:
        metadata["status"] = "Registered - " + _clean_text(classification_match.group(1))
    return metadata


def _grls_search_url(search_term: str, page_size: int) -> str:
    params = {
        "RegNumber": "",
        "MnnR": search_term,
        "lf": "",
        "TradeNmR": "",
        "OwnerName": "",
        "MnfOrg": "",
        "MnfOrgCountry": "",
        "isfs": "0",
        "regtype": "1,6",
        "pageSize": str(page_size),
        "order": "Registered",
        "orderType": "desc",
        "pageNum": "1",
    }
    return "https://grls.rosminzdrav.ru/GRLS.aspx?" + urlencode(params)


def _run_grls_russia_search(substance: str, limit: int = MAX_RESULTS) -> list[dict[str, Any]]:
    config = REGIONAL_SOURCES["GRLS Russia"]
    clean_substance = substance.strip()
    if not clean_substance:
        return []

    search_terms = [clean_substance]
    russian_term = RUSSIAN_INN_TERMS.get(clean_substance.lower())
    if russian_term and russian_term not in search_terms:
        search_terms.insert(0, russian_term)

    rows: list[dict[str, Any]] = []
    seen = set()
    for search_term in search_terms:
        rows.extend(_grls_rows_from_browser(clean_substance, search_term, limit - len(rows), seen))
        if len(rows) >= limit:
            return rows[:limit]
    return rows or _fallback_rows(config, clean_substance, limit)


def _grls_rows_from_browser(
    substance: str,
    search_term: str,
    limit: int,
    seen: set[tuple[str, str, str]],
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    config = REGIONAL_SOURCES["GRLS Russia"]
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        logger.warning("Playwright unavailable for %s: %s", config.source, exc)
        return []

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(config.search_url, wait_until="domcontentloaded", timeout=30_000)
            page.fill("#ctl00_plate_txtMNN", search_term)
            page.click("#ctl00_plate_bSeek")
            page.wait_for_load_state("domcontentloaded", timeout=30_000)
            page.wait_for_timeout(2_000)
            html = page.content()
            source_url = page.url
            browser.close()
    except Exception as exc:
        logger.warning("%s browser search failed for %s: %s", config.source, search_term, exc)
        return _grls_rows_from_browser_subprocess(substance, search_term, limit)

    soup = BeautifulSoup(html, "html.parser")
    rows: list[dict[str, Any]] = []
    for table_row in soup.select("tr"):
        cells = [_clean_text(cell.get_text(" ", strip=True)) for cell in table_row.find_all("td", recursive=False)]
        if len(cells) < 11 or not cells[0].isdigit():
            continue
        product = cells[1]
        active = cells[2]
        if search_term.lower() not in f"{product} {active}".lower():
            continue
        registration_number = cells[6]
        key = (product.lower(), active.lower(), registration_number.lower())
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "substance": substance,
                "active_substance": active,
                "product": product,
                "company": cells[4],
                "country": config.country,
                "region": config.region,
                "status": cells[10] or "Registered in GRLS",
                "strength": extract_strength(product),
                "dosage_form": cells[3] or extract_dosage_form(product),
                "pack_size": extract_pack_size(product),
                "registration_number": registration_number,
                "registration_date": cells[7],
                "expiry_date": cells[8],
                "manufacturer_name": cells[4],
                "manufacturer_country": cells[5],
                "source": config.source,
                "source_url": source_url,
                "product_url": source_url,
                "url": source_url,
            }
        )
        if len(rows) >= limit:
            break
    return rows


def _grls_rows_from_browser_subprocess(substance: str, search_term: str, limit: int) -> list[dict[str, Any]]:
    helper = Path(__file__).with_name("grls_browser_helper.py")
    try:
        completed = subprocess.run(
            [sys.executable, str(helper), substance, search_term, str(limit)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=45,
            check=True,
        )
        payload = json.loads(completed.stdout or "[]")
        return payload if isinstance(payload, list) else []
    except Exception as exc:
        logger.warning("GRLS Russia helper subprocess failed for %s: %s", search_term, exc)
        return []


def _parse_link_results(
    html: str,
    substance: str,
    config: RegionalSourceConfig,
    source_url: str,
) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    query = substance.strip().lower()
    rows = []
    seen = set()
    for anchor in soup.select("a[href]"):
        text = _clean_text(anchor.get_text(" ", strip=True))
        if not text or query not in text.lower():
            continue
        href = urljoin(config.search_url, anchor.get("href", ""))
        key = (text.lower(), href.lower())
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "substance": substance,
                "product": text,
                "company": "",
                "country": config.country,
                "region": config.region,
                "status": "Listed by regulator source",
                "strength": extract_strength(text),
                "dosage_form": extract_dosage_form(text),
                "pack_size": extract_pack_size(text),
                "registration_number": "",
                "source": config.source,
                "source_url": source_url,
                "product_url": href,
                "url": href,
            }
        )
    return rows


def parse_regional_source_results(
    html: str,
    substance: str,
    config: RegionalSourceConfig,
    source_url: str,
) -> list[dict[str, Any]]:
    rows = _parse_table_results(html, substance, config, source_url)
    if rows:
        return rows
    return _parse_link_results(html, substance, config, source_url)


def run_regional_source_search(
    source_name: str,
    substance: str,
    limit: int = MAX_RESULTS,
) -> list[dict[str, Any]]:
    config = REGIONAL_SOURCES[source_name]
    clean_substance = substance.strip()
    if not clean_substance:
        return []
    if source_name == "Hong Kong Drug Office":
        return _run_hong_kong_search(clean_substance, limit)
    if source_name == "BPOM Indonesia":
        return _run_bpom_indonesia_json_search(clean_substance, limit)
    if source_name == "NPRA Malaysia":
        return _run_npra_malaysia_search(clean_substance, limit)
    if source_name == "DAV Vietnam":
        return _run_dav_vietnam_api_search(clean_substance, limit)

    source_url = _source_url(config, clean_substance)
    try:
        response = requests.get(
            source_url,
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("%s live search unavailable: %s", config.source, exc)
        return _fallback_rows(config, clean_substance, limit)

    rows = parse_regional_source_results(response.text, clean_substance, config, response.url)[:limit]
    if not rows:
        return _fallback_rows(config, clean_substance, limit)
    return rows


def run_sahpra_search(substance: str) -> list[dict[str, Any]]:
    return run_regional_source_search("SAHPRA South Africa", substance)


def run_sfda_saudi_search(substance: str) -> list[dict[str, Any]]:
    return run_regional_source_search("SFDA Saudi Arabia", substance)


def run_fda_ghana_search(substance: str) -> list[dict[str, Any]]:
    return run_regional_source_search("FDA Ghana", substance)


def run_cdsco_india_search(substance: str) -> list[dict[str, Any]]:
    return _run_cdsco_india_pdf_search(substance)


def run_nmpa_china_search(substance: str) -> list[dict[str, Any]]:
    rows = _fallback_rows(REGIONAL_SOURCES["NMPA China"], substance)
    for row in rows:
        row["status"] = "Official NMPA China registry handoff - live product parser not implemented yet"
        row["document_type"] = "Official registry search handoff"
        row["connector_mode"] = "manual_registry"
    return rows


def run_bpom_indonesia_search(substance: str) -> list[dict[str, Any]]:
    return run_regional_source_search("BPOM Indonesia", substance)


def run_npra_malaysia_search(substance: str) -> list[dict[str, Any]]:
    return run_regional_source_search("NPRA Malaysia", substance)


def run_fda_philippines_search(substance: str) -> list[dict[str, Any]]:
    return run_regional_source_search("FDA Philippines", substance)


def run_hsa_singapore_search(substance: str) -> list[dict[str, Any]]:
    return run_regional_source_search("HSA Singapore", substance)


def run_mfds_south_korea_search(substance: str) -> list[dict[str, Any]]:
    return run_regional_source_search("MFDS South Korea", substance)


def run_thai_fda_search(substance: str) -> list[dict[str, Any]]:
    return run_regional_source_search("Thai FDA", substance)


def run_dav_vietnam_search(substance: str) -> list[dict[str, Any]]:
    return run_regional_source_search("DAV Vietnam", substance)


def run_israel_drug_registry_search(substance: str) -> list[dict[str, Any]]:
    return run_regional_source_search("Israel Drug Registry", substance)


def run_pmda_japan_search(substance: str) -> list[dict[str, Any]]:
    return run_regional_source_search("PMDA Japan", substance)


def run_ukraine_drlz_search(substance: str) -> list[dict[str, Any]]:
    return run_regional_source_search("Ukraine DRLZ", substance)


def run_grls_russia_search(substance: str) -> list[dict[str, Any]]:
    return _run_grls_russia_search(substance)


def run_cyprus_pharmaceutical_services_search(substance: str) -> list[dict[str, Any]]:
    return run_regional_source_search("Cyprus Pharmaceutical Services", substance)


def run_hong_kong_drug_office_search(substance: str) -> list[dict[str, Any]]:
    return _run_hong_kong_search(substance, MAX_RESULTS)


def run_fda_orange_book_search(substance: str) -> list[dict[str, Any]]:
    return run_regional_source_search("FDA Orange Book", substance)


def run_fda_purple_book_search(substance: str) -> list[dict[str, Any]]:
    return run_regional_source_search("FDA Purple Book", substance)
