import os
import sqlite3

print(os.path.abspath("pharmasearch.db"))
def save_product_details(
    substance,
    product,
    company,
    country,
    status,
    product_url="",
    atc_code="",
    registration_date="",
    smpc_url="",
    assessment_report_url=""
):
    conn = sqlite3.connect("pharmasearch.db")

    cursor = conn.cursor()

    # Check if product already exists
    cursor.execute("""
        SELECT id
        FROM product_details
        WHERE product = ?
    """, (product,))

    existing = cursor.fetchone()

    if existing:

        conn.close()

        print("Product already exists")

        return

    # Insert new product
    cursor.execute("""
        INSERT INTO product_details (
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
        substance,
        product,
        company,
        country,
        status,
        atc_code,
        registration_date,
        smpc_url,
        assessment_report_url,
        product_url
    ))

    conn.commit()

    conn.close()

    print("Product details saved")
