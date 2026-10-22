# ⚡ Micro-Orchestrator

**Micro-Orchestrator** is a modern, lightweight task scheduler and monitoring system engineered for high reliability, zero-overhead background operation, and complete environment isolation.

---

## 🌟 Core Philosophy

1. **Zero-Daemon Dispatcher**
   - The dispatcher (`dispatch.py`) executes only when awakened every minute by the operating system's Crontab.
   - It performs fast schedule matching using `croniter`, dispatches asynchronous tasks via `subprocess.Popen(..., start_new_session=True)`, and **exits in < 0.1 seconds**, consuming zero background RAM and CPU.
2. **Complete Environment Isolation**
   - Each task has its own directory, isolated Python virtual environment (`.venv`), and dedicated logs (`logs/`).
   - Even if Task A requires Pandas 1.x and Task B requires Pandas 2.x, they run without dependency conflicts.
3. **Pure Shell State Reporting**
   - **Zero intrusion** on business code (`main.py`): scripts do not need to inherit from framework classes or import orchestrator SDKs.
   - Execution lifecycle tracking (`RUNNING -> SUCCESS / FAILED`), duration calculation, and exit codes (`$?`) are managed entirely inside an assembled Bash chain via the `sqlite3` CLI.
4. **High-Concurrency Lock-Free SQLite (WAL Mode)**
   - Powered by SQLite WAL (Write-Ahead Logging) mode, enabling non-blocking concurrent reads and writes between the Bash execution chain, the Dispatcher CLI, and the WebUI API.
5. **On-Demand WebUI Dashboard**
   - The management interface **does not need to run continuously in the background**. Whenever you need to check job status or edit configurations, simply double-click `run_ui.command` on macOS (or run `./run_ui.sh`) to automatically launch the browser dashboard.
   - Features an embedded Monaco Editor with YAML syntax highlighting, schema validation, instant saving, real-time status tracking, manual "Run Now" execution, and terminal-styled log viewing.

---

## 📁 Project Directory Structure

```text
micro-orchestrator/
├── orchestrator.db          # State database (SQLite WAL mode)
├── init_db.py               # Database initialization script (table creation & WAL setup)
├── jobs.yaml                # Job schedule definitions
├── dispatch.py              # Stateless async dispatcher invoked by Cron
├── server.py                # Lightweight FastAPI management API
├── run_ui.command           # macOS double-clickable launcher (auto-opens browser)
├── run_ui.sh                # Linux / universal launch script
├── requirements.txt         # Dispatcher and API dependencies
├── static/
│   └── index.html           # Dark-themed dashboard UI (embedded Monaco Editor)
└── tasks/                   # Business task directories (isolated environments)
    ├── crm_sync/
    │   ├── .venv/           # Dedicated Python virtual environment
    │   ├── main.py          # Business script (zero intrusion)
    │   └── logs/            # Execution log output
    ├── report_gen/
    │   ├── .venv/
    │   ├── main.py
    │   └── logs/
    └── test_fail_job/
        ├── .venv/
        ├── main.py
        └── logs/
```

---

## 🚀 Quick Start

### 1. Environment Setup & Initialization
This project requires Python 3.10+ and SQLite 3.35+ (supporting the `RETURNING` clause).

```bash
# Create root virtual environment and install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Initialize database (creates job_runs table and enables WAL mode)
python init_db.py
```

### 2. Test Task Dispatching
```bash
# List all configured jobs
python dispatch.py --list

# Manually trigger a single task (test crm_sync)
python dispatch.py --run crm_sync

# Manually trigger a simulated failing task (test error handling)
python dispatch.py --run test_fail_job
```

Inspect database execution records:
```bash
sqlite3 orchestrator.db "SELECT id, job_name, status, exit_code, log_file FROM job_runs ORDER BY id DESC LIMIT 5;"
```

### 3. Launch WebUI Dashboard (On-Demand)
- **macOS Users**: Double-click **`run_ui.command`** in Finder; it automatically launches `http://127.0.0.1:8000` in your default browser.
- **Terminal Users**:
  ```bash
  ./run_ui.sh
  ```
  Inside the dashboard, you can:
  - Edit `jobs.yaml` in the Monaco Editor on the left panel, and click **Save Changes** for instant validation and atomic saving.
  - View configured jobs on the right panel and click **Run Now** to trigger immediate execution.
  - Monitor recent execution history (green `SUCCESS`, pulsing amber `RUNNING`, red `FAILED`).
  - Click **View Log** to view real-time log outputs.
  - When finished, press `Ctrl + C` in the terminal to stop the server—no permanent background process needed.

---

## ⚙️ Configuration Specification (`jobs.yaml`)

```yaml
jobs:
  - name: crm_sync              # Required: Unique job name
    schedule: "*/10 * * * *"     # Required: Standard 5-field Cron expression
    dir: tasks/crm_sync         # Required: Job working directory (relative or absolute)
    python_bin: .venv/bin/python # Optional: Python interpreter path (defaults to system python3)
    script: main.py             # Required: Entry script to execute

  - name: report_gen
    schedule: "0 2 * * *"
    dir: tasks/report_gen
    python_bin: .venv/bin/python
    script: main.py
```

---

## ⏰ Production Deployment & Maintenance

### 1. Crontab Scheduled Dispatch
Add the Micro-Orchestrator dispatcher to Linux/macOS Cron to be awakened every minute:

```bash
crontab -e
```
Add the following line (use absolute paths):
```cron
# Wake up the dispatcher every minute (exits in < 0.1s)
* * * * * /Users/cenz.wong/Project/orchestrator/.venv/bin/python /Users/cenz.wong/Project/orchestrator/dispatch.py >> /Users/cenz.wong/Project/orchestrator/cron.log 2>&1
```

### 2. Log Maintenance (Periodic Cleanup)
To prevent unlimited log file growth, configure a weekly cleanup cron job for logs older than 30 days:

```cron
# Clean up task logs older than 30 days every Sunday at 03:00
0 3 * * 0 find /Users/cenz.wong/Project/orchestrator/tasks -type f -name "*.log" -mtime +30 -delete
```

---

## 🔌 API Reference

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/config` | Retrieve raw text content of `jobs.yaml` |
| `POST` | `/api/config` | Validate YAML syntax and job schema, then safely overwrite |
| `GET` | `/api/jobs` | Retrieve all configured jobs with their latest execution status |
| `GET` | `/api/status` | Fetch the last 50 execution runs (status, duration, exit code) |
| `POST` | `/api/run/{job_name}` | Manually trigger a specified job immediately (via shared Bash chain) |
| `GET` | `/api/logs?file=...` | Safely view tail content of a task log (protected against directory traversal) |
| `GET` | `/` | Dashboard Single-Page Application (`static/index.html`) |
