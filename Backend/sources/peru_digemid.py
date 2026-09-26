"""Peru (DIGEMID) - Dirección General de Medicamentos, Insumos y Drogas

DIGEMID is Peru's pharmaceutical regulatory authority.
Source: https://www.digemid.minsa.gob.pe

Pharmaceutical registry via API/web interface.
"""

from __future__ import annotations

import logging
import re
from typing import Iterator

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

TIMEOUT = 20
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
}

def run_digemid_peru_search(substance: str, country_filter: str = "") -> Iterator[dict]:
    """
    Search DIGEMID Peru pharmaceutical registry.

    Yields products with fields:
    - product: product name
    - substance: active substance
    - applicant_sponsor: company
    - company: manufacturer
    - country: "Peru"
    - status: authorization status
    - source: "DIGEMID"
    """

    if not substance or len(substance.strip()) < 2:
        return

    try:
        logger.info(f"Searching DIGEMID Peru for: {substance}")

        session = requests.Session()
        session.headers.update(HEADERS)

        # DIGEMID search via web interface
        search_url = "https://www.digemid.minsa.gob.pe/Publico/BuscadorProductos.aspx"

        # Try direct search parameters
        search_params = {
            'txtNombreProducto': substance,
            'txtNombreEmpresa': '',
            'ddlTipoProducto': '',
            'ddlEstado': '',
        }

        try:
            resp = session.get(search_url, params=search_params, timeout=TIMEOUT)
            resp.raise_for_status()
        except (requests.ConnectionError, requests.Timeout) as e:
            logger.warning(f"DIGEMID connection error: {e}")
            return

        if resp.status_code != 200:
            logger.debug(f"DIGEMID returned {resp.status_code}")
            return

        soup = BeautifulSoup(resp.content, 'html.parser')

        # Find results table
        results_table = soup.select_one('table.gridview, table[summary*="productos"], table#gvProductos')

        if not results_table:
            logger.debug("No results table found")
            return

        products_found = 0
        rows = results_table.select('tbody tr, tr[class*="item"]')

        for row in rows:
            try:
                cells = row.select('td')
                if len(cells) < 3:
                    continue

                # Typical DIGEMID table: Name | Company | Substance | Status
                product_name = cells[0].get_text(strip=True)
                company = cells[1].get_text(strip=True) if len(cells) > 1 else ''
                active_substance = cells[2].get_text(strip=True) if len(cells) > 2 else substance
                status = cells[3].get_text(strip=True) if len(cells) > 3 else 'Autorizado'

                if product_name and len(product_name) > 2:
                    products_found += 1
                    yield {
                        'product': product_name,
                        'substance': active_substance,
                        'applicant_sponsor': company,
                        'company': company,
                        'country': 'Peru',
                        'status': status,
                        'source': 'DIGEMID',
                    }

                    if products_found >= 100:
                        return

            except Exception as e:
                logger.debug(f"Error parsing row: {e}")
                continue

        if products_found == 0:
            logger.info(f"No DIGEMID products found for {substance}")

    except Exception as e:
        logger.error(f"DIGEMID search error: {e}")
