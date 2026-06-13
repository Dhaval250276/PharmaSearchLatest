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

    conn = sqlite3.connect("pharmasearch.db")

    query = f"""
        SELECT *
        FROM medicines
        WHERE substance LIKE '%{substance}%'
    """

    df = pd.read_sql_query(query, conn)

    file_name = f"exports/{substance}.xlsx"

    df.to_excel(file_name, index=False)

    conn.close()

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
