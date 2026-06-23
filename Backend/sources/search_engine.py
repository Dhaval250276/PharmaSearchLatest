from sources.source_registry import SOURCES


def search_substance(substance):

    results = []

    for source in SOURCES:

        try:

            print(f"RUNNING {source['name']} SEARCH")

            source_results = source["function"](substance)

            results.extend(source_results)

        except Exception as e:

            print(f"{source['name']} ERROR:", e)

    unique = []
    seen = set()

    for item in results:

        key = (
            item.get("product", "").strip().lower(),
            item.get("country", "").strip().lower()
        )

        if key not in seen:

            seen.add(key)

            unique.append(item)

    print("RAW RESULTS:", len(results))
    print("UNIQUE RESULTS:", len(unique))

    return unique
