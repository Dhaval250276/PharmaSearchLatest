"""Guatemala - MSPAS (Ministry of Public Health)

Guatemala's pharmaceutical regulatory authority.
Source: https://www.mspas.gob.gt

Pharmaceutical products registry - Central America.
"""

from __future__ import annotations
import logging
from typing import Iterator
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)
TIMEOUT = 20
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

def run_mspas_guatemala_search(substance: str, country_filter: str = "") -> Iterator[dict]:
    """Search Guatemala MSPAS pharmaceutical registry."""
    if not substance or len(substance.strip()) < 2:
        return

    try:
        logger.info(f"Searching Guatemala MSPAS for: {substance}")
        session = requests.Session()
        session.headers.update(HEADERS)

        search_url = "https://www.mspas.gob.gt/medicamentos/search"
        search_params = {'q': substance}

        try:
            resp = session.get(search_url, params=search_params, timeout=TIMEOUT)
            resp.raise_for_status()
        except (requests.ConnectionError, requests.Timeout):
            return

        if resp.status_code != 200:
            return

        soup = BeautifulSoup(resp.content, 'html.parser')
        products = soup.select('div.medicamento, tr.product, li.medicine')

        products_found = 0
        for product in products:
            try:
                name_elem = product.select_one('h3, .nombre')
                name = name_elem.get_text(strip=True) if name_elem else ''

                company_elem = product.select_one('.empresa, .manufacturer')
                company = company_elem.get_text(strip=True) if company_elem else ''

                if name and len(name) > 2:
                    products_found += 1
                    yield {
                        'product': name,
                        'substance': substance,
                        'applicant_sponsor': company,
                        'company': company,
                        'country': 'Guatemala',
                        'status': 'Autorizado',
                        'source': 'MSPAS',
                    }
                    if products_found >= 100:
                        return
            except:
                continue
    except Exception as e:
        logger.error(f"Guatemala search error: {e}")
