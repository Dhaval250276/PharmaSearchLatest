"""Thailand (TFDA) - Thai Food and Drug Administration

TFDA is Thailand's pharmaceutical regulatory authority.
Source: https://www.fda.moph.go.th

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

def run_thailand_fda_search(substance: str, country_filter: str = "") -> Iterator[dict]:
    """
    Search Thailand FDA pharmaceutical registry.

    Yields products with fields:
    - product: product name
    - substance: active substance
    - applicant_sponsor: company
    - company: manufacturer
    - country: "Thailand"
    - status: authorization status
    - source: "Thailand FDA"
    """

    if not substance or len(substance.strip()) < 2:
        return

    try:
        logger.info(f"Searching Thailand FDA for: {substance}")

        session = requests.Session()
        session.headers.update(HEADERS)

        # Thailand FDA product search
        search_url = "https://www.fda.moph.go.th/sites/default/files"

        # Try search interface
        search_url_alt = "https://www.fda.moph.go.th/search"

        search_params = {
            'q': substance,
            'product_type': 'medicine'
        }

        try:
            resp = session.get(search_url_alt, params=search_params, timeout=TIMEOUT)
            resp.raise_for_status()
        except (requests.ConnectionError, requests.Timeout) as e:
            logger.warning(f"Thailand FDA connection error: {e}")
            return

        if resp.status_code != 200:
            logger.debug(f"Thailand FDA returned {resp.status_code}")
            return

        soup = BeautifulSoup(resp.content, 'html.parser')

        # Find pharmaceutical products
        products = soup.select(
            'div.medicine-item, tr.product-row, li.pharmaceutical, article.medicine'
        )

        if not products:
            logger.debug("No products found")
            return

        products_found = 0

        for product in products:
            try:
                # Extract product information
                name_elem = product.select_one('h2, h3, .product-name, .medicine-name, td:nth-child(1)')
                name = name_elem.get_text(strip=True) if name_elem else ''

                # Company/Manufacturer
                company_elem = product.select_one('.company, .manufacturer, .holder, td:nth-child(2)')
                company = company_elem.get_text(strip=True) if company_elem else ''

                # Active substance
                substance_elem = product.select_one('.substance, .ingredient, .composition, td:nth-child(3)')
                active_substance = substance_elem.get_text(strip=True) if substance_elem else substance

                # Status
                status_elem = product.select_one('.status, .authorization, .approval, td:nth-child(4)')
                status = status_elem.get_text(strip=True) if status_elem else 'Approved'

                if name and len(name) > 2:
                    products_found += 1
                    yield {
                        'product': name,
                        'substance': active_substance,
                        'applicant_sponsor': company,
                        'company': company,
                        'country': 'Thailand',
                        'status': status,
                        'source': 'Thailand FDA',
                    }

                    if products_found >= 100:
                        return

            except Exception as e:
                logger.debug(f"Error parsing product: {e}")
                continue

        if products_found == 0:
            logger.info(f"No Thailand FDA products found for {substance}")

    except Exception as e:
        logger.error(f"Thailand FDA search error: {e}")
