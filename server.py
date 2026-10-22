"""
Micro-Orchestrator: Lightweight On-Demand Management API.

Endpoints:
- GET  /api/config       -> Retrieve raw jobs.yaml content
- POST /api/config       -> Validate YAML syntax & schema, then safely overwrite jobs.yaml
- GET  /api/jobs         -> Retrieve configured jobs with latest run status
- GET  /api/status       -> Query recent 50 executions with duration and details
- POST /api/run/{job}    -> Trigger a specific job immediately using the shared Bash chain
- GET  /api/logs         -> Safe tail viewer for job log files
- GET  /                 -> Serves single-page UI (static/index.html)
"""

import os
import sqlite3
import datetime
import yaml
from croniter import croniter
from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import dispatch

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "jobs.yaml")
DB_PATH = os.path.join(BASE_DIR, "orchestrator.db")
STATIC_DIR = os.path.join(BASE_DIR, "static")

app = FastAPI(title="Micro-Orchestrator Management API")


class ConfigUpdatePayload(BaseModel):
    content: str


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@app.get("/api/config")
def get_config():
    if not os.path.exists(CONFIG_PATH):
        return {"content": ""}
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return {"content": f.read()}


@app.post("/api/config")
def update_config(payload: ConfigUpdatePayload):
    # 1. Parse YAML syntax
    try:
        data = yaml.safe_load(payload.content)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"YAML syntax error: {str(e)}")

    if not isinstance(data, dict) or "jobs" not in data or not isinstance(data["jobs"], list):
        raise HTTPException(status_code=400, detail="Invalid schema: root must be a dict containing a 'jobs' list.")

    # 2. Validate individual job fields & cron expressions
    seen_names = set()
    for idx, job in enumerate(data["jobs"]):
        if not isinstance(job, dict):
            raise HTTPException(status_code=400, detail=f"Job at index {idx} must be a dictionary.")

        name = job.get("name")
        if not name or not isinstance(name, str):
            raise HTTPException(status_code=400, detail=f"Job at index {idx} missing valid 'name'.")
        if name in seen_names:
            raise HTTPException(status_code=400, detail=f"Duplicate job name '{name}' found.")
        seen_names.add(name)

        schedule = job.get("schedule")
        if not schedule or not croniter.is_valid(schedule):
            raise HTTPException(status_code=400, detail=f"Job '{name}' has invalid cron expression: '{schedule}'")

        if not job.get("dir"):
            raise HTTPException(status_code=400, detail=f"Job '{name}' missing 'dir'.")
        if not job.get("script"):
            raise HTTPException(status_code=400, detail=f"Job '{name}' missing 'script'.")

    # 3. Safe atomic write
    temp_path = f"{CONFIG_PATH}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        f.write(payload.content)
    os.replace(temp_path, CONFIG_PATH)

    return {"status": "ok", "message": f"Successfully updated {len(data['jobs'])} jobs."}


@app.get("/api/jobs")
def list_jobs():
    jobs = dispatch.load_jobs(CONFIG_PATH)
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        for job in jobs:
            cursor.execute(
                "SELECT status, start_time, end_time, exit_code, log_file FROM job_runs WHERE job_name = ? ORDER BY id DESC LIMIT 1",
                (job.get("name"),)
            )
            row = cursor.fetchone()
            if row:
                job["latest_run"] = dict(row)
            else:
                job["latest_run"] = None
    finally:
        conn.close()

    return {"jobs": jobs}


@app.get("/api/status")
def get_status(limit: int = Query(default=50, ge=1, le=100)):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, job_name, status, start_time, end_time, exit_code, log_file
            FROM job_runs
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,)
        )
        rows = cursor.fetchall()
        runs = []
        for r in rows:
            item = dict(r)
            # Calculate duration if both start_time and end_time exist
            duration_sec = None
            if item.get("start_time") and item.get("end_time"):
                try:
                    fmt = "%Y-%m-%d %H:%M:%S"
                    t0 = datetime.datetime.strptime(item["start_time"], fmt)
                    t1 = datetime.datetime.strptime(item["end_time"], fmt)
                    duration_sec = round((t1 - t0).total_seconds(), 2)
                except Exception:
                    pass
            item["duration_sec"] = duration_sec
            runs.append(item)
        return {"runs": runs}
    finally:
        conn.close()


@app.post("/api/run/{job_name}")
def run_job(job_name: str):
    jobs = dispatch.load_jobs(CONFIG_PATH)
    target = next((j for j in jobs if j.get("name") == job_name), None)
    if not target:
        raise HTTPException(status_code=404, detail=f"Job '{job_name}' not found in configuration.")

    dispatch.launch_job(target, base_dir=BASE_DIR, db_path=DB_PATH)
    return {"status": "ok", "message": f"Job '{job_name}' initiated in background."}


@app.get("/api/logs")
def get_logs(file: str = Query(..., description="Relative log file path"), lines: int = Query(default=200, ge=10, le=1000)):
    # Prevent directory traversal
    normalized = os.path.normpath(file)
    if normalized.startswith("..") or os.path.isabs(normalized):
        raise HTTPException(status_code=400, detail="Invalid log file path.")

    target_path = os.path.join(BASE_DIR, normalized)
    if not os.path.exists(target_path) or not os.path.isfile(target_path):
        raise HTTPException(status_code=404, detail=f"Log file not found: {file}")

    with open(target_path, "r", encoding="utf-8", errors="replace") as f:
        all_lines = f.readlines()
        tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
        content = "".join(tail)

    return {
        "file": file,
        "total_lines": len(all_lines),
        "content": content
    }


# Static single page app
if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", response_class=HTMLResponse)
def index():
    index_file = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_file):
        with open(index_file, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse("<h1>Micro-Orchestrator</h1><p>UI file not found in static/index.html</p>")
