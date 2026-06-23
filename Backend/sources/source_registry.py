from sources.fda import run_fda_search
from sources.mhra import run_mhra_search
from sources.ema import run_ema_search
from sources.health_canada import run_health_canada_search

SOURCES = [
    {"name": "FDA", "function": run_fda_search},
    {"name": "MHRA", "function": run_mhra_search},
    # {"name": "EMA", "function": run_ema_search},
    {"name": "Health Canada", "function": run_health_canada_search}
]

