import sqlite3

conn = sqlite3.connect("pharmasearch.db")

cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS medicines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    substance TEXT,
    product TEXT,
    company TEXT,
    country TEXT,
    status TEXT,
    source TEXT
)
""")

conn.commit()

print("Database created successfully")

conn.close()
