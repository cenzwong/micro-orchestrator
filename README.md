# ⚡ Micro-Orchestrator

**Micro-Orchestrator** is a modern, lightweight single-node Python task scheduler and monitoring system engineered for high reliability, zero-overhead background operation, and complete environment isolation.

---

## 🌟 Core Philosophy

1. **Zero-Daemon Dispatcher**
   - The dispatcher (`dispatch.py`) executes only when awakened every minute by the operating system's Crontab.
   - It performs fast schedule matching using `croniter`, runs lightweight reconciliation for stale executions, atomically claims eligible tasks in SQLite, launches detached Python worker processes (`worker.py`), and **exits in < 0.1 seconds**.
2. **Complete Environment Isolation**
   - Each task has its own directory, isolated Python virtual environment (`.venv`), and dedicated logs (`tasks/<job_name>/logs/<run_id>.log`).
   - Tasks execute independently without cross-task dependency conflicts.
3. **Zero-Intrusion Business Tasks**
   - Business scripts (`main.py`) remain standalone scripts without needing orchestrator SDKs or decorators.
   - Execution lifecycle tracking (`STARTING -> RUNNING -> SUCCESS / FAILED / TIMEOUT / LOST`), process heartbeats, exit codes, and log capturing are managed externally by `worker.py`.
4. **Reliable State & Lock-Free SQLite (WAL Mode)**
   - Powered by SQLite WAL mode and explicit transactions, enabling non-blocking concurrent reads and writes between workers, dispatcher, and the WebUI.
5. **On-Demand WebUI Dashboard**
   - The management interface does not need to run continuously in the background. Simply launch `./run_ui.sh` on demand.
   - Includes Monaco Editor with YAML validation, real-time job status tracking, manual "Run Now" execution, and log viewing.

---

## 📁 Project Directory Structure

```text
micro-orchestrator/
├── orchestrator.db          # State database (SQLite WAL mode, git-ignored)
├── init_db.py               # Database initialization & migration script
├── jobs.yaml                # Job schedule definitions
├── dispatch.py              # Stateless cron dispatcher & reconciler
├── worker.py                # Detached worker process executor
├── server.py                # Lightweight FastAPI management API
├── run_ui.command           # macOS double-clickable launcher
├── run_ui.sh                # Linux / universal launch script
├── requirements.txt         # Orchestrator dependencies
├── static/
│   └── index.html           # Dashboard UI
└── tasks/                   # Task directories
    └── example_job/
        ├── main.py          # Standalone business script
        └── logs/            # Execution logs
```

---

## 🚀 Quick Start

### 1. Environment Setup & Initialization
Requires Python 3.10+ and SQLite 3.35+.

```bash
# Create root virtual environment and install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Initialize database and schema migrations
python init_db.py
```

### 2. Test Task Dispatching
```bash
# List configured jobs
python dispatch.py --list

# Manually trigger example job
python dispatch.py --run example_job
```

Inspect database execution records:
```bash
sqlite3 orchestrator.db "SELECT id, run_id, job_name, status, exit_code, log_file FROM job_runs ORDER BY id DESC LIMIT 5;"
```

### 3. Launch WebUI Dashboard (On-Demand)
```bash
./run_ui.sh
```
Open `http://127.0.0.1:8000` in your browser.

---

## ⚙️ Configuration Specification (`jobs.yaml`)

```yaml
jobs:
  - name: example_job           # Required: Unique job identifier
    schedule: "*/5 * * * *"     # Required: Standard 5-field Cron expression
    dir: tasks/example_job      # Required: Working directory
    python_bin: .venv/bin/python# Optional: Python binary (defaults to system python3)
    script: main.py             # Required: Entry script
    concurrency: forbid         # Optional: 'allow' (default) or 'forbid'
    timeout_seconds: 600        # Optional: Execution timeout in seconds
```

---

## ⏰ Production Deployment

### Crontab Scheduled Dispatch
Add the dispatcher to Cron to wake up every minute:

```cron
* * * * * /path/to/micro-orchestrator/.venv/bin/python /path/to/micro-orchestrator/dispatch.py >> /path/to/micro-orchestrator/cron.log 2>&1
```

### Periodic Log Maintenance
Clean up log files older than 30 days:
```cron
0 3 * * 0 find /path/to/micro-orchestrator/tasks -type f -name "*.log" -mtime +30 -delete
```

---

## 🔌 API Reference

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/config` | Retrieve raw `jobs.yaml` content |
| `POST` | `/api/config` | Validate and update `jobs.yaml` |
| `GET` | `/api/jobs` | Retrieve configured jobs and latest status |
| `GET` | `/api/status` | Fetch recent execution runs |
| `POST` | `/api/run/{job_name}` | Trigger job manually (HTTP 202 or HTTP 409 Conflict) |
| `GET` | `/api/runs/{run_id}/logs` | Safely view execution log tail by `run_id` |
| `GET` | `/` | Dashboard Single-Page Application |
