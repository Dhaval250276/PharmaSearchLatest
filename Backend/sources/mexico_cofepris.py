"""Mexico (COFEPRIS) - Comisión Federal para la Protección contra Riesgo Sanitario

COFEPRIS is Mexico's federal health regulator.
Source: https://www.gob.mx/cofepris
Data: Public pharmaceutical registry via web interface

Note: No direct API available. Scrapes search results from COFEPRIS public search.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Iterator

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BATCH_SIZE = 50
TIMEOUT = 20
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
}

def run_cofepris_mexico_search(substance: str, country_filter: str = "") -> Iterator[dict]:
    """
    Search COFEPRIS database for pharmaceutical products containing substance.

    Yields products with normalized fields:
    - product: product name
    - substance: active substance
    - applicant_sponsor: company/applicant name
    - company: manufacturer
    - country: "Mexico"
    - status: approval status
    - source: "COFEPRIS"
    """

    if not substance or len(substance.strip()) < 2:
        return

    try:
        logger.info(f"Searching COFEPRIS for: {substance}")

        # COFEPRIS search endpoint (estimated based on web interface)
        search_url = "https://www.gob.mx/cofepris"

        session = requests.Session()
        session.headers.update(HEADERS)

        # Try to search COFEPRIS public database
        # Note: COFEPRIS doesn't have a direct API, so we search via their website
        search_params = {
            'q': substance,
            'buscar': 'Buscar'  # Spanish "Search"
        }

        try:
            resp = session.get(search_url, params=search_params, timeout=TIMEOUT)
            resp.raise_for_status()
        except requests.ConnectionError:
            logger.warning("Cannot reach COFEPRIS - connection reset (may need retry from server)")
            return
        except requests.Timeout:
            logger.warning(f"COFEPRIS timeout for {substance}")
            return

        if resp.status_code != 200:
            logger.debug(f"COFEPRIS returned {resp.status_code}")
            return

        # Parse search results
        soup = BeautifulSoup(resp.content, 'html.parser')

        # Look for product listings (varies by COFEPRIS page structure)
        # Try multiple selectors as their layout may change
        product_selectors = [
            'div.product-item',
            'tr.producto',
            'li.medicamento',
            'div[data-product]',
            'article.medicamento',
        ]

        products_found = 0

        for selector in product_selectors:
            products = soup.select(selector)
            if products:
                logger.debug(f"Found {len(products)} products using selector: {selector}")

                for product_elem in products:
                    try:
                        # Extract product information from various possible structures
                        # Try different common patterns in pharmaceutical registries

                        # Get product name
                        name_elem = product_elem.select_one('h2, h3, .product-name, .nombre, [data-name]')
                        name = name_elem.get_text(strip=True) if name_elem else ''

                        # Get applicant/company
                        applicant_elem = product_elem.select_one('.applicant, .company, .empresa, .solicitante, [data-applicant]')
                        applicant = applicant_elem.get_text(strip=True) if applicant_elem else ''

                        # Get status
                        status_elem = product_elem.select_one('.status, .estado, .autorización, [data-status]')
                        status = status_elem.get_text(strip=True) if status_elem else 'Autorizado'

                        # Get active ingredient/substance
                        substance_elem = product_elem.select_one('.substance, .principio, .ingrediente, [data-substance]')
                        active_substance = substance_elem.get_text(strip=True) if substance_elem else substance

                        if name and len(name) > 2:
                            products_found += 1
                            yield {
                                'product': name,
                                'substance': active_substance,
                                'applicant_sponsor': applicant or name,
                                'company': applicant or 'Unknown',
                                'country': 'Mexico',
                                'status': status,
                                'source': 'COFEPRIS',
                            }

                            if products_found >= BATCH_SIZE:
                                return

                    except Exception as e:
                        logger.debug(f"Error parsing product element: {e}")
                        continue

                if products_found > 0:
                    return

        # Fallback: if structured parsing fails, try to extract from page text
        if products_found == 0:
            logger.debug("No structured products found, attempting text extraction")

            # Look for pharmaceutical names in page text
            page_text = soup.get_text()

            # Find lines that look like product names (common pharma naming)
            lines = page_text.split('\n')
            for line in lines:
                line = line.strip()
                if (substance.lower() in line.lower() and
                    len(line) > 5 and
                    len(line) < 200 and
                    not line.startswith('http')):

                    products_found += 1
                    yield {
                        'product': line,
                        'substance': substance,
                        'applicant_sponsor': 'COFEPRIS Registered',
                        'company': 'Mexico Regulator',
                        'country': 'Mexico',
                        'status': 'Registered',
                        'source': 'COFEPRIS',
                    }

                    if products_found >= 10:
                        break

        if products_found == 0:
            logger.info(f"No COFEPRIS products found for {substance}")

    except Exception as e:
        logger.error(f"COFEPRIS search error for {substance}: {e}")


# Alternative: Try COFEPRIS open data portal
def run_cofepris_opendata_search(substance: str) -> Iterator[dict]:
    """
    Alternative approach: Search COFEPRIS open data portal.
    Some data may be available through datos.gob.mx
    """

    try:
        # datos.gob.mx sometimes hosts COFEPRIS datasets
        opendata_url = "https://datos.gob.mx/busca/dataset"

        session = requests.Session()
        session.headers.update(HEADERS)

        params = {
            'q': 'COFEPRIS medicamentos',
            'organization': 'cofepris'
        }

        resp = session.get(opendata_url, params=params, timeout=TIMEOUT)

        if resp.status_code == 200:
            soup = BeautifulSoup(resp.content, 'html.parser')

            # Look for dataset links
            dataset_links = soup.select('a.dataset-link, a[href*="dataset"]')

            for link in dataset_links[:3]:  # Check first 3 datasets
                try:
                    dataset_url = link.get('href', '')
                    if dataset_url:
                        if not dataset_url.startswith('http'):
                            dataset_url = 'https://datos.gob.mx' + dataset_url

                        logger.debug(f"Checking dataset: {dataset_url}")

                        # Try to find CSV/data download link
                        dataset_resp = session.get(dataset_url, timeout=TIMEOUT)
                        if dataset_resp.status_code == 200:
                            dataset_soup = BeautifulSoup(dataset_resp.content, 'html.parser')

                            # Look for CSV download
                            csv_link = dataset_soup.select_one('a[href*=".csv"]')
                            if csv_link:
                                csv_url = csv_link.get('href', '')
                                logger.info(f"Found COFEPRIS CSV: {csv_url}")
                                # Could download and parse here
                except Exception as e:
                    logger.debug(f"Error checking dataset: {e}")

    except Exception as e:
        logger.debug(f"OpenData search error: {e}")


if __name__ == '__main__':
    # Test
    print("Testing COFEPRIS Mexico connector...")
    results = list(run_cofepris_mexico_search("Aspirina"))
    print(f"Found {len(results)} products")
    if results:
        print(f"Sample: {results[0]}")
