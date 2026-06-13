from sources.ema import find_product_url
from sources.ema_product_parser import extract_product_page
from sources.product_details import save_product_details
from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
import sqlite3
import pandas as pd


from sources.ema import run_ema_search
from sources.mhra import run_mhra_search
from sources.mhra_product_parser import extract_mhra_product_page


app = FastAPI()

templates = Jinja2Templates(directory="templates")

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html"
    )

@app.get("/crawl_mhra")
def crawl_mhra(substance: str):

    url = f"https://products.mhra.gov.uk/search/?search={substance}&page=1"

    result = extract_mhra_product_page(url)

    print("MHRA RESULT:")
    print(result)

    save_product_details(
        result["active_substance"],
        result["product_name"],
        "",
        "UK",
        "Authorised",
        result["product_url"],
        "",
        "",
        "",
        ""
    )

    return result



@app.get("/crawl/{substance}")
def crawl(substance: str):

    result = run_ema_search(substance)

    return result


@app.get("/search/{substance}")
def search(substance: str):

    conn = sqlite3.connect("pharmasearch.db")

    cursor = conn.cursor()

    cursor.execute("""
        SELECT substance, product, company, country, status, source
        FROM medicines
        WHERE substance LIKE ?
    """, (f"%{substance}%",))
    print(cursor.fetchall())
    rows = cursor.fetchall()

    conn.close()

    results = []

    for row in rows:
        
        results.append({
            "substance": row[0],
            "product": row[1],
            "company": row[2],
            "country": row[3],
            "status": row[4],
            "source": row[5]
        })

    return results


@app.get("/reset_db")
def reset_db():

    conn = sqlite3.connect("pharmasearch.db")

    cursor = conn.cursor()

    cursor.execute("DELETE FROM medicines")

    conn.commit()

    conn.close()

    return {"message": "Database cleared"}


@app.get("/export/{substance}")
def export(substance: str):

    import pandas as pd

    conn = sqlite3.connect("pharmasearch.db")

    query = """
        SELECT
        product,
        substance,
        company,
        country,
        region,
        status,
        atc_code,
        registration_date,
        smpc_url,
        pil_url,
        product_url
        FROM product_details
        WHERE substance LIKE ?
    """
    df = pd.read_sql_query(
        query,
        conn,
        params=(f"%{substance}%",)
    )
    print(df[["product", "country", "region"]])
    print("ROWS FOUND:", len(df))
    print(df[["product", "substance"]])

    conn.close()

    export_df = pd.DataFrame()

    export_df["Country"] = df["country"]
    export_df["Region"] = export_df["Country"].apply(
        lambda x: "UK" if x == "United Kingdom" else "EU"
    )
    export_df["Brand Name"] = df["product"]
    export_df["Molecule (Active Ingredient(s))"] = df["substance"]
    export_df["Strength"] = export_df["Brand Name"].str.extract(
        r'(\d+\s*MG)',
        expand=False
        ).fillna("10 MG")
    export_df["Dosage Form"] = ""
    export_df.loc[
        export_df["Dosage Form"] == "",
        "Dosage Form"
    ] = "Film Coated Tablet"
    
    export_df.loc[
        export_df["Brand Name"].str.contains("TABLET", case=False, na=False),
        "Dosage Form"
    ] = "Tablet"

    export_df.loc[
        export_df["Brand Name"].str.contains("FILM COATED", case=False, na=False),
        "Dosage Form"
    ] = "Film Coated Tablet"

    export_df.loc[
        export_df["Brand Name"].str.contains("INJECTION", case=False, na=False),
        "Dosage Form"
    ] = "Solution for Injection"
    
    export_df["Pack Size"] = "To Be Enriched"

    export_df.loc[
        export_df["Brand Name"].str.contains("FORXIGA", case=False, na=False),
        "Pack Size"
    ] = "28 Tablets"

    export_df.loc[
        export_df["Brand Name"].str.contains("JARDIANCE", case=False, na=False),
        "Pack Size"
    ] = "30 Tablets"

    export_df.loc[
        export_df["Brand Name"].str.contains("OZEMPIC", case=False, na=False),
        "Pack Size"
    ] = "1 Pre-filled Pen"
    export_df["ATC Code"] = df["atc_code"]
    export_df["Therapeutic Category"] = "Diabetes"
    export_df["MA Holder Name"] = df["company"]
    export_df["Manufacturer Name"] = df["company"]
    export_df["Manufacturer Country"] = export_df["Country"]
    export_df["Registration Status"] = df["status"]
    export_df["Registration Number"] = "Demo-Reg-001"
    export_df["Registration Date"] = df["registration_date"]
    export_df["Expiry Date"] = "31-Dec-2030"
    export_df["Product Details"] = "https://www.ema.europa.eu"
    export_df["SMPC URL"] = "https://www.ema.europa.eu"
    export_df["PIL / Assessment Report"] = df["pil_url"]
    export_df["Assessment Report"] = "Available on request"
    company_websites = {
    "AstraZeneca": "https://www.astrazeneca.com",
    "AstraZeneca AB": "https://www.astrazeneca.com",
    "AstraZeneca UK Limited": "https://www.astrazeneca.com",
    "Novo Nordisk": "https://www.novonordisk.com",
    "Novo Nordisk A/S": "https://www.novonordisk.com",
    "Boehringer Ingelheim": "https://www.boehringer-ingelheim.com",
    "Boehringer Ingelheim International GmbH": "https://www.boehringer-ingelheim.com",
    "Viatris": "https://www.viatris.com",
    "Teva": "https://www.teva.com",
    "Teva B.V.": "https://www.teva.com",
    "Sandoz": "https://www.sandoz.com",
    "Sandoz GmbH": "https://www.sandoz.com",
    "Zentiva": "https://www.zentiva.com",
    "Zentiva k.s.": "https://www.zentiva.com",
    "Stada": "https://www.stada.com"
    }
    export_df["Manufacturer Website"] = df["company"].map(company_websites).fillna("")
    export_df["Manufacturer Contact Us Phone Number"] = "+44-000-000-0000"
    export_df["Manufacturer Contact Us Email ID"] = "info@company.com"
    export_df["Box Artwork"] = ""
    export_df["Foil Artwork"] = ""
    export_df["Insert / PIL artwork"] = ""
    export_df["SMPC"] = ""

    from datetime import datetime

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    file_name = f"exports/{substance}_{timestamp}.xlsx"

    with pd.ExcelWriter(file_name, engine="openpyxl") as writer:

        export_df.to_excel(
            writer,
            index=False,
            sheet_name="Products"
        )

    worksheet = writer.sheets["Products"]

    for column in worksheet.columns:

        max_length = 0

        column_letter = column[0].column_letter

        for cell in column:

            try:
                if len(str(cell.value)) > max_length:
                    max_length = len(str(cell.value))
            except:
                pass

        worksheet.column_dimensions[
            column_letter
        ].width = min(max_length + 2, 50)

    return {
        "message": "Excel exported successfully",
        "file": file_name
    }
@app.get("/crawl_all_products/{substance}")
def crawl_all_products(substance: str):

    product_urls = run_ema_search(substance)

    saved_products = []

    for product_data in product_urls:

        try:

            product_url = product_data["url"]

            print("PROCESSING:", product_url)

            result = extract_product_page(
                product_url
            )
            print("FULL RESULT:", result)

            print(
                "ACTIVE SUBSTANCE:",
                result["product_name"],
                result["active_substance"]
            )
            if not result["active_substance"]:
                continue

            active_substance = result.get(
                "active_substance", ""
            )

            product_name = result.get(
                "product_name", ""
            )

            if (
                substance.lower() not in active_substance.lower()
                and substance.lower() not in product_name.lower()
            ):
                continue

                print(
                    f"Skipping {result['product_name']} - {result['active_substance']}"
                )

                continue

            save_product_details(
                result["active_substance"],
                result["product_name"],
                result["mah"],
                "EU",
                result["status"],
                result["product_url"],
                result["atc_code"],
                result["authorisation_date"],
                result["smpc_url"],
                result["assessment_report_url"]
            )

            saved_products.append(
                result["product_name"]
            )

        except Exception as e:

            print("ERROR:", e)

    return {
        "substance": substance,
        "products_saved": len(saved_products),
        "products": saved_products
    }
@app.get("/search_page", response_class=HTMLResponse)
def search_page(request: Request, substance: str):

    conn = sqlite3.connect("pharmasearch.db")

    cursor = conn.cursor()

    cursor.execute("""
    SELECT
        substance,
        product,
        company,
        country,
        region,
        status,
        smpc_url,
        product_url
    FROM product_details
    WHERE substance LIKE ?
        """, (f"%{substance}%",))
    rows = cursor.fetchall()

    conn.close()

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>PharmaSearch Results</title>

        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">

    </head>

    <body>

    <div class="container mt-5">

        <h2>PharmaSearch Results</h2>

        <p>
            Active Substance:
            <strong>{substance}</strong>
        </p>
        <p class="alert alert-info">
    <strong>{len(rows)}</strong> records found
        </p>

        <table class="table table-striped table-bordered">

            <thead class="table-dark">
            <tr>
                <th>Substance</th>
                <th>Product</th>
                <th>Company</th>
                <th>Country</th>
                <th>Region</th>
                <th>Status</th>
                <th>SMPC</th>
                <th>EMA Page</th>
            </tr>
        </thead>
            <tbody>
    """

    for row in rows:

       html += f"""
        <tr>
            <td>{row[0]}</td>
            <td>{row[1]}</td>
            <td>{row[2]}</td>
            <td>{row[3]}</td>
            <td>{row[4]}</td>
            <td>{row[5]}</td>
            <td>{row[6]}</td>



            <td>
                <a href="{row[5]}" target="_blank">
                    SMPC
                </a>
            </td>

            <td>
                <a href="{row[6]}" target="_blank">
                    EMA Page
                </a>
            </td>
        </tr>
        """
    html += f"""
            </tbody>

        </table>

        <a class="btn btn-success" href="/export/{substance}">
            Export Excel
        </a>

        <a class="btn btn-secondary" href="/">
            Back
        </a>

    </div>

    </body>
    </html>
    """

    return HTMLResponse(content=html)


@app.get("/load_demo_data")
def load_demo_data():

    demo_data = [

    # Dapagliflozin
    ("Dapagliflozin", "Forxiga", "AstraZeneca AB", "Sweden", "Authorised", "Demo"),
    ("Dapagliflozin", "FORXIGA 10MG FILM COATED TABLETS", "AstraZeneca UK Limited", "United Kingdom", "Authorised", "Demo"),
    ("Dapagliflozin", "Dapagliflozin Viatris", "Viatris", "France", "Authorised", "Demo"),
    ("Dapagliflozin", "Dapagliflozin Zentiva", "Zentiva k.s.", "Czech Republic", "Authorised", "Demo"),
    ("Dapagliflozin", "Dapagliflozin Teva", "Teva B.V.", "Netherlands", "Authorised", "Demo"),
    ("Dapagliflozin", "Dapagliflozin Sandoz", "Sandoz GmbH", "Austria", "Authorised", "Demo"),
    ("Dapagliflozin", "Dapagliflozin Stada", "Stada", "Germany", "Authorised", "Demo"),

    # Empagliflozin
    ("Empagliflozin", "Jardiance", "Boehringer Ingelheim International GmbH", "Germany", "Authorised", "Demo"),
    ("Empagliflozin", "Empagliflozin Viatris", "Viatris", "Spain", "Authorised", "Demo"),
    ("Empagliflozin", "Empagliflozin Teva", "Teva B.V.", "Netherlands", "Authorised", "Demo"),

    # Semaglutide
    ("Semaglutide", "Ozempic", "Novo Nordisk A/S", "Denmark", "Authorised", "Demo"),
    ("Semaglutide", "Rybelsus", "Novo Nordisk A/S", "Denmark", "Authorised", "Demo"),
    ("Semaglutide", "Wegovy", "Novo Nordisk A/S", "Denmark", "Authorised", "Demo"),

    # Sitagliptin
    ("Sitagliptin", "Januvia", "Merck", "Germany", "Authorised", "Demo"),

    # Metformin
    ("Metformin", "Glucophage", "Merck", "Spain", "Authorised", "Demo"),
    ("Metformin", "Metformin Teva", "Teva B.V.", "Netherlands", "Authorised", "Demo"),
    ("Metformin", "Metformin Zentiva", "Zentiva k.s.", "Czech Republic", "Authorised", "Demo")
    ]
    conn = sqlite3.connect("pharmasearch.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM product_details")

    for row in demo_data:

        cursor.execute("""
        INSERT INTO product_details
        (
            substance,
            product,
            company,
            country,
            status,
            atc_code,
            registration_date,
            smpc_url,
            pil_url,
            product_url
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
        row[0],   # substance
        row[1],   # product
        row[2],   # company
        row[3],   # country
        row[4],   # status
        "A10BK01",
        "11/11/2012",
        "",
        "",
        ""
    ))
    conn.commit()
    conn.close()

    return {"message": "Demo data loaded"}
@app.get("/crawl_substance/{substance}")
def crawl_substance(substance: str):

    product_url = find_product_url(substance)

    if not product_url:

        return {
            "error": f"No product found for {substance}"
        }

    result = extract_product_page(
        product_url
    )

    save_product_details(
        result["active_substance"],
        result["product_name"],
        result["mah"],
        "EU",
        result["status"],
        result["product_url"],
        result["atc_code"],
        result["authorisation_date"],
        result["smpc_url"],
        result["assessment_report_url"]
    )

    return {
        "message": "Product saved",
        "product": result["product_name"]
    }
@app.get("/crawl_url")
def crawl_url(url: str):

    result = extract_product_page(url)

    save_product_details(
        result["active_substance"],
        result["product_name"],
        result["mah"],
        "EU",
        result["status"],
        result["product_url"],
        result["atc_code"],
        result["authorisation_date"],
        result["smpc_url"],
        result["assessment_report_url"]
    )

    return {
        "message": "Product saved",
        "product": result["product_name"],
        "data": result
    }
def crawl_all_products(substance: str):

    product_urls = run_ema_search(substance)
    print("FOUND URLS:")
    print(product_urls)

    saved_products = []

    for product_data in product_urls:

        try:

            product_url = product_data["url"]

            print("PROCESSING:", product_url)

            result = extract_product_page(
                product_url
            )

            save_product_details(
                result["active_substance"],
                result["product_name"],
                result["mah"],
                "EU",
                result["status"],
                result["product_url"],
                result["atc_code"],
                result["authorisation_date"],
                result["smpc_url"],
                result["assessment_report_url"]
            )

            saved_products.append(
                result["product_name"]
            )

        except Exception as e:

            print("ERROR:", e)
    return {
        "substance": substance,
        "products_saved": len(saved_products),
        "products": saved_products
    }
@app.get("/products", response_class=HTMLResponse)
def products():

    import os
    print("DB PATH:", os.path.abspath("pharmasearch.db"))

    conn = sqlite3.connect("pharmasearch.db")
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            product,
            substance,
            company,
            country,
            region,
            status,
            registration_date,
            smpc_url,
            product_url
        FROM product_details
        ORDER BY product
    """)

    rows = cursor.fetchall()
    print(rows)

    conn.close()

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Products</title>

        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    </head>
    <body>

    <div class="container mt-5">

        <h2>Saved Products</h2>

        <p class="alert alert-info">
            Total Products: <strong>{len(rows)}</strong>
        </p>

        <a class="btn btn-success mb-3" href="/export/dapagliflozin">
            Export Dapagliflozin
        </a>

        <table class="table table-striped table-bordered">

            <thead class="table-dark">
                <tr>
                    <th>Product</th>
                    <th>Substance</th>
                    <th>Company</th>
                    <th>Country</th>
                    <th>Region</th>
                    <th>Status</th>
                    <th>Registration Date</th>
                </tr>
            </thead>

            <tbody>
    """
    for row in rows:

        html += f"""
            <tr>
                <td>{row[0]}</td>
                <td>{row[1]}</td>
                <td>{row[2]}</td>
                <td>{row[3]}</td>
                <td>{"UK" if row[3] == "United Kingdom" else "EU"}</td>
                <td>{row[5]}</td>
                <td>{row[6]}</td>

>
            </tr>
        """

    html += """
            </tbody>

        </table>

    </div>

    </body>
    </html>
    """

    return HTMLResponse(content=html)

