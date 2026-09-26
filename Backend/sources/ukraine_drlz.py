"""Ukraine - DRLZ (State Register of Medicinal Products and Medical Devices)

Ukraine's pharmaceutical regulatory authority.
Source: https://drlz.com.ua

Pharmaceutical products registry - Eastern Europe.
"""

from __future__ import annotations
import logging
from typing import Iterator
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)
TIMEOUT = 20
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

def run_drlz_ukraine_search(substance: str, country_filter: str = "") -> Iterator[dict]:
    """Search Ukraine DRLZ pharmaceutical registry."""
    if not substance or len(substance.strip()) < 2:
        return

    try:
        logger.info(f"Searching Ukraine DRLZ for: {substance}")
        session = requests.Session()
        session.headers.update(HEADERS)

        search_url = "https://drlz.com.ua/medpreparat/search"
        search_params = {'query': substance}

        try:
            resp = session.get(search_url, params=search_params, timeout=TIMEOUT)
            resp.raise_for_status()
        except (requests.ConnectionError, requests.Timeout):
            return

        if resp.status_code != 200:
            return

        soup = BeautifulSoup(resp.content, 'html.parser')
        products = soup.select('div.medicine, tr.product, li.medication')

        products_found = 0
        for product in products:
            try:
                name_elem = product.select_one('h3, .med-name')
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
                        'country': 'Ukraine',
                        'status': 'Registered',
                        'source': 'DRLZ',
                    }
                    if products_found >= 100:
                        return
            except:
                continue
    except Exception as e:
        logger.error(f"Ukraine search error: {e}")
