from typing import Any

from sources.connectors.base import (
    FunctionSourceConnector,
    SourceConnector,
    SourceMetadata,
)
from sources.belgium_famhp import run_belgium_famhp_search
from sources.ema import run_ema_search
from sources.eu_mri import run_eu_mri_search
from sources.fda import run_fda_search
from sources.france_bdpm import run_france_bdpm_search
from sources.health_canada import run_health_canada_search
from sources.ireland_medicines_ie import run_ireland_medicines_search
from sources.medsafe import run_medsafe_search
from sources.mhra import run_mhra_search
from sources.regional_live import (
    run_bpom_indonesia_search,
    run_cdsco_india_search,
    run_dav_vietnam_search,
    run_cyprus_pharmaceutical_services_search,
    run_fda_orange_book_search,
    run_fda_purple_book_search,
    run_fda_ghana_search,
    run_fda_philippines_search,
    run_grls_russia_search,
    run_hong_kong_drug_office_search,
    run_hsa_singapore_search,
    run_israel_drug_registry_search,
    run_mfds_south_korea_search,
    run_nmpa_china_search,
    run_npra_malaysia_search,
    run_pmda_japan_search,
    run_sahpra_search,
    run_sfda_saudi_search,
    run_thai_fda_search,
    run_ukraine_drlz_search,
)
from sources.spain_cima import run_spain_cima_search
from sources.swissmedic import run_swissmedic_search
from sources.tga import run_tga_search


CONNECTORS: list[SourceConnector] = [
    FunctionSourceConnector(
        SourceMetadata(
            name="FDA",
            region="US",
            countries=("United States",),
            supports_documents=True,
        ),
        lambda substance: run_fda_search(substance, limit=25),
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="FDA Orange Book",
            region="US",
            countries=("United States",),
            supports_documents=False,
        ),
        run_fda_orange_book_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="FDA Purple Book",
            region="US",
            countries=("United States",),
            supports_documents=False,
        ),
        run_fda_purple_book_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="MHRA",
            region="UK",
            countries=("United Kingdom",),
            supports_documents=True,
        ),
        run_mhra_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="EMA",
            region="EU",
            countries=(
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
            ),
            supports_documents=True,
        ),
        run_ema_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="EU MRI Product Index",
            region="EU",
            countries=(
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
            ),
            supports_documents=True,
        ),
        run_eu_mri_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="Health Canada",
            region="CA",
            countries=("Canada",),
            supports_documents=False,
        ),
        lambda substance: run_health_canada_search(substance, limit=25),
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="TGA Australia",
            region="AU",
            countries=("Australia",),
            supports_documents=True,
        ),
        run_tga_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="Medsafe New Zealand",
            region="NZ",
            countries=("New Zealand",),
            supports_documents=True,
        ),
        run_medsafe_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="SAHPRA South Africa",
            region="AF",
            countries=("South Africa",),
            supports_documents=False,
        ),
        run_sahpra_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="FDA Ghana",
            region="AF",
            countries=("Ghana",),
            supports_documents=False,
        ),
        run_fda_ghana_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="SFDA Saudi Arabia",
            region="ME",
            countries=("Saudi Arabia",),
            supports_documents=False,
        ),
        run_sfda_saudi_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="Israel Drug Registry",
            region="ME",
            countries=("Israel",),
            supports_documents=False,
        ),
        run_israel_drug_registry_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="CDSCO India",
            region="AS",
            countries=("India",),
            supports_documents=False,
        ),
        run_cdsco_india_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="NMPA China",
            region="AS",
            countries=("China",),
            supports_documents=False,
        ),
        run_nmpa_china_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="BPOM Indonesia",
            region="AS",
            countries=("Indonesia",),
            supports_documents=False,
        ),
        run_bpom_indonesia_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="NPRA Malaysia",
            region="AS",
            countries=("Malaysia",),
            supports_documents=False,
        ),
        run_npra_malaysia_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="FDA Philippines",
            region="AS",
            countries=("Philippines",),
            supports_documents=False,
        ),
        run_fda_philippines_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="HSA Singapore",
            region="AS",
            countries=("Singapore",),
            supports_documents=False,
        ),
        run_hsa_singapore_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="MFDS South Korea",
            region="AS",
            countries=("South Korea",),
            supports_documents=False,
        ),
        run_mfds_south_korea_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="Thai FDA",
            region="AS",
            countries=("Thailand",),
            supports_documents=False,
        ),
        run_thai_fda_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="DAV Vietnam",
            region="AS",
            countries=("Vietnam",),
            supports_documents=False,
        ),
        run_dav_vietnam_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="Swissmedic",
            region="CH",
            countries=("Switzerland",),
            supports_documents=False,
            enabled=False,
        ),
        run_swissmedic_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="PMDA Japan",
            region="JP",
            countries=("Japan",),
            supports_documents=False,
        ),
        run_pmda_japan_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="Hong Kong Drug Office",
            region="AS",
            countries=("Hong Kong",),
            supports_documents=False,
        ),
        run_hong_kong_drug_office_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="Belgium FAMHP",
            region="EU",
            countries=("Belgium",),
            supports_documents=True,
        ),
        run_belgium_famhp_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="France BDPM",
            region="EU",
            countries=("France",),
            supports_documents=False,
        ),
        run_france_bdpm_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="Ireland medicines.ie",
            region="EU",
            countries=("Ireland",),
            supports_documents=True,
        ),
        run_ireland_medicines_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="Spain CIMA",
            region="EU",
            countries=("Spain",),
            supports_documents=True,
        ),
        run_spain_cima_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="Cyprus Pharmaceutical Services",
            region="EU",
            countries=("Cyprus",),
            supports_documents=False,
        ),
        run_cyprus_pharmaceutical_services_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="Ukraine DRLZ",
            region="EU",
            countries=("Ukraine",),
            supports_documents=False,
        ),
        run_ukraine_drlz_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="GRLS Russia",
            region="RU",
            countries=("Russia",),
            supports_documents=False,
        ),
        run_grls_russia_search,
    ),
]


def enabled_connectors() -> list[SourceConnector]:
    return [connector for connector in CONNECTORS if connector.metadata.enabled]


def connector_metadata() -> list[dict[str, Any]]:
    return [connector.metadata.to_dict() for connector in CONNECTORS]


def connector_by_name(name: str) -> SourceConnector | None:
    normalized = name.strip().lower()
    for connector in CONNECTORS:
        if connector.metadata.name.lower() == normalized:
            return connector
    return None


# Backward-compatible registry consumed by the current search engine.
SOURCES = [
    {"name": connector.metadata.name, "function": connector.search}
    for connector in enabled_connectors()
]
