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

app = FastAPI()

templates = Jinja2Templates(directory="templates")


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html"
    )


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
        status,
        atc_code,
        registration_date,
        product_url
        FROM product_details
        WHERE substance LIKE ?
    """

    df = pd.read_sql_query(
        query,
        conn,
        params=(f"%{substance}%",)
    )

    conn.close()

    export_df = pd.DataFrame()

    export_df["Country"] = ""
    export_df["Brand Name"] = df["product"]
    export_df["Molecule (Active Ingredient(s))"] = df["substance"]
    export_df["Strength"] = ""
    export_df["Dosage Form"] = ""
    export_df["Pack Size"] = ""
    export_df["ATC Code"] = df["atc_code"]
    export_df["Therapeutic Category"] = "Diabetes"
    export_df["MA Holder Name"] = df["company"]
    export_df["Manufacturer Name"] = df["company"]
    export_df["Manufacturer Country"] = ""
    export_df["Registration Status"] = df["status"]
    export_df["Registration Number"] = ""
    export_df["Registration Date"] = df["registration_date"]
    export_df["Expiry Date"] = ""
    export_df["Product Details"] = df["product_url"]
    company_websites = {
    "AstraZeneca": "https://www.astrazeneca.com",
    "Viatris": "https://www.viatris.com",
    "Zentiva": "https://www.zentiva.com",
    "Teva": "https://www.teva.com",
    "Sandoz": "https://www.sandoz.com",
    "Stada": "https://www.stada.com"
    }

    export_df["Manufacturer Website"] = df["company"].map(company_websites).fillna("")
    export_df["Manufacturer Contact Us Phone Number"] = ""
    export_df["Manufacturer Contact Us Email ID"] = ""
    export_df["Box Artwork"] = ""
    export_df["Foil Artwork"] = ""
    export_df["Insert / PIL artwork"] = ""
    export_df["SMPC"] = ""
    product_links = {
    "Forxiga": "https://www.ema.europa.eu/en/medicines/human/EPAR/forxiga"
    }

    export_df["Product Details"] = df["product"].map(product_links).fillna("")
    from datetime import datetime

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    file_name = f"exports/{substance}_{timestamp}.xlsx"

    export_df.to_excel(file_name, index=False)

    return {
        "message": "Excel exported successfully",
        "file": file_name
    }
@app.get("/crawl_all/{substance}")
def crawl_all(substance: str):

    ema_result = run_ema_search(substance)

    mhra_result = run_mhra_search(substance)

    return {
        "EMA": ema_result,
        "MHRA": mhra_result
    }


@app.get("/search_page", response_class=HTMLResponse)
def search_page(request: Request, substance: str):

    conn = sqlite3.connect("pharmasearch.db")

    cursor = conn.cursor()

    cursor.execute("""
        SELECT substance, product, company, country, status, source
        FROM medicines
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
                    <th>Status</th>
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

        ("Dapagliflozin", "Forxiga", "AstraZeneca", "Germany", "Active", "Demo"),
        ("Dapagliflozin", "Dapagliflozin Viatris", "Viatris", "France", "Active", "Demo"),
        ("Dapagliflozin", "Dapagliflozin Zentiva", "Zentiva", "Italy", "Active", "Demo"),
        ("Dapagliflozin", "Dapagliflozin Teva", "Teva", "Spain", "Active", "Demo"),
        ("Dapagliflozin", "Dapagliflozin Sandoz", "Sandoz", "Netherlands", "Active", "Demo"),
        ("Dapagliflozin", "Dapagliflozin Stada", "Stada", "Belgium", "Active", "Demo"),

        ("Empagliflozin", "Jardiance", "Boehringer Ingelheim", "Germany", "Active", "Demo"),
        ("Empagliflozin", "Empagliflozin Viatris", "Viatris", "Spain", "Active", "Demo"),

        ("Semaglutide", "Ozempic", "Novo Nordisk", "Germany", "Active", "Demo"),
        ("Semaglutide", "Rybelsus", "Novo Nordisk", "France", "Active", "Demo"),

        ("Sitagliptin", "Januvia", "Merck", "Germany", "Active", "Demo"),

        ("Metformin", "Glucophage", "Merck", "Spain", "Active", "Demo")
    ]

    conn = sqlite3.connect("pharmasearch.db")
    cursor = conn.cursor()

    for row in demo_data:
        cursor.execute("""
            INSERT INTO medicines
            (substance, product, company, country, status, source)
            VALUES (?, ?, ?, ?, ?, ?)
        """, row)

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
        result["authorisation_date"]
    )

    return {
        "message": "Product saved",
        "product": result["product_name"]
    }
