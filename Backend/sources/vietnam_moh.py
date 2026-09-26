"""Vietnam - Ministry of Health Pharmaceutical Database

Vietnam's national pharmaceutical regulatory database.
Source: https://www.moh.gov.vn

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

def run_vietnam_moh_search(substance: str, country_filter: str = "") -> Iterator[dict]:
    """
    Search Vietnam Ministry of Health pharmaceutical registry.

    Yields products with fields:
    - product: product name
    - substance: active substance
    - applicant_sponsor: company
    - company: manufacturer
    - country: "Vietnam"
    - status: authorization status
    - source: "Vietnam MOH"
    """

    if not substance or len(substance.strip()) < 2:
        return

    try:
        logger.info(f"Searching Vietnam MOH for: {substance}")

        session = requests.Session()
        session.headers.update(HEADERS)

        # Vietnam MOH pharmaceutical search
        search_url = "https://www.moh.gov.vn/web/guest/search"

        search_params = {
            'keyword': substance,
            'type': 'pharmaceutical'
        }

        try:
            resp = session.get(search_url, params=search_params, timeout=TIMEOUT)
            resp.raise_for_status()
        except (requests.ConnectionError, requests.Timeout) as e:
            logger.warning(f"Vietnam MOH connection error: {e}")
            return

        if resp.status_code != 200:
            logger.debug(f"Vietnam MOH returned {resp.status_code}")
            return

        soup = BeautifulSoup(resp.content, 'html.parser')

        # Find pharmaceutical results
        results = soup.select('div.pharmaceutical-result, div.product-result, li.medicine')

        if not results:
            logger.debug("No pharmaceutical results found")
            return

        products_found = 0

        for result in results:
            try:
                # Extract product details
                name_elem = result.select_one('h3, h4, .product-title, .medicine-name')
                name = name_elem.get_text(strip=True) if name_elem else ''

                # Try to find company/manufacturer
                company_elem = result.select_one('.company, .manufacturer, .maker, span.company')
                company = company_elem.get_text(strip=True) if company_elem else ''

                # Active ingredient
                substance_elem = result.select_one('.substance, .ingredient, .active-component')
                active_substance = substance_elem.get_text(strip=True) if substance_elem else substance

                # Status
                status_elem = result.select_one('.status, .approval-status, .reg-status')
                status = status_elem.get_text(strip=True) if status_elem else 'Approved'

                if name and len(name) > 2:
                    products_found += 1
                    yield {
                        'product': name,
                        'substance': active_substance,
                        'applicant_sponsor': company,
                        'company': company,
                        'country': 'Vietnam',
                        'status': status,
                        'source': 'Vietnam MOH',
                    }

                    if products_found >= 100:
                        return

            except Exception as e:
                logger.debug(f"Error parsing result: {e}")
                continue

        if products_found == 0:
            logger.info(f"No Vietnam MOH products found for {substance}")

    except Exception as e:
        logger.error(f"Vietnam MOH search error: {e}")
