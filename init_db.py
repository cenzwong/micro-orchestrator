"""
Database initialization for Micro-Orchestrator.
Creates job_runs table and sets SQLite journal mode to WAL.
"""
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "orchestrator.db")

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS job_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_name TEXT NOT NULL,
    status TEXT NOT NULL,          -- 'RUNNING', 'SUCCESS', 'FAILED'
    start_time DATETIME NOT NULL,
    end_time DATETIME,
    exit_code INTEGER,
    log_file TEXT
);
"""

def init_db(db_path: str = DB_PATH):
    conn = sqlite3.connect(db_path)
    try:
        # Enable WAL mode for high-concurrency non-locking reads and writes
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        mode = cursor.fetchone()[0]
        cursor.execute(CREATE_TABLE_SQL)
        conn.commit()
        print(f"Database initialized at {db_path} with journal_mode={mode}")
    finally:
        conn.close()

if __name__ == "__main__":
    init_db()
