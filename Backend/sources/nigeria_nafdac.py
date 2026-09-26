"""Nigeria (NAFDAC) - National Agency for Food and Drug Administration and Control

NAFDAC is Nigeria's pharmaceutical regulatory authority.
Source: https://www.nafdac.gov.ng

Pharmaceutical products registry and approved medicines.
"""

from __future__ import annotations

import logging
from typing import Iterator

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

TIMEOUT = 20
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
}

def run_nafdac_nigeria_search(substance: str, country_filter: str = "") -> Iterator[dict]:
    """
    Search NAFDAC Nigeria pharmaceutical registry.

    Yields products with fields:
    - product: product name
    - substance: active substance
    - applicant_sponsor: company
    - company: manufacturer
    - country: "Nigeria"
    - status: authorization status
    - source: "NAFDAC"
    """

    if not substance or len(substance.strip()) < 2:
        return

    try:
        logger.info(f"Searching NAFDAC Nigeria for: {substance}")

        session = requests.Session()
        session.headers.update(HEADERS)

        # NAFDAC pharmaceutical search
        search_url = "https://www.nafdac.gov.ng/index.php/search"

        search_params = {
            'search': substance,
            'product_type': 'pharmaceutical'
        }

        try:
            resp = session.get(search_url, params=search_params, timeout=TIMEOUT)
            resp.raise_for_status()
        except (requests.ConnectionError, requests.Timeout) as e:
            logger.warning(f"NAFDAC connection error: {e}")
            return

        if resp.status_code != 200:
            logger.debug(f"NAFDAC returned {resp.status_code}")
            return

        soup = BeautifulSoup(resp.content, 'html.parser')

        # Find pharmaceutical products
        products = soup.select(
            'div.product-listing, div.pharmaceutical-item, tr.medicine-row, li.registered-product'
        )

        if not products:
            logger.debug("No products found")
            return

        products_found = 0

        for product in products:
            try:
                # Extract product information
                name_elem = product.select_one('h2, h3, .product-title, .medicine-name')
                name = name_elem.get_text(strip=True) if name_elem else ''

                # Company/Manufacturer
                company_elem = product.select_one('.manufacturer, .company, .producer, .applicant')
                company = company_elem.get_text(strip=True) if company_elem else ''

                # Active substance
                substance_elem = product.select_one('.active-ingredient, .substance, .composition')
                active_substance = substance_elem.get_text(strip=True) if substance_elem else substance

                # Status
                status_elem = product.select_one('.approval-status, .status, .reg-status')
                status = status_elem.get_text(strip=True) if status_elem else 'Approved'

                if name and len(name) > 2:
                    products_found += 1
                    yield {
                        'product': name,
                        'substance': active_substance,
                        'applicant_sponsor': company,
                        'company': company,
                        'country': 'Nigeria',
                        'status': status,
                        'source': 'NAFDAC',
                    }

                    if products_found >= 100:
                        return

            except Exception as e:
                logger.debug(f"Error parsing product: {e}")
                continue

        if products_found == 0:
            logger.info(f"No NAFDAC products found for {substance}")

    except Exception as e:
        logger.error(f"NAFDAC search error: {e}")
