import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("PHARMASEARCH_DB_PATH", BASE_DIR / "pharmasearch.db"))
EXPORT_DIR = Path(os.getenv("PHARMASEARCH_EXPORT_DIR", BASE_DIR / "exports"))

EXPORT_DIR.mkdir(parents=True, exist_ok=True)
