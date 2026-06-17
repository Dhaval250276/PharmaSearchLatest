from sources.mhra import run_mhra_search
from sources.ema import run_ema_search
from sources.fda import run_fda_search


def search_substance(substance):

    results = []

    try:
        fda_results = run_fda_search(substance)
        results.extend(fda_results)
    except Exception as e:
        print("FDA Error:", e)

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

    unique = []
    seen = set()

    for item in results:

        key = (
            item.get("product", "").lower(),
            item.get("country", "")
        )

        if key not in seen:
            seen.add(key)
            unique.append(item)

    return unique
