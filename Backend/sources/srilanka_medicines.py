"""Sri Lanka - National Medicines Regulatory Authority (NMRA)

NMRA is Sri Lanka's pharmaceutical regulator.
Source: https://www.nmra.gov.lk

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

def run_nmra_srilanka_search(substance: str, country_filter: str = "") -> Iterator[dict]:
    """Search NMRA Sri Lanka pharmaceutical registry."""

    if not substance or len(substance.strip()) < 2:
        return

    try:
        logger.info(f"Searching NMRA Sri Lanka for: {substance}")

        session = requests.Session()
        session.headers.update(HEADERS)

        search_url = "https://www.nmra.gov.lk/medicines/search"
        search_params = {'q': substance}

        try:
            resp = session.get(search_url, params=search_params, timeout=TIMEOUT)
            resp.raise_for_status()
        except (requests.ConnectionError, requests.Timeout) as e:
            logger.warning(f"NMRA connection error: {e}")
            return

        if resp.status_code != 200:
            return

        soup = BeautifulSoup(resp.content, 'html.parser')
        products = soup.select('div.product, tr.medicine, li.pharmaceutical')

        products_found = 0
        for product in products:
            try:
                name_elem = product.select_one('h3, .product-name')
                name = name_elem.get_text(strip=True) if name_elem else ''

                company_elem = product.select_one('.company, .holder')
                company = company_elem.get_text(strip=True) if company_elem else ''

                if name and len(name) > 2:
                    products_found += 1
                    yield {
                        'product': name,
                        'substance': substance,
                        'applicant_sponsor': company,
                        'company': company,
                        'country': 'Sri Lanka',
                        'status': 'Registered',
                        'source': 'NMRA',
                    }

                    if products_found >= 100:
                        return
            except Exception as e:
                logger.debug(f"Error parsing: {e}")
                continue

    except Exception as e:
        logger.error(f"NMRA search error: {e}")
