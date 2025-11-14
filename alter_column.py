import sqlite3
with sqlite3.connect("camera_analytics.db") as conn:
    conn.execute("ALTER TABLE presence_log ADD COLUMN age REAL;")