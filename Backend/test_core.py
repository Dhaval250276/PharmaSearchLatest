import unittest
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests
import repository
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
from services.evidence_assertions import missing_reason_for_row
from services.evidence_backfill import backfill_evidence
from sources.ema import _ema_result_from_record, _expand_xlsx_records
from sources.synonyms import get_substance_search_terms
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
    complete_fields,
    names_two_strengths,
)
from services.harvest import CONSECUTIVE_FAILURE_LIMIT, run_harvest
from services.harvest_vocabulary import (
    _Accumulator,
    is_combination,
    molecule_group_key,
    row_molecule_key,
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
from services.field_availability import (
    NOT_COLLECTED,
    NOT_SUPPLIED,
    NOT_SUPPLIED_LABEL,
    PENDING_ENRICHMENT,
    field_value,
)
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


class StructuredEvidenceRepositoryTests(unittest.TestCase):
    def test_persists_multiple_sites_documents_and_field_evidence(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            repository, "DB_PATH", Path(directory) / "test.db"
        ):
            repository.save_product_detail(
                {
                    "substance": "example",
                    "product": "Example 10 mg tablets",
                    "country": "Exampleland",
                    "source": "Example Regulator",
                    "registration_number": "EX-1",
                    "product_url": "https://regulator.test/products/1",
                    "smpc_url": "https://regulator.test/products/1/smpc.pdf",
                    "manufacturers": [
                        {
                            "name": "Example Manufacturing Ltd",
                            "address": "Site One",
                            "country": "Germany",
                            "role": "FINISHED_PRODUCT_MANUFACTURER",
                            "verification_status": "VERIFIED_OFFICIAL_DOCUMENT",
                        },
                        {
                            "name": "Example Release GmbH",
                            "address": "Site Two",
                            "country": "Germany",
                            "role": "BATCH_RELEASE_MANUFACTURER",
                            "verification_status": "VERIFIED_OFFICIAL_DOCUMENT",
                        },
                    ],
                    "evidence": [{
                        "field_name": "manufacturer_name",
                        "value": "Example Manufacturing Ltd",
                        "evidence_url": "https://regulator.test/products/1/smpc.pdf",
                        "evidence_page": "42",
                        "verification_status": "VERIFIED_OFFICIAL_DOCUMENT",
                    }],
                }
            )
            row = repository.search_product_details("example")[0]
            self.assertEqual(len(row["manufacturers"]), 2)
            self.assertEqual(row["documents"][0]["document_type"], "SMPC")
            self.assertEqual(row["evidence"][0]["evidence_page"], "42")

    def test_registry_handoff_is_not_persisted_as_a_product(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            repository, "DB_PATH", Path(directory) / "test.db"
        ):
            result = repository.save_product_detail(
                {
                    "substance": "example",
                    "product": "Example official registry search",
                    "source": "Example Regulator",
                    "connector_mode": "manual_registry",
                }
            )
            self.assertEqual(result["persistence_status"], "SOURCE_RUN_ONLY")
            self.assertEqual(repository.search_product_details("example"), [])


class FieldAvailabilityTests(unittest.TestCase):
    def test_missing_document_is_not_claimed_unavailable_without_an_attempt(self):
        self.assertEqual(field_value({"source": "FDA"}, "smpc_url"), NOT_SUPPLIED_LABEL)

    def test_manufacturer_is_not_pending_for_fda_schema(self):
        row = {"source": "FDA", "product_url": "https://example.test/label"}
        self.assertEqual(field_value(row, "manufacturer_name"), NOT_COLLECTED)

    def test_manufacturer_is_not_pending_for_regional_api_schema(self):
        row = {"source": "BPOM Indonesia", "product_url": "https://example.test/product"}
        self.assertEqual(field_value(row, "manufacturer_name"), NOT_COLLECTED)

    def test_missing_mhra_manufacturer_is_marked_for_enrichment(self):
        row = {"source": "MHRA", "pil_url": "https://example.test/pil.pdf"}
        self.assertEqual(field_value(row, "manufacturer_name"), PENDING_ENRICHMENT)

    def test_missing_mhra_manufacturer_is_not_pending_after_document_parse(self):
        row = {
            "source": "MHRA",
            "pil_url": "https://example.test/pil.pdf",
            "document_enrichment_attempted": True,
        }
        self.assertEqual(field_value(row, "manufacturer_name"), NOT_SUPPLIED_LABEL)

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

        self.assertEqual(len(header_rows[0].select("th")), 26)
        filter_cells = header_rows[1].select("th")
        self.assertEqual(len(filter_cells), 26)
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
        # Brazil was the example here until ANVISA gave it a connector and its
        # own region; Argentina still has neither.
        rows = _country_lookup_rows("atorvastatin", country="Argentina")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["country"], "Argentina")
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

    def test_all_manufacturing_sites_are_kept_apart_from_the_applicant(self):
        row = self._search("dapagliflozin")[0]

        self.assertEqual(row["manufacturer_name"], "PURE & CURE HEALTHCARE Pvt. Ltd.")
        self.assertEqual(row["manufacturer_country"], "India")
        self.assertEqual(len(row["manufacturers"]), 2)
        self.assertEqual(
            row["manufacturers"][0]["address"],
            "Precise Chemipharma Pvt.Ltd, Navi Mumbai Maharashtra India-400703",
        )
        self.assertEqual(row["manufacturers"][0]["role"], "FINISHED_PRODUCT_MANUFACTURER")

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
        self.assertEqual(row["dosage_form"], "Prolonged-release tablet")

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
        # Health Canada was the quick source here until it was measured at
        # ~19s and moved to SLOW_SOURCES; Spain CIMA answers in ~5s.
        sources = order_sources_for_job(["GRLS Russia", "FDA", "Spain CIMA"])
        self.assertEqual(sources[:2], ["FDA", "Spain CIMA"])
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

    def test_never_guesses_a_category_from_the_molecule_name(self):
        # A category no regulator published for this product is not shown.
        self.assertEqual(short_therapeutic_category("", "mirabegron", ""), "")

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

    def test_keys_a_combination_on_all_its_molecules_in_any_order(self):
        # Keyed on its first molecule, Janumet sat in metformin's group and
        # lent or borrowed metformin's ATC code.
        self.assertEqual(molecule_group_key("sitagliptin;metformin hydrochloride"), "metformin+sitagliptin")
        self.assertEqual(
            molecule_group_key("METFORMIN HYDROCHLORIDE, SITAGLIPTIN PHOSPHATE MONOHYDRATE"),
            molecule_group_key("sitagliptin / metformin"),
        )
        self.assertNotEqual(molecule_group_key("sitagliptin;metformin"), molecule_group_key("metformin"))

    def test_a_molecule_listed_beside_its_own_salt_is_still_one_molecule(self):
        self.assertFalse(is_combination("ATORVASTATINE, ATORVASTATINE CALCIUM"))
        self.assertFalse(is_combination("\u00c9SOM\u00c9PRAZOLE, \u00c9SOM\u00c9PRAZOLE MAGN\u00c9SIQUE"))
        self.assertTrue(is_combination("VALSARTAN, AMLODIPINE BESYLATE"))

    def test_a_searched_row_is_keyed_on_what_the_registry_says_it_contains(self):
        # A search stores the typed term as the substance.
        janumet = {"substance": "metformin", "source_substance": "SITAGLIPTIN; METFORMIN HYDROCHLORIDE"}
        italian = {"substance": "metformin", "source_substance": "METFORMINA CLORIDRATO"}
        self.assertEqual(row_molecule_key(janumet), "metformin+sitagliptin")
        # A single molecule keeps the searched name, which is what groups it
        # across the registries' languages.
        self.assertEqual(row_molecule_key(italian), "metformin")

    def test_a_combination_and_its_molecule_lend_each_other_nothing(self):
        rows = [
            {"id": 1, "source": "EMA", "substance": "metformin", "atc_code": "A10BA02"},
            {"id": 2, "source": "EMA", "substance": "metformin", "atc_code": "A10BA02"},
            {"id": 3, "source": "MHRA", "substance": "metformin",
             "source_substance": "SITAGLIPTIN, METFORMIN HYDROCHLORIDE", "atc_code": ""},
            {"id": 4, "source": "EMA", "substance": "sitagliptin;metformin", "atc_code": "A10BD07"},
            {"id": 5, "source": "Spain CIMA", "substance": "metformin", "atc_code": "",
             "product": "EUCREAS 50 MG/850 MG COMPRIMIDOS"},
        ]
        groups = {}
        for row in rows:
            groups.setdefault(row_molecule_key(row), []).append(row)
        changes = {}
        for group in groups.values():
            changes.update({row_id: change for row_id, change in _completions_for_group(group)})

        self.assertEqual(changes[3]["atc_code"], "A10BD07")
        # A combination the registry did not state is recognised by its pair
        # of strengths and not given the molecule's code.
        self.assertNotIn("atc_code", changes.get(5, {}))

    def test_a_pair_of_strengths_names_a_combination(self):
        self.assertTrue(names_two_strengths("FENZIL 10 MG/160 MG COMPRIMIDOS"))
        self.assertTrue(names_two_strengths("Co-codamol 15mg/500mg Tablets"))
        self.assertFalse(names_two_strengths("Calpol 120mg/5ml Infant Oral Suspension"))
        self.assertFalse(names_two_strengths("Atorvastatin 10 mg/20 mg/40 mg Tablets"))
        self.assertFalse(names_two_strengths("Metformin 500 mg tablets"))

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


class ExportSpeedTests(unittest.TestCase):
    """Saving a large search's rows is what made its export take an hour."""

    def test_the_schema_is_checked_once_per_database_not_once_per_call(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            repository, "DB_PATH", Path(directory) / "test.db"
        ), patch.object(repository, "_create_and_migrate_schema",
                        wraps=repository._create_and_migrate_schema) as schema:
            for _ in range(5):
                repository.initialize_database()
            self.assertEqual(schema.call_count, 1)

    def test_a_replaced_database_file_is_checked_again(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            repository, "DB_PATH", Path(directory) / "test.db"
        ):
            repository.initialize_database()
            Path(repository.DB_PATH).unlink()
            repository.initialize_database()
            with repository.get_connection() as conn:
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("product_details", tables)

    def test_many_rows_are_saved_together_and_returned_in_order(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            repository, "DB_PATH", Path(directory) / "test.db"
        ):
            records = [
                {"substance": "example", "product": f"Example {index} mg", "source": "Example Regulator",
                 "registration_number": f"EX-{index}", "product_url": f"https://regulator.test/{index}"}
                for index in range(30)
            ]
            stored = repository.save_product_details(records)
            again = repository.save_product_details(records[:5])
            with repository.get_connection() as conn:
                count = conn.execute(
                    "SELECT COUNT(*) FROM product_details WHERE source = 'Example Regulator'"
                ).fetchone()[0]
        self.assertEqual([row["product"] for row in stored], [record["product"] for record in records])
        self.assertEqual(count, 30)          # saving the same rows again updates them
        self.assertEqual(len(again), 5)

    def test_the_duplicate_lookup_uses_an_index(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            repository, "DB_PATH", Path(directory) / "test.db"
        ):
            repository.initialize_database()
            with repository.get_connection() as conn:
                plan = " ".join(
                    str(step[-1]) for step in conn.execute(
                        "EXPLAIN QUERY PLAN SELECT id FROM product_details "
                        "WHERE COALESCE(source,'') = COALESCE(?,'') AND COALESCE(product,'') = COALESCE(?,'') "
                        "AND COALESCE(country,'') = COALESCE(?,'')",
                        ("FDA", "X", "United States"),
                    )
                )
        self.assertIn("idx_product_details_identity", plan)

    def test_an_exported_row_carries_the_provenance_its_save_recorded(self):
        from services import search_pipeline

        with tempfile.TemporaryDirectory() as directory, patch.object(
            repository, "DB_PATH", Path(directory) / "test.db"
        ), patch.object(search_pipeline, "enrich_deep_results", lambda rows: rows), \
             patch.object(search_pipeline, "attach_ai_enrichment_metadata", lambda rows: rows):
            rows = search_pipeline.enriched_cached_results([{
                "substance": "example", "product": "Example 10 mg", "source": "Example Regulator",
                "registration_number": "EX-1", "product_url": "https://regulator.test/products/1",
            }])
        # Before, the export wrote this row without the evidence its save stamped,
        # and every row read "Evidence URL: Not available".
        self.assertEqual(rows[0]["evidence_url"], "https://regulator.test/products/1")
        self.assertEqual(rows[0]["verification_status"], "VERIFIED_REGULATOR_RECORD")


def _ndc_record(**overrides):
    record = {
        "product_ndc": "33342-409",
        "product_type": "HUMAN PRESCRIPTION DRUG",
        "finished": True,
        "brand_name": "Metformin Hydrochloride",
        "active_ingredients": [{"name": "METFORMIN HYDROCHLORIDE", "strength": "500 mg/1"}],
        "dosage_form": "TABLET, FILM COATED",
        "route": ["ORAL"],
        "labeler_name": "Macleods Pharmaceuticals Limited",
        "marketing_category": "ANDA",
        "application_number": "ANDA211559",
        "marketing_start_date": "20260406",
        "listing_expiration_date": "20271231",
        "pharm_class": ["Biguanide [EPC]", "Biguanides [CS]"],
        "packaging": [{"description": "30 TABLET, FILM COATED in 1 BOTTLE (33342-409-07)"}],
        "openfda": {"spl_set_id": ["7d575ca8-9c1f-469d-a341-34daf2f036dd"]},
    }
    record.update(overrides)
    return record


class FdaNdcRegisterTests(unittest.TestCase):
    """The FDA's NDC directory, streamed from openFDA's download and searched locally."""

    def setUp(self):
        from sources import open_registers

        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        patcher = patch.object(open_registers, "INDEX_DIR", Path(self.directory.name) / "idx")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _zip(self, records):
        import json as _json
        import zipfile as _zipfile

        path = Path(self.directory.name) / "ndc.json.zip"
        document = {"meta": {"results": {"total": len(records)}}, "results": records}
        with _zipfile.ZipFile(path, "w") as archive:
            archive.writestr("drug-ndc-0001-of-0001.json", _json.dumps(document, indent=2))
        return path

    def test_the_stream_yields_every_record_however_the_reads_fall(self):
        from sources.fda_ndc import stream_results

        records = [_ndc_record(product_ndc=f"0000-{index:03d}") for index in range(40)]
        path = self._zip(records)
        for chunk_size in (7, 1 << 20):
            streamed = [record["product_ndc"] for record in stream_results(path, chunk_size=chunk_size)]
            self.assertEqual(streamed, [record["product_ndc"] for record in records], chunk_size)

    def test_a_listing_becomes_a_row_with_its_approval_and_label(self):
        from sources.fda_ndc import FDA_NDC
        from sources.open_registers import build_index, search_register

        build_index(FDA_NDC, self._zip([_ndc_record()]))
        (row,) = search_register(FDA_NDC, "metformin")
        self.assertEqual(row["product"], "Metformin Hydrochloride 500 mg (NDC 33342-409)")
        self.assertEqual(row["authorisation_scope"], "ANDA (generic)")
        self.assertEqual(row["registration_number"], "ANDA211559")
        self.assertEqual(row["registration_date"], "2026-04-06")
        self.assertEqual(row["strength"], "500 mg")
        self.assertEqual(row["therapeutic_category"], "Biguanide")
        self.assertEqual(row["status"], "Marketed")
        self.assertEqual(row["pack_size"], "30 TABLET, FILM COATED in 1 BOTTLE")
        self.assertEqual(
            row["smpc_url"],
            "https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=7d575ca8-9c1f-469d-a341-34daf2f036dd",
        )

    def test_a_repackager_under_the_same_application_stays_its_own_row(self):
        from sources.fda_ndc import FDA_NDC
        from sources.open_registers import build_index, search_register

        repackaged = _ndc_record(product_ndc="50090-6123", labeler_name="A-S Medication Solutions")
        build_index(FDA_NDC, self._zip([_ndc_record(), repackaged]))
        rows = search_register(FDA_NDC, "metformin")
        self.assertEqual(len({row["product"] for row in rows}), 2)

    def test_products_that_are_not_finished_human_medicines_are_left_out(self):
        from sources.fda_ndc import FDA_NDC
        from sources.open_registers import build_index, search_register

        build_index(FDA_NDC, self._zip([
            _ndc_record(),
            _ndc_record(product_ndc="1-1", product_type="BULK INGREDIENT"),
            _ndc_record(product_ndc="1-2", finished=False),
            _ndc_record(product_ndc="1-3", marketing_category="UNAPPROVED HOMEOPATHIC"),
        ]))
        self.assertEqual(len(search_register(FDA_NDC, "metformin")), 1)

    def test_a_search_past_the_bound_says_how_many_there_are(self):
        import dataclasses
        from sources.fda_ndc import FDA_NDC
        from sources.open_registers import build_index, search_register

        small = dataclasses.replace(FDA_NDC, max_results=2)
        build_index(small, self._zip([_ndc_record(product_ndc=f"9-{index}") for index in range(5)]))
        rows = search_register(small, "metformin")
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["available_total"] for row in rows}, {5})


class HealthCanadaDpdRegisterTests(unittest.TestCase):
    """Health Canada's DPD, fetched dataset by dataset and joined locally."""

    def setUp(self):
        from sources import open_registers

        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        patcher = patch.object(open_registers, "INDEX_DIR", Path(self.directory.name) / "idx")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _datasets(self):
        import json as _json

        workdir = Path(self.directory.name) / "dpd"
        workdir.mkdir()
        data = {
            "drugproduct": [
                {"drug_code": 98443, "class_name": "Human", "drug_identification_number": "02494418",
                 "brand_name": "AG-METFORMIN", "company_name": "ANGITA PHARMA INC."},
                {"drug_code": 1, "class_name": "Veterinary", "drug_identification_number": "1",
                 "brand_name": "METFORMIN FOR CATS", "company_name": "VET CO"},
                {"drug_code": 2, "class_name": "Human", "drug_identification_number": "2",
                 "brand_name": "METFORMINUM 30CH", "company_name": "BOIRON"},
            ],
            "activeingredient": [
                {"drug_code": 98443, "ingredient_name": "METFORMIN HYDROCHLORIDE", "strength": "500", "strength_unit": "MG"},
                {"drug_code": 1, "ingredient_name": "METFORMIN HYDROCHLORIDE", "strength": "5", "strength_unit": "MG"},
                {"drug_code": 2, "ingredient_name": "METFORMIN", "strength": "30", "strength_unit": "CH"},
            ],
            "form": [{"drug_code": 98443, "pharmaceutical_form_name": "Tablet"}],
            "route": [{"drug_code": 98443, "route_of_administration_name": "Oral"}],
            "packaging": [{"drug_code": 98443, "package_size": "100", "package_size_unit": "TAB", "package_type": "BOTTLE"}],
            "status": [{"drug_code": 98443, "status": "Marketed", "original_market_date": "2023-01-04"}],
            "schedule": [
                {"drug_code": 98443, "schedule_name": "PRESCRIPTION"},
                {"drug_code": 2, "schedule_name": "HOMEOPATHIC"},
            ],
            "therapeuticclass": [{"drug_code": 98443, "tc_atc_number": "A10BA02", "tc_atc": "METFORMIN"}],
        }
        for name, records in data.items():
            (workdir / f"{name}.json").write_text(_json.dumps(records), encoding="utf-8")
        return workdir

    def test_a_product_is_joined_from_every_dataset(self):
        from sources.health_canada_dpd import HEALTH_CANADA_DPD
        from sources.open_registers import build_index, search_register

        build_index(HEALTH_CANADA_DPD, self._datasets())
        (row,) = search_register(HEALTH_CANADA_DPD, "metformin")
        # Named and numbered as the per-product connector did, so saved rows
        # are updated in place rather than duplicated.
        self.assertEqual(row["product"], "AG-METFORMIN")
        self.assertEqual(row["registration_number"], "02494418")
        self.assertEqual(row["atc_code"], "A10BA02")
        self.assertEqual(row["therapeutic_category"], "Metformin")
        self.assertEqual(row["authorisation_scope"], "Prescription")
        self.assertEqual(row["strength"], "500 MG")
        self.assertEqual((row["dosage_form"], row["route"]), ("Tablet", "Oral"))
        self.assertEqual(row["pack_size"], "100 TAB BOTTLE")
        self.assertEqual((row["status"], row["registration_date"]), ("Marketed", "2023-01-04"))

    def test_veterinary_and_homeopathic_products_are_left_out(self):
        from sources.health_canada_dpd import HEALTH_CANADA_DPD
        from sources.open_registers import build_index, search_register

        build_index(HEALTH_CANADA_DPD, self._datasets())
        self.assertEqual([row["product"] for row in search_register(HEALTH_CANADA_DPD, "metformin")], ["AG-METFORMIN"])

    def test_the_product_link_is_the_api_record_that_still_answers(self):
        from sources.health_canada_dpd import HEALTH_CANADA_DPD
        from sources.open_registers import build_index, search_register

        build_index(HEALTH_CANADA_DPD, self._datasets())
        (row,) = search_register(HEALTH_CANADA_DPD, "metformin")
        self.assertEqual(
            row["product_url"],
            "https://health-products.canada.ca/api/drug/drugproduct/?lang=en&type=json&id=98443",
        )

    def test_fetching_asks_for_every_dataset_the_join_needs(self):
        from sources import health_canada_dpd

        requested = []
        with patch.object(health_canada_dpd, "download_file",
                          side_effect=lambda register, url, destination: requested.append(url) or destination):
            health_canada_dpd.fetch_datasets(health_canada_dpd.HEALTH_CANADA_DPD, Path(self.directory.name))
        names = {url.split("/api/drug/")[1].split("/")[0] for url in requested}
        self.assertEqual(names, set(health_canada_dpd.DATASETS))


class CappedResultTests(unittest.TestCase):
    """Registries that stop at a fixed number say how many they hold."""

    def test_the_fda_passes_on_how_many_labels_match(self):
        from sources import fda

        response = MagicMock(status_code=200)
        response.json.return_value = {
            "meta": {"results": {"total": 3276}},
            "results": [{"openfda": {"brand_name": ["TYLENOL"], "manufacturer_name": ["Kenvue"]}}],
        }
        with patch.object(fda.requests, "get", return_value=response), \
             patch.object(fda, "_fetch_ndc_records", return_value=[]):
            rows = fda.run_fda_search("acetaminophen", limit=100)
        self.assertEqual(rows[0]["available_total"], 3276)

    def test_health_canada_passes_on_how_many_drug_codes_it_found(self):
        from sources import health_canada

        ingredients = [{"drug_code": str(code)} for code in range(1, 8)]
        with patch.object(health_canada, "_ingredient_rows", return_value=ingredients), \
             patch.object(health_canada, "_drug_record",
                          side_effect=lambda substance, code, rows: {"product": f"P{code}"}):
            rows = health_canada.run_health_canada_search("acetaminophen", limit=3)
        self.assertEqual(len(rows), 3)
        self.assertEqual({row["available_total"] for row in rows}, {7})

    def test_a_count_shows_the_total_only_when_it_is_larger(self):
        from main import records_with_total

        self.assertEqual(records_with_total(100, 3276), "100 of 3,276")
        self.assertEqual(records_with_total(8, 8), "8")
        self.assertEqual(records_with_total(21, 0), "21")

    def test_the_results_page_names_only_the_registries_that_were_capped(self):
        from main import capped_sources_note

        rows = [{"source": "FDA", "available_total": 3276}] * 100
        rows += [{"source": "FDA", "available_total": 0}]           # a synonym term that found nothing
        rows += [{"source": "Spain CIMA"}] * 20                      # says no total
        rows += [{"source": "Health Canada", "available_total": 3}] * 3  # returned all it holds
        note = capped_sources_note(rows)
        self.assertIn("FDA returned 100 of 3,276", note)
        self.assertNotIn("Spain CIMA", note)
        self.assertNotIn("Health Canada", note)
        self.assertEqual(capped_sources_note([{"source": "Spain CIMA"}]), "")

    def test_a_job_records_and_keeps_the_registry_total(self):
        from services import search_jobs

        with tempfile.TemporaryDirectory() as directory, patch.object(
            repository, "DB_PATH", Path(directory) / "test.db"
        ):
            job = search_jobs.SearchJob(
                job_id="capped-job",
                substance="acetaminophen",
                sources=["FDA"],
                progress={"FDA": search_jobs.SourceProgress(source="FDA")},
            )
            with search_jobs._lock:
                search_jobs._jobs[job.job_id] = job
            rows = [{"source": "FDA", "product": f"P{i}", "available_total": 3276} for i in range(100)]
            with patch.object(search_jobs, "_run_source_for_job", return_value=("FDA", rows, "")):
                search_jobs._run_job(job.job_id)
            live = search_jobs.get_search_job(job.job_id)
            with search_jobs._lock:
                search_jobs._jobs.pop(job.job_id, None)
            persisted = repository.get_persisted_search_job(job.job_id)

        self.assertEqual((live["progress"][0]["records"], live["progress"][0]["available"]), (100, 3276))
        # Read back from the database, as a job looks after a restart.
        self.assertEqual(persisted["progress"][0]["available"], 3276)


ITALY_FIXTURE = (
    "CODICE_AIC;COD_FARMACO;COD_CONFEZIONE;DENOMINAZIONE;DESCRIZIONE;CODICE_DITTA;RAGIONE_SOCIALE;"
    "STATO_AMMINISTRATIVO;TIPO_PROCEDURA;FORMA;CODICE_ATC;PA_ASSOCIATI;LINK\n"
    "026846010;026846;010;GLUCOPHAGE;500 MG COMPRESSE RIVESTITE- 30 COMPRESSE;1;MERCK SERONO S.P.A.;"
    "Autorizzata;Procedura Nazionale;Compressa rivestita con film;A10BA02;METFORMINA CLORIDRATO;\n"
    "026846022;026846;022;GLUCOPHAGE;500 MG COMPRESSE RIVESTITE- 60 COMPRESSE;1;MERCK SERONO S.P.A.;"
    "Autorizzata;Procedura Nazionale;Compressa rivestita con film;A10BA02;METFORMINA CLORIDRATO;\n"
    "026846034;026846;034;GLUCOPHAGE;30 COMPRESSE IN BLISTER DA 1000 MG;1;MERCK SERONO S.P.A.;"
    "Sospesa;Procedura Nazionale;Compressa rivestita con film;A10BA02;METFORMINA CLORIDRATO;\n"
    "049877011;049877;011;ADIABIN;50 MG/1000 MG COMPRESSE- 56 COMPRESSE;2;PHARMEXTRACTA S.P.A.;"
    "Autorizzata;Procedura Mutuo riconoscimento o Decentrata;Compressa rivestita con film;A10BD07;"
    "SITAGLIPTIN/METFORMINA CLORIDRATO;\n"
    "033001011;033001;011;NORVASC;5 MG COMPRESSE- 28 COMPRESSE;3;PFIZER ITALIA S.R.L.;"
    "Autorizzata;Procedura Nazionale;Compressa;C08CA01;AMLODIPINA BESILATO;\n"
    "900001011;900001;011;METFORMIN HOMEOPATHIC GRANULES;GRANULI 4 G;4;BOIRON;"
    "Autorizzata;Omeopatico;Granuli;V03AX;METFORMINA;\n"
)

BRAZIL_FIXTURE = (
    "TIPO_PRODUTO;NOME_PRODUTO;DATA_FINALIZACAO_PROCESSO;CATEGORIA_REGULATORIA;NUMERO_REGISTRO_PRODUTO;"
    "DATA_VENCIMENTO_REGISTRO;NUMERO_PROCESSO;CLASSE_TERAPEUTICA;EMPRESA_DETENTORA_REGISTRO;"
    "SITUACAO_REGISTRO;PRINCIPIO_ATIVO\n"
    '"MEDICAMENTO";"CLORIDRATO DE METFORMINA";"02/09/2024";"Genérico";155840680;"092034";"25351";'
    '"ANTIDIABETICOS";"06626253000151 - BRAINFARMA INDÚSTRIA QUÍMICA E FARMACÊUTICA S.A";"Ativo";'
    '"cloridrato de metformina"\n'
    '"MEDICAMENTO";"ANLO";"11/03/2015";"Similar";102351148;"032025";"25352";"ANTI-HIPERTENSIVOS";'
    '"57507378000365 - EMS S/A";"Inativo";"besilato de anlodipino"\n'
    '"MEDICAMENTO";"METFORMINUM";"22/10/2013";"DINAMIZADO";;;;;"60862208000141 - HOMEOPATICO LTDA";'
    '"Ativo";"metformina"\n'
)


class OpenRegisterTests(unittest.TestCase):
    """Italy's and Brazil's published registers, searched from a local index."""

    def setUp(self):
        from sources import open_registers

        self.registers = open_registers
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        index_dir = Path(self.directory.name) / "open_registers"
        patcher = patch.object(open_registers, "INDEX_DIR", index_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _index(self, register, text):
        csv_path = Path(self.directory.name) / f"{register.slug}.csv"
        csv_path.write_bytes(text.encode(register.encoding))
        self.registers.build_index(register, csv_path)

    def test_an_english_molecule_matches_its_italian_and_portuguese_spelling(self):
        tokens = self.registers.query_tokens
        inn = self.registers.inn_tokens
        for english, italian, portuguese in [
            ("amlodipine", "AMLODIPINA BESILATO", "besilato de anlodipino"),
            ("simvastatin", "SIMVASTATINA", "sinvastatina"),
            ("levothyroxine", "LEVOTIROXINA SODICA", "levotiroxina sódica"),
            ("amoxicillin", "AMOXICILLINA TRIIDRATO", "amoxicilina tri-hidratada"),
            ("metformin hydrochloride", "METFORMINA CLORIDRATO", "cloridrato de metformina"),
        ]:
            for register_text in (italian, portuguese):
                self.assertTrue(set(tokens(english)) <= set(inn(register_text)), (english, register_text))

    def test_salt_words_are_ignored_unless_they_are_the_whole_search(self):
        self.assertEqual(self.registers.query_tokens("metformin hydrochloride"), ["metformin"])
        self.assertEqual(len(self.registers.query_tokens("sodium chloride")), 2)

    def test_italian_packs_fold_into_one_row_per_product_strength_and_form(self):
        self._index(self.registers.ITALY, ITALY_FIXTURE)
        rows = self.registers.search_register(self.registers.ITALY, "metformin")
        by_product = {row["product"]: row for row in rows}

        glucophage = by_product["GLUCOPHAGE 500 MG, Film-coated tablet"]
        self.assertEqual(glucophage["pack_size"], "30 COMPRESSE; 60 COMPRESSE")
        self.assertEqual(glucophage["registration_number"], "026846")
        self.assertEqual(glucophage["dosage_form"], "Film-coated tablet")
        self.assertEqual(glucophage["status"], "Authorised")
        self.assertEqual(glucophage["authorisation_scope"], "National")
        self.assertEqual(glucophage["atc_code"], "A10BA02")
        # The pack written pack-first is still read, and keeps its own strength.
        self.assertEqual(by_product["GLUCOPHAGE 1000 MG, Film-coated tablet"]["pack_size"], "30 COMPRESSE")
        self.assertEqual(by_product["GLUCOPHAGE 1000 MG, Film-coated tablet"]["status"], "Suspended")
        # A combination keeps the register's own combination code.
        self.assertEqual(by_product["ADIABIN 50 MG/1000 MG, Film-coated tablet"]["atc_code"], "A10BD07")

    def test_two_forms_under_one_code_stay_two_products(self):
        powder = ITALY_FIXTURE + (
            "026846046;026846;046;GLUCOPHAGE;500 MG POLVERE PER SOLUZIONE ORALE- 30 BUSTINE;1;"
            "MERCK SERONO S.P.A.;Autorizzata;Procedura Nazionale;Polvere per soluzione orale;A10BA02;"
            "METFORMINA CLORIDRATO;\n"
        )
        self._index(self.registers.ITALY, powder)
        names = [row["product"] for row in self.registers.search_register(self.registers.ITALY, "metformin")]
        # Same AIC code and strength, different medicines: they must not share a
        # name, or the search job and the save path merge them into one row.
        self.assertIn("GLUCOPHAGE 500 MG, Film-coated tablet", names)
        self.assertIn("GLUCOPHAGE 500 MG, Powder for oral solution", names)
        self.assertEqual(len(names), len(set(names)))

    def test_homeopathic_registrations_are_left_out(self):
        self._index(self.registers.ITALY, ITALY_FIXTURE)
        self._index(self.registers.BRAZIL, BRAZIL_FIXTURE)
        for register in (self.registers.ITALY, self.registers.BRAZIL):
            products = [row["product"] for row in self.registers.search_register(register, "metformin")]
            self.assertFalse(any("HOMEOPATHIC" in p or p == "METFORMINUM" for p in products), products)

    def test_only_the_registers_own_active_ingredient_decides_a_match(self):
        self._index(self.registers.ITALY, ITALY_FIXTURE)
        amlodipine = self.registers.search_register(self.registers.ITALY, "amlodipine")
        self.assertEqual([row["product"] for row in amlodipine], ["NORVASC 5 MG, Tablet"])
        self.assertEqual(self.registers.search_register(self.registers.ITALY, "atorvastatin"), [])

    def test_authorised_rows_come_before_suspended_ones(self):
        self._index(self.registers.ITALY, ITALY_FIXTURE)
        statuses = [row["status"] for row in self.registers.search_register(self.registers.ITALY, "metformin")]
        self.assertEqual(statuses[-1], "Suspended")

    def test_brazilian_registrations_keep_category_dates_and_holder_name(self):
        self._index(self.registers.BRAZIL, BRAZIL_FIXTURE)
        (metformin,) = self.registers.search_register(self.registers.BRAZIL, "metformin")
        self.assertEqual(metformin["authorisation_scope"], "Generic")
        self.assertEqual(metformin["registration_date"], "02/09/2024")
        self.assertEqual(metformin["expiry_date"], "2034-09")
        self.assertEqual(metformin["company"], "BRAINFARMA INDÚSTRIA QUÍMICA E FARMACÊUTICA S.A")
        self.assertEqual(metformin["status"], "Active")
        self.assertEqual(metformin["region"], "BR")

        (amlodipine,) = self.registers.search_register(self.registers.BRAZIL, "amlodipine")
        self.assertEqual(amlodipine["product"], "ANLO")
        self.assertEqual(amlodipine["status"], "Inactive")

    def test_a_register_not_yet_downloaded_says_so_and_starts_the_download(self):
        with patch.object(self.registers, "refresh_in_background", return_value=True) as refresh:
            with self.assertRaises(self.registers.RegisterNotReady) as raised:
                self.registers.search_register(self.registers.BRAZIL, "metformin")
        refresh.assert_called_once_with(self.registers.BRAZIL)
        self.assertIn("search again in a few minutes", str(raised.exception))

    def test_a_stale_register_is_still_searched_while_it_refreshes(self):
        self._index(self.registers.BRAZIL, BRAZIL_FIXTURE)
        with patch.object(self.registers, "_is_stale", return_value=True), \
             patch.object(self.registers, "refresh_in_background") as refresh:
            rows = self.registers.search_register(self.registers.BRAZIL, "metformin")
        self.assertEqual(len(rows), 1)
        refresh.assert_called_once_with(self.registers.BRAZIL)

    def test_the_brazil_download_trusts_the_intermediate_its_server_omits(self):
        bundle = Path(self.registers._ca_bundle(self.registers.BRAZIL)).read_text(encoding="ascii")
        shipped = (
            self.registers.CERT_DIR / "sectigo_public_server_authentication_ca_ov_r36.pem"
        ).read_text(encoding="ascii").strip()
        self.assertIn(shipped, bundle)
        # certifi's own roots are still there: verification is extended, not replaced.
        import certifi
        self.assertIn(Path(certifi.where()).read_text(encoding="ascii").strip()[:200], bundle)

    def test_italy_and_brazil_are_searched_for_their_countries_and_regions(self):
        from services.search_pipeline import DEFAULT_SOURCES, sources_for_scope

        self.assertIn("AIFA Italy", sources_for_scope(DEFAULT_SOURCES, country="Italy"))
        self.assertIn("AIFA Italy", sources_for_scope(DEFAULT_SOURCES, region="EU"))
        self.assertEqual(sources_for_scope(DEFAULT_SOURCES, country="Brazil"), ["ANVISA Brazil"])
        self.assertEqual(sources_for_scope(DEFAULT_SOURCES, region="BR"), ["ANVISA Brazil"])


class EnvFileTests(unittest.TestCase):
    def test_credentials_in_the_env_file_reach_the_environment(self):
        from config import load_env_file

        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / ".env"
            env.write_text(
                "# admin sign-in\n"
                "PHARMASEARCH_TEST_USER=demo-admin\n"
                'export PHARMASEARCH_TEST_QUOTED="scrypt$abc=def"\n'
                "not a setting\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("PHARMASEARCH_TEST_USER", None)
                os.environ.pop("PHARMASEARCH_TEST_QUOTED", None)
                load_env_file(env)
                self.assertEqual(os.environ["PHARMASEARCH_TEST_USER"], "demo-admin")
                # A digest can hold "=" and "$"; only the first "=" splits.
                self.assertEqual(os.environ["PHARMASEARCH_TEST_QUOTED"], "scrypt$abc=def")

    def test_a_variable_already_in_the_environment_is_not_overridden(self):
        from config import load_env_file

        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / ".env"
            env.write_text("PHARMASEARCH_TEST_USER=from-file\n", encoding="utf-8")
            with patch.dict(os.environ, {"PHARMASEARCH_TEST_USER": "from-deployment"}):
                load_env_file(env)
                self.assertEqual(os.environ["PHARMASEARCH_TEST_USER"], "from-deployment")

    def test_writing_credentials_keeps_the_other_lines(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "make_admin_password", Path(__file__).parent / "tools" / "make_admin_password.py"
        )
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / ".env"
            env.write_text(
                "OPENAI_API_KEY=keep-me\nPHARMASEARCH_ADMIN_USERNAME=old\n", encoding="utf-8"
            )
            tool.write_env({
                "PHARMASEARCH_ADMIN_USERNAME": "new",
                "PHARMASEARCH_SESSION_SECRET": "s",
            }, env)
            lines = env.read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            lines,
            ["OPENAI_API_KEY=keep-me", "PHARMASEARCH_ADMIN_USERNAME=new", "PHARMASEARCH_SESSION_SECRET=s"],
        )


class EmaPageUrlTests(unittest.TestCase):
    def test_only_https_pages_on_the_ema_website_are_read(self):
        from sources.ema_product_parser import is_ema_page_url

        self.assertTrue(is_ema_page_url("https://www.ema.europa.eu/en/medicines/human/EPAR/avandamet"))
        for url in [
            "http://www.ema.europa.eu/en/x",          # not https
            "http://127.0.0.1:8765/api/version",      # internal address
            "https://169.254.169.254/latest/meta-data",
            "file:///C:/Windows/win.ini",
            "https://ema.europa.eu@evil.example/x",   # credentials trick
            "https://ema.europa.eu.evil.example/x",   # look-alike host
            "https://evilema.europa.eu/x",
            "https://www.ema.europa.eu:8443/x",       # unexpected port
            "",
        ]:
            self.assertFalse(is_ema_page_url(url), url)

    def test_a_refused_address_never_starts_a_browser(self):
        from sources import ema_product_parser

        with patch.object(ema_product_parser, "sync_playwright") as playwright:
            with self.assertRaises(ema_product_parser.NotAnEmaPage):
                ema_product_parser.extract_product_page("http://127.0.0.1/")
        playwright.assert_not_called()

    def test_a_page_with_no_product_is_not_saved(self):
        import main

        with patch.object(main, "extract_product_page", lambda url: {"product_name": ""}), \
             patch.object(main, "save_product_detail") as save:
            with self.assertRaises(main.EmptyProductPage):
                main.save_ema_product_page("https://www.ema.europa.eu/en/x")
        save.assert_not_called()


class RegulatoryDateTests(unittest.TestCase):
    def test_every_shape_a_registry_publishes_is_read(self):
        from datetime import date
        from services.regulatory_dates import parse_regulatory_date

        for text, expected in [
            ("2024-08-23", date(2024, 8, 23)),
            ("2025-09-15T10:56:55Z", date(2025, 9, 15)),
            ("2018-04-03T09:47:15+00:00", date(2018, 4, 3)),
            ("31/12/2009", date(2009, 12, 31)),
            ("17 Dec, 2013", date(2013, 12, 17)),
            ("22-DEC-2020", date(2020, 12, 22)),
            ("2025-Apr-07", date(2025, 4, 7)),
            ("22.12.2023", date(2023, 12, 22)),
            ("20261231", date(2026, 12, 31)),
        ]:
            self.assertEqual(parse_regulatory_date(text), expected, text)

    def test_a_slashed_date_is_read_day_first(self):
        from datetime import date
        from services.regulatory_dates import parse_regulatory_date

        self.assertEqual(parse_regulatory_date("03/04/2020"), date(2020, 4, 3))

    def test_an_impossible_or_empty_date_reads_as_none(self):
        from services.regulatory_dates import parse_regulatory_date

        for text in ["99/99/9999", "31/02/2020", "Not yet assigned", "", None]:
            self.assertIsNone(parse_regulatory_date(text))

    def test_newest_first_orders_by_the_date_not_the_text(self):
        from services.search_pipeline import sort_rows

        rows = [
            {"registration_date": "31/12/2009"},
            {"registration_date": ""},
            {"registration_date": "2024-01-01"},
            {"registration_date": "17 Dec, 2013"},
        ]
        newest = [r["registration_date"] for r in sort_rows(rows, [], "registration_date", "desc")]
        oldest = [r["registration_date"] for r in sort_rows(rows, [], "registration_date", "asc")]
        self.assertEqual(newest, ["2024-01-01", "17 Dec, 2013", "31/12/2009", ""])
        # A row with no date answers neither question, so it goes last both ways.
        self.assertEqual(oldest, ["31/12/2009", "17 Dec, 2013", "2024-01-01", ""])


class SearchTermCleaningTests(unittest.TestCase):
    def test_padding_and_repeated_spaces_are_removed(self):
        from repository import clean_search_term

        self.assertEqual(clean_search_term("  metformin  "), "metformin")
        self.assertEqual(clean_search_term("amlodipine  /\tvalsartan "), "amlodipine / valsartan")
        self.assertEqual(clean_search_term(None), "")

    def test_a_padded_search_finds_the_same_rows(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            repository, "DB_PATH", Path(directory) / "test.db"
        ):
            repository.save_product_detail({
                "substance": "examplezumab",
                "product": "Brandname examplezumab 10 mg",
                "source": "Example Regulator",
                "registration_number": "EX-PAD",
            })
            plain = repository.search_product_details("examplezumab")
            padded = repository.search_product_details("  examplezumab  ")
        self.assertEqual(len(plain), 1)
        self.assertEqual(len(padded), len(plain))


class CombinationRouteTests(unittest.TestCase):
    def test_a_substance_containing_a_slash_reaches_the_export_route(self):
        import main
        from starlette.routing import Match

        paths = {
            "/export/amlodipine / valsartan": "export_live",
            "/deep_export/amlodipine / valsartan": "deep_export_live",
            "/search/amlodipine / valsartan": "search_saved",
        }
        for path, endpoint_name in paths.items():
            scope = {"type": "http", "path": path, "method": "GET", "root_path": ""}
            matched = [
                route for route in main.app.router.routes
                if getattr(route, "matches", None) and route.matches(scope)[0] == Match.FULL
            ]
            self.assertTrue(matched, path)
            self.assertEqual(matched[0].endpoint.__name__, endpoint_name)
            self.assertEqual(
                matched[0].matches(scope)[1]["path_params"]["substance"], "amlodipine / valsartan"
            )


class BoundedResultCacheTests(unittest.TestCase):
    def test_only_the_most_recent_entries_are_kept(self):
        from services.result_cache import BoundedResultCache

        cache = BoundedResultCache(max_entries=3)
        for key in "abcd":
            cache[key] = [key]
        self.assertEqual(len(cache), 3)
        self.assertIsNone(cache.get("a"))
        self.assertEqual(cache.get("d"), ["d"])

    def test_reading_an_entry_keeps_it_from_being_dropped(self):
        from services.result_cache import BoundedResultCache

        cache = BoundedResultCache(max_entries=2)
        cache["search"] = ["rows"]
        cache["sorted"] = ["rows"]
        cache.get("search")          # the export reads it back
        cache["filtered"] = ["rows"]
        self.assertEqual(cache.get("search"), ["rows"])
        self.assertIsNone(cache.get("sorted"))

    def test_an_entry_lapses_after_its_time(self):
        from services.result_cache import BoundedResultCache

        now = [1000.0]
        cache = BoundedResultCache(max_entries=4, ttl_seconds=60, clock=lambda: now[0])
        cache["search"] = ["rows"]
        now[0] += 59
        self.assertEqual(cache.get("search"), ["rows"])
        now[0] += 120
        self.assertIsNone(cache.get("search"))
        self.assertEqual(len(cache), 0)


class SubstanceSynonymTests(unittest.TestCase):
    def test_a_us_adopted_name_is_reached_from_the_inn_and_back(self):
        for inn, usan in [
            ("salbutamol", "albuterol"),
            ("adrenaline", "epinephrine"),
            ("glibenclamide", "glyburide"),
            ("rifampicin", "rifampin"),
        ]:
            self.assertIn(usan, get_substance_search_terms(inn))
            self.assertIn(inn, get_substance_search_terms(usan))

    def test_the_term_typed_is_always_searched_first(self):
        self.assertEqual(get_substance_search_terms("salbutamol")[0], "salbutamol")
        self.assertEqual(get_substance_search_terms("Albuterol")[0], "Albuterol")

    def test_a_molecule_with_no_alias_searches_only_itself(self):
        self.assertEqual(get_substance_search_terms("tirzepatide"), ["tirzepatide"])

    def test_a_combination_reaches_its_parts_but_not_the_reverse(self):
        combination = get_substance_search_terms("lidocaine+prilocaine")
        self.assertIn("lidocaine", combination)
        self.assertIn("prilocaine", combination)
        # A search for one molecule must not drag in every combination it
        # appears in, or paracetamol would search half the register.
        self.assertNotIn("prilocaine", get_substance_search_terms("lidocaine"))


class SearchJobTimeoutTests(unittest.TestCase):
    def test_a_slow_term_does_not_discard_the_terms_that_answered(self):
        import time as _time
        from services import search_jobs

        def fake_connector(source, term, substance):
            if term == "slow-term":
                _time.sleep(10)
                return [{"product": "Late", "source": source}]
            return [{"product": "Answered", "source": source, "country": "X"}]

        with patch.object(search_jobs, "_run_connector_once", fake_connector), \
             patch.object(search_jobs, "get_substance_search_terms",
                          lambda s: [s, "slow-term"]), \
             patch.object(search_jobs, "_source_timeout", lambda s: 1), \
             patch.object(search_jobs, "_set_source_progress", lambda *a, **k: None), \
             patch.object(search_jobs, "record_source_health", lambda *a, **k: None):
            _source, rows, error = search_jobs._run_source_for_job("job", "example", "FDA")

        self.assertEqual([row["product"] for row in rows], ["Answered"])
        # Rows arrived, so the source did not fail even though a term ran on.
        self.assertEqual(error, "")

    def test_the_registries_measured_slower_than_the_default_are_given_longer(self):
        from services.search_jobs import (
            JOB_SLOW_SOURCE_TIMEOUT_SECONDS,
            _source_timeout,
        )
        for source in ("France BDPM", "CDSCO India"):
            self.assertEqual(_source_timeout(source), JOB_SLOW_SOURCE_TIMEOUT_SECONDS)


class EvidenceBackfillTests(unittest.TestCase):
    """The provenance reconstructed for rows saved before evidence existed."""

    def _backfill(self, *records):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            repository, "DB_PATH", Path(directory) / "test.db"
        ):
            for record in records:
                repository.save_product_detail(record)
            summary = backfill_evidence()
            rows = repository.search_product_details("example")
            return summary, {row["registration_number"]: row for row in rows}

    def test_register_values_point_at_the_record_they_were_read_from(self):
        _, rows = self._backfill({
            "substance": "example",
            "product": "Example 10 mg tablets",
            "country": "Exampleland",
            "source": "Example Regulator",
            "registration_number": "EX-1",
            "product_url": "https://regulator.test/products/1",
            "strength": "10 mg",
        })

        assertions = {item["field_name"]: item for item in rows["EX-1"]["evidence"]}
        self.assertEqual(assertions["strength"]["value"], "10 mg")
        self.assertEqual(
            assertions["strength"]["evidence_url"], "https://regulator.test/products/1"
        )
        self.assertEqual(
            assertions["strength"]["verification_status"], "VERIFIED_REGULATOR_RECORD"
        )
        self.assertEqual(assertions["strength"]["source_regulator"], "Example Regulator")

    def test_a_completed_field_credits_the_lenders_and_stays_unverified(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            repository, "DB_PATH", Path(directory) / "test.db"
        ):
            # A row with no ATC code of its own, and a lender in the same
            # molecule group for completion to borrow one from.
            repository.save_product_detail({
                "substance": "example",
                "product": "Example 10 mg tablets",
                "source": "Example Regulator",
                "registration_number": "EX-2",
                "product_url": "https://regulator.test/products/2",
            })
            repository.save_product_detail({
                "substance": "example",
                "product": "Example lender 10 mg tablets",
                "source": "EMA",
                "registration_number": "EX-2-LENDER",
                "product_url": "https://ema.test/products/2",
                "atc_code": "N02BE01",
            })
            complete_fields()
            rows = {
                row["registration_number"]: row
                for row in repository.search_product_details("example")
            }

        atc = next(
            item for item in rows["EX-2"]["evidence"] if item["field_name"] == "atc_code"
        )
        # Agreed across a molecule group, so it must not borrow this
        # register's authority for a value the register never published.
        self.assertEqual(atc["value"], "N02BE01")
        self.assertEqual(atc["source_regulator"], "EMA")
        self.assertEqual(atc["verification_status"], "UNVERIFIED")

    def test_completion_replaces_the_assertion_it_makes_stale(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            repository, "DB_PATH", Path(directory) / "test.db"
        ):
            repository.save_product_detail({
                "substance": "example",
                "product": "Example 10 mg tablets",
                "source": "Example Regulator",
                "registration_number": "EX-6",
                "product_url": "https://regulator.test/products/6",
                "atc_code": "N02BE01",
                "therapeutic_category": "Hipocolesterolemiante",
            })
            repository.save_product_detail({
                "substance": "example",
                "product": "Example lender 10 mg tablets",
                "source": "EMA",
                "registration_number": "EX-6-LENDER",
                "product_url": "https://ema.test/products/6",
                "atc_code": "N02BE01",
            })
            complete_fields()
            rows = {
                row["registration_number"]: row
                for row in repository.search_product_details("example")
            }

        row = rows["EX-6"]
        categories = [
            item for item in row["evidence"]
            if item["field_name"] == "therapeutic_category"
        ]
        # Completion rewrites the category, so exactly one assertion should
        # describe it -- the superseded one would contradict the column.
        self.assertEqual(len(categories), 1)
        self.assertEqual(categories[0]["value"], row["therapeutic_category"])

    def test_a_manufacturer_read_from_a_document_cites_the_document(self):
        _, rows = self._backfill({
            "substance": "example",
            "product": "Example 10 mg tablets",
            "source": "MHRA",
            "registration_number": "EX-3",
            "product_url": "https://regulator.test/products/3",
            "smpc_url": "https://regulator.test/products/3/smpc.pdf",
            "manufacturer_name": "Example Manufacturing Ltd",
            "manufacturer_source": "MHRA document",
        })

        manufacturer = next(
            item for item in rows["EX-3"]["evidence"]
            if item["field_name"] == "manufacturer_name"
        )
        self.assertEqual(
            manufacturer["evidence_url"], "https://regulator.test/products/3/smpc.pdf"
        )
        self.assertEqual(
            manufacturer["verification_status"], "VERIFIED_OFFICIAL_DOCUMENT"
        )

    def test_a_row_with_no_address_asserts_nothing(self):
        summary, rows = self._backfill({
            "substance": "example",
            "product": "Example 10 mg tablets",
            "source": "Example Regulator",
            "registration_number": "EX-4",
            "strength": "10 mg",
        })

        self.assertEqual(summary["rows_without_source_url"], 1)
        self.assertEqual(rows["EX-4"]["evidence"], [])

    def test_only_a_parsed_registry_is_told_its_documents_are_pending(self):
        parsed = {
            "source": "MHRA",
            "smpc_url": "https://regulator.test/smpc.pdf",
            "manufacturer_name": "",
        }
        unparsed = {**parsed, "source": "Health Canada"}
        undocumented = {"source": "MHRA", "manufacturer_name": ""}

        self.assertEqual(missing_reason_for_row(parsed), "Pending document enrichment")
        self.assertEqual(missing_reason_for_row(unparsed), "Not collected for this source")
        self.assertEqual(missing_reason_for_row(undocumented), "NOT_PUBLISHED")

    def test_a_row_that_names_its_manufacturer_is_given_no_reason(self):
        # Role and batch release site are almost never published, so asking
        # for every manufacturer column would contradict the name in the
        # export row beside it.
        named = {
            "source": "Health Canada",
            "smpc_url": "https://regulator.test/smpc.pdf",
            "manufacturer_name": "Example Manufacturing Ltd",
            "manufacturer_role": "",
            "batch_release_manufacturer": "",
        }
        self.assertEqual(missing_reason_for_row(named), "")

    def test_a_second_pass_refreshes_rather_than_duplicates(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            repository, "DB_PATH", Path(directory) / "test.db"
        ):
            repository.save_product_detail({
                "substance": "example",
                "product": "Example 10 mg tablets",
                "source": "Example Regulator",
                "registration_number": "EX-5",
                "product_url": "https://regulator.test/products/5",
                "strength": "10 mg",
            })
            first = backfill_evidence()
            with repository.get_connection() as conn:
                after_first = conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]

            second = backfill_evidence()
            with repository.get_connection() as conn:
                after_second = conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]

        self.assertGreater(first["assertions_written"], 0)
        self.assertEqual(second["rows_scanned"], 0)
        self.assertEqual(after_first, after_second)


class CombinationAndMhraRepairTests(unittest.TestCase):
    """Rows stored before combinations were keyed apart and MHRA rows were checked."""

    def setUp(self):
        # An empty database is otherwise filled from the seed fixture.
        seed = patch.object(repository, "PRODUCT_DETAILS_SEED_PATH", Path("no-seed.jsonl"))
        seed.start()
        self.addCleanup(seed.stop)

    def _database(self, directory):
        return patch.object(repository, "DB_PATH", Path(directory) / "test.db")

    def test_the_stored_search_finds_a_molecule_inside_a_combination(self):
        with tempfile.TemporaryDirectory() as directory, self._database(directory):
            repository.save_product_details([
                {"substance": "SITAGLIPTINE, METFORMINE", "product": "Janumet 50 mg/1000 mg",
                 "source": "France BDPM", "country": "France"},
                {"substance": "metoprolol", "product": "Lopressor", "source": "FDA", "country": "United States"},
            ])
            found = [row["product"] for row in repository.search_product_details("metformin")]
        self.assertEqual(found, ["Janumet 50 mg/1000 mg"])

    def test_the_stored_search_finds_every_name_and_salt_a_register_used(self):
        with tempfile.TemporaryDirectory() as directory, self._database(directory):
            repository.save_product_details([
                {"substance": "cephalexin", "product": "APO-CEPHALEX", "source": "Health Canada", "country": "Canada"},
                {"substance": "CÉFALEXINE MONOHYDRATÉE", "product": "CEFALEXINE BIOGARAN",
                 "source": "France BDPM", "country": "France"},
                {"substance": "cefuroxime", "product": "Zinnat", "source": "MHRA", "country": "United Kingdom"},
            ])
            for typed in ("cefalexin", "céfalexine"):
                found = sorted(row["product"] for row in repository.search_product_details(typed))
                self.assertEqual(found, ["APO-CEPHALEX", "CEFALEXINE BIOGARAN"], typed)

    def test_mhra_rows_are_kept_only_when_their_licence_is_for_the_molecule(self):
        from services.data_repairs import verify_mhra_rows

        rows = [
            # An amlodipine report filed under atorvastatin.
            {"substance": "atorvastatin", "product": "Amlodipine 10mg Tablets - PL 15764/0016",
             "registration_number": "PL 15764/0016", "product_url": "https://blob/amlodipine",
             "source": "MHRA", "country": "United Kingdom"},
            # A real atorvastatin leaflet, stored under its file name, whose PDF
            # has since been replaced by a revised one.
            {"substance": "atorvastatin", "product": "leaflet MAH GENERIC_PL 49445-0400.pdf",
             "registration_number": "PL 49445/0400", "product_url": "https://blob/old-leaflet",
             "pil_url": "https://blob/old-leaflet", "document_type": "PIL",
             "source": "MHRA", "country": "United Kingdom"},
            # A licence no longer in the register.
            {"substance": "atorvastatin", "product": "spc-doc_PL 00001-0001.pdf",
             "registration_number": "PL 00001/0001", "product_url": "https://blob/gone",
             "source": "MHRA", "country": "United Kingdom"},
            # Named for its molecule, so not in doubt and never looked up.
            {"substance": "atorvastatin", "product": "Atorvastatin 20 mg Tablets",
             "registration_number": "PL 00001/0002", "product_url": "https://blob/fine",
             "source": "MHRA", "country": "United Kingdom"},
        ]
        documents = [
            {"metadata_storage_path": "https://blob/amlodipine", "pl_number": ["PL157640016"],
             "substance_name": ["AMLODIPINE BESILATE"], "title": "Amlodipine 10mg Tablets",
             "product_name": "", "doc_type": "Par"},
            {"metadata_storage_path": "https://blob/new-leaflet", "pl_number": ["PL494450400"],
             "substance_name": ["ATORVASTATIN CALCIUM TRIHYDRATE"],
             "title": "leaflet MAH GENERIC_PL 49445-0400.pdf",
             "product_name": "ATORVASTATIN 20 MG FILM-COATED TABLETS", "doc_type": "Pil"},
        ]
        looked_up = []

        def fetch(licences):
            looked_up.extend(licences)
            return documents

        with tempfile.TemporaryDirectory() as directory, self._database(directory):
            repository.save_product_details(rows)
            result = verify_mhra_rows(fetch=fetch)
            with repository.get_connection() as conn:
                stored = {
                    row["registration_number"]: dict(row)
                    for row in conn.execute("SELECT * FROM product_details WHERE source = 'MHRA'")
                }

        self.assertNotIn("PL00010002", looked_up)
        self.assertEqual(sorted(stored), ["PL 00001/0002", "PL 49445/0400"])
        leaflet = stored["PL 49445/0400"]
        self.assertEqual(leaflet["product"], "ATORVASTATIN 20 MG FILM-COATED TABLETS")
        self.assertEqual(leaflet["substance"], "ATORVASTATIN CALCIUM TRIHYDRATE")
        self.assertEqual(leaflet["pil_url"], "https://blob/new-leaflet")
        self.assertEqual(result["removed_because"]["another_molecule"], 1)
        self.assertEqual(result["removed_because"]["licence_not_in_register"], 1)

    def test_a_failed_lookup_leaves_rows_untouched(self):
        from services.data_repairs import verify_mhra_rows

        def fetch(licences):
            raise requests.ConnectionError("down")

        with tempfile.TemporaryDirectory() as directory, self._database(directory):
            repository.save_product_details([
                {"substance": "atorvastatin", "product": "Amlodipine 10mg Tablets",
                 "registration_number": "PL 15764/0016", "source": "MHRA", "country": "United Kingdom"},
            ])
            result = verify_mhra_rows(fetch=fetch)
            with repository.get_connection() as conn:
                count = conn.execute("SELECT COUNT(*) FROM product_details").fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(result["left_unchecked_lookup_failed"], 1)

    def test_codes_crossed_between_a_molecule_and_its_combination_are_corrected(self):
        from services.data_repairs import correct_group_atc_codes

        rows = [
            {"substance": "metformin", "product": f"Metformin {n} mg", "atc_code": "A10BA02",
             "source": "EMA", "country": "EU"} for n in (500, 850, 1000)
        ] + [
            # Metformin lent the combination's code.
            {"substance": "metformin", "product": "Metformin Teva 750 mg", "atc_code": "A10BD07",
             "source": "MHRA", "country": "United Kingdom"},
            # Janumet lent metformin's code, beside two carrying their own.
            {"substance": "metformin", "source_substance": "SITAGLIPTIN, METFORMIN",
             "product": "Janumet 50 mg/850 mg", "atc_code": "A10BA02", "source": "MHRA",
             "country": "United Kingdom"},
            {"substance": "sitagliptin;metformin", "product": "Janumet", "atc_code": "A10BD07",
             "source": "EMA", "country": "EU"},
            {"substance": "sitagliptin;metformin", "product": "Velmetia", "atc_code": "A10BD07",
             "source": "EMA", "country": "EU"},
            # A combination Spain did not state, lent the molecule's code.
            {"substance": "metformin", "product": "EUCREAS 50 MG/850 MG COMPRIMIDOS",
             "atc_code": "A10BA02", "source": "Spain CIMA", "country": "Spain"},
        ]
        with tempfile.TemporaryDirectory() as directory, self._database(directory):
            repository.save_product_details(rows)
            result = correct_group_atc_codes()
            with repository.get_connection() as conn:
                codes = {row[0]: row[1] for row in conn.execute("SELECT product, atc_code FROM product_details")}

        self.assertEqual(codes["Metformin Teva 750 mg"], "A10BA02")
        self.assertEqual(codes["Janumet 50 mg/850 mg"], "A10BD07")
        self.assertEqual(codes["EUCREAS 50 MG/850 MG COMPRIMIDOS"], "")
        self.assertEqual(codes["Metformin 500 mg"], "A10BA02")
        self.assertEqual(result["single_molecule_rows_corrected"], 1)
        self.assertEqual(result["combination_rows_corrected"], 1)


class MhraCombinationTitleTests(unittest.TestCase):
    def test_a_report_with_no_substance_field_takes_the_combination_its_title_names(self):
        from sources.mhra import _extract_mhra_json_record

        record = {
            "title": "Amlodipine/Valsartan 5 mg/80 mg film-coated tablets - PL 12345/0001",
            "substance_name": [], "pl_number": ["PL123450001"], "doc_type": "Par",
            "metadata_storage_path": "https://blob/par",
        }
        row = _extract_mhra_json_record(record, "amlodipine")
        self.assertEqual(row["substance"], "Amlodipine/Valsartan")

    def test_a_single_molecule_report_keeps_the_molecule(self):
        from sources.mhra import _extract_mhra_json_record

        record = {
            "title": "Amlodipine 10mg Tablets (amlodipine besilate) - PL 15764/0016",
            "substance_name": [], "pl_number": ["PL157640016"], "doc_type": "Par",
            "metadata_storage_path": "https://blob/par",
        }
        self.assertEqual(_extract_mhra_json_record(record, "amlodipine")["substance"], "amlodipine besilate")


class VendorDisplayTests(unittest.TestCase):
    """What a vendor reads in the result table and the export."""

    def test_markup_and_placeholders_are_not_shown_as_values(self):
        from services.vendor_display import clean_atc_code, clean_text

        self.assertEqual(clean_text("AMLODIPINE BESILATE&lt;br&gt;VALSARTAN"), "AMLODIPINE BESILATE; VALSARTAN")
        self.assertEqual(clean_text("PROCTER &amp;amp; GAMBLE"), "PROCTER & GAMBLE")
        for placeholder in ("N/A", "-", "--", "Not yet assigned", "na"):
            self.assertEqual(clean_text(placeholder), "", placeholder)
        self.assertEqual(clean_atc_code("Not yet assigned"), "")
        self.assertEqual(clean_atc_code("A02BC011"), "")
        self.assertEqual(clean_atc_code("c10aa05"), "C10AA05")

    def test_company_names_lose_page_footers_split_words_and_leaflet_prose(self):
        from services.vendor_display import clean_company

        self.assertEqual(clean_company("The Boots Compan y PLC"), "The Boots Company PLC")
        self.assertEqual(clean_company("Sandoz Ltd Page 8 of 8"), "Sandoz Ltd")
        self.assertEqual(
            clean_company("Jarama, 111; 45007-Toledo; Espana; Esta informacion esta destinada unicamente a medicos"),
            "Jarama, 111; 45007-Toledo; Espana",
        )

    def test_every_registry_status_reads_in_one_vocabulary(self):
        from services.vendor_display import vendor_status

        cases = {
            "Commercialisée": "Marketed",
            "Non commercialisée": "Authorised, not marketed",
            "available, not_commercialised": "Marketed",
            "not_commercialised, unavailable": "Authorised, not marketed",
            "Cancelled Post Market": "Withdrawn",
            "Berlaku": "Authorised",
            "Berlaku (Khusus Ekspor)": "Authorised for export only",
            "Registered - Part 1 Poison": "Authorised",
            "Lapsed": "Expired",
            "Application withdrawn": "Application withdrawn",
        }
        for registry, shown in cases.items():
            self.assertEqual(vendor_status(registry), shown, registry)

    def test_dosage_forms_are_shown_in_english(self):
        from services.vendor_display import english_dosage_form

        cases = {
            "comprimé pelliculé sécable": "Film-coated tablet (scored)",
            "comprime pellicule et comprime pellicule": "Film-coated tablet",
            "comprime orodispersible": "Orodispersible tablet",
            "solution injectable": "Solution for injection",
            "KAPSUL; 75 mg": "Capsule",
            "Vien nen bao phim": "Film-coated tablet",
            "LIOF. ORAL": "Oral lyophilisate",
            "Film-coated tablet": "Film-coated tablet",
        }
        for registry, shown in cases.items():
            self.assertEqual(english_dosage_form(registry), shown, registry)

    def test_dates_are_shown_as_iso(self):
        from services.vendor_display import display_date

        self.assertEqual(display_date("31/12/2009"), "2009-12-31")
        self.assertEqual(display_date("2020-03-13T06:44:23Z"), "2020-03-13")

    def test_the_result_row_shows_the_holder_as_company_and_a_readable_missing_value(self):
        from services.result_formatter import formatted_result_row

        row = formatted_result_row({
            "source": "Health Canada", "product": "METFORMIN", "company": "APOTEX INC",
            "status": "Cancelled Post Market", "dosage_form": "Tablet",
        })
        self.assertEqual(row["company"], "APOTEX INC")
        self.assertEqual(row["registration_status"], "Withdrawn")
        self.assertNotIn("NOT_PUBLISHED", row.values())


class DeadMhraDocumentTests(unittest.TestCase):
    OLD = "https://mhraproducts4853.blob.core.windows.net/docs/old-leaflet"
    NEW = "https://mhraproducts4853.blob.core.windows.net/docs/new-leaflet"

    def test_only_404_and_410_count_as_dead(self):
        from sources import document_links

        answers = {"https://a/404": 404, "https://a/200": 200, "https://a/410": 410}

        def head(url, **_kwargs):
            if url == "https://a/timeout":
                raise requests.Timeout("slow")
            return MagicMock(status_code=answers[url])

        document_links._cache.clear()
        with patch("requests.Session.head", side_effect=head):
            dead = document_links.dead_links([*answers, "https://a/timeout"])
        self.assertEqual(dead, {"https://a/404", "https://a/410"})

    def test_a_live_search_drops_a_deleted_pdf_and_keeps_the_licence_current_one(self):
        from sources import mhra

        results = [
            {"registration_number": "PL 1/1", "document_type": "PIL", "pil_url": self.OLD,
             "url": self.OLD, "product_url": self.OLD, "product": "BETMIGA"},
            {"registration_number": "PL 1/1", "document_type": "PIL", "pil_url": self.NEW,
             "url": self.NEW, "product_url": self.NEW, "product": "BETMIGA"},
        ]
        with patch.object(mhra, "dead_links", return_value={self.OLD}):
            rows = mhra._finalize_mhra_results(results)
        self.assertEqual([row["pil_url"] for row in rows], [self.NEW])

    def test_stored_dead_links_take_the_current_document_or_are_removed(self):
        from services.data_repairs import relink_dead_mhra_documents

        documents = [{"metadata_storage_path": self.NEW, "pl_number": ["PLPI163780990"], "doc_type": "Pil",
                      "substance_name": ["MIRABEGRON"], "title": "", "product_name": "BETMIGA"}]
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(repository, "DB_PATH", Path(directory) / "test.db"), \
                patch.object(repository, "PRODUCT_DETAILS_SEED_PATH", Path("no-seed.jsonl")):
            repository.save_product_details([
                {"product": "BETMIGA 50 MG", "substance": "mirabegron", "source": "MHRA", "country": "United Kingdom",
                 "registration_number": "PLPI 16378/0990", "document_type": "PIL",
                 "pil_url": self.OLD, "product_url": self.OLD},
                {"product": "OTHER 5 MG", "substance": "mirabegron", "source": "MHRA", "country": "United Kingdom",
                 "registration_number": "PL 00000/0001", "document_type": "SPC",
                 "smpc_url": self.OLD + "-spc", "product_url": self.OLD + "-spc"},
            ])
            result = relink_dead_mhra_documents(
                fetch=lambda licences: documents,
                check=lambda urls: {url for url in urls if "old-leaflet" in url},
            )
            with repository.get_connection() as conn:
                rows = {row["product"]: dict(row) for row in conn.execute("SELECT * FROM product_details")}

        self.assertEqual(rows["BETMIGA 50 MG"]["pil_url"], self.NEW)
        self.assertEqual(rows["BETMIGA 50 MG"]["product_url"], self.NEW)
        self.assertEqual(rows["OTHER 5 MG"]["smpc_url"], "")
        # Its SmPC, product and evidence links all pointed at the deleted file.
        self.assertEqual(result["links_removed_no_current_document"], 3)


class OnlyRegulatorValuesTests(unittest.TestCase):
    """A row shows what its own regulator published, nothing lent from another."""

    def setUp(self):
        seed = patch.object(repository, "PRODUCT_DETAILS_SEED_PATH", Path("no-seed.jsonl"))
        seed.start()
        self.addCleanup(seed.stop)

    def _rows(self):
        with repository.get_connection() as conn:
            return {row["product"]: dict(row) for row in conn.execute("SELECT * FROM product_details")}

    def test_lent_values_and_mhra_document_dates_are_withdrawn(self):
        from services.data_repairs import revoke_borrowed_values

        with tempfile.TemporaryDirectory() as directory, patch.object(repository, "DB_PATH", Path(directory) / "t.db"):
            repository.save_product_details([
                # MHRA publishes no ATC code: this one was lent.
                {"product": "APIXABAN TEVA", "source": "MHRA", "country": "United Kingdom",
                 "atc_code": "B01AF02", "therapeutic_category": "Anticoagulant",
                 "completion_source": "EMA", "registration_date": "2022-04-26T22:09:40Z",
                 "reference_smpc_url": "https://ema/eliquis"},
                # EMA publishes its own code.
                {"product": "Eliquis", "source": "EMA", "country": "EU", "atc_code": "B01AF02",
                 "therapeutic_category": "Anticoagulant"},
                # Spain publishes ATC codes, but this one was lent.
                {"product": "APIXABAN CINFA", "source": "Spain CIMA", "country": "Spain",
                 "atc_code": "B01AF02"},
            ])
            with repository.get_connection() as conn:
                # Field completion wrote these; the save path does not.
                conn.execute(
                    "UPDATE product_details SET completion_source = 'EMA', reference_smpc_url = 'https://ema/eliquis'"
                    " WHERE product IN ('APIXABAN TEVA', 'APIXABAN CINFA')"
                )
            revoke_borrowed_values()
            rows = self._rows()

        uk = rows["APIXABAN TEVA"]
        self.assertEqual((uk["atc_code"], uk["therapeutic_category"], uk["registration_date"]), ("", "", ""))
        self.assertFalse(uk["reference_smpc_url"])
        self.assertEqual(rows["Eliquis"]["atc_code"], "B01AF02")
        self.assertEqual(rows["APIXABAN CINFA"]["atc_code"], "")

    def test_canadian_rows_take_health_canadas_own_code_by_din(self):
        from services.data_repairs import restore_health_canada_from_register

        with tempfile.TemporaryDirectory() as directory, patch.object(repository, "DB_PATH", Path(directory) / "t.db"):
            repository.save_product_details([
                {"product": "GLUCOPHAGE", "source": "Health Canada", "country": "Canada",
                 "registration_number": "02099233"},
            ])
            result = restore_health_canada_from_register(register_rows=[
                {"registration_number": "02099233", "atc_code": "A10BA02", "therapeutic_category": "Metformin"},
            ])
            row = self._rows()["GLUCOPHAGE"]
        self.assertEqual((row["atc_code"], row["therapeutic_category"]), ("A10BA02", "Metformin"))
        self.assertEqual(result["atc_codes_restored_from_register"], 1)

    def test_live_results_share_nothing_across_regulators(self):
        from services.search_pipeline import propagate_molecule_fields

        rows = propagate_molecule_fields([
            {"source": "EMA", "substance": "apixaban", "product": "Eliquis", "atc_code": "B01AF02",
             "therapeutic_category": "Anticoagulant"},
            {"source": "MHRA", "substance": "apixaban", "product": "Eliquis"},
        ])
        self.assertEqual(rows[1].get("atc_code", ""), "")
        self.assertEqual(rows[1].get("therapeutic_category", ""), "")

    def test_mhra_rows_carry_no_registration_date(self):
        from sources.mhra import _extract_mhra_json_record

        row = _extract_mhra_json_record({
            "title": "Apixaban 5 mg Tablets", "substance_name": ["APIXABAN"], "pl_number": ["PL002892534"],
            "doc_type": "Spc", "metadata_storage_path": "https://blob/spc", "created": "2022-04-26T22:09:40Z",
        }, "apixaban")
        self.assertEqual(row["registration_date"], "")


class RomanianPackTests(unittest.TestCase):
    PACKS = [
        ("16519/2026/01", "Cutie cu blist. PVC-PVDC/Al x 10 compr. film."),
        ("16519/2026/02", "Cutie cu blist. PVC-PVDC/Al x 12 compr. film."),
        ("16519/2026/03", "Cutie cu blist. doze unitare PVC-PVDC/Al x 20x1 compr. film."),
    ]

    def _records(self):
        return [
            {"product": "ABATIXENT 2,5 mg", "strength": "2,5mg", "dosage_form": "Film-coated tablet",
             "company": "SANDOZ", "registration_number": number, "pack_size": pack,
             "product_url": f"https://anm/{number}", "source": "ANMDMR Romania", "country": "Romania",
             "atc_code": "B01AF02"}
            for number, pack in self.PACKS
        ] + [{"product": "ABATIXENT 5 mg", "strength": "5mg", "dosage_form": "Film-coated tablet",
              "company": "SANDOZ", "registration_number": "16520/2026/01", "pack_size": "x 10 compr.",
              "product_url": "https://anm/16520", "source": "ANMDMR Romania", "country": "Romania"}]

    def test_packs_of_one_product_become_one_row_listing_every_pack(self):
        from sources.romania_anmdmr import fold_pack_registrations

        rows = fold_pack_registrations(self._records())
        self.assertEqual([row["product"] for row in rows], ["ABATIXENT 2,5 mg", "ABATIXENT 5 mg"])
        self.assertEqual(rows[0]["registration_number"], "16519/2026/01-03")
        self.assertEqual(
            rows[0]["pack_size"],
            "Cutie cu blist. PVC-PVDC/Al x 10 / 12 compr. film.; "
            "Cutie cu blist. doze unitare PVC-PVDC/Al x 20x1 compr. film.",
        )
        self.assertEqual(rows[0]["product_url"], "https://anm/16519/2026/01")
        self.assertEqual(rows[1]["registration_number"], "16520/2026/01")

    def test_stored_pack_rows_are_merged_and_a_later_search_updates_the_same_row(self):
        from services.data_repairs import fold_romanian_pack_rows
        from sources.romania_anmdmr import fold_pack_registrations

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(repository, "DB_PATH", Path(directory) / "t.db"), \
                patch.object(repository, "PRODUCT_DETAILS_SEED_PATH", Path("no-seed.jsonl")):
            repository.save_product_details(self._records())
            result = fold_romanian_pack_rows()
            repository.save_product_details(fold_pack_registrations(self._records()))
            with repository.get_connection() as conn:
                rows = [dict(row) for row in conn.execute(
                    "SELECT product, registration_number FROM product_details ORDER BY product"
                )]
        self.assertEqual(result["pack_rows_merged_away"], 2)
        self.assertEqual(rows, [
            {"product": "ABATIXENT 2,5 mg", "registration_number": "16519/2026/01-03"},
            {"product": "ABATIXENT 5 mg", "registration_number": "16520/2026/01"},
        ])


class WeeklyMhraLinkJobTests(unittest.TestCase):
    def test_each_run_backs_up_first_and_keeps_the_newest_four_backups(self):
        import sqlite3
        import time

        from tools.weekly_mhra_links import back_up

        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "store.db"
            connection = sqlite3.connect(db)
            connection.execute("CREATE TABLE t (x)")
            connection.commit()
            connection.close()
            backups = Path(directory) / "backups"
            backups.mkdir()
            for day in range(1, 6):
                (backups / f"weekly-mhra-links-2026010{day}-000000.db").write_bytes(b"")
            (backups / "pharmasearch-before-vendor-cleanup.db").write_bytes(b"keep")
            time.sleep(0.01)
            made = back_up(db, backups, kept=4)
            weekly = sorted(path.name for path in backups.glob("weekly-mhra-links-*.db"))

            self.assertIn(made.name, weekly)
            self.assertEqual(len(weekly), 4)
            self.assertTrue((backups / "pharmasearch-before-vendor-cleanup.db").exists())


class ExcelControlCharacterTests(unittest.TestCase):
    def test_text_from_a_pdf_with_control_characters_still_exports(self):
        from export_service import write_excel_export

        leaflet_text = "Marketing Authorization Holder and \x0b Lyme disease \x03"
        with tempfile.TemporaryDirectory() as directory, patch("export_service.EXPORT_DIR", Path(directory)):
            path = write_excel_export("amoxicillin", [{
                "source": "MHRA", "product": "Amoxicillin 500 mg", "manufacturer_name": leaflet_text,
                "evidence": [{"field_name": "manufacturer_name", "value": leaflet_text}],
            }])
            self.assertTrue(path.exists())


class UsDocumentLinkTests(unittest.TestCase):
    def test_a_us_application_links_its_drugs_at_fda_page_as_the_assessment_report(self):
        from services.result_formatter import formatted_result_row

        row = formatted_result_row({
            "source": "FDA", "product": "Metformin 500 mg (NDC 0000-0000)", "registration_number": "ANDA076002",
            "smpc_url": "https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=abc",
        })
        self.assertEqual(
            row["assessment_report_url"],
            "https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm?event=overview.process&ApplNo=076002",
        )

    def test_monographs_biologics_and_other_regulators_get_no_drugs_at_fda_link(self):
        from services.field_availability import drugs_at_fda_url

        for item in (
            {"source": "FDA", "registration_number": "M012"},
            {"source": "FDA", "registration_number": "BLA125856"},
            {"source": "MHRA", "registration_number": "NDA021202"},
        ):
            self.assertEqual(drugs_at_fda_url(item), "", item)

    def test_the_us_pil_is_said_to_be_in_the_label(self):
        from services.field_availability import US_PIL_IN_LABEL, missing_field_value

        self.assertEqual(missing_field_value({"source": "FDA", "smpc_url": "https://dailymed"}, "pil_url"), US_PIL_IN_LABEL)


class AccentedSearchTests(unittest.TestCase):
    def test_an_accented_name_matches_the_plain_spelling(self):
        from services.search_pipeline import row_relevant_to_substance

        self.assertTrue(row_relevant_to_substance({"product": "CEFALEXINE BIOGARAN 500 mg"}, "céfalexine"))

    def test_an_accented_name_also_searches_the_plain_and_english_spellings(self):
        terms = get_substance_search_terms("céfalexine")
        self.assertIn("cefalexine", terms)
        self.assertIn("cefalexin", terms)
        self.assertIn("cephalexin", terms)


class IndonesianNonMedicineTests(unittest.TestCase):
    def test_cosmetic_food_and_supplement_notifications_are_recognised(self):
        from sources.regional_live import bpom_is_non_medicine

        for number in ("NA18230105897", "NC24230100028", "MD 234512", "SD151234", "SI0123"):
            self.assertTrue(bpom_is_non_medicine(number), number)
        for number in ("GKL1234567", "DKL0101010", "DBL9876543"):
            self.assertFalse(bpom_is_non_medicine(number), number)


class VendorDataRepairTests(unittest.TestCase):
    def setUp(self):
        seed = patch.object(repository, "PRODUCT_DETAILS_SEED_PATH", Path("no-seed.jsonl"))
        seed.start()
        self.addCleanup(seed.stop)

    def _database(self, directory):
        return patch.object(repository, "DB_PATH", Path(directory) / "test.db")

    def _products(self):
        with repository.get_connection() as conn:
            return {row[0]: dict(row) for row in conn.execute(
                "SELECT product, * FROM product_details"
            )}

    def test_rows_no_buyer_should_see_are_removed(self):
        from services.data_repairs import remove_non_pharmaceutical_rows

        with tempfile.TemporaryDirectory() as directory, self._database(directory):
            repository.save_product_details([
                {"product": "Glucophage", "substance": "metformin", "source": ""},
                {"product": "Arnica Montana Boiron", "source": "France BDPM", "country": "France",
                 "substance": "ARNICA MONTANA POUR PRÉPARATIONS HOMÉOPATHIQUES"},
                {"product": "Allergy Relief", "source": "FDA", "country": "United States",
                 "substance": "sodium chloride", "strength": "12 [hp_X]"},
                {"product": "Daily Toner", "source": "BPOM Indonesia", "country": "Indonesia",
                 "registration_number": "NA18221205669"},
                {"product": "Amlodipine 5 mg", "source": "BPOM Indonesia", "country": "Indonesia",
                 "registration_number": "GKL1234567"},
            ])
            result = remove_non_pharmaceutical_rows()
            remaining = sorted(self._products())
        self.assertEqual(remaining, ["Amlodipine 5 mg"])
        self.assertEqual(result["homeopathic"], 2)

    def test_identical_rows_are_merged_and_the_keeper_takes_what_only_a_copy_had(self):
        from services.data_repairs import merge_identical_rows

        base = {"product": "Metformin 500 mg", "source": "FDA", "country": "United States",
                "registration_number": "ANDA123", "strength": "500 mg"}
        with tempfile.TemporaryDirectory() as directory, self._database(directory):
            with repository.get_connection() as conn:
                repository.initialize_database()
                for extra in ({"atc_code": "A10BA02"}, {"smpc_url": "https://label"}):
                    record = {**base, **extra}
                    conn.execute(
                        f"INSERT INTO product_details ({', '.join(record)}) VALUES ({', '.join('?' for _ in record)})",
                        tuple(record.values()),
                    )
            result = merge_identical_rows()
            with repository.get_connection() as conn:
                rows = [dict(row) for row in conn.execute("SELECT atc_code, smpc_url FROM product_details")]
        self.assertEqual(result["copies_removed"], 1)
        self.assertEqual(rows, [{"atc_code": "A10BA02", "smpc_url": "https://label"}])

    def test_canadian_rows_point_at_the_record_that_still_answers(self):
        from services.data_repairs import relink_health_canada_products

        old = "https://health-products.canada.ca/dpd-bdpp/info.do?lang=en&code=12345"
        with tempfile.TemporaryDirectory() as directory, self._database(directory):
            repository.save_product_details([
                {"product": "APO-METFORMIN", "source": "Health Canada", "country": "Canada",
                 "product_url": old, "source_url": old},
            ])
            relink_health_canada_products()
            row = self._products()["APO-METFORMIN"]
        expected = "https://health-products.canada.ca/api/drug/drugproduct/?lang=en&type=json&id=12345"
        self.assertEqual(row["product_url"], expected)
        self.assertEqual(row["source_url"], expected)

    def test_stored_markup_goes_but_the_registry_wording_stays(self):
        from services.data_repairs import clean_stored_markup

        with tempfile.TemporaryDirectory() as directory, self._database(directory):
            repository.save_product_details([
                {"product": "Hydrocortisone &amp; Zinc Cream", "source": "BPOM Indonesia",
                 "country": "Indonesia", "atc_code": "Not yet assigned", "strength": "N/A",
                 "status": "Berlaku", "substance": "salmeterol;fluticasone"},
            ])
            clean_stored_markup()
            row = next(iter(self._products().values()))
        self.assertEqual(row["product"], "Hydrocortisone & Zinc Cream")
        self.assertEqual(row["atc_code"], "")
        self.assertEqual(row["strength"], "")
        self.assertEqual(row["status"], "Berlaku")
        self.assertEqual(row["substance"], "salmeterol;fluticasone")


if __name__ == "__main__":
    unittest.main()
