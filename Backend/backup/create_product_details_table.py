import sqlite3

conn = sqlite3.connect("pharmasearch.db")

cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS product_details (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    substance TEXT,
    product TEXT,
    company TEXT,
    country TEXT,
    status TEXT,

    strength TEXT,
    dosage_form TEXT,
    pack_size TEXT,

    atc_code TEXT,
    therapeutic_category TEXT,

    registration_number TEXT,
    registration_date TEXT,
    expiry_date TEXT,

    manufacturer_website TEXT,

    smpc_url TEXT,
    pil_url TEXT,
    product_url TEXT
)
""")

conn.commit()

conn.close()

print("product_details table created successfully")
