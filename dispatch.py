"""
Micro-Orchestrator: Lightweight Zero-Daemon Cron Dispatcher.

Features:
- Reads jobs.yaml and evaluates cron schedules via croniter.
- Builds an atomic Bash execution chain:
    A. Inserts 'RUNNING' record into SQLite (WAL) and retrieves RUN_ID via RETURNING id.
    B. Changes to task directory and runs task with its own python interpreter.
    C. Captures exit code ($?).
    D. Updates record to 'SUCCESS' or 'FAILED' with timestamp and exit code.
- Launches jobs with subprocess.Popen(..., start_new_session=True) and exits in < 0.1s.
"""

import os
import sys
import argparse
import datetime
import subprocess
import yaml
from croniter import croniter

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB_PATH = os.path.join(BASE_DIR, "orchestrator.db")
DEFAULT_CONFIG_PATH = os.path.join(BASE_DIR, "jobs.yaml")


def load_jobs(config_path: str = DEFAULT_CONFIG_PATH) -> list:
    if not os.path.exists(config_path):
        return []
    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("jobs", [])


def build_bash_chain(job: dict, base_dir: str = BASE_DIR, db_path: str = DEFAULT_DB_PATH) -> tuple[str, str]:
    """
    Builds the Bash execution chain string and relative log path.
    Returns: (bash_script, rel_log_path)
    """
    job_name = job["name"]
    raw_dir = job.get("dir", ".")
    task_dir = os.path.abspath(os.path.join(base_dir, raw_dir)) if not os.path.isabs(raw_dir) else raw_dir

    raw_python = job.get("python_bin", "python3")
    if not os.path.isabs(raw_python):
        # Resolve relative to task_dir first, then relative to base_dir, then fallback to raw_python
        candidate1 = os.path.join(task_dir, raw_python)
        candidate2 = os.path.join(base_dir, raw_python)
        if os.path.exists(candidate1):
            python_bin = os.path.abspath(candidate1)
        elif os.path.exists(candidate2):
            python_bin = os.path.abspath(candidate2)
        else:
            python_bin = raw_python
    else:
        python_bin = raw_python

    script = job.get("script", "main.py")
    script_abs = os.path.abspath(os.path.join(task_dir, script)) if not os.path.isabs(script) else script

    # Ensure logs directory
    logs_dir = os.path.join(task_dir, "logs")
    rel_logs_dir = os.path.relpath(logs_dir, base_dir)

    # Database absolute path
    abs_db = os.path.abspath(db_path)

    # Shell script with escaped variables
    bash_script = f"""set -o pipefail
mkdir -p "{logs_dir}"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
LOG_FILE="{logs_dir}/${{TIMESTAMP}}_{job_name}.log"
REL_LOG="{rel_logs_dir}/${{TIMESTAMP}}_{job_name}.log"

# Step A: Write RUNNING state and capture RUN_ID via RETURNING id
RUN_ID=$(sqlite3 "{abs_db}" "INSERT INTO job_runs (job_name, status, start_time, log_file) VALUES ('{job_name}', 'RUNNING', datetime('now', 'localtime'), '${{REL_LOG}}') RETURNING id;")

if [ -z "$RUN_ID" ]; then
    echo "CRITICAL: Could not insert job_runs record for {job_name}" >&2
    exit 1
fi

# Step B: cd to task dir and execute isolated python script
cd "{task_dir}"
"{python_bin}" "{script_abs}" > "$LOG_FILE" 2>&1
EXIT_CODE=$?

# Step C: Determine status based on exit code
if [ $EXIT_CODE -eq 0 ]; then
    STATUS="SUCCESS"
else
    STATUS="FAILED"
fi

# Step D: Update record with final status, end_time, and exit_code
sqlite3 "{abs_db}" "UPDATE job_runs SET status='$STATUS', end_time=datetime('now', 'localtime'), exit_code=$EXIT_CODE WHERE id=$RUN_ID;"
"""
    return bash_script, f"{rel_logs_dir}/<TIMESTAMP>_{job_name}.log"


def launch_job(job: dict, base_dir: str = BASE_DIR, db_path: str = DEFAULT_DB_PATH) -> bool:
    """
    Spawns background detached bash session and immediately returns.
    """
    bash_script, _ = build_bash_chain(job, base_dir, db_path)
    subprocess.Popen(
        ["bash", "-c", bash_script],
        cwd=base_dir,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL
    )
    return True


def dispatch_jobs(force_name: str | None = None, now: datetime.datetime | None = None) -> list[str]:
    """
    Checks schedules and triggers jobs. Returns list of triggered job names.
    """
    if now is None:
        now = datetime.datetime.now()

    jobs = load_jobs()
    triggered = []

    for job in jobs:
        name = job.get("name")
        if not name:
            continue

        if force_name:
            if name == force_name:
                launch_job(job)
                triggered.append(name)
        else:
            schedule = job.get("schedule")
            if schedule and croniter.match(schedule, now):
                launch_job(job)
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
            print(f"  - {j.get('name')}: schedule='{j.get('schedule')}' dir='{j.get('dir')}' script='{j.get('script')}'")
        return

    triggered = dispatch_jobs(force_name=args.run)
    if args.run:
        if triggered:
            print(f"Triggered job '{args.run}' successfully.")
        else:
            print(f"Job '{args.run}' not found in {DEFAULT_CONFIG_PATH}.", file=sys.stderr)
            sys.exit(1)
    else:
        if triggered:
            print(f"Dispatched {len(triggered)} job(s): {', '.join(triggered)}")


if __name__ == "__main__":
    main()
