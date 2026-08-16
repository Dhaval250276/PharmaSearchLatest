import unittest
import os
from unittest.mock import MagicMock, patch

import requests
from bs4 import BeautifulSoup

from export_service import build_export_rows
from models.regulatory import RegulatoryProduct
from repository import (
    get_persisted_search_job,
    get_persisted_search_job_results,
    save_search_job,
    save_search_job_progress,
    save_search_job_results,
)
from sources.ema import _ema_result_from_record, _expand_xlsx_records
from sources.eu_mri import _parse_table_rows, run_eu_mri_search
from sources.medsafe import (
    _fallback_rows as _medsafe_fallback_rows,
    _parse_product_search_results as _parse_medsafe_product_search_results,
)
from sources.mhra_document_parser import (
    _document_links_from_html,
    _invalid_manufacturer_value,
    _metadata_from_text,
)
from sources.parser import (
    clean_product_name,
    extract_dosage_form,
    extract_registration_number,
    extract_strength,
)
from sources.regional_live import (
    REGIONAL_SOURCES,
    parse_regional_source_results,
    run_cdsco_india_search,
    run_nmpa_china_search,
)
from services.field_completion import (
    _completions_for_group,
    attach_reference_documents,
    molecule_group_key,
)
from services.harvest import CONSECUTIVE_FAILURE_LIMIT, run_harvest
from services.harvest_vocabulary import (
    _Accumulator,
    normalize_molecule,
    split_combination,
)
from sources.connectors.base import SourceMetadata
from sources.source_registry import CONNECTORS, SOURCES, connector_metadata
from sources.tga import _fallback_rows, _merge_detail, _parse_artg_detail, _parse_artg_search_results
from services.ai_client import ai_extract_regulatory_fields, ai_status
from services.ai_enrichment import enrichment_metadata, missing_enrichment_fields
from services.connector_health import connector_health_rows, record_source_health
from services.connector_status import connector_status_rows
from services.english_normalizer import english_row, english_text
from services.field_availability import NOT_APPLICABLE, PENDING_ENRICHMENT, field_value
from services.result_formatter import manufacturer_name_value
from services.search_pipeline import (
    _country_lookup_rows,
    _eu_lookup_rows,
    combined_search,
    filtered_search_results,
    prepared_cached_results,
    sources_for_scope,
    suppress_generic_lookup_rows,
    is_connector_lookup_fallback,
    row_relevant_to_substance,
)
from services.search_jobs import SLOW_SOURCES, _dedupe_rows, order_sources_for_job, source_skipped_in_mode
from services.therapeutic_category import short_therapeutic_category


class CachedResultPreparationTests(unittest.TestCase):
    def test_prepares_background_results_without_mutating_saved_rows(self):
        saved_rows = [{"substance": "metformin", "product": "Metformin 500 mg", "source": "FDA"}]

        prepared = prepared_cached_results(saved_rows)

        self.assertEqual(saved_rows[0].get("data_confidence"), None)
        self.assertEqual(prepared[0]["product"], "Metformin 500 mg")
        self.assertIn("data_confidence", prepared[0])


class FieldAvailabilityTests(unittest.TestCase):
    def test_document_fields_are_not_applicable_to_fda_schema(self):
        self.assertEqual(field_value({"source": "FDA"}, "smpc_url"), NOT_APPLICABLE)

    def test_manufacturer_is_not_pending_for_fda_schema(self):
        row = {"source": "FDA", "product_url": "https://example.test/label"}
        self.assertEqual(field_value(row, "manufacturer_name"), "Not supplied by regulator")

    def test_manufacturer_is_not_pending_for_regional_api_schema(self):
        row = {"source": "BPOM Indonesia", "product_url": "https://example.test/product"}
        self.assertEqual(field_value(row, "manufacturer_name"), "Not supplied by regulator")

    def test_missing_mhra_manufacturer_is_marked_for_enrichment(self):
        row = {"source": "MHRA", "pil_url": "https://example.test/pil.pdf"}
        self.assertEqual(field_value(row, "manufacturer_name"), PENDING_ENRICHMENT)

    def test_missing_mhra_manufacturer_is_not_pending_after_document_parse(self):
        row = {
            "source": "MHRA",
            "pil_url": "https://example.test/pil.pdf",
            "document_enrichment_attempted": True,
        }
        self.assertEqual(field_value(row, "manufacturer_name"), "Not supplied by regulator")

    def test_export_excludes_registry_handoff_rows(self):
        rows = build_export_rows(
            "metformin",
            [
                {
                    "substance": "metformin",
                    "product": "Official registry search",
                    "source": "NMPA China",
                    "connector_mode": "manual_registry",
                    "document_type": "Official registry search handoff",
                }
            ],
        )
        self.assertEqual(rows, [])


class SubstanceRelevanceTests(unittest.TestCase):
    def test_combination_search_rejects_single_ingredient_record(self):
        row = {
            "source": "FDA",
            "product": "Prilocaine topical cream",
            "source_substance": "prilocaine",
        }
        self.assertFalse(row_relevant_to_substance(row, "Prilocaine+Lidocaine"))

    def test_combination_search_accepts_both_ingredients(self):
        row = {
            "source": "FDA",
            "product": "Lidocaine and Prilocaine cream",
            "source_substance": "lidocaine prilocaine",
        }
        self.assertTrue(row_relevant_to_substance(row, "Prilocaine+Lidocaine"))


class LiveSourceSelectionTests(unittest.TestCase):
    @patch("services.search_pipeline.search_medicines", return_value=[])
    @patch("services.search_pipeline.search_product_details")
    @patch("services.search_pipeline.search_substance", return_value=[])
    def test_cached_sources_and_live_sources_are_separate(
        self,
        search_substance_mock,
        search_product_details_mock,
        _search_medicines_mock,
    ):
        search_product_details_mock.return_value = [
            {
                "substance": "metformin",
                "product": "Cached MHRA product",
                "source": "MHRA",
                "country": "United Kingdom",
            }
        ]

        rows = combined_search(
            "metformin",
            sources=["FDA", "MHRA"],
            live_sources=["FDA"],
            live_timeout=5,
        )

        self.assertEqual(rows[0]["source"], "MHRA")
        self.assertEqual(search_substance_mock.call_args.kwargs["source_names"], ["FDA"])


class SearchPageFilterTests(unittest.TestCase):
    def test_every_result_column_has_a_header_filter(self):
        from main import search_page

        response = search_page("metformin", live=False, sources=["FDA"], page_size=10)
        soup = BeautifulSoup(response.body.decode(), "html.parser")
        table = soup.select_one("table.results-table")
        header_rows = table.select("thead tr")

        self.assertEqual(len(header_rows[0].select("th")), 22)
        filter_cells = header_rows[1].select("th")
        self.assertEqual(len(filter_cells), 22)
        self.assertTrue(all(cell.select_one("input, select") for cell in filter_cells))


class ParserTests(unittest.TestCase):
    def test_extracts_uk_registration_variants(self):
        self.assertEqual(
            extract_registration_number("Example 5 mg tablets - PLGB 12345/0001"),
            "PLGB 12345/0001",
        )
        self.assertEqual(
            extract_registration_number("Example syrup PLNI 99999/0002-001"),
            "PLNI 99999/0002-001",
        )

    def test_extracts_strength_and_dosage_form(self):
        product = "DAPAGLIFLOZIN 5 MG FILM-COATED TABLETS - PL 59787/0020"
        self.assertEqual(extract_strength(product), "5 MG")
        self.assertEqual(extract_dosage_form(product), "Film-coated tablet")

    def test_cleans_repeated_product_text(self):
        self.assertEqual(clean_product_name("Forxiga Forxiga PL 12345/0001"), "Forxiga PL 12345/0001")


class ExportTests(unittest.TestCase):
    def test_build_export_rows_adds_provenance_columns(self):
        rows = build_export_rows(
            "Dapagliflozin",
            [
                {
                    "substance": "Dapagliflozin",
                    "product": "Forxiga 10 mg film-coated tablets",
                    "country": "United Kingdom",
                    "source": "MHRA",
                    "url": "https://example.test/doc",
                }
            ],
        )
        self.assertEqual(rows[0]["Region"], "UK")
        self.assertEqual(rows[0]["Strength"], "10 mg")
        self.assertEqual(rows[0]["Dosage Form"], "Film-coated tablet")
        self.assertEqual(rows[0]["Source"], "MHRA")

    def test_build_export_rows_uses_mhra_document_metadata(self):
        rows = build_export_rows(
            "Ibuprofen",
            [
                {
                    "substance": "Ibuprofen",
                    "product": "Ibuprofen 200 mg tablets",
                    "country": "United Kingdom",
                    "source": "MHRA",
                    "company": "Example MA Holder Ltd",
                    "manufacturer_name": "Example Manufacturer Ltd",
                    "pack_size": "24 tablets",
                    "smpc_url": "https://example.test/spc.pdf",
                    "pil_url": "https://example.test/pil.pdf",
                }
            ],
        )
        self.assertEqual(rows[0]["MA Holder Name"], "Example MA Holder Ltd")
        self.assertEqual(rows[0]["Manufacturer Name"], "Example Manufacturer Ltd")
        self.assertEqual(rows[0]["Pack Size"], "24 tablets")
        self.assertEqual(rows[0]["SMPC URL"], "https://example.test/spc.pdf")
        self.assertEqual(rows[0]["PIL / Assessment Report"], "https://example.test/pil.pdf")


class MHRADocumentParserTests(unittest.TestCase):
    def test_rejects_leaflet_narrative_as_manufacturer(self):
        self.assertTrue(
            _invalid_manufacturer_value(
                "This leaflet was last revised in August 2022. EVER Pharma Jena GmbH "
                "Contents of the pack and other information"
            )
        )
        self.assertFalse(_invalid_manufacturer_value("EVER Pharma Jena GmbH"))

    def test_preserves_explicit_manufacturer_when_it_is_also_the_holder(self):
        row = {
            "company": "Henry Schein UK Holdings Ltd",
            "manufacturer_name": "Henry Schein UK Holdings Ltd.",
            "manufacturer_source": "MHRA document",
        }
        self.assertEqual(manufacturer_name_value(row), "Henry Schein UK Holdings Ltd.")

    def test_extracts_document_metadata_from_text(self):
        metadata = _metadata_from_text(
            """
            Marketing Authorisation Holder
            Example Pharma Ltd
            10 High Street

            Manufacturer responsible for batch release
            Example Manufacturing Ltd
            Industrial Estate

            Pack sizes
            24 tablets
            """
        )
        self.assertEqual(metadata["company"], "Example Pharma Ltd")
        self.assertEqual(metadata["manufacturer_name"], "Example Manufacturing Ltd")
        self.assertEqual(metadata["pack_size"], "24 tablets")

    def test_extracts_related_document_links_from_html(self):
        soup = BeautifulSoup(
            """
            <a href="/docs/example-spc.pdf">Summary of Product Characteristics</a>
            <a href="/docs/example-pil.pdf">Patient Information Leaflet</a>
            """,
            "html.parser",
        )
        links = _document_links_from_html(soup)
        self.assertEqual(links["smpc_url"], "https://products.mhra.gov.uk/docs/example-spc.pdf")
        self.assertEqual(links["pil_url"], "https://products.mhra.gov.uk/docs/example-pil.pdf")

    def test_extracts_multiple_manufacturers_and_pack_section(self):
        metadata = _metadata_from_text(
            """
            Marketing Authorisation Holder
            Mylan Ltd, Potters Bar, United Kingdom

            Manufacturer
            Merckle GmbH, Ludwig-Merckle-Strasse 3, Germany
            Mylan Hungary Kft, Mylan utca 1, Hungary
            McDermott Laboratories Ltd, Dublin, Ireland

            Contents of the pack
            The tablets are available in packs of 10, 30 and 60 tablets.
            """
        )
        self.assertEqual(metadata["company"], "Mylan Ltd")
        self.assertIn("Merckle GmbH", metadata["manufacturer_name"])
        self.assertIn("Mylan Hungary Kft", metadata["manufacturer_name"])
        self.assertEqual(metadata["manufacturer_country"], "Germany; Hungary; Ireland")
        self.assertIn("packs of 10, 30 and 60 tablets", metadata["pack_size"])


class EMAConnectorTests(unittest.TestCase):
    def test_maps_ema_record_to_source_schema(self):
        result = _ema_result_from_record(
            {
                "active_substance": "Metformin",
                "name_of_medicine": "Glucophage 500 mg tablets",
                "marketing_authorisation_holder_company_name": "Example Holder GmbH",
                "pharmaceutical_form": "Tablet",
                "medicine_url": "/en/medicines/human/EPAR/glucophage",
                "medicine_status": "Authorised",
                "ema_product_number": "EMEA/H/C/000000",
            },
            "Metformin",
            "Germany",
            "https://example.test/ema.xlsx",
        )

        self.assertEqual(result["substance"], "Metformin")
        self.assertEqual(result["product"], "Glucophage 500 mg tablets")
        self.assertEqual(result["company"], "Example Holder GmbH")
        self.assertEqual(result["country"], "Germany")
        self.assertEqual(result["strength"], "500 mg")
        self.assertEqual(result["dosage_form"], "Tablet")
        self.assertEqual(result["source"], "EMA")
        self.assertEqual(
            result["product_url"],
            "https://www.ema.europa.eu/en/medicines/human/EPAR/glucophage",
        )

    def test_ema_search_filters_by_active_substance(self):
        results = _expand_xlsx_records(
            [
                {
                    "active_substance": "Metformin hydrochloride",
                    "name_of_medicine": "Metformin Example 500 mg tablets",
                    "marketing_authorisation_developer_applicant_holder": "Example Holder",
                    "medicine_url": "https://example.test/metformin",
                },
                {
                    "active_substance": "Ibuprofen",
                    "name_of_medicine": "Ibuprofen Example",
                    "medicine_url": "https://example.test/ibuprofen",
                },
            ],
            "Metformin",
        )

        self.assertTrue(results)
        self.assertTrue(all(item["source"] == "EMA" for item in results))
        self.assertTrue(all("Metformin" in item["substance"] for item in results))


class EUMRIConnectorTests(unittest.TestCase):
    def test_parses_mri_table_rows(self):
        rows = _parse_table_rows(
            """
            <table>
              <thead>
                <tr>
                  <th>Full Name</th>
                  <th>Active Substance</th>
                  <th>MAH/Owner</th>
                  <th>Authorisation Country</th>
                  <th>Authorisation Status</th>
                  <th>MRP/DCP/CP Nr.</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td><a href="/product/1">Example 10 mg tablets</a></td>
                  <td>metformin</td>
                  <td>Example Pharma Ltd</td>
                  <td>Denmark</td>
                  <td>Authorised</td>
                  <td>DK/H/1234/001</td>
                </tr>
              </tbody>
            </table>
            """,
            "https://mri-production.cts-mrp.eu/product-search",
            "metformin",
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "EU MRI Product Index")
        self.assertEqual(rows[0]["country"], "Denmark")
        self.assertEqual(rows[0]["company"], "Example Pharma Ltd")
        self.assertEqual(rows[0]["registration_number"], "DK/H/1234/001")

    def test_mri_connector_returns_empty_for_blank_query(self):
        self.assertEqual(run_eu_mri_search(""), [])


class PlatformCoreTests(unittest.TestCase):
    def test_unified_product_converts_to_export_schema(self):
        product = RegulatoryProduct(
            source="Swissmedic",
            active_substance="Metformin",
            product_name="Metformin Example 500 mg tablets",
            marketing_authorisation_holder="Example Holder AG",
            country="Switzerland",
            region="CH",
            strength="500 mg",
            dosage_form="Tablet",
            product_url="https://example.test/product",
        )
        record = product.to_source_record()
        self.assertEqual(record["source"], "Swissmedic")
        self.assertEqual(record["substance"], "Metformin")
        self.assertEqual(record["product"], "Metformin Example 500 mg tablets")
        self.assertEqual(record["company"], "Example Holder AG")
        self.assertEqual(record["url"], "https://example.test/product")

    def test_connector_registry_exposes_target_regulators(self):
        names = {item["name"] for item in connector_metadata()}
        self.assertTrue(
            {
                "FDA",
                "MHRA",
                "EMA",
                "EU MRI Product Index",
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
                "Swissmedic",
                "PMDA Japan",
            }.issubset(names)
        )
        self.assertTrue(all("function" in item for item in SOURCES))
        self.assertTrue(all(connector.metadata.name for connector in CONNECTORS))

    def test_enabled_sources_include_tga_australia(self):
        self.assertIn("TGA Australia", {item["name"] for item in SOURCES})

    def test_enabled_sources_include_medsafe_new_zealand(self):
        self.assertIn("Medsafe New Zealand", {item["name"] for item in SOURCES})

    def test_eu_lookup_returns_country_row_for_missing_connector(self):
        rows = _eu_lookup_rows("atorvastatin", country="Greece")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["country"], "Greece")
        self.assertEqual(rows[0]["source"], "EU National Registry")
        self.assertEqual(rows[0]["document_type"], "EU national lookup fallback")

    def test_filtered_search_adds_eu_country_fallback_when_empty(self):
        rows, _, _ = filtered_search_results(
            "atorvastatin",
            live=False,
            sources=["EMA"],
            country="Greece",
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["country"], "Greece")
        self.assertEqual(rows[0]["source"], "EU National Registry")

    def test_country_lookup_returns_row_for_any_country(self):
        rows = _country_lookup_rows("atorvastatin", country="Brazil")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["country"], "Brazil")
        self.assertEqual(rows[0]["region"], "Global")
        self.assertEqual(rows[0]["source"], "Regulatory Registry Lookup")

    def test_filtered_search_adds_global_country_fallback_when_empty(self):
        rows, _, _ = filtered_search_results(
            "atorvastatin",
            live=False,
            sources=["EMA"],
            country="Japan",
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["country"], "Japan")
        self.assertEqual(rows[0]["region"], "JP")
        self.assertEqual(rows[0]["source"], "Regulatory Registry Lookup")

    def test_country_lookup_returns_new_zealand_row(self):
        rows = _country_lookup_rows("atorvastatin", country="New Zealand")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["country"], "New Zealand")
        self.assertEqual(rows[0]["region"], "NZ")

    def test_country_lookup_returns_africa_country_row(self):
        rows = _country_lookup_rows("atorvastatin", country="Kenya")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["country"], "Kenya")
        self.assertEqual(rows[0]["region"], "AF")
        self.assertEqual(rows[0]["source"], "Africa Generic Registry Lookup")
        self.assertEqual(rows[0]["document_type"], "Generic Africa registry lookup fallback")
        self.assertIn("pharmacyboardkenya", rows[0]["url"])

    def test_country_lookup_returns_middle_east_country_row(self):
        rows = _country_lookup_rows("atorvastatin", country="Saudi Arabia")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["country"], "Saudi Arabia")
        self.assertEqual(rows[0]["region"], "ME")
        self.assertEqual(rows[0]["source"], "Middle East Generic Registry Lookup")
        self.assertEqual(rows[0]["document_type"], "Generic Middle East registry lookup fallback")
        self.assertIn("sfda.gov.sa", rows[0]["url"])

    def test_region_lookup_returns_africa_rows(self):
        rows = _country_lookup_rows("atorvastatin", region="AF")
        countries = {row["country"] for row in rows}
        self.assertEqual(len(rows), 54)
        self.assertIn("South Africa", countries)
        self.assertIn("Nigeria", countries)
        self.assertIn("Zimbabwe", countries)
        self.assertTrue(all(row["region"] == "AF" for row in rows))
        self.assertTrue(all(row["source"] == "Africa Generic Registry Lookup" for row in rows))

    def test_region_lookup_returns_middle_east_rows(self):
        rows = _country_lookup_rows("atorvastatin", region="ME")
        countries = {row["country"] for row in rows}
        self.assertEqual(len(rows), 17)
        self.assertIn("Saudi Arabia", countries)
        self.assertIn("United Arab Emirates", countries)
        self.assertIn("Yemen", countries)
        self.assertTrue(all(row["region"] == "ME" for row in rows))
        self.assertTrue(all(row["source"] == "Middle East Generic Registry Lookup" for row in rows))

    def test_country_lookup_returns_asia_country_row(self):
        rows = _country_lookup_rows("atorvastatin", country="India")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["country"], "India")
        self.assertEqual(rows[0]["region"], "AS")
        self.assertEqual(rows[0]["source"], "Asia Generic Registry Lookup")
        self.assertEqual(rows[0]["document_type"], "Generic Asia registry lookup fallback")
        self.assertIn("cdsco.gov.in", rows[0]["url"])

    def test_sources_for_scope_uses_india_connector_for_india_country(self):
        sources = sources_for_scope(
            ["CDSCO India"],
            country="India",
        )
        self.assertEqual(sources, ["CDSCO India"])

    def test_sources_for_scope_auto_adds_country_connector_when_missing(self):
        sources = sources_for_scope(
            ["FDA", "Hong Kong Drug Office", "France BDPM", "Spain CIMA"],
            country="China",
        )
        self.assertEqual(sources, ["NMPA China"])

    def test_manual_registry_rows_are_not_treated_as_product_records(self):
        self.assertTrue(
            is_connector_lookup_fallback(
                {
                    "country": "China",
                    "source": "NMPA China",
                    "product": "paracetamol official China registry search",
                    "document_type": "Official registry search handoff",
                    "connector_mode": "manual_registry",
                }
            )
        )

    def test_region_lookup_returns_asia_rows(self):
        rows = _country_lookup_rows("atorvastatin", region="AS")
        countries = {row["country"] for row in rows}
        self.assertIn("India", countries)
        self.assertIn("China", countries)
        self.assertIn("Saudi Arabia", countries)
        self.assertIn("Japan", countries)
        self.assertTrue(all(row["region"] == "AS" for row in rows))
        self.assertTrue(all(row["source"] == "Asia Generic Registry Lookup" for row in rows))

    def test_region_lookup_returns_all_country_rows(self):
        rows = _country_lookup_rows("atorvastatin", region="ALL")
        countries = {row["country"] for row in rows}
        self.assertIn("India", countries)
        self.assertIn("South Africa", countries)
        self.assertIn("Saudi Arabia", countries)
        self.assertIn("United States", countries)
        self.assertTrue(all(row["region"] == "ALL" for row in rows))
        self.assertTrue(all(row["source"] == "Global Generic Registry Lookup" for row in rows))

    def test_filtered_search_adds_africa_region_fallback_when_empty(self):
        rows, _, _ = filtered_search_results(
            "atorvastatin",
            live=False,
            sources=["EMA"],
            region="AF",
        )
        self.assertEqual(len(rows), 54)
        self.assertTrue(all(row["region"] == "AF" for row in rows))

    def test_filtered_search_keeps_africa_connector_rows_and_fills_missing_countries(self):
        rows, _, _ = filtered_search_results(
            "atorvastatin",
            live=False,
            sources=["SAHPRA South Africa"],
            region="AF",
        )
        countries = {row["country"] for row in rows}
        self.assertIn("South Africa", countries)
        self.assertIn("Zimbabwe", countries)
        self.assertEqual(len(countries), 54)

    def test_filtered_search_adds_middle_east_region_fallback_when_empty(self):
        rows, _, _ = filtered_search_results(
            "atorvastatin",
            live=False,
            sources=["EMA"],
            region="ME",
        )
        self.assertEqual(len(rows), 17)
        self.assertTrue(all(row["region"] == "ME" for row in rows))

    def test_filtered_search_adds_asia_region_fallback_when_empty(self):
        rows, _, _ = filtered_search_results(
            "atorvastatin",
            live=False,
            sources=["EMA"],
            region="AS",
        )
        self.assertGreaterEqual(len(rows), 10)
        self.assertTrue(all(row["region"] == "AS" for row in rows))

    def test_filtered_search_adds_all_country_fallback_when_empty(self):
        rows, _, _ = filtered_search_results(
            "atorvastatin",
            live=False,
            sources=["EMA"],
            region="ALL",
        )
        self.assertGreaterEqual(len(rows), 100)
        self.assertIn("Global Generic Registry Lookup", {row["source"] for row in rows})

    def test_suppresses_generic_row_when_country_connector_row_exists(self):
        rows = suppress_generic_lookup_rows(
            [
                {
                    "country": "India",
                    "source": "CDSCO India",
                    "product": "CDSCO India regulator lookup",
                },
                {
                    "country": "India",
                    "source": "Asia Generic Registry Lookup",
                    "product": "paracetamol India generic regulatory registry lookup",
                },
            ]
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "CDSCO India")


class RegionalLiveConnectorTests(unittest.TestCase):
    def test_parses_regional_source_table_results(self):
        rows = parse_regional_source_results(
            """
            <table>
              <tr>
                <th>Product Name</th>
                <th>Active Ingredient</th>
                <th>Company</th>
                <th>Registration No</th>
                <th>Status</th>
              </tr>
              <tr>
                <td><a href="/product/1">Atorvastatin Example 20 mg tablet</a></td>
                <td>atorvastatin calcium</td>
                <td>Example Holder</td>
                <td>REG-123</td>
                <td>Registered</td>
              </tr>
            </table>
            """,
            "atorvastatin",
            REGIONAL_SOURCES["SFDA Saudi Arabia"],
            "https://www.sfda.gov.sa/en/drugs-list",
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "SFDA Saudi Arabia")
        self.assertEqual(rows[0]["country"], "Saudi Arabia")
        self.assertEqual(rows[0]["region"], "ME")
        self.assertEqual(rows[0]["company"], "Example Holder")
        self.assertEqual(rows[0]["registration_number"], "REG-123")
        self.assertEqual(rows[0]["strength"], "20 mg")
        self.assertEqual(rows[0]["dosage_form"], "Tablet")

    def test_regional_fallback_uses_stable_product_for_synonym_dedupe(self):
        from sources.regional_live import _fallback_rows

        rows = _fallback_rows(REGIONAL_SOURCES["CDSCO India"], "acetaminophen")
        self.assertEqual(rows[0]["product"], "acetaminophen official India registry search")
        self.assertEqual(rows[0]["connector_mode"], "manual_registry")

    def test_cdsco_india_search_delegates_to_approvals_connector(self):
        expected = [
            {
                "substance": "dapagliflozin",
                "product": "Dapagliflozin approval row",
                "country": "India",
                "source": "CDSCO India",
            }
        ]

        with patch("sources.regional_live._run_cdsco_india_search", return_value=expected) as connector:
            rows = run_cdsco_india_search("dapagliflozin")

        connector.assert_called_once_with("dapagliflozin")
        self.assertEqual(rows, expected)

    def test_cdsco_india_search_falls_back_when_no_approvals_match(self):
        with patch("sources.regional_live._run_cdsco_india_search", return_value=[]):
            rows = run_cdsco_india_search("acetaminophen")

        self.assertEqual(rows[0]["connector_mode"], "manual_registry")
        self.assertEqual(rows[0]["country"], "India")


class CDSCOIndiaConnectorTests(unittest.TestCase):
    PAYLOAD = {
        "iTotalRecords": 2,
        "aaData": [
            {
                "num_form_id": 39942,
                "str_man_unit_name": "PURE & CURE HEALTHCARE Pvt. Ltd.",
                "str_address": "305, Mohan Place, Saraswati Vihar, , Delhi, India, 110034",
                "str_drug_name": "Dapagliflozin+Metoprolol Succinate (Er)",
                "str_composition": "Dapagliflozin Propanediol Monohydrate Eq. To Dapagliflozin 10.0000 Milligram (Mg)",
                "manuf_addr": "Precise Chemipharma Pvt.Ltd, Navi Mumbai Maharashtra India-400703"
                "<br>Reine Lifescience, Ankleshwar Gujarat India-393002",
                "str_dosage": "Tablets",
                "str_indication": "Indicated In Patients With Heart Failure",
                "dt_closure_dt": "09-JAN-2026",
                "str_applied_for": "Finished Formulation",
            },
            {
                "num_form_id": 54683,
                "str_man_unit_name": "Serum Institute Of India Pvt. Ltd.",
                "str_address": "212/2, Hadapsar, , Maharashtra, India, 411028",
                "str_drug_name": "Hepatitis B Surface Antigen Concentrate Bulk",
                "str_composition": "Hepatitis B Protein 0.2000 Mg/Ml",
                "manuf_addr": "Serum Institute Of India Pvt Ltd.., Pune Maharashtra India-411028",
                "str_dosage": "NA",
                "str_indication": "NA",
                "dt_closure_dt": "13-AUG-2026",
                "str_applied_for": "Bulk Drug",
            },
        ],
    }

    def setUp(self):
        from sources import cdsco_india

        # The connector caches the corpus on disk and in memory; both have to be
        # bypassed so a real cache file cannot mask the payload under test.
        cdsco_india._MEMORY_CACHE.clear()
        self.addCleanup(cdsco_india._MEMORY_CACHE.clear)
        for target in ("_read_cache", "_write_cache"):
            patcher = patch.object(
                cdsco_india, target, return_value=None if target == "_read_cache" else None
            )
            patcher.start()
            self.addCleanup(patcher.stop)

    def _search(self, substance, payload=None):
        from sources import cdsco_india

        response = MagicMock()
        response.json.return_value = payload or self.PAYLOAD
        response.raise_for_status.return_value = None
        with patch.object(cdsco_india.requests, "get", return_value=response):
            return cdsco_india.run_cdsco_india_search(substance)

    def test_maps_company_and_supply_type(self):
        rows = self._search("dapagliflozin")

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["company"], "PURE & CURE HEALTHCARE Pvt. Ltd.")
        self.assertEqual(row["country"], "India")
        self.assertEqual(row["source"], "CDSCO India")
        self.assertEqual(row["status"], "Approved (finished formulation)")
        self.assertEqual(row["dosage_form"], "Tablets")
        self.assertEqual(row["registration_number"], "39942")

    def test_first_manufacturing_site_is_kept_apart_from_the_applicant(self):
        row = self._search("dapagliflozin")[0]

        self.assertEqual(row["manufacturer_name"], "Precise Chemipharma Pvt.Ltd, Navi Mumbai Maharashtra India-400703")
        self.assertEqual(row["manufacturer_country"], "India")

    def test_bulk_drug_approvals_are_labelled_as_api(self):
        row = self._search("hepatitis b")[0]

        self.assertEqual(row["company"], "Serum Institute Of India Pvt. Ltd.")
        self.assertEqual(row["status"], "Approved (bulk drug / API)")
        # "NA" placeholders must not leak into the output.
        self.assertEqual(row["therapeutic_category"], "")

    def test_ignores_rows_that_only_mention_the_substance_in_the_indication(self):
        payload = {
            "aaData": [
                {
                    "num_form_id": 1,
                    "str_man_unit_name": "Some Pharma Ltd",
                    "str_drug_name": "Ramipril Tablets",
                    "str_composition": "Ramipril 5mg",
                    "str_indication": "Used alongside dapagliflozin therapy",
                    "str_applied_for": "Finished Formulation",
                    "manuf_addr": "",
                    "str_dosage": "Tablets",
                    "dt_closure_dt": "01-JAN-2026",
                }
            ]
        }
        self.assertEqual(self._search("dapagliflozin", payload), [])

    def test_serves_repeat_searches_from_the_cached_corpus(self):
        from sources import cdsco_india

        response = MagicMock()
        response.json.return_value = self.PAYLOAD
        response.raise_for_status.return_value = None
        with patch.object(cdsco_india, "_read_cache", side_effect=[None, self.PAYLOAD["aaData"]]):
            with patch.object(cdsco_india.requests, "get", return_value=response) as fetch:
                cdsco_india.run_cdsco_india_search("dapagliflozin")
                cdsco_india.run_cdsco_india_search("hepatitis b")

        self.assertEqual(fetch.call_count, 1)

    def test_returns_empty_when_the_endpoint_is_unavailable(self):
        from sources import cdsco_india

        with patch.object(cdsco_india.requests, "get", side_effect=requests.RequestException("boom")):
            self.assertEqual(cdsco_india.run_cdsco_india_search("dapagliflozin"), [])


class TGAConnectorTests(unittest.TestCase):
    def test_parses_artg_search_results(self):
        rows = _parse_artg_search_results(
            """
            <a href="/resources/artg/528326">
              ADMED PARACETAMOL SUSPENSION FOR CHILDREN 1-5 YEARS paracetamol 24 mg/mL strawberry flavour oral suspension bottle (528326)
            </a>
            """,
            "paracetamol",
            "https://www.tga.gov.au/resources/artg?keywords=paracetamol",
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "TGA Australia")
        self.assertEqual(rows[0]["country"], "Australia")
        self.assertEqual(rows[0]["region"], "AU")
        self.assertEqual(rows[0]["registration_number"], "528326")
        self.assertEqual(rows[0]["strength"], "24 mg/mL")
        self.assertEqual(rows[0]["dosage_form"], "Suspension")
        self.assertEqual(rows[0]["product_url"], "https://www.tga.gov.au/resources/artg/528326")

    def test_parses_artg_detail_metadata(self):
        metadata = _parse_artg_detail(
            """
            <main>
              <div>Product name</div><div>Example paracetamol 500 mg tablet blister pack</div>
              <div>Sponsor</div><div>Example Pharma Pty Ltd</div>
              <div>Manufacturer</div><div>Example Manufacturing Pty Ltd</div>
              <div>Active ingredients</div><div>paracetamol</div>
              <div>Dosage form</div><div>Tablet</div>
              <div>Route of administration</div><div>Oral</div>
              <a href="/resources/product-information/example-pi.pdf">Product Information</a>
              <a href="/resources/consumer-medicine-information/example-cmi.pdf">Consumer Medicine Information</a>
            </main>
            """
        )

        self.assertEqual(metadata["product"], "Example paracetamol 500 mg tablet blister pack")
        self.assertEqual(metadata["company"], "Example Pharma Pty Ltd")
        self.assertEqual(metadata["manufacturer_name"], "Example Manufacturing Pty Ltd")
        self.assertEqual(metadata["substance"], "paracetamol")
        self.assertEqual(metadata["dosage_form"], "Tablet")
        self.assertEqual(metadata["route"], "Oral")
        self.assertEqual(metadata["smpc_url"], "https://www.tga.gov.au/resources/product-information/example-pi.pdf")
        self.assertEqual(metadata["pil_url"], "https://www.tga.gov.au/resources/consumer-medicine-information/example-cmi.pdf")

    def test_merges_tga_detail_without_losing_search_values(self):
        row = {
            "product": "Example paracetamol 500 mg tablet blister pack",
            "company": "",
            "strength": "",
        }
        merged = _merge_detail(row, {"company": "Example Pharma Pty Ltd"})
        self.assertEqual(merged["company"], "Example Pharma Pty Ltd")
        self.assertEqual(merged["strength"], "500 mg")

    def test_tga_fallback_returns_registry_handoff_for_paracetamol(self):
        rows = _fallback_rows("paracetamol", 10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["country"], "Australia")
        self.assertEqual(rows[0]["source"], "TGA Australia")
        self.assertEqual(rows[0]["connector_mode"], "manual_registry")

    def test_tga_fallback_returns_lookup_row_for_any_substance(self):
        rows = _fallback_rows("metformin", 10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["country"], "Australia")
        self.assertEqual(rows[0]["source"], "TGA Australia")
        self.assertEqual(rows[0]["document_type"], "Official registry search handoff")
        self.assertEqual(rows[0]["connector_mode"], "manual_registry")
        self.assertIn("metformin", rows[0]["product"].lower())


class MedsafeConnectorTests(unittest.TestCase):
    def test_parses_medsafe_product_search_results(self):
        rows = _parse_medsafe_product_search_results(
            """
            <table>
              <tr>
                <th>Trade Name</th>
                <th>Ingredient</th>
                <th>Sponsor</th>
                <th>Classification</th>
                <th>Status</th>
                <th>Approval date</th>
              </tr>
              <tr>
                <td><a href="/DbSearch/DrugDetails/123">Paracetamol Example 500 mg tablet</a></td>
                <td>paracetamol</td>
                <td>Example Pharma NZ Limited</td>
                <td>General sale</td>
                <td>Consent given</td>
                <td>1 Jan 2024</td>
              </tr>
            </table>
            """,
            "paracetamol",
            "https://www.medsafe.govt.nz/DbSearch/",
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "Medsafe New Zealand")
        self.assertEqual(rows[0]["country"], "New Zealand")
        self.assertEqual(rows[0]["region"], "NZ")
        self.assertEqual(rows[0]["company"], "Example Pharma NZ Limited")
        self.assertEqual(rows[0]["status"], "Consent given - General sale")
        self.assertEqual(rows[0]["strength"], "500 mg")
        self.assertEqual(rows[0]["dosage_form"], "Tablet")
        self.assertEqual(rows[0]["registration_date"], "1 Jan 2024")

    def test_medsafe_fallback_returns_lookup_row_for_any_substance(self):
        rows = _medsafe_fallback_rows("metformin", 10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["country"], "New Zealand")
        self.assertEqual(rows[0]["region"], "NZ")
        self.assertEqual(rows[0]["source"], "Medsafe New Zealand")
        self.assertEqual(rows[0]["document_type"], "Official registry search handoff")
        self.assertEqual(rows[0]["connector_mode"], "manual_registry")
        self.assertIn("metformin", rows[0]["product"].lower())


class ConnectorStatusTests(unittest.TestCase):
    def test_core_connector_status_contains_step_one_sources(self):
        rows = connector_status_rows()
        names = {row["name"] for row in rows}

        for source in [
            "FDA",
            "MHRA",
            "EMA",
            "Health Canada",
            "TGA Australia",
            "Medsafe New Zealand",
            "Hong Kong Drug Office",
            "GRLS Russia",
        ]:
            self.assertIn(source, names)

    def test_manual_or_browser_sources_are_explicitly_marked(self):
        rows = {row["name"]: row for row in connector_status_rows()}

        self.assertEqual(rows["GRLS Russia"]["status"], "ready_with_browser_permission")
        self.assertEqual(rows["TGA Australia"]["status"], "partial")
        self.assertEqual(rows["Medsafe New Zealand"]["status"], "partial")

    def test_nmpa_china_is_explicitly_marked_parser_needed(self):
        rows = {row["name"]: row for row in connector_status_rows()}

        self.assertEqual(rows["NMPA China"]["status"], "manual_parser_needed")
        self.assertEqual(rows["NMPA China"]["mode"], "official_registry_handoff")

    def test_nmpa_china_returns_registry_handoff_not_fake_product_rows(self):
        rows = run_nmpa_china_search("metformin")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "NMPA China")
        self.assertEqual(rows[0]["country"], "China")
        self.assertEqual(rows[0]["connector_mode"], "manual_registry")
        self.assertIn("parser", rows[0]["status"].lower())

    def test_connector_health_records_last_source_result(self):
        record_source_health("NMPA China", "timeout", 0, 10.2, "timeout")
        rows = {row["name"]: row for row in connector_health_rows()}

        self.assertEqual(rows["NMPA China"]["last_status"], "timeout")
        self.assertEqual(rows["NMPA China"]["last_records"], 0)
        self.assertEqual(rows["NMPA China"]["last_error"], "timeout")


class EnglishNormalizerTests(unittest.TestCase):
    def test_translates_known_regulatory_values_to_english(self):
        self.assertEqual(english_text("Д"), "Active")
        self.assertEqual(english_text("Бетмига"), "Betmiga")
        self.assertEqual(english_text("Астеллас Фарма Юроп Б.В."), "Astellas Pharma Europe B.V.")
        self.assertEqual(english_text("Нидерланды"), "Netherlands")

    def test_normalizes_row_fields_for_display_and_export(self):
        row = english_row(
            {
                "product": "Бетмига",
                "company": "Астеллас Фарма Юроп Б.В.",
                "manufacturer_country": "Нидерланды",
                "source_substance": "Мирабегрон",
                "dosage_form": "таблетки пролонгированного высвобождения",
            }
        )

        self.assertEqual(row["product"], "Betmiga")
        self.assertEqual(row["company"], "Astellas Pharma Europe B.V.")
        self.assertEqual(row["manufacturer_country"], "Netherlands")
        self.assertEqual(row["source_substance"], "Mirabegron")
        self.assertIn("tablets", row["dosage_form"])

    def test_marks_non_latin_values_that_need_ai_translation(self):
        self.assertEqual(
            english_text("药品"),
            "Local-language registry value: 药品",
        )


class AIEnrichmentTests(unittest.TestCase):
    def test_marks_complete_connector_row_as_high_confidence(self):
        metadata = enrichment_metadata(
            {
                "substance": "Example",
                "product": "Example 10 mg tablets",
                "company": "Example Holder Ltd",
                "country": "United Kingdom",
                "source": "MHRA",
                "registration_number": "PL 12345/0001",
                "registration_date": "1 Jan 2024",
                "manufacturer_name": "Example Manufacturer Ltd",
                "manufacturer_country": "Germany",
                "pack_size": "28 tablets",
                "product_url": "https://example.test/product",
                "smpc_url": "https://example.test/spc.pdf",
                "pil_url": "https://example.test/pil.pdf",
                "assessment_report_url": "https://example.test/par.pdf",
            }
        )

        self.assertEqual(metadata["data_confidence"], "High")
        self.assertEqual(metadata["missing_fields"], "None")

    def test_marks_manual_registry_rows_for_review(self):
        metadata = enrichment_metadata(
            {
                "connector_mode": "manual_registry",
                "product": "example official registry search",
                "country": "Exampleland",
                "source": "Example Registry",
            }
        )

        self.assertEqual(metadata["data_confidence"], "Needs manual review")
        self.assertIn("Product record not extracted", metadata["missing_fields"])

    def test_missing_fields_are_generic(self):
        fields = missing_enrichment_fields(
            {
                "product": "Example 5 mg tablets",
                "country": "Canada",
                "source": "Health Canada",
            }
        )

        self.assertIn("MA Holder", fields)
        self.assertIn("Manufacturer Name", fields)
        self.assertIn("SmPC URL", fields)

    def test_ai_client_is_safe_when_api_key_missing(self):
        original_key = os.environ.pop("OPENAI_API_KEY", None)
        try:
            self.assertFalse(ai_status()["api_key_configured"])
            self.assertEqual(ai_extract_regulatory_fields({"product": "Example"}, "Example text"), {})
        finally:
            if original_key is not None:
                os.environ["OPENAI_API_KEY"] = original_key

    def test_ai_enriched_rows_are_labelled(self):
        metadata = enrichment_metadata(
            {
                "substance": "Example",
                "product": "Example 10 mg tablets",
                "company": "Example Holder Ltd",
                "country": "United Kingdom",
                "source": "MHRA",
                "product_url": "https://example.test/product",
                "registration_number": "PL 12345/0001",
                "manufacturer_name": "Example Manufacturer Ltd",
                "manufacturer_country": "Germany",
                "smpc_url": "https://example.test/spc.pdf",
                "ai_enriched": "true",
                "ai_confidence": "High",
            }
        )

        self.assertEqual(metadata["data_confidence"], "High")
        self.assertIn("AI extracted", metadata["enrichment_status"])


class SearchJobTests(unittest.TestCase):
    def test_background_job_orders_fast_sources_before_slow_sources(self):
        sources = order_sources_for_job(["GRLS Russia", "FDA", "Health Canada"])
        self.assertEqual(sources[:2], ["FDA", "Health Canada"])
        self.assertIn("GRLS Russia", SLOW_SOURCES)

    def test_fast_background_job_skips_heavy_sources(self):
        self.assertTrue(source_skipped_in_mode("GRLS Russia", "fast"))
        self.assertFalse(source_skipped_in_mode("FDA", "fast"))

    def test_full_background_job_keeps_heavy_sources_queued(self):
        self.assertFalse(source_skipped_in_mode("GRLS Russia", "full"))

    def test_dedupes_job_rows_by_source_product_country_registration_and_url(self):
        rows = _dedupe_rows(
            [
                {
                    "source": "FDA",
                    "product": "Example",
                    "country": "United States",
                    "registration_number": "1",
                    "url": "https://example.test",
                },
                {
                    "source": "FDA",
                    "product": "Example",
                    "country": "United States",
                    "registration_number": "1",
                    "url": "https://example.test",
                },
            ]
        )

        self.assertEqual(len(rows), 1)


class SearchJobPersistenceTests(unittest.TestCase):
    def test_persists_search_job_progress_and_results(self):
        job_id = "test_job_persist"
        save_search_job(
            {
                "job_id": job_id,
                "substance": "metformin",
                "sources": ["FDA"],
                "mode": "fast",
                "status": "done",
                "created_at": "2026-01-01T00:00:00",
                "started_at": "2026-01-01T00:00:01",
                "finished_at": "2026-01-01T00:00:02",
            }
        )
        save_search_job_progress(
            job_id,
            {
                "source": "FDA",
                "status": "done",
                "records": 1,
                "error": "",
                "started_at": "2026-01-01T00:00:01",
                "finished_at": "2026-01-01T00:00:02",
            },
        )
        save_search_job_results(
            job_id,
            [
                {
                    "substance": "metformin",
                    "product": "Metformin 500 mg tablets",
                    "country": "United States",
                    "source": "FDA",
                    "url": "https://example.test/metformin",
                }
            ],
        )

        job = get_persisted_search_job(job_id)
        results = get_persisted_search_job_results(job_id)

        self.assertEqual(job["substance"], "metformin")
        self.assertEqual(job["record_count"], 1)
        self.assertEqual(job["progress"][0]["source"], "FDA")
        self.assertEqual(results[0]["product"], "Metformin 500 mg tablets")


class TherapeuticCategoryTests(unittest.TestCase):
    def test_shortens_long_indication_to_generic_category(self):
        category = short_therapeutic_category(
            "Quetiapine is indicated for the treatment of schizophrenia and bipolar disorder in adults.",
            "quetiapine",
            "",
        )

        self.assertEqual(category, "Antipsychotic")

    def test_uses_substance_category_when_available(self):
        self.assertEqual(short_therapeutic_category("", "mirabegron", ""), "Overactive bladder medicine")

    def test_uses_atc_category_when_available(self):
        self.assertEqual(short_therapeutic_category("", "", "N02BE01"), "Analgesic")


class HarvestVocabularyTests(unittest.TestCase):
    def test_reduces_a_registry_substance_string_to_its_molecule(self):
        self.assertEqual(
            normalize_molecule(
                "Dapagliflozin Propanediol Monohydrate IP eq. To Dapagliflozin 10.0000 Milligram (Mg)"
            ),
            "dapagliflozin",
        )

    def test_strips_a_trailing_salt(self):
        self.assertEqual(normalize_molecule("DICLOFENAC SODIUM"), "diclofenac")
        self.assertEqual(normalize_molecule("ATORVASTATIN CALCIUM"), "atorvastatin")

    def test_keeps_a_salt_that_is_itself_the_molecule(self):
        self.assertEqual(normalize_molecule("SODIUM CHLORIDE"), "sodium chloride")
        self.assertEqual(normalize_molecule("MAGNESIUM SULFATE"), "magnesium sulfate")

    def test_rejects_containers_and_excipients(self):
        for value in ("Vial", "Water For Injection", "Tablets", "Polysorbate 80"):
            self.assertEqual(normalize_molecule(value), "", value)

    def test_splits_a_fixed_dose_combination_into_its_molecules(self):
        self.assertEqual(
            split_combination("Amoxycillin Trihydrate Ip Eq. To Amoxycillin+Vonoprazan Fumarate"),
            ["amoxycillin", "vonoprazan"],
        )

    def test_ranks_multi_registry_molecules_before_high_count_singletons(self):
        accumulator = _Accumulator()
        # A US-only monograph ingredient with an enormous label count.
        accumulator.add("ZINC OXIDE", "openfda", evidence=4148)
        # A prescription molecule attested by two registries.
        accumulator.add("METFORMIN", "openfda", evidence=200)
        accumulator.add("Metformin Hydrochloride IP", "cdsco")

        self.assertEqual(
            [entry["molecule"] for entry in accumulator.entries()],
            ["metformin", "zinc oxide"],
        )


class _StubConnector:
    """Stands in for a registry connector in harvest tests."""

    def __init__(self, name, rows=None, error=None):
        self.metadata = SourceMetadata(
            name=name, region="EU", countries=("Germany",), rate_limit_per_minute=6000
        )
        self._rows = rows or []
        self._error = error
        self.calls = []

    def search(self, substance):
        self.calls.append(substance)
        if self._error:
            raise self._error
        return [dict(row) for row in self._rows]


class HarvestRunnerTests(unittest.TestCase):
    def setUp(self):
        self.persisted = []
        persist = patch(
            "services.harvest.save_product_detail",
            side_effect=lambda record: self.persisted.append(record) or record,
        )
        # Progress is a database write; the runner's logic is what is under test.
        progress = patch("services.harvest.record_progress")
        tables = patch("services.harvest.initialize_harvest_tables")
        self.record_progress = progress.start()
        persist.start()
        tables.start()
        self.addCleanup(patch.stopall)

    def test_harvests_every_molecule_and_stores_the_rows(self):
        connector = _StubConnector("Stub", rows=[{"product": "Glucophage", "source": "Stub"}])

        with patch("services.harvest.CONNECTORS", [connector]), patch(
            "services.harvest.completed_pairs", return_value=set()
        ):
            result = run_harvest(molecules=["metformin", "atorvastatin"])

        self.assertEqual(connector.calls, ["metformin", "atorvastatin"])
        self.assertEqual(result.rows, 2)
        # A connector row without a substance is filed under the molecule that
        # produced it, or the harvested row could never be found again.
        self.assertEqual([row["substance"] for row in self.persisted], ["metformin", "atorvastatin"])

    def test_skips_pairs_already_harvested(self):
        connector = _StubConnector("Stub", rows=[{"product": "Glucophage"}])

        with patch("services.harvest.CONNECTORS", [connector]), patch(
            "services.harvest.completed_pairs", return_value={("Stub", "metformin")}
        ):
            result = run_harvest(molecules=["metformin", "atorvastatin"])

        self.assertEqual(connector.calls, ["atorvastatin"])
        self.assertEqual(result.outcomes["Stub"].skipped, 1)

    def test_one_failing_registry_does_not_stop_the_others(self):
        broken = _StubConnector("Broken", error=requests.RequestException("down"))
        working = _StubConnector("Working", rows=[{"product": "Glucophage"}])

        with patch("services.harvest.CONNECTORS", [broken, working]), patch(
            "services.harvest.completed_pairs", return_value=set()
        ):
            result = run_harvest(molecules=["metformin", "atorvastatin"])

        self.assertEqual(result.outcomes["Broken"].failures, 2)
        self.assertEqual(result.outcomes["Broken"].rows, 0)
        self.assertEqual(result.outcomes["Working"].rows, 2)

    def test_stops_a_registry_that_has_gone_down(self):
        broken = _StubConnector("Broken", error=requests.RequestException("down"))
        molecules = [f"molecule-{index}" for index in range(CONSECUTIVE_FAILURE_LIMIT + 5)]

        with patch("services.harvest.CONNECTORS", [broken]), patch(
            "services.harvest.completed_pairs", return_value=set()
        ):
            result = run_harvest(molecules=molecules)

        outcome = result.outcomes["Broken"]
        self.assertTrue(outcome.stopped_early)
        self.assertEqual(len(broken.calls), CONSECUTIVE_FAILURE_LIMIT)
        # The molecules never attempted stay unrecorded, so the next run retries
        # them rather than treating a dead registry as covered.
        recorded = {call.args[1] for call in self.record_progress.call_args_list}
        self.assertEqual(recorded, set(molecules[:CONSECUTIVE_FAILURE_LIMIT]))


class FieldCompletionTests(unittest.TestCase):
    def _group(self, *rows):
        return {row_id: changes for row_id, changes in _completions_for_group(list(rows))}

    def test_lends_a_molecule_level_code_to_a_registry_that_omits_it(self):
        ema = {"id": 1, "source": "EMA", "substance": "metformin", "atc_code": "A10BA02"}
        cdsco = {"id": 2, "source": "CDSCO India", "substance": "Metformin Hydrochloride IP"}

        changes = self._group(ema, cdsco)

        self.assertEqual(changes[2]["atc_code"], "A10BA02")
        self.assertEqual(changes[2]["completion_source"], "EMA")
        # The lender keeps its own code and gains nothing from itself.
        self.assertNotIn("atc_code", changes.get(1, {}))

    def test_groups_a_molecule_across_languages_and_registers(self):
        # France files IBUPROFÈNE and the Latin INN keeps a final "e" English
        # drops, so grouping on the plain name leaves the French rows with
        # nobody to lend them a document.
        for french, english in (
            ("IBUPROFÈNE", "ibuprofen"),
            ("AMOXICILLINE, AMOXICILLINE TRIHYDRATE", "amoxicillin"),
            ("ATORVASTATINE, ATORVASTATINE CALCIUM", "ATORVASTATIN CALCIUM"),
            ("CHLORHYDRATE DE METFORMINE, METFORMINE", "metformin"),
            ("ÉSOMÉPRAZOLE, ÉSOMÉPRAZOLE MAGNÉSIQUE", "esomeprazole"),
        ):
            self.assertEqual(
                molecule_group_key(french), molecule_group_key(english), french
            )

    def test_groups_a_semicolon_combination_under_its_first_molecule(self):
        self.assertEqual(
            molecule_group_key("sitagliptin;metformin hydrochloride"),
            molecule_group_key("Sitagliptin"),
        )

    def test_lends_stored_documents_to_a_live_row(self):
        # A live search builds rows from the connectors, so they carry no
        # completion at all even when the database holds the molecule.
        live_row = {"source": "CDSCO India", "substance": "QUÉTIAPINE"}
        stored = [
            {
                "substance_key": "quetiapin",
                "source": "MHRA",
                "product": "Seroquel 25mg",
                "smpc_url": "https://mhra/seroquel-smpc",
                "pil_url": "",
                "assessment_report_url": "",
            }
        ]

        connection = MagicMock()
        connection.execute.return_value.fetchall.return_value = stored
        with patch("services.field_completion.get_connection") as get_connection:
            get_connection.return_value.__enter__.return_value = connection
            rows = attach_reference_documents([live_row])

        self.assertEqual(rows[0]["reference_smpc_url"], "https://mhra/seroquel-smpc")
        self.assertEqual(rows[0]["reference_source"], "MHRA")
        # Nothing is written into the row's own column, in memory or otherwise.
        self.assertNotIn("smpc_url", rows[0])

    def test_does_not_overwrite_a_live_rows_own_document(self):
        live_row = {"source": "MHRA", "substance": "quetiapine", "smpc_url": "https://mhra/own"}

        with patch("services.field_completion.get_connection") as get_connection:
            rows = attach_reference_documents([live_row])

        get_connection.assert_not_called()
        self.assertEqual(rows[0]["smpc_url"], "https://mhra/own")
        self.assertNotIn("reference_smpc_url", rows[0])

    def test_keeps_different_molecules_apart(self):
        self.assertNotEqual(molecule_group_key("metformin"), molecule_group_key("metoprolol"))
        self.assertNotEqual(molecule_group_key("ibuprofen"), molecule_group_key("naproxen"))

    def test_derives_the_category_instead_of_lending_another_registrys_wording(self):
        # Romania states the ATC class in Romanian and would win a majority
        # vote; lending that text would put it on every other country's rows.
        romania = {
            "id": 1,
            "source": "ANMDMR Romania",
            "substance": "metformin",
            "atc_code": "A10BA02",
            "therapeutic_category": "Medicamente De Scadere A Glucozei Din Sang, Excl. Insuline",
        }
        cdsco = {"id": 2, "source": "CDSCO India", "substance": "metformin"}

        changes = self._group(romania, cdsco)

        self.assertEqual(changes[2]["therapeutic_category"], "Antidiabetic")
        # And the Romanian regulator's own text is left on the Romanian row.
        self.assertNotIn(
            "Medicamente",
            " ".join(value for row in changes.values() for value in row.values()),
        )

    def test_takes_the_code_most_registries_agree_on(self):
        rows = [
            {"id": 1, "source": "EMA", "substance": "metformin", "atc_code": "A10BA02"},
            {"id": 2, "source": "MHRA", "substance": "metformin", "atc_code": "A10BA02"},
            # A single mis-parsed value must not be lent to the whole molecule.
            {"id": 3, "source": "Thai FDA", "substance": "metformin", "atc_code": "XXXX"},
            {"id": 4, "source": "CDSCO India", "substance": "metformin"},
        ]

        self.assertEqual(self._group(*rows)[4]["atc_code"], "A10BA02")

    def test_never_writes_another_countrys_document_into_the_products_own_column(self):
        ema = {
            "id": 1,
            "source": "EMA",
            "substance": "metformin",
            "product": "Glucophage",
            "smpc_url": "https://ema.europa.eu/glucophage-smpc",
        }
        cdsco = {"id": 2, "source": "CDSCO India", "substance": "metformin"}

        changes = self._group(ema, cdsco)[2]

        self.assertNotIn("smpc_url", changes)
        self.assertEqual(changes["reference_smpc_url"], "https://ema.europa.eu/glucophage-smpc")
        self.assertEqual(changes["reference_source"], "EMA")
        self.assertEqual(changes["reference_product"], "Glucophage")

    def test_never_lends_an_assessment_report(self):
        # An assessment report evaluates one licence application -- it is
        # headed with that procedure and licence number -- so it describes a
        # decision about another country's authorisation, not this medicine.
        ema = {
            "id": 1,
            "source": "EMA",
            "substance": "metformin",
            "product": "Glucophage",
            "smpc_url": "https://ema/smpc",
            "assessment_report_url": "https://ema/glucophage-assessment",
        }
        cdsco = {"id": 2, "source": "CDSCO India", "substance": "metformin"}

        changes = self._group(ema, cdsco)[2]

        self.assertEqual(changes["reference_smpc_url"], "https://ema/smpc")
        self.assertNotIn("reference_assessment_report_url", changes)
        self.assertNotIn("assessment_report_url", changes)

    def test_leaves_a_row_that_has_its_own_document_alone(self):
        ema = {"id": 1, "source": "EMA", "substance": "metformin", "smpc_url": "https://ema/one"}
        mhra = {"id": 2, "source": "MHRA", "substance": "metformin", "smpc_url": "https://mhra/two"}

        changes = self._group(ema, mhra)

        self.assertNotIn("reference_smpc_url", changes.get(2, {}))

    def test_lends_from_the_same_dosage_form(self):
        # Triamcinolone is a nasal spray in one country and an injectable
        # suspension in another; the injection's assessment report describes a
        # different medicine and must not be lent to the spray.
        injection = {
            "id": 1,
            "source": "MHRA",
            "substance": "triamcinolone",
            "product": "TRIAMCINOLONE HEXACETONIDE SUSPENSION FOR INJECTION",
            "dosage_form": "Suspension for injection",
            "smpc_url": "https://mhra/injection-smpc",
            "pil_url": "https://mhra/injection-pil",
            "assessment_report_url": "https://mhra/injection-par",
        }
        spray = {
            "id": 2,
            "source": "MHRA",
            "substance": "triamcinolone",
            "product": "NASACORT ALLERGY NASAL SPRAY",
            "dosage_form": "Nasal spray",
            "smpc_url": "https://mhra/spray-smpc",
        }
        us_spray = {
            "id": 3,
            "source": "FDA",
            "substance": "triamcinolone",
            "product": "Good Sense Nasal Allergy",
            "dosage_form": "Spray",
        }

        changes = self._group(injection, spray, us_spray)[3]

        self.assertEqual(changes["reference_smpc_url"], "https://mhra/spray-smpc")
        self.assertEqual(changes["reference_product"], "NASACORT ALLERGY NASAL SPRAY")

    def test_lends_from_the_same_salt(self):
        # Kenalog is triamcinolone acetonide and Aristospan is hexacetonide:
        # same molecule, same injectable suspension, different medicines. The
        # FDA states the salt in substance_name while the product is a brand.
        hexacetonide = {
            "id": 1,
            "source": "MHRA",
            "substance": "Triamcinolone",
            "product": "TRIAMCINOLONE HEXACETONIDE 20MG/ML SUSPENSION FOR INJECTION",
            "dosage_form": "Suspension for injection",
            "smpc_url": "https://mhra/hexacetonide-smpc",
            "pil_url": "https://mhra/hexacetonide-pil",
        }
        acetonide = {
            "id": 2,
            "source": "MHRA",
            "substance": "Triamcinolone",
            "product": "KENALOG INTRA-ARTICULAR TRIAMCINOLONE ACETONIDE",
            "dosage_form": "Suspension for injection",
            "smpc_url": "https://mhra/acetonide-smpc",
        }
        kenalog = {
            "id": 3,
            "source": "FDA",
            "substance": "triamcinolone",
            "source_substance": "TRIAMCINOLONE ACETONIDE",
            "product": "KENALOG-10",
            "dosage_form": "Suspension",
        }

        changes = self._group(hexacetonide, acetonide, kenalog)[3]

        self.assertEqual(changes["reference_smpc_url"], "https://mhra/acetonide-smpc")

    def test_prefers_the_regulator_that_publishes_a_full_document_set(self):
        stray = {"id": 1, "source": "Thai FDA", "substance": "metformin", "pil_url": "https://thai/pil"}
        ema = {
            "id": 2,
            "source": "EMA",
            "substance": "metformin",
            "smpc_url": "https://ema/smpc",
            "pil_url": "https://ema/pil",
        }
        cdsco = {"id": 3, "source": "CDSCO India", "substance": "metformin"}

        changes = self._group(stray, ema, cdsco)[3]

        self.assertEqual(changes["reference_source"], "EMA")
        self.assertEqual(changes["reference_pil_url"], "https://ema/pil")


if __name__ == "__main__":
    unittest.main()
