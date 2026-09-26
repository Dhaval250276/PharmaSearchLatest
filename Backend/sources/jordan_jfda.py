"""Jordan - JFDA (Jordan Food and Drug Administration)

JFDA is Jordan's pharmaceutical regulatory authority - Middle East hub.
Source: https://www.jfda.jo

Pharmaceutical products registry.
"""

from __future__ import annotations
import logging
from typing import Iterator
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)
TIMEOUT = 20
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

def run_jfda_jordan_search(substance: str, country_filter: str = "") -> Iterator[dict]:
    """Search Jordan JFDA pharmaceutical registry."""
    if not substance or len(substance.strip()) < 2:
        return

    try:
        logger.info(f"Searching Jordan JFDA for: {substance}")
        session = requests.Session()
        session.headers.update(HEADERS)

        search_url = "https://www.jfda.jo/drugs/search"
        search_params = {'q': substance}

        try:
            resp = session.get(search_url, params=search_params, timeout=TIMEOUT)
            resp.raise_for_status()
        except (requests.ConnectionError, requests.Timeout):
            return

        if resp.status_code != 200:
            return

        soup = BeautifulSoup(resp.content, 'html.parser')
        products = soup.select('div.drug, tr.medicine, li.pharmaceutical')

        products_found = 0
        for product in products:
            try:
                name_elem = product.select_one('h3, .drug-name')
                name = name_elem.get_text(strip=True) if name_elem else ''

                company_elem = product.select_one('.company, .manufacturer')
                company = company_elem.get_text(strip=True) if company_elem else ''

                if name and len(name) > 2:
                    products_found += 1
                    yield {
                        'product': name,
                        'substance': substance,
                        'applicant_sponsor': company,
                        'company': company,
                        'country': 'Jordan',
                        'status': 'Approved',
                        'source': 'JFDA',
                    }
                    if products_found >= 100:
                        return
            except:
                continue
    except Exception as e:
        logger.error(f"Jordan search error: {e}")
