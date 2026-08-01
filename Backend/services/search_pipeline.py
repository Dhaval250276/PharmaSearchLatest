from typing import Any
from urllib.parse import quote

from repository import (
    save_product_detail,
    search_medicines,
    search_product_details,
)
from sources.deep_enrichment import enrich_deep_results
from sources.ema import EU_COUNTRIES
from sources.mhra_document_parser import enrich_mhra_document_metadata
from sources.search_engine import LIVE_SEARCH_TIMEOUT_SECONDS, search_substance
from services.ai_enrichment import attach_ai_enrichment_metadata
from services.result_formatter import formatted_result_row


DEFAULT_SOURCES = [
    "EMA",
    "EU MRI Product Index",
    "Belgium FAMHP",
    "France BDPM",
    "Ireland medicines.ie",
    "Spain CIMA",
    "MHRA",
    "FDA",
    "FDA Orange Book",
    "FDA Purple Book",
    "Health Canada",
    "TGA Australia",
    "Medsafe New Zealand",
    "SAHPRA South Africa",
    "FDA Ghana",
    "SFDA Saudi Arabia",
    "Israel Drug Registry",
    "CDSCO India",
    "NMPA China",
    "BPOM Indonesia",
    "NPRA Malaysia",
    "FDA Philippines",
    "HSA Singapore",
    "MFDS South Korea",
    "Thai FDA",
    "DAV Vietnam",
    "PMDA Japan",
    "Hong Kong Drug Office",
    "Cyprus Pharmaceutical Services",
    "Ukraine DRLZ",
    "GRLS Russia",
]
EU_NATIONAL_SOURCES = [
    "Belgium FAMHP",
    "EU MRI Product Index",
    "France BDPM",
    "Ireland medicines.ie",
    "Spain CIMA",
    "Cyprus Pharmaceutical Services",
    "Ukraine DRLZ",
]
AFRICA_COUNTRIES = [
    "Algeria",
    "Angola",
    "Benin",
    "Botswana",
    "Burkina Faso",
    "Burundi",
    "Cabo Verde",
    "Cameroon",
    "Central African Republic",
    "Chad",
    "Comoros",
    "Democratic Republic of the Congo",
    "Djibouti",
    "Egypt",
    "Equatorial Guinea",
    "Eritrea",
    "Eswatini",
    "Ethiopia",
    "Gabon",
    "Gambia",
    "Ghana",
    "Guinea",
    "Guinea-Bissau",
    "Ivory Coast",
    "Kenya",
    "Lesotho",
    "Liberia",
    "Libya",
    "Madagascar",
    "Malawi",
    "Mali",
    "Mauritania",
    "Mauritius",
    "Morocco",
    "Mozambique",
    "Namibia",
    "Niger",
    "Nigeria",
    "Republic of the Congo",
    "Rwanda",
    "Sao Tome and Principe",
    "Senegal",
    "Seychelles",
    "Sierra Leone",
    "Somalia",
    "South Africa",
    "South Sudan",
    "Sudan",
    "Tanzania",
    "Togo",
    "Tunisia",
    "Uganda",
    "Zambia",
    "Zimbabwe",
]
MIDDLE_EAST_COUNTRIES = [
    "Bahrain",
    "Cyprus",
    "Egypt",
    "Iran",
    "Iraq",
    "Israel",
    "Jordan",
    "Kuwait",
    "Lebanon",
    "Oman",
    "Palestine",
    "Qatar",
    "Saudi Arabia",
    "Syria",
    "Turkey",
    "United Arab Emirates",
    "Yemen",
]
ASIA_COUNTRIES = [
    "Afghanistan",
    "Armenia",
    "Azerbaijan",
    "Bahrain",
    "Bangladesh",
    "Bhutan",
    "Brunei",
    "Cambodia",
    "China",
    "Cyprus",
    "Georgia",
    "Hong Kong",
    "India",
    "Indonesia",
    "Iran",
    "Iraq",
    "Israel",
    "Japan",
    "Jordan",
    "Kazakhstan",
    "Kuwait",
    "Kyrgyzstan",
    "Laos",
    "Lebanon",
    "Malaysia",
    "Maldives",
    "Mongolia",
    "Myanmar",
    "Nepal",
    "North Korea",
    "Oman",
    "Pakistan",
    "Palestine",
    "Philippines",
    "Qatar",
    "Saudi Arabia",
    "Singapore",
    "South Korea",
    "Sri Lanka",
    "Syria",
    "Taiwan",
    "Tajikistan",
    "Thailand",
    "Timor-Leste",
    "Turkmenistan",
    "Ukraine",
    "United Arab Emirates",
    "Uzbekistan",
    "Vietnam",
    "Yemen",
]
REGIONAL_COUNTRIES = {
    "AF": AFRICA_COUNTRIES,
    "ME": MIDDLE_EAST_COUNTRIES,
    "AS": ASIA_COUNTRIES,
}
REGION_OPTIONS = ["ALL", "EU", "UK", "US", "CA", "AU", "NZ", "AF", "ME", "AS", "CH", "JP", "RU"]
GLOBAL_COUNTRIES = [
    "Afghanistan",
    "Albania",
    "Algeria",
    "Andorra",
    "Angola",
    "Argentina",
    "Armenia",
    "Australia",
    "Austria",
    "Azerbaijan",
    "Bahamas",
    "Bahrain",
    "Bangladesh",
    "Barbados",
    "Belarus",
    "Belgium",
    "Belize",
    "Benin",
    "Bhutan",
    "Bolivia",
    "Bosnia and Herzegovina",
    "Botswana",
    "Brazil",
    "Brunei",
    "Bulgaria",
    "Burkina Faso",
    "Burundi",
    "Cambodia",
    "Cameroon",
    "Canada",
    "Chile",
    "China",
    "Colombia",
    "Costa Rica",
    "Croatia",
    "Cyprus",
    "Czech Republic",
    "Denmark",
    "Dominican Republic",
    "Ecuador",
    "Egypt",
    "El Salvador",
    "Estonia",
    "Ethiopia",
    "Finland",
    "France",
    "Georgia",
    "Germany",
    "Ghana",
    "Greece",
    "Guatemala",
    "Honduras",
    "Hong Kong",
    "Hungary",
    "Iceland",
    "India",
    "Indonesia",
    "Ireland",
    "Israel",
    "Italy",
    "Japan",
    "Jordan",
    "Kazakhstan",
    "Kenya",
    "Kuwait",
    "Latvia",
    "Lebanon",
    "Lithuania",
    "Luxembourg",
    "Malaysia",
    "Malta",
    "Mexico",
    "Morocco",
    "Netherlands",
    "New Zealand",
    "Nigeria",
    "Norway",
    "Pakistan",
    "Panama",
    "Paraguay",
    "Peru",
    "Philippines",
    "Poland",
    "Portugal",
    "Qatar",
    "Romania",
    "Saudi Arabia",
    "Singapore",
    "Slovakia",
    "Slovenia",
    "South Africa",
    "South Korea",
    "Spain",
    "Sri Lanka",
    "Sweden",
    "Switzerland",
    "Taiwan",
    "Thailand",
    "Tunisia",
    "Turkey",
    "Ukraine",
    "United Arab Emirates",
    "United Kingdom",
    "United States",
    "Uruguay",
    "Vietnam",
    "Russia",
]
LEGACY_GLOBAL_COUNTRIES = [
    "Argentina",
    "Australia",
    "Brazil",
    "Canada",
    "China",
    "India",
    "Japan",
    "Mexico",
    "New Zealand",
    "South Korea",
    "Switzerland",
    "United Kingdom",
    "United States",
]
COUNTRY_OPTIONS = sorted(
    set(
        EU_COUNTRIES
        + GLOBAL_COUNTRIES
        + LEGACY_GLOBAL_COUNTRIES
        + AFRICA_COUNTRIES
        + MIDDLE_EAST_COUNTRIES
        + ASIA_COUNTRIES
    ),
    key=str.lower,
)
SORT_COLUMNS = {
    "substance": "substance",
    "product": "product",
    "company": "company",
    "country": "country",
    "region": "region",
    "status": "status",
    "source": "source",
    "strength": "strength",
    "dosage_form": "dosage_form",
    "pack_size": "pack_size",
    "atc_code": "atc_code",
    "therapeutic_category": "therapeutic_category",
    "ma_holder": "company",
    "manufacturer_name": "manufacturer_name",
    "manufacturer_country": "manufacturer_country",
    "registration_number": "registration_number",
    "registration_date": "registration_date",
}
SOURCE_COUNTRIES = {
    "fda": {"United States"},
    "health canada": {"Canada"},
    "tga australia": {"Australia"},
    "medsafe new zealand": {"New Zealand"},
    "sahpra south africa": {"South Africa"},
    "fda ghana": {"Ghana"},
    "sfda saudi arabia": {"Saudi Arabia"},
    "israel drug registry": {"Israel"},
    "cdsco india": {"India"},
    "nmpa china": {"China"},
    "bpom indonesia": {"Indonesia"},
    "npra malaysia": {"Malaysia"},
    "fda philippines": {"Philippines"},
    "hsa singapore": {"Singapore"},
    "hong kong drug office": {"Hong Kong"},
    "mfds south korea": {"South Korea"},
    "thai fda": {"Thailand"},
    "dav vietnam": {"Vietnam"},
    "pmda japan": {"Japan"},
    "fda orange book": {"United States"},
    "fda purple book": {"United States"},
    "mhra": {"United Kingdom"},
    "ema": {
        "Austria",
        "Belgium",
        "Bulgaria",
        "Croatia",
        "Cyprus",
        "Czech Republic",
        "Denmark",
        "Estonia",
        "Finland",
        "France",
        "Germany",
        "Greece",
        "Hungary",
        "Ireland",
        "Italy",
        "Latvia",
        "Lithuania",
        "Luxembourg",
        "Malta",
        "Netherlands",
        "Poland",
        "Portugal",
        "Romania",
        "Slovakia",
        "Slovenia",
        "Spain",
        "Sweden",
        "Norway",
        "Iceland",
        "Liechtenstein",
        "European Union",
    },
    "eu mri product index": {
        "Austria",
        "Belgium",
        "Bulgaria",
        "Croatia",
        "Cyprus",
        "Czech Republic",
        "Denmark",
        "Estonia",
        "Finland",
        "France",
        "Germany",
        "Greece",
        "Hungary",
        "Ireland",
        "Italy",
        "Latvia",
        "Lithuania",
        "Luxembourg",
        "Malta",
        "Netherlands",
        "Poland",
        "Portugal",
        "Romania",
        "Slovakia",
        "Slovenia",
        "Spain",
        "Sweden",
        "European Union",
    },
    "belgium famhp": {"Belgium"},
    "france bdpm": {"France"},
    "ireland medicines.ie": {"Ireland"},
    "spain cima": {"Spain"},
    "cyprus pharmaceutical services": {"Cyprus"},
    "ukraine drlz": {"Ukraine"},
    "grls russia": {"Russia"},
}
COUNTRY_SOURCE_DEFAULTS = {
    "Australia": "TGA Australia",
    "Canada": "Health Canada",
    "Japan": "PMDA Japan",
    "Hong Kong": "Hong Kong Drug Office",
    "New Zealand": "Medsafe New Zealand",
    "Ghana": "FDA Ghana",
    "China": "NMPA China",
    "India": "CDSCO India",
    "Indonesia": "BPOM Indonesia",
    "Israel": "Israel Drug Registry",
    "Malaysia": "NPRA Malaysia",
    "Philippines": "FDA Philippines",
    "Saudi Arabia": "SFDA Saudi Arabia",
    "Singapore": "HSA Singapore",
    "South Africa": "SAHPRA South Africa",
    "South Korea": "MFDS South Korea",
    "Thailand": "Thai FDA",
    "Vietnam": "DAV Vietnam",
    "Switzerland": "Swissmedic",
    "United Kingdom": "MHRA",
    "United States": "FDA",
    "Ukraine": "Ukraine DRLZ",
    "Russia": "GRLS Russia",
    "Cyprus": "Cyprus Pharmaceutical Services",
}
REGION_SOURCE_DEFAULTS = {
    "AU": "TGA Australia",
    "CA": "Health Canada",
    "CH": "Swissmedic",
    "JP": "PMDA Japan",
    "NZ": "Medsafe New Zealand",
    "UK": "MHRA",
    "US": "FDA",
    "RU": "GRLS Russia",
}
EU_LOOKUP_SOURCE = "EU National Registry"
REGISTRY_LOOKUP_SOURCE = "Regulatory Registry Lookup"
GLOBAL_LOOKUP_SOURCE = "Global Generic Registry Lookup"
AFRICA_LOOKUP_SOURCE = "Africa Generic Registry Lookup"
MIDDLE_EAST_LOOKUP_SOURCE = "Middle East Generic Registry Lookup"
ASIA_LOOKUP_SOURCE = "Asia Generic Registry Lookup"
LOOKUP_ONLY_SOURCE = "__regional_lookup_only__"
GENERIC_LOOKUP_SOURCES = {
    REGISTRY_LOOKUP_SOURCE,
    GLOBAL_LOOKUP_SOURCE,
    AFRICA_LOOKUP_SOURCE,
    MIDDLE_EAST_LOOKUP_SOURCE,
    ASIA_LOOKUP_SOURCE,
    EU_LOOKUP_SOURCE,
}
REGIONAL_LIVE_SOURCES = {
    "AF": ["SAHPRA South Africa", "FDA Ghana"],
    "ME": ["SFDA Saudi Arabia", "Israel Drug Registry"],
    "AS": [
        "CDSCO India",
        "NMPA China",
        "BPOM Indonesia",
        "NPRA Malaysia",
        "FDA Philippines",
        "HSA Singapore",
        "MFDS South Korea",
        "Thai FDA",
        "DAV Vietnam",
        "PMDA Japan",
        "Hong Kong Drug Office",
        "Israel Drug Registry",
        "SFDA Saudi Arabia",
    ],
    "EU": EU_NATIONAL_SOURCES,
    "US": ["FDA", "FDA Orange Book", "FDA Purple Book"],
    "RU": ["GRLS Russia"],
}
REGIONAL_REGISTRY_URLS = {
    "Egypt": "https://edaegypt.gov.eg/",
    "Ghana": "https://fdaghana.gov.gh/product-register/",
    "Kenya": "https://products.pharmacyboardkenya.org/",
    "Nigeria": "https://registration.nafdac.gov.ng/",
    "South Africa": "https://www.sahpra.org.za/registered-health-products/",
    "Saudi Arabia": "https://www.sfda.gov.sa/en/drugs-list",
    "United Arab Emirates": "https://mohap.gov.ae/en/services/registered-medical-product-directory",
    "Israel": "https://israeldrugs.health.gov.il/",
    "Jordan": "https://www.jfda.jo/",
    "Qatar": "https://www.moph.gov.qa/",
    "China": "https://english.nmpa.gov.cn/database.html",
    "India": "https://cdsco.gov.in/opencms/opencms/en/Approval_new/FDC-New-Drugs-Marketing/",
    "Indonesia": "https://cekbpom.pom.go.id/",
    "Malaysia": "https://www.npra.gov.my/index.php/my/consumers-2/maklumat/carian-produk-berdaftar-bernotifikasi.html",
    "Philippines": "https://verification.fda.gov.ph/drug_productslist.php",
    "Singapore": "https://eservice.hsa.gov.sg/prism/common/enquirepublic/SearchDRBProduct.do?action=load",
    "South Korea": "https://nedrug.mfds.go.kr/",
    "Thailand": "https://pertento.fda.moph.go.th/FDA_SEARCH_DRUG/SEARCH_DRUG/FRM_SEARCH_DRUG.aspx",
    "Vietnam": "https://dichvucong.dav.gov.vn/congbothuoc/index",
    "Japan": "https://www.pmda.go.jp/files/000278243.pdf",
    "Hong Kong": "https://www.drugoffice.gov.hk/eps/do/en/consumer/search_drug_database2.html",
    "Ukraine": "http://www.drlz.com.ua/ibp/ddsite.nsf/all/shlist?opendocument",
    "Russia": "https://grls.rosminzdrav.ru/grls.aspx",
    "Cyprus": "https://www.phs.moh.gov.cy/human-search/home.xhtml?lang=en",
}
SHARED_MOLECULE_FIELDS = [
    "atc_code",
    "therapeutic_category",
]


def _ensure_source(selected: list[str], source_name: str) -> list[str]:
    if any(item.lower() == source_name.lower() for item in selected):
        return selected
    return selected + [source_name]


def _lookup_url(substance: str, country: str) -> str:
    clean_substance = substance.strip()
    if country in EU_COUNTRIES:
        return "https://www.ema.europa.eu/en/search?" f"search_api_fulltext={quote(clean_substance)}"
    if country == "United Kingdom":
        return "https://products.mhra.gov.uk/search/?" f"search={quote(clean_substance)}&page=1"
    if country == "United States":
        return "https://api.fda.gov/drug/label.json?" f"search=openfda.substance_name:{quote(clean_substance)}"
    if country == "Canada":
        return "https://health-products.canada.ca/dpd-bdpp/index-eng.jsp"
    if country == "Australia":
        return "https://www.tga.gov.au/resources/artg?" f"keywords={quote(clean_substance)}"
    if country == "New Zealand":
        return "https://www.medsafe.govt.nz/DbSearch/"
    if country in REGIONAL_REGISTRY_URLS:
        return REGIONAL_REGISTRY_URLS[country]
    if country == "Switzerland":
        return "https://www.swissmedic.ch/swissmedic/en/home/services/listen_neu.html"
    if country == "Japan":
        return "https://www.pmda.go.jp/PmdaSearch/iyakuSearch/"
    return "https://www.google.com/search?" f"q={quote(country + ' medicine register ' + clean_substance)}"


def _country_region(country: str) -> str:
    if country in EU_COUNTRIES:
        return "EU"
    if country == "United Kingdom":
        return "UK"
    if country == "United States":
        return "US"
    if country == "Canada":
        return "CA"
    if country == "Australia":
        return "AU"
    if country == "New Zealand":
        return "NZ"
    if country in AFRICA_COUNTRIES:
        return "AF"
    if country in MIDDLE_EAST_COUNTRIES:
        return "ME"
    if country == "Switzerland":
        return "CH"
    if country == "Japan":
        return "JP"
    if country == "Russia":
        return "RU"
    if country in ASIA_COUNTRIES:
        return "AS"
    return "Global"


def _registry_lookup_row(substance: str, country: str, region_override: str = "") -> dict[str, Any]:
    clean_substance = substance.strip()
    search_url = _lookup_url(clean_substance, country)
    region = region_override or _country_region(country)
    if region_override == "ALL":
        source = GLOBAL_LOOKUP_SOURCE
        document_type = "Generic global registry lookup fallback"
    elif region_override == "AF":
        source = AFRICA_LOOKUP_SOURCE
        document_type = "Generic Africa registry lookup fallback"
    elif region_override == "ME":
        source = MIDDLE_EAST_LOOKUP_SOURCE
        document_type = "Generic Middle East registry lookup fallback"
    elif region_override == "AS":
        source = ASIA_LOOKUP_SOURCE
        document_type = "Generic Asia registry lookup fallback"
    elif country in EU_COUNTRIES:
        source = EU_LOOKUP_SOURCE
        document_type = "EU national lookup fallback"
    elif region == "AF":
        source = AFRICA_LOOKUP_SOURCE
        document_type = "Generic Africa registry lookup fallback"
    elif region == "ME":
        source = MIDDLE_EAST_LOOKUP_SOURCE
        document_type = "Generic Middle East registry lookup fallback"
    elif region == "AS":
        source = ASIA_LOOKUP_SOURCE
        document_type = "Generic Asia registry lookup fallback"
    else:
        source = REGISTRY_LOOKUP_SOURCE
        document_type = "Country registry lookup fallback"
    return {
        "substance": clean_substance,
        "product": f"{clean_substance} {country} generic regulatory registry lookup",
        "company": "",
        "country": country,
        "region": region,
        "status": "Country-specific live connector unavailable or no direct match - verify national register",
        "strength": "",
        "dosage_form": "",
        "pack_size": "",
        "registration_number": "",
        "document_type": document_type,
        "source": source,
        "source_url": search_url,
        "product_url": search_url,
        "url": search_url,
    }


def _eu_lookup_row(substance: str, country: str) -> dict[str, Any]:
    return _registry_lookup_row(substance, country)


def _eu_lookup_rows(
    substance: str,
    country: str = "",
    region: str = "",
    existing_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if country in EU_COUNTRIES:
        return [_eu_lookup_row(substance, country)]
    if region != "EU":
        return []

    existing_countries = {
        row.get("country")
        for row in existing_rows or []
        if row.get("region") == "EU" and row.get("country")
    }
    return [
        _registry_lookup_row(substance, eu_country)
        for eu_country in EU_COUNTRIES
        if eu_country not in existing_countries
    ]


def _country_lookup_rows(
    substance: str,
    country: str = "",
    region: str = "",
    existing_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if country:
        return [_registry_lookup_row(substance, country)]
    if region == "ALL":
        existing_countries = {
            row.get("country")
            for row in existing_rows or []
            if row.get("country")
        }
        return [
            _registry_lookup_row(substance, lookup_country, region_override=region)
            for lookup_country in COUNTRY_OPTIONS
            if lookup_country not in existing_countries
        ]
    if region == "EU":
        return _eu_lookup_rows(substance, region=region, existing_rows=existing_rows)
    if region in REGIONAL_COUNTRIES:
        existing_countries = {
            row.get("country")
            for row in existing_rows or []
            if row.get("region") == region and row.get("country")
        }
        return [
            _registry_lookup_row(substance, region_country, region_override=region)
            for region_country in REGIONAL_COUNTRIES[region]
            if region_country not in existing_countries
        ]
    if region in {"UK", "US", "CA", "AU", "NZ", "CH", "JP"}:
        region_country = {
            "UK": "United Kingdom",
            "US": "United States",
            "CA": "Canada",
            "AU": "Australia",
            "NZ": "New Zealand",
            "CH": "Switzerland",
            "JP": "Japan",
        }[region]
        return [_registry_lookup_row(substance, region_country)]
    return []


def result_key(item: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        (item.get("source") or "").strip().lower(),
        (item.get("product") or "").strip().lower(),
        (item.get("country") or "").strip().lower(),
        (item.get("registration_number") or "").strip().lower(),
        (item.get("product_url") or item.get("url") or "").strip().lower(),
    )


def molecule_key(item: dict[str, Any]) -> str:
    for field in ["searched_substance", "substance", "source_substance"]:
        value = str(item.get(field) or "").strip().lower()
        if value:
            return value
    return ""


def normalized_tokens(value: object) -> list[str]:
    import re

    return [
        token
        for token in re.split(r"[^a-z0-9]+", str(value or "").lower())
        if token
    ]


def contains_all_tokens(value: object, tokens: list[str]) -> bool:
    haystack = set(normalized_tokens(value))
    return bool(tokens) and all(token in haystack for token in tokens)


def row_relevant_to_substance(row: dict[str, Any], substance: str) -> bool:
    import re

    query_tokens = normalized_tokens(substance)
    if not query_tokens:
        return True

    product = str(row.get("product") or "")
    product_has_query = contains_all_tokens(product, query_tokens)
    if product_has_query:
        return True

    parenthetical_values = re.findall(r"\(([^)]{3,120})\)", product)
    if parenthetical_values:
        return any(contains_all_tokens(value, query_tokens) for value in parenthetical_values)

    active_substance = " ".join(
        str(row.get(field) or "")
        for field in ["active_substance", "active_substances"]
    )
    if contains_all_tokens(active_substance, query_tokens):
        return True

    source_substance = str(row.get("source_substance") or "")
    if contains_all_tokens(source_substance, query_tokens):
        return True

    if str(row.get("source") or "").strip().lower() == "mhra":
        return False

    return True


def is_connector_lookup_fallback(item: dict[str, Any]) -> bool:
    document_type = str(item.get("document_type") or "").strip().lower()
    connector_mode = str(item.get("connector_mode") or "").strip().lower()
    product = str(item.get("product") or "").strip().lower()
    source = str(item.get("source") or "").strip()
    return (
        bool(source)
        and (
            "lookup fallback" in document_type
            or "registry search handoff" in document_type
            or connector_mode == "manual_registry"
            or product.endswith("regulator lookup")
        )
        and source not in GENERIC_LOOKUP_SOURCES
    )


def propagate_molecule_fields(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    values_by_molecule: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = molecule_key(row)
        if not key:
            continue
        bucket = values_by_molecule.setdefault(key, {})
        for field in SHARED_MOLECULE_FIELDS:
            if row.get(field) and not bucket.get(field):
                bucket[field] = row[field]

    for row in rows:
        key = molecule_key(row)
        values = values_by_molecule.get(key, {})
        for field, value in values.items():
            if value and not row.get(field):
                row[field] = value
    return rows


def source_rank(source: str | None, selected_sources: list[str]) -> int:
    selected = [item.strip().lower() for item in selected_sources if item.strip()]
    source_name = (source or "").strip().lower()
    if source_name in selected:
        return selected.index(source_name)
    return len(selected)


def filter_rows(
    rows: list[dict[str, Any]],
    country: str = "",
    region: str = "",
    source: str = "",
    status: str = "",
    table_search: str = "",
    substance_filter: str = "",
    product_filter: str = "",
    company_filter: str = "",
    strength_filter: str = "",
    dosage_form_filter: str = "",
    pack_size_filter: str = "",
    atc_code_filter: str = "",
    therapeutic_category_filter: str = "",
    ma_holder_filter: str = "",
    manufacturer_name_filter: str = "",
    manufacturer_country_filter: str = "",
    registration_number_filter: str = "",
    registration_date_filter: str = "",
) -> list[dict[str, Any]]:
    query = table_search.strip().lower()
    text_filters = [
        ("substance", substance_filter),
        ("product", product_filter),
        ("company", company_filter),
        ("strength", strength_filter),
        ("dosage_form", dosage_form_filter),
        ("pack_size", pack_size_filter),
        ("atc_code", atc_code_filter),
        ("therapeutic_category", therapeutic_category_filter),
        ("ma_holder", ma_holder_filter),
        ("manufacturer_name", manufacturer_name_filter),
        ("manufacturer_country", manufacturer_country_filter),
        ("registration_number", registration_number_filter),
        ("registration_date", registration_date_filter),
    ]
    filtered = []
    for row in rows:
        display_row = formatted_result_row(row)
        searchable_values = {
            "substance": display_row.get("molecule", row.get("substance", "")),
            "product": display_row.get("product", row.get("product", "")),
            "company": display_row.get("company", row.get("company", "")),
            "ma_holder": display_row.get("ma_holder", row.get("company", "")),
            "strength": display_row.get("strength", row.get("strength", "")),
            "dosage_form": display_row.get("dosage_form", row.get("dosage_form", "")),
            "pack_size": display_row.get("pack_size", row.get("pack_size", "")),
            "atc_code": display_row.get("atc_code", row.get("atc_code", "")),
            "therapeutic_category": display_row.get(
                "therapeutic_category",
                row.get("therapeutic_category", ""),
            ),
            "manufacturer_name": display_row.get(
                "manufacturer_name",
                row.get("manufacturer_name", ""),
            ),
            "manufacturer_country": display_row.get(
                "manufacturer_country",
                row.get("manufacturer_country", ""),
            ),
            "registration_number": display_row.get(
                "registration_number",
                row.get("registration_number", ""),
            ),
            "registration_date": display_row.get(
                "registration_date",
                row.get("registration_date", ""),
            ),
        }
        if any(
            filter_value.strip().lower()
            and filter_value.strip().lower() not in str(searchable_values.get(field, "")).lower()
            for field, filter_value in text_filters
        ):
            continue
        if country and row.get("country") != country:
            continue
        if region and region != "ALL" and row.get("region") != region:
            continue
        if source and row.get("source") != source:
            continue
        if status and display_row.get("registration_status") != status:
            continue
        if query:
            haystack = " ".join(
                str(row.get(field) or "")
                for field in [
                    "substance",
                    "product",
                    "company",
                    "country",
                    "region",
                    "status",
                    "source",
                    "registration_number",
                    "registration_date",
                    "document_type",
                ]
            ).lower()
            haystack = f"{haystack} " + " ".join(str(value or "") for value in searchable_values.values()).lower()
            if query not in haystack:
                continue
        filtered.append(row)
    return filtered


def sort_rows(
    rows: list[dict[str, Any]],
    selected_sources: list[str],
    sort_by: str = "",
    sort_dir: str = "asc",
) -> list[dict[str, Any]]:
    if sort_by not in SORT_COLUMNS:
        return sorted(
            rows,
            key=lambda item: (
                source_rank(item.get("source"), selected_sources),
                (item.get("country") or ""),
                (item.get("product") or ""),
                (item.get("document_type") or ""),
            ),
        )
    field = SORT_COLUMNS[sort_by]
    reverse = sort_dir == "desc"
    return sorted(
        rows,
        key=lambda item: str(item.get(field) or "").lower(),
        reverse=reverse,
    )


def suppress_generic_lookup_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    countries_with_connector_rows = {
        row.get("country")
        for row in rows
        if row.get("country") and row.get("source") not in GENERIC_LOOKUP_SOURCES
    }
    if not countries_with_connector_rows:
        return rows
    return [
        row
        for row in rows
        if not (
            row.get("country") in countries_with_connector_rows
            and row.get("source") in GENERIC_LOOKUP_SOURCES
        )
    ]


def parse_sources(sources: list[str] | str | None) -> list[str]:
    if isinstance(sources, str):
        parsed = [source.strip() for source in sources.split(",") if source.strip()]
    else:
        parsed = [source.strip() for source in sources or [] if source.strip()]

    normalized = {source.lower() for source in parsed}
    if "ema" in normalized or "eu" in normalized or "eu national" in normalized:
        for source in EU_NATIONAL_SOURCES:
            if source.lower() not in normalized:
                parsed.append(source)
                normalized.add(source.lower())
    return parsed


def item_matches_selected_sources(item: dict[str, Any], selected_sources: list[str]) -> bool:
    if not selected_sources:
        return True
    selected = {source.strip().lower() for source in selected_sources}
    item_source = (item.get("source") or "").strip().lower()
    if item_source in selected:
        return True
    country = (item.get("country") or "").strip()
    return any(country in SOURCE_COUNTRIES.get(source, set()) for source in selected)


def sources_for_scope(
    selected_sources: list[str],
    country: str = "",
    region: str = "",
    source: str = "",
) -> list[str]:
    selected = parse_sources(selected_sources)
    if source:
        return [source]

    if region == "EU":
        allowed = {"ema"} | {item.lower() for item in EU_NATIONAL_SOURCES}
        return [item for item in selected if item.lower() in allowed]
    if region == "UK":
        return [item for item in selected if item.lower() == "mhra"]
    if region == "US":
        return [item for item in selected if item.lower() == "fda"]
    if region == "CA":
        return [item for item in selected if item.lower() == "health canada"]
    if region == "AU":
        return [item for item in _ensure_source(selected, "TGA Australia") if item.lower() == "tga australia"]
    if region == "NZ":
        return [
            item
            for item in _ensure_source(selected, "Medsafe New Zealand")
            if item.lower() == "medsafe new zealand"
        ]
    if region == "ALL":
        regional_sources = []
        for sources_for_region in REGIONAL_LIVE_SOURCES.values():
            regional_sources.extend(sources_for_region)
        return [
            item
            for item in selected
            if item in regional_sources or item in DEFAULT_SOURCES
        ] or [LOOKUP_ONLY_SOURCE]
    if region in REGIONAL_COUNTRIES:
        live_sources = REGIONAL_LIVE_SOURCES.get(region, [])
        selected_live_sources = [item for item in selected if item in live_sources]
        return selected_live_sources or [LOOKUP_ONLY_SOURCE]

    if region in REGION_SOURCE_DEFAULTS:
        selected = _ensure_source(selected, REGION_SOURCE_DEFAULTS[region])

    if country:
        if country in COUNTRY_SOURCE_DEFAULTS:
            selected = _ensure_source(selected, COUNTRY_SOURCE_DEFAULTS[country])
        elif country in AFRICA_COUNTRIES or country in MIDDLE_EAST_COUNTRIES or country in ASIA_COUNTRIES:
            return [LOOKUP_ONLY_SOURCE]
        scoped = []
        for item in selected:
            name = item.lower()
            if country in SOURCE_COUNTRIES.get(name, set()):
                scoped.append(item)
        return scoped or selected

    return selected


def combined_search(
    substance: str,
    include_live: bool = True,
    sources: list[str] | None = None,
    live_timeout: int | None = None,
) -> list[dict[str, Any]]:
    rows = []
    seen = set()
    index_by_key = {}
    selected_sources = parse_sources(sources)

    for item in search_product_details(substance) + search_medicines(substance):
        if is_connector_lookup_fallback(item):
            continue
        if not item_matches_selected_sources(item, selected_sources):
            continue
        key = result_key(item)
        if key not in seen:
            seen.add(key)
            index_by_key[key] = len(rows)
            rows.append(item)

    if include_live:
        for item in search_substance(
            substance,
            source_names=selected_sources,
            timeout_seconds=live_timeout or LIVE_SEARCH_TIMEOUT_SECONDS,
        ):
            if is_connector_lookup_fallback(item):
                continue
            key = result_key(item)
            if key in index_by_key:
                existing = rows[index_by_key[key]]
                for field, value in item.items():
                    if value and not existing.get(field):
                        existing[field] = value
            else:
                seen.add(key)
                index_by_key[key] = len(rows)
                rows.append(item)

    return sort_rows(propagate_molecule_fields(rows), selected_sources)


def enriched_cached_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = [dict(item) for item in results]
    mhra_rows = [row for row in rows if row.get("source") == "MHRA"]
    if mhra_rows:
        enrich_mhra_document_metadata(mhra_rows)
    enriched_results = attach_ai_enrichment_metadata(
        propagate_molecule_fields(enrich_deep_results(rows))
    )
    for item in enriched_results:
        save_product_detail(item)
    return enriched_results


def filtered_search_results(
    substance: str,
    live: bool,
    sources: list[str],
    country: str = "",
    region: str = "",
    source: str = "",
    status: str = "",
    table_search: str = "",
    substance_filter: str = "",
    product_filter: str = "",
    company_filter: str = "",
    strength_filter: str = "",
    dosage_form_filter: str = "",
    pack_size_filter: str = "",
    atc_code_filter: str = "",
    therapeutic_category_filter: str = "",
    ma_holder_filter: str = "",
    manufacturer_name_filter: str = "",
    manufacturer_country_filter: str = "",
    registration_number_filter: str = "",
    registration_date_filter: str = "",
    sort_by: str = "",
    sort_dir: str = "asc",
    live_timeout: int | None = None,
    include_lookup_rows: bool = True,
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    selected_sources = parse_sources(sources)
    scoped_sources = sources_for_scope(
        selected_sources,
        country=country,
        region=region,
        source=source,
    )
    all_rows = combined_search(
        substance,
        include_live=live,
        sources=scoped_sources,
        live_timeout=live_timeout,
    )
    all_rows = [row for row in all_rows if row_relevant_to_substance(row, substance)]
    normalized_sort_dir = "desc" if sort_dir == "desc" else "asc"
    rows = filter_rows(
        all_rows,
        country=country,
        region=region,
        source=source,
        status=status,
        table_search=table_search,
        substance_filter=substance_filter,
        product_filter=product_filter,
        company_filter=company_filter,
        strength_filter=strength_filter,
        dosage_form_filter=dosage_form_filter,
        pack_size_filter=pack_size_filter,
        atc_code_filter=atc_code_filter,
        therapeutic_category_filter=therapeutic_category_filter,
        ma_holder_filter=ma_holder_filter,
        manufacturer_name_filter=manufacturer_name_filter,
        manufacturer_country_filter=manufacturer_country_filter,
        registration_number_filter=registration_number_filter,
        registration_date_filter=registration_date_filter,
    )
    if include_lookup_rows and not source:
        fallback_rows = []
        if region == "EU":
            fallback_rows = _country_lookup_rows(
                substance,
                region=region,
                existing_rows=rows,
            )
        elif region in REGIONAL_COUNTRIES or region == "ALL":
            fallback_rows = _country_lookup_rows(
                substance,
                region=region,
                existing_rows=rows,
            )
        elif country and not rows:
            fallback_rows = _country_lookup_rows(substance, country=country)
        elif region and not rows:
            fallback_rows = _country_lookup_rows(substance, region=region)
        if fallback_rows:
            rows.extend(
                filter_rows(
                    fallback_rows,
                    country=country,
                    region=region,
                    status=status,
                    table_search=table_search,
                    substance_filter=substance_filter,
                    product_filter=product_filter,
                    company_filter=company_filter,
                    strength_filter=strength_filter,
                    dosage_form_filter=dosage_form_filter,
                    pack_size_filter=pack_size_filter,
                    atc_code_filter=atc_code_filter,
                    therapeutic_category_filter=therapeutic_category_filter,
                    ma_holder_filter=ma_holder_filter,
                    manufacturer_name_filter=manufacturer_name_filter,
                    manufacturer_country_filter=manufacturer_country_filter,
                    registration_number_filter=registration_number_filter,
                    registration_date_filter=registration_date_filter,
                )
            )
            all_rows.extend(fallback_rows)

    visible_rows = suppress_generic_lookup_rows(rows)
    if not include_lookup_rows:
        visible_rows = [row for row in visible_rows if row.get("source") not in GENERIC_LOOKUP_SOURCES]
    return (
        sort_rows(visible_rows, selected_sources, sort_by=sort_by, sort_dir=normalized_sort_dir),
        selected_sources,
        all_rows,
    )
