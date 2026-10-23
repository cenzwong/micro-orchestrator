"""
Database initialization and migration for Micro-Orchestrator.
Creates/migrates job_runs table and sets SQLite journal mode to WAL.
"""

import os
import sqlite3
import uuid

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "orchestrator.db")

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS job_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT UNIQUE,
    job_name TEXT NOT NULL,
    status TEXT NOT NULL,          -- 'STARTING', 'RUNNING', 'SUCCESS', 'FAILED', 'TIMEOUT', 'LOST'
    trigger_type TEXT DEFAULT 'scheduled', -- 'scheduled', 'manual'
    scheduled_slot TEXT,
    config_snapshot TEXT,          -- JSON string of job config
    start_time DATETIME NOT NULL,
    end_time DATETIME,
    last_heartbeat DATETIME,
    worker_pid INTEGER,
    child_pid INTEGER,
    exit_code INTEGER,
    error_desc TEXT,
    log_file TEXT
);
"""

# New columns to add during migration if table existed from older version
ADD_COLUMNS = [
    ("run_id", "TEXT"),
    ("trigger_type", "TEXT DEFAULT 'scheduled'"),
    ("scheduled_slot", "TEXT"),
    ("config_snapshot", "TEXT"),
    ("last_heartbeat", "DATETIME"),
    ("worker_pid", "INTEGER"),
    ("child_pid", "INTEGER"),
    ("error_desc", "TEXT"),
]


def init_db(db_path: str = DB_PATH):
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()

        # Enable WAL mode for high-concurrency non-locking reads and writes
        cursor.execute("PRAGMA journal_mode=WAL;")
        mode = cursor.fetchone()[0]

        # Enable busy timeout (5 seconds)
        cursor.execute("PRAGMA busy_timeout = 5000;")

        # Create table if it doesn't exist
        cursor.execute(CREATE_TABLE_SQL)

        # Check existing columns for migration
        cursor.execute("PRAGMA table_info(job_runs);")
        existing_cols = {row[1] for row in cursor.fetchall()}

        for col_name, col_def in ADD_COLUMNS:
            if col_name not in existing_cols:
                cursor.execute(f"ALTER TABLE job_runs ADD COLUMN {col_name} {col_def};")

        # Backfill run_id for any historical records where run_id IS NULL
        cursor.execute("SELECT id FROM job_runs WHERE run_id IS NULL OR run_id = '';")
        null_rows = cursor.fetchall()
        for row in null_rows:
            cursor.execute("UPDATE job_runs SET run_id = ? WHERE id = ?;", (str(uuid.uuid4()), row[0]))

        # Create indexes
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_job_runs_job_name ON job_runs(job_name);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_job_runs_status ON job_runs(status);")
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_job_runs_run_id ON job_runs(run_id);")
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_job_runs_slot ON job_runs(job_name, scheduled_slot, trigger_type);"
        )

        conn.commit()
        print(f"Database initialized and migrated at {db_path} with journal_mode={mode}")
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
