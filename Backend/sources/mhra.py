from playwright.sync_api import sync_playwright


def run_mhra_search(substance):

    results = []

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=True
        )

        page = browser.new_page()

        url = f"https://products.mhra.gov.uk/search/?search={substance}&page=1"

        print("SEARCH URL:", url)

        page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=60000
        )

        page.wait_for_timeout(5000)

        try:

            checkbox = page.locator("input[type='checkbox']")

            if checkbox.count() > 0:

                checkbox.check()

                page.wait_for_timeout(1000)

                page.get_by_text("Agree", exact=True).click()

                page.wait_for_timeout(3000)

        except Exception as e:

            print("COOKIE WARNING:", e)
        links = page.locator("a").all()

        for link in links:

            try:

                text = link.inner_text().strip()

                href = link.get_attribute("href")

                if not href:
                    continue

                    clean_text = " ".join(text.split())

                    words = clean_text.split()

                    half = len(words) // 2

                if len(words) > 4 and words[:half] == words[half:]:
                    clean_text = " ".join(words[:half])

                    clean_text = clean_text.replace(
                        "METFORMIN HYDROCHLORIDE METFORMIN HYDROCHLORIDE",
                        "METFORMIN HYDROCHLORIDE"
                    )
                    parts = clean_text.split(" ", 2)

                if len(parts) > 2:
                    first_half = " ".join(parts[:2])

                    if clean_text.startswith(first_half + " " + first_half):
                        clean_text = clean_text.replace(first_half + " ", "", 1)

                if clean_text.startswith(substance.upper() + " " + substance.upper()):
                    clean_text = clean_text.replace(
                        substance.upper() + " ",
                        "",
                        1
                    )

                if "PL " not in clean_text:
                    continue

                results.append({
                    "substance": substance,
                    "product": clean_text,
                    "country": "United Kingdom",
                    "source": "MHRA",
                    "url": href
                })

            except Exception:
                pass

        browser.close()

        print("TOTAL MHRA RESULTS:", len(results))

        return results
