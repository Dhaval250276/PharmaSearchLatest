import traceback
from playwright.sync_api import sync_playwright
from sources.parser import clean_product_name


def run_mhra_search(substance):

    results = []

    try:

        with sync_playwright() as p:

            browser = p.chromium.launch(headless=True)

            page = browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/137.0.0.0 Safari/537.36"
                )
            )

            url = (
                f"https://products.mhra.gov.uk/search/"
                f"?search={substance}&page=1"
            )

            print("SEARCH URL:", url)

            success = False

            for attempt in range(3):

                try:

                    page.goto(
                        url,
                        wait_until="networkidle",
                        timeout=90000
                    )

                    success = True
                    break

                except Exception as e:

                    print(
                        f"ATTEMPT {attempt + 1} FAILED:",
                        e
                    )

                    page.wait_for_timeout(5000)

            if not success:

                print("MHRA CONNECTION FAILED")

                browser.close()

                return []

            page.wait_for_timeout(5000)

            try:

                checkbox = page.locator(
                    "input[type='checkbox']"
                )

                if checkbox.count() > 0:

                    checkbox.check()

                    page.wait_for_timeout(1000)

                    page.get_by_text(
                        "Agree",
                        exact=True
                    ).click()

                    page.wait_for_timeout(3000)

            except Exception as e:

                print("COOKIE WARNING:", e)

            print("PAGE TITLE:", page.title())
            print("PAGE URL:", page.url)

            with open(
                "mhra_debug.html",
                "w",
                encoding="utf-8"
            ) as f:

                f.write(page.content())

            links = page.locator("a").all()

            print("TOTAL LINKS FOUND:", len(links))

            for link in links:

                try:

                    text = link.inner_text().strip()

                    href = link.get_attribute("href")

                    if not href:
                        continue
                    print("DOCUMENT URL:", href)

                    clean_text = " ".join(
                        text.split()
                    )

                    clean_text = clean_product_name(
                        clean_text
                    )

                    # Ignore non-product links
                    if "PL " not in clean_text:
                        continue

                    print(
                        "PRODUCT:",
                        clean_text
                    )

                    print(
                        "URL:",
                        href
                    )
                    print("DOCUMENT URL:", href)
                    doc_page = browser.new_page()

                    doc_page.goto(
                        href,
                        wait_until="networkidle",
                        timeout=60000
                    )

                    html = doc_page.content()

                    print("DOCUMENT LOADED")

                    doc_page.close()


                    print("-" * 100)

                    results.append({
                        "substance": substance,
                        "product": clean_text,
                        "country": "United Kingdom",
                        "source": "MHRA",
                        "url": href
                    })

                except Exception as e:

                    print("ERROR:", e)

                    traceback.print_exc()

            browser.close()

    except Exception as e:

        print("MHRA ERROR:", e)

        traceback.print_exc()

    print("TOTAL MHRA RESULTS:", len(results))

    return results
