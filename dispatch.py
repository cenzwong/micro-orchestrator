"""
Micro-Orchestrator: Zero-Daemon Cron Dispatcher & Stale Run Reconciler.

Features:
- Validates job configurations and schedule matches (via croniter).
- Performs lightweight reconciliation on active job_runs (detecting dead workers / missing heartbeats).
- Claims job runs atomically in SQLite and launches detached worker processes (worker.py).
- Enforces per-job concurrency control ('forbid' / 'allow') and cron slot deduplication.
"""

import argparse
import datetime
import json
import os
import sqlite3
import subprocess
import sys
import uuid
from croniter import croniter

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB_PATH = os.path.join(BASE_DIR, "orchestrator.db")
DEFAULT_CONFIG_PATH = os.path.join(BASE_DIR, "jobs.yaml")
STALE_HEARTBEAT_THRESHOLD_SEC = 90  # 90 seconds threshold as agreed


def utc_now_str() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def get_db_conn(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout = 5000;")
    conn.row_factory = sqlite3.Row
    return conn


def load_jobs(config_path: str = DEFAULT_CONFIG_PATH) -> list:
    if not os.path.exists(config_path):
        return []
    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml_safe_load(f) or {}
    return data.get("jobs", [])


def yaml_safe_load(f):
    import yaml
    return yaml.safe_load(f)


def is_process_alive(pid: int) -> bool:
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def reconcile_stale_runs(db_path: str = DEFAULT_DB_PATH):
    """
    Inspects active runs ('STARTING' or 'RUNNING') and marks dead/stale ones as 'LOST'.
    A run is marked 'LOST' if:
    - Its worker process is dead AND heartbeat age > 90 seconds.
    - Or it has been stuck in 'STARTING' for > 120 seconds with no worker process alive.
    """
    conn = get_db_conn(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, run_id, job_name, status, start_time, last_heartbeat, worker_pid, child_pid
            FROM job_runs
            WHERE status IN ('STARTING', 'RUNNING')
            """
        )
        active_runs = cursor.fetchall()
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        fmt = "%Y-%m-%d %H:%M:%S"

        for run in active_runs:
            run_id = run["run_id"]
            status = run["status"]
            worker_pid = run["worker_pid"]
            hb_str = run["last_heartbeat"] or run["start_time"]

            hb_age_sec = 999999
            if hb_str:
                try:
                    hb_dt = datetime.datetime.strptime(hb_str.split(".")[0], fmt).replace(tzinfo=datetime.timezone.utc)
                    hb_age_sec = (now_utc - hb_dt).total_seconds()
                except Exception:
                    hb_age_sec = 999999

            worker_alive = is_process_alive(worker_pid) if worker_pid else False

            if not worker_alive and hb_age_sec > STALE_HEARTBEAT_THRESHOLD_SEC:
                err = f"Worker process {worker_pid} disappeared (stale heartbeat: {int(hb_age_sec)}s)"
                cursor.execute(
                    """
                    UPDATE job_runs
                    SET status = 'LOST',
                        end_time = ?,
                        error_desc = ?
                    WHERE run_id = ? AND status IN ('STARTING', 'RUNNING')
                    """,
                    (utc_now_str(), err, run_id),
                )

        conn.commit()
    finally:
        conn.close()


def claim_and_launch_job(
    job: dict,
    trigger_type: str = "scheduled",
    scheduled_slot: str | None = None,
    base_dir: str = BASE_DIR,
    db_path: str = DEFAULT_DB_PATH,
) -> tuple[bool, str, str | None]:
    """
    Atomically checks concurrency and claims a job in SQLite.
    Then launches worker.py in a detached process.

    Returns: (success: bool, run_id: str, message_or_error: str)
    """
    job_name = job.get("name")
    if not job_name:
        return False, "", "Job missing 'name'"

    concurrency = job.get("concurrency", "allow")
    conn = get_db_conn(db_path)

    try:
        conn.execute("BEGIN IMMEDIATE;")
        cursor = conn.cursor()

        # Concurrency check
        if concurrency == "forbid":
            cursor.execute(
                """
                SELECT run_id FROM job_runs
                WHERE job_name = ? AND status IN ('STARTING', 'RUNNING')
                LIMIT 1
                """,
                (job_name,),
            )
            active = cursor.fetchone()
            if active:
                conn.rollback()
                return False, active["run_id"], f"Job '{job_name}' already active (concurrency: forbid)"

        # Slot deduplication for scheduled triggers
        if trigger_type == "scheduled" and scheduled_slot:
            cursor.execute(
                """
                SELECT run_id FROM job_runs
                WHERE job_name = ? AND scheduled_slot = ? AND trigger_type = 'scheduled'
                  AND status NOT IN ('LOST')
                LIMIT 1
                """,
                (job_name, scheduled_slot),
            )
            existing_slot = cursor.fetchone()
            if existing_slot:
                conn.rollback()
                return False, existing_slot["run_id"], f"Job '{job_name}' already claimed for slot {scheduled_slot}"

        # Generate run_id and prepare record
        run_id = str(uuid.uuid4())
        raw_dir = job.get("dir", ".")
        task_dir = os.path.abspath(os.path.join(base_dir, raw_dir)) if not os.path.isabs(raw_dir) else raw_dir
        logs_dir = os.path.join(task_dir, "logs")
        rel_logs_dir = os.path.relpath(logs_dir, base_dir)
        rel_log_file = os.path.join(rel_logs_dir, f"{run_id}.log")

        # Include base_dir in job config snapshot so worker knows path resolution
        job_snapshot = dict(job)
        job_snapshot["_base_dir"] = base_dir
        snapshot_json = json.dumps(job_snapshot)
        now_str = utc_now_str()

        cursor.execute(
            """
            INSERT INTO job_runs (
                run_id, job_name, status, trigger_type, scheduled_slot,
                config_snapshot, start_time, last_heartbeat, log_file
            ) VALUES (?, ?, 'STARTING', ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                job_name,
                trigger_type,
                scheduled_slot,
                snapshot_json,
                now_str,
                now_str,
                rel_log_file,
            ),
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        return False, "", f"Database claim error: {e}"
    finally:
        conn.close()

    # Launch detached worker process (worker.py lives in BASE_DIR)
    python_sys = sys.executable or "python3"
    worker_script = os.path.join(BASE_DIR, "worker.py")

    try:
        subprocess.Popen(
            [python_sys, worker_script, "--run-id", run_id, "--db", db_path],
            cwd=base_dir,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
    except Exception as e:
        conn = get_db_conn(db_path)
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE job_runs
                SET status = 'FAILED',
                    error_desc = ?,
                    end_time = ?
                WHERE run_id = ? AND status = 'STARTING'
                """,
                (f"Worker launch failed: {e}", utc_now_str(), run_id),
            )
            conn.commit()
        finally:
            conn.close()
        return False, run_id, f"Failed to launch worker process: {e}"

    return True, run_id, "Dispatched successfully"


def dispatch_jobs(force_name: str | None = None, now: datetime.datetime | None = None) -> list[str]:
    """
    Runs reconciliation and checks schedules to trigger jobs.
    """
    if now is None:
        now = datetime.datetime.now()

    reconcile_stale_runs()

    jobs = load_jobs()
    triggered = []

    current_slot = now.strftime("%Y-%m-%d %H:%M")

    for job in jobs:
        name = job.get("name")
        if not name:
            continue

        if force_name:
            if name == force_name:
                ok, rid, msg = claim_and_launch_job(job, trigger_type="manual")
                if ok:
                    triggered.append(name)
                else:
                    print(f"Manual run skipped for '{name}': {msg}", file=sys.stderr)
        else:
            schedule = job.get("schedule")
            if schedule and croniter.match(schedule, now):
                ok, rid, msg = claim_and_launch_job(
                    job, trigger_type="scheduled", scheduled_slot=current_slot
                )
                if ok:
                    triggered.append(name)

    return triggered


def main():
    parser = argparse.ArgumentParser(description="Micro-Orchestrator Cron Dispatcher")
    parser.add_argument("--run", type=str, help="Manually run a specific job by name immediately")
    parser.add_argument("--list", action="store_true", help="List configured jobs")
    args = parser.parse_args()

    if args.list:
        jobs = load_jobs()
        print(f"Loaded {len(jobs)} jobs from {DEFAULT_CONFIG_PATH}:")
        for j in jobs:
            print(
                f"  - {j.get('name')}: schedule='{j.get('schedule')}' dir='{j.get('dir')}' script='{j.get('script')}' concurrency='{j.get('concurrency', 'allow')}'"
            )
        return

    triggered = dispatch_jobs(force_name=args.run)
    if args.run:
        if triggered:
            print(f"Triggered job '{args.run}' successfully.")
        else:
            print(f"Job '{args.run}' could not be triggered or not found.", file=sys.stderr)
            sys.exit(1)
    else:
        if triggered:
            print(f"Dispatched {len(triggered)} job(s): {', '.join(triggered)}")


if __name__ == "__main__":
    main()
