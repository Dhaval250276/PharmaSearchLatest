from playwright.sync_api import sync_playwright


def run_ema_search(substance):

    results = []

    with sync_playwright() as p:

        browser = p.chromium.launch(headless=True)

        page = browser.new_page()

        url = f"https://www.ema.europa.eu/en/search?search_api_fulltext={substance}"

        print("SEARCH URL:", url)

        page.goto(url, timeout=60000)

        page.wait_for_timeout(5000)

        body_text = page.locator("body").inner_text()

        if "technical difficulties with our search function" in body_text.lower():
            print("EMA search unavailable")
            browser.close()
            return []
        print("TITLE:", page.title())

        print("BODY LENGTH:",len(body_text))
        page.wait_for_load_state("networkidle")
        with open("ema_search_output.txt",
              "w",
              encoding="utf-8"
        ) as f:
            f.write(body_text)

        print("Output saved to ema_search_output.txt")
        results = []
        links = page.locator("a").all()

        for link in links:

            try:

                href = link.get_attribute("href")

                text = link.inner_text().strip()

                if (
                        href
                        and "/medicines/human/EPAR/" in str(href)
                        and substance.lower() in text.lower()
                    ):
                    if href.startswith("/"):
                        href = "https://www.ema.europa.eu" + href

                    results.append({
                        "product": text,
                        "url": href
                    })

            except:
                pass

        browser.close()
        print("BODY LENGTH:", len(body_text))
        print(body_text[:2000])
        print("TOTAL EMA RESULTS:", len(results))

        for r in results:
            print("EMA PRODUCT:", r["product"])

        return results
def find_product_url(substance):

    results = run_ema_search(substance)

    if not results:
        return None

    return results[0]["url"]

