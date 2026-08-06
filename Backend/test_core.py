import unittest
import os
from unittest.mock import patch

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
from sources.mhra_document_parser import _document_links_from_html, _metadata_from_text
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
from sources.source_registry import CONNECTORS, SOURCES, connector_metadata
from sources.tga import _fallback_rows, _merge_detail, _parse_artg_detail, _parse_artg_search_results
from services.ai_client import ai_extract_regulatory_fields, ai_status
from services.ai_enrichment import enrichment_metadata, missing_enrichment_fields
from services.connector_health import connector_health_rows, record_source_health
from services.connector_status import connector_status_rows
from services.english_normalizer import english_row, english_text
from services.field_availability import NOT_APPLICABLE, PENDING_ENRICHMENT, field_value
from services.search_pipeline import (
    _country_lookup_rows,
    _eu_lookup_rows,
    filtered_search_results,
    prepared_cached_results,
    sources_for_scope,
    suppress_generic_lookup_rows,
    is_connector_lookup_fallback,
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

    def test_missing_mhra_manufacturer_is_marked_for_enrichment(self):
        row = {"source": "MHRA", "pil_url": "https://example.test/pil.pdf"}
        self.assertEqual(field_value(row, "manufacturer_name"), PENDING_ENRICHMENT)

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

    def test_cdsco_india_search_uses_pdf_parser(self):
        expected = [
            {
                "substance": "dapagliflozin",
                "product": "Dapagliflozin approval row",
                "country": "India",
                "source": "CDSCO India",
            }
        ]

        with patch("sources.regional_live._run_cdsco_india_pdf_search", return_value=expected) as parser:
            rows = run_cdsco_india_search("dapagliflozin")

        parser.assert_called_once_with("dapagliflozin")
        self.assertEqual(rows, expected)


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


if __name__ == "__main__":
    unittest.main()
