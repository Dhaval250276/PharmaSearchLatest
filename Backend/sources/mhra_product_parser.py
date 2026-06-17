from playwright.sync_api import sync_playwright


def extract_mhra_product_page(url):

    with sync_playwright() as p:

        browser = p.chromium.launch(headless=False)

        page = browser.new_page()

        page.goto(url)

        page.wait_for_timeout(3000)
        print("URL:", page.url)
        print("TITLE:", page.title())

        body_text = page.locator("body").inner_text()

        print(body_text[:3000])

        try:

            checkbox = page.locator("input[type='checkbox']")

            if checkbox.count() > 0:

                checkbox.check()

                page.wait_for_timeout(1000)

                page.get_by_text("Agree", exact=True).click()

                page.wait_for_timeout(3000)

        except Exception as e:

            print("COOKIE WARNING:", e)
        current_url = page.url
        title = page.title()

        print("CURRENT URL:", current_url)
        print("TITLE:", title)

        body_text = page.locator("body").inner_text()

        # Use page title instead of hardcoded FORXIGA
        product_name = title.strip()
        print("PRODUCT:", product_name)


        active_substance = ""

        for line in body_text.split("\n"):

            if "Active substances:" in line:

                active_substance = (
                    line.replace("Active substances:", "")
                    .strip()
                )

                break

        print("ACTIVE:", active_substance)

        with open(
            "mhra_output_after_agree.txt",
            "w",
            encoding="utf-8"
        ) as f:
            f.write(body_text)
            print("Saved output after agree")

        browser.close()

        return {
            "product_name": product_name,
            "active_substance": active_substance,
            "product_url": current_url
        }
