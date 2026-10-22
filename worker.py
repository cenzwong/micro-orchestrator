"""
Micro-Orchestrator Worker Process.

Executes a single job run based on its run_id and stored config snapshot.
Handles:
- Loading configuration snapshot
- Transitioning status to RUNNING and setting worker_pid
- Spawning task process via subprocess.Popen in task directory
- Periodic heartbeat updates (every 15s)
- Enforcing execution timeout with process group termination
- Safely capturing exit code and writing terminal state (SUCCESS, FAILED, TIMEOUT)
"""

import argparse
import datetime
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB_PATH = os.path.join(BASE_DIR, "orchestrator.db")
HEARTBEAT_INTERVAL = 15  # seconds


def utc_now_str() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def get_db_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout = 5000;")
    conn.row_factory = sqlite3.Row
    return conn


def run_worker(run_id: str, db_path: str = DEFAULT_DB_PATH):
    conn = get_db_conn(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM job_runs WHERE run_id = ?", (run_id,))
        row = cursor.fetchone()
        if not row:
            print(f"Error: Run ID '{run_id}' not found in database.", file=sys.stderr)
            sys.exit(1)

        run_data = dict(row)
        snapshot_raw = run_data.get("config_snapshot")
        if not snapshot_raw:
            error_msg = f"No configuration snapshot found for run_id '{run_id}'"
            cursor.execute(
                "UPDATE job_runs SET status = 'FAILED', error_desc = ?, end_time = ? WHERE run_id = ? AND status IN ('STARTING', 'RUNNING')",
                (error_msg, utc_now_str(), run_id),
            )
            conn.commit()
            print(error_msg, file=sys.stderr)
            sys.exit(1)

        config = json.loads(snapshot_raw)
        job_name = config.get("name", run_data.get("job_name"))
        raw_dir = config.get("dir", ".")

        # Check base_dir from snapshot or fallback to BASE_DIR
        base_dir_cfg = config.get("_base_dir", BASE_DIR)
        task_dir = os.path.abspath(os.path.join(base_dir_cfg, raw_dir)) if not os.path.isabs(raw_dir) else raw_dir

        raw_python = config.get("python_bin", "python3")
        if not os.path.isabs(raw_python):
            candidate1 = os.path.join(task_dir, raw_python)
            candidate2 = os.path.join(base_dir_cfg, raw_python)
            if os.path.exists(candidate1):
                python_bin = os.path.abspath(candidate1)
            elif os.path.exists(candidate2):
                python_bin = os.path.abspath(candidate2)
            else:
                python_bin = raw_python
        else:
            python_bin = raw_python

        script = config.get("script", "main.py")
        script_abs = os.path.abspath(os.path.join(task_dir, script)) if not os.path.isabs(script) else script

        timeout_seconds = config.get("timeout_seconds")
        if timeout_seconds is not None:
            try:
                timeout_seconds = float(timeout_seconds)
            except (ValueError, TypeError):
                timeout_seconds = None

        log_file = run_data.get("log_file")
        if not log_file:
            logs_dir = os.path.join(task_dir, "logs")
            rel_logs_dir = os.path.relpath(logs_dir, base_dir_cfg)
            log_file = os.path.join(rel_logs_dir, f"{run_id}.log")

        abs_log_path = os.path.abspath(os.path.join(base_dir_cfg, log_file)) if not os.path.isabs(log_file) else log_file
        os.makedirs(os.path.dirname(abs_log_path), exist_ok=True)

        worker_pid = os.getpid()
        now_str = utc_now_str()

        # Update status to RUNNING
        cursor.execute(
            """
            UPDATE job_runs
            SET status = 'RUNNING',
                worker_pid = ?,
                last_heartbeat = ?,
                log_file = ?
            WHERE run_id = ? AND status = 'STARTING'
            """,
            (worker_pid, now_str, log_file, run_id),
        )
        conn.commit()

        if cursor.rowcount == 0:
            print(f"Run ID '{run_id}' was not in STARTING state. Exiting.", file=sys.stderr)
            sys.exit(0)

    finally:
        conn.close()

    # Open log file for subprocess output
    log_fd = open(abs_log_path, "a", encoding="utf-8", errors="replace")

    # Launch sub-process in isolated process group
    try:
        proc = subprocess.Popen(
            [python_bin, script_abs],
            cwd=task_dir,
            stdout=log_fd,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        log_fd.close()
        err_text = f"Failed to spawn task process: {e}"
        conn = get_db_conn(db_path)
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE job_runs
                SET status = 'FAILED',
                    error_desc = ?,
                    end_time = ?
                WHERE run_id = ? AND status IN ('STARTING', 'RUNNING')
                """,
                (err_text, utc_now_str(), run_id),
            )
            conn.commit()
        finally:
            conn.close()
        sys.exit(1)

    # Update child_pid in DB
    conn = get_db_conn(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE job_runs SET child_pid = ? WHERE run_id = ? AND status = 'RUNNING'",
            (proc.pid, run_id),
        )
        conn.commit()
    finally:
        conn.close()

    start_mono = time.monotonic()
    last_hb_mono = start_mono
    is_timeout = False

    while True:
        ret_code = proc.poll()
        now_mono = time.monotonic()

        if ret_code is not None:
            break

        # Check timeout
        if timeout_seconds and (now_mono - start_mono) >= timeout_seconds:
            is_timeout = True
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except Exception as e:
                print(f"Error terminating process group: {e}", file=sys.stderr)

            grace_start = time.monotonic()
            while proc.poll() is None and (time.monotonic() - grace_start) < 2.0:
                time.sleep(0.1)

            if proc.poll() is None:
                try:
                    pgid = os.getpgid(proc.pid)
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except Exception as e:
                    print(f"Error force killing process group: {e}", file=sys.stderr)

            ret_code = proc.poll()
            break

        # Send heartbeat every HEARTBEAT_INTERVAL seconds
        if (now_mono - last_hb_mono) >= HEARTBEAT_INTERVAL:
            conn = get_db_conn(db_path)
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE job_runs SET last_heartbeat = ? WHERE run_id = ? AND status = 'RUNNING'",
                    (utc_now_str(), run_id),
                )
                conn.commit()
            except Exception as e:
                print(f"Heartbeat update failed: {e}", file=sys.stderr)
            finally:
                conn.close()
            last_hb_mono = now_mono

        time.sleep(0.2)

    log_fd.flush()
    log_fd.close()

    # Record terminal state
    end_time_str = utc_now_str()
    conn = get_db_conn(db_path)
    try:
        cursor = conn.cursor()
        if is_timeout:
            status = "TIMEOUT"
            err_desc = f"Execution timed out after {timeout_seconds} seconds"
        elif ret_code == 0:
            status = "SUCCESS"
            err_desc = None
        else:
            status = "FAILED"
            err_desc = f"Process exited with non-zero exit code: {ret_code}"

        cursor.execute(
            """
            UPDATE job_runs
            SET status = ?,
                end_time = ?,
                exit_code = ?,
                error_desc = ?
            WHERE run_id = ? AND status IN ('STARTING', 'RUNNING')
            """,
            (status, end_time_str, ret_code, err_desc, run_id),
        )
        conn.commit()
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Micro-Orchestrator Task Worker")
    parser.add_argument("--run-id", type=str, required=True, help="Execution run ID (UUID)")
    parser.add_argument("--db", type=str, default=DEFAULT_DB_PATH, help="Path to SQLite database")
    args = parser.parse_args()

    run_worker(args.run_id, db_path=args.db)


if __name__ == "__main__":
    main()
