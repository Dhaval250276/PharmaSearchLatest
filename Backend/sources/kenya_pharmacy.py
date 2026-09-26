"""Kenya - Pharmacy and Poisons Board (PPB)

Kenya's pharmaceutical regulatory authority.
Source: https://www.pharmacyboardkenya.org

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

def run_kenya_ppb_search(substance: str, country_filter: str = "") -> Iterator[dict]:
    """
    Search Kenya Pharmacy and Poisons Board pharmaceutical registry.

    Yields products with fields:
    - product: product name
    - substance: active substance
    - applicant_sponsor: company
    - company: manufacturer
    - country: "Kenya"
    - status: authorization status
    - source: "Kenya PPB"
    """

    if not substance or len(substance.strip()) < 2:
        return

    try:
        logger.info(f"Searching Kenya PPB for: {substance}")

        session = requests.Session()
        session.headers.update(HEADERS)

        # Kenya PPB pharmaceutical search
        search_url = "https://www.pharmacyboardkenya.org/index.php/search"

        search_params = {
            'q': substance,
            'type': 'medicines'
        }

        try:
            resp = session.get(search_url, params=search_params, timeout=TIMEOUT)
            resp.raise_for_status()
        except (requests.ConnectionError, requests.Timeout) as e:
            logger.warning(f"Kenya PPB connection error: {e}")
            return

        if resp.status_code != 200:
            logger.debug(f"Kenya PPB returned {resp.status_code}")
            return

        soup = BeautifulSoup(resp.content, 'html.parser')

        # Find pharmaceutical products
        products = soup.select(
            'div.medicine-item, tr.product-row, li.medicine, div.drug-entry'
        )

        if not products:
            logger.debug("No products found")
            return

        products_found = 0

        for product in products:
            try:
                # Extract product information
                name_elem = product.select_one('h3, h4, .product-name, .medicine-name')
                name = name_elem.get_text(strip=True) if name_elem else ''

                # Company/Manufacturer
                company_elem = product.select_one('.company, .manufacturer, .holder')
                company = company_elem.get_text(strip=True) if company_elem else ''

                # Active substance
                substance_elem = product.select_one('.substance, .ingredient, .composition')
                active_substance = substance_elem.get_text(strip=True) if substance_elem else substance

                # Status
                status_elem = product.select_one('.status, .approval-status, .registration')
                status = status_elem.get_text(strip=True) if status_elem else 'Registered'

                if name and len(name) > 2:
                    products_found += 1
                    yield {
                        'product': name,
                        'substance': active_substance,
                        'applicant_sponsor': company,
                        'company': company,
                        'country': 'Kenya',
                        'status': status,
                        'source': 'Kenya PPB',
                    }

                    if products_found >= 100:
                        return

            except Exception as e:
                logger.debug(f"Error parsing product: {e}")
                continue

        if products_found == 0:
            logger.info(f"No Kenya PPB products found for {substance}")

    except Exception as e:
        logger.error(f"Kenya PPB search error: {e}")
