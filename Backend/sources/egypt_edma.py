"""Egypt (EDMA) - Egyptian Drug Authority

EDMA is Egypt's pharmaceutical regulatory authority.
Source: https://www.eda.gov.eg

Pharmaceutical products registry.
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

def run_edma_egypt_search(substance: str, country_filter: str = "") -> Iterator[dict]:
    """
    Search EDMA Egypt pharmaceutical registry.

    Yields products with fields:
    - product: product name
    - substance: active substance
    - applicant_sponsor: company
    - company: manufacturer
    - country: "Egypt"
    - status: authorization status
    - source: "EDMA"
    """

    if not substance or len(substance.strip()) < 2:
        return

    try:
        logger.info(f"Searching EDMA Egypt for: {substance}")

        session = requests.Session()
        session.headers.update(HEADERS)

        # EDMA product search
        search_url = "https://www.eda.gov.eg/ar/search"

        search_params = {
            'q': substance,
            'type': 'product'
        }

        try:
            resp = session.get(search_url, params=search_params, timeout=TIMEOUT)
            resp.raise_for_status()
        except (requests.ConnectionError, requests.Timeout) as e:
            logger.warning(f"EDMA connection error: {e}")
            return

        if resp.status_code != 200:
            logger.debug(f"EDMA returned {resp.status_code}")
            return

        soup = BeautifulSoup(resp.content, 'html.parser')

        # Find product listings
        product_items = soup.select(
            'div.product-item, div.medicine-card, li.product, article.medicine'
        )

        if not product_items:
            logger.debug("No product items found")
            return

        products_found = 0

        for item in product_items:
            try:
                # Extract product information
                name_elem = item.select_one('h2, h3, .product-name, .medicine-name')
                name = name_elem.get_text(strip=True) if name_elem else ''

                company_elem = item.select_one('.company, .manufacturer, .producer')
                company = company_elem.get_text(strip=True) if company_elem else ''

                substance_elem = item.select_one('.active-ingredient, .substance, .ingredient')
                active_substance = substance_elem.get_text(strip=True) if substance_elem else substance

                status_elem = item.select_one('.status, .authorization')
                status = status_elem.get_text(strip=True) if status_elem else 'Registered'

                if name and len(name) > 2:
                    products_found += 1
                    yield {
                        'product': name,
                        'substance': active_substance,
                        'applicant_sponsor': company,
                        'company': company,
                        'country': 'Egypt',
                        'status': status,
                        'source': 'EDMA',
                    }

                    if products_found >= 100:
                        return

            except Exception as e:
                logger.debug(f"Error parsing product item: {e}")
                continue

        if products_found == 0:
            logger.info(f"No EDMA products found for {substance}")

    except Exception as e:
        logger.error(f"EDMA search error: {e}")
