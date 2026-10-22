"""
Micro-Orchestrator: Management API.

Endpoints:
- GET  /api/config          -> Retrieve raw jobs.yaml content
- POST /api/config          -> Validate YAML syntax & job schema, then safely overwrite
- GET  /api/jobs            -> Retrieve configured jobs with latest run status
- GET  /api/status          -> Query recent executions with duration and details
- POST /api/run/{job_name}  -> Manually trigger job (HTTP 202 Accepted or HTTP 409 Conflict)
- GET  /api/runs/{run_id}/logs -> Secure log tailing by run_id with path traversal protection
- GET  /                    -> Serves single-page UI (static/index.html)
"""

import os
import re
import sqlite3
import datetime
import yaml
from croniter import croniter
from fastapi import FastAPI, HTTPException, Request, Query, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import dispatch
import init_db

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "jobs.yaml")
DB_PATH = os.path.join(BASE_DIR, "orchestrator.db")
STATIC_DIR = os.path.join(BASE_DIR, "static")

# Ensure DB is initialized
init_db.init_db(DB_PATH)

app = FastAPI(title="Micro-Orchestrator Management API")

# Identifier regex: letters, numbers, underscores, hyphens
JOB_NAME_REGEX = re.compile(r"^[a-zA-Z0-9_-]+$")


class ConfigUpdatePayload(BaseModel):
    content: str


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout = 5000;")
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
        if not name or not isinstance(name, str) or not JOB_NAME_REGEX.match(name):
            raise HTTPException(
                status_code=400,
                detail=f"Job at index {idx} has invalid or missing 'name'. Must contain alphanumeric, '_' or '-'.",
            )
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

        concurrency = job.get("concurrency", "allow")
        if concurrency not in ("allow", "forbid"):
            raise HTTPException(
                status_code=400, detail=f"Job '{name}' invalid concurrency '{concurrency}'. Must be 'allow' or 'forbid'."
            )

        timeout = job.get("timeout_seconds")
        if timeout is not None:
            try:
                t_val = float(timeout)
                if t_val <= 0:
                    raise ValueError()
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Job '{name}' timeout_seconds must be a positive number.")

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
                """
                SELECT run_id, status, start_time, end_time, exit_code, log_file, error_desc
                FROM job_runs
                WHERE job_name = ?
                ORDER BY id DESC LIMIT 1
                """,
                (job.get("name"),),
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
            SELECT id, run_id, job_name, status, trigger_type, start_time, end_time, exit_code, log_file, error_desc
            FROM job_runs
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = cursor.fetchall()
        runs = []
        fmt = "%Y-%m-%d %H:%M:%S"
        for r in rows:
            item = dict(r)
            duration_sec = None
            if item.get("start_time") and item.get("end_time"):
                try:
                    t0 = datetime.datetime.strptime(item["start_time"].split(".")[0], fmt)
                    t1 = datetime.datetime.strptime(item["end_time"].split(".")[0], fmt)
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
    if not JOB_NAME_REGEX.match(job_name):
        raise HTTPException(status_code=400, detail="Invalid job name format.")

    jobs = dispatch.load_jobs(CONFIG_PATH)
    target = next((j for j in jobs if j.get("name") == job_name), None)
    if not target:
        raise HTTPException(status_code=404, detail=f"Job '{job_name}' not found in configuration.")

    ok, run_id, msg = dispatch.claim_and_launch_job(
        target, trigger_type="manual", base_dir=BASE_DIR, db_path=DB_PATH
    )

    if not ok:
        if "already active" in msg:
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={"status": "error", "message": msg, "job_name": job_name},
            )
        raise HTTPException(status_code=400, detail=msg)

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"status": "ok", "message": f"Job '{job_name}' dispatched successfully.", "run_id": run_id},
    )


@app.get("/api/runs/{run_id}/logs")
def get_run_logs(run_id: str, lines: int = Query(default=200, ge=10, le=1000)):
    # Look up execution record
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT job_name, config_snapshot, log_file FROM job_runs WHERE run_id = ?", (run_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Run ID '{run_id}' not found.")
        run_data = dict(row)
    finally:
        conn.close()

    log_file = run_data.get("log_file")
    if not log_file:
        raise HTTPException(status_code=404, detail="No log file recorded for this execution.")

    # Resolve log path and ensure safe resolution
    abs_log_path = os.path.abspath(os.path.join(BASE_DIR, log_file)) if not os.path.isabs(log_file) else os.path.abspath(log_file)

    # Determine allowed task directory
    snapshot_raw = run_data.get("config_snapshot")
    allowed_dir = BASE_DIR
    if snapshot_raw:
        try:
            cfg = yaml.safe_load(snapshot_raw) if not snapshot_raw.startswith("{") else json.loads(snapshot_raw)
            raw_dir = cfg.get("dir", ".")
            allowed_dir = os.path.abspath(os.path.join(BASE_DIR, raw_dir)) if not os.path.isabs(raw_dir) else os.path.abspath(raw_dir)
        except Exception:
            pass

    # Ensure canonical log file path stays within allowed task or base directory
    real_log_path = os.path.realpath(abs_log_path)
    real_allowed_dir = os.path.realpath(allowed_dir)
    real_base_dir = os.path.realpath(BASE_DIR)

    if not (real_log_path.startswith(real_allowed_dir) or real_log_path.startswith(real_base_dir)):
        raise HTTPException(status_code=403, detail="Access denied to requested log file path.")

    if not os.path.exists(real_log_path) or not os.path.isfile(real_log_path):
        return {"run_id": run_id, "total_lines": 0, "content": "(Log file does not exist or has been deleted)"}

    with open(real_log_path, "r", encoding="utf-8", errors="replace") as f:
        all_lines = f.readlines()
        tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
        content = "".join(tail)

    return {
        "run_id": run_id,
        "log_file": log_file,
        "total_lines": len(all_lines),
        "content": content,
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
