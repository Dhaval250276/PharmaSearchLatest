from sources.mhra import run_mhra_search
from sources.ema import run_ema_search

def search_substance(substance):

    results = []

    try:
        mhra_results = run_mhra_search(substance)
        results.extend(mhra_results)
    except Exception as e:
        print("MHRA Error:", e)

    try:
        ema_results = run_ema_search(substance)
        results.extend(ema_results)
    except Exception as e:
        print("EMA Error:", e)

    return results
