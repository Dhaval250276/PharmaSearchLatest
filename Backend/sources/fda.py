import requests


def run_fda_search(substance):

    url = (
        "https://api.fda.gov/drug/label.json"
        f"?search=openfda.substance_name:{substance}"
        "&limit=10"
    )

    print("FDA URL:", url)

    try:

        response = requests.get(url, timeout=30)

        data = response.json()

        results = []

        for item in data.get("results", []):

            brand = ""

            if "openfda" in item:
                brand = (
                    item["openfda"]
                    .get("brand_name", [""])[0]
                )

            results.append({
                "substance": substance,
                "product": brand,
                "country": "United States",
                "source": "FDA",
                "url": "https://www.fda.gov"
            })

        print("FDA RESULTS:", len(results))

        return results

    except Exception as e:

        print("FDA Error:", e)

        return []
