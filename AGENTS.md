# AGENTS.md - Developer & Agent Guidelines

This document outlines the architectural principles, behavioral guidelines, and best practices for AI Coding Agents and software engineers maintaining, extending, and debugging the **Micro-Orchestrator** project.

---

## 🏛️ Core Architecture Invariants

When making any modifications to this project, **the following invariants must be strictly preserved**:

### 1. Strict "Zero-Daemon Dispatcher"
- `dispatch.py` must remain **stateless** and complete execution in minimal time (< 0.1 seconds goal).
- **Never** introduce `while True`, `time.sleep()`, or persistent daemon processes listening for schedules into `dispatch.py`.
- Scheduled execution relies entirely on operating-system-level schedulers (e.g., Linux `cron` or macOS `launchd`).

### 2. Python Worker Lifecycle Management
- Execution lifecycle (`STARTING -> RUNNING -> SUCCESS / FAILED / TIMEOUT / LOST`) is managed via `worker.py`.
- `dispatch.py` claims runs in SQLite and spawns `worker.py --run-id <run_id>` as a detached process.
- `worker.py` loads an immutable JSON configuration snapshot from the DB, executes the task subprocess with argument arrays (avoiding shell execution), updates heartbeats, and records output to `tasks/<job>/logs/<run_id>.log`.

### 3. Zero Intrusion on Tasks
- `tasks/*/main.py` contains pure, standalone business logic. **Never** force tasks to import orchestrator SDKs or use decorators.

### 4. Strict Environment Isolation
- Each task directory should be treated as an isolated micro-project with its own `.venv` and logs.
- Tasks must be invoked using their configured Python binary path.

### 5. Database WAL Mode & Concurrency Guarantees
- `orchestrator.db` must always maintain `PRAGMA journal_mode=WAL;` and `PRAGMA busy_timeout = 5000;`.
- Job claiming and concurrency checks (`allow` / `forbid`) are performed inside SQLite transactions (`BEGIN IMMEDIATE`).

### 6. On-Demand WebUI
- The management Web server runs on-demand (`./run_ui.sh`).
- Manual job execution (`POST /api/run/{job_name}`) reuses the shared job claiming and worker launching pathway.

---

## 🛠️ Development & Maintenance Guidelines

### 1. Standard Procedure for Adding a Task
1. Create a directory under `tasks/`, e.g., `tasks/my_task/`.
2. Setup virtual environment if dependencies are needed.
3. Write script `tasks/my_task/main.py`.
4. Register in `jobs.yaml`:
   ```yaml
   - name: my_task
     schedule: "15 * * * *"
     dir: tasks/my_task
     python_bin: .venv/bin/python
     script: main.py
     concurrency: forbid
     timeout_seconds: 300
   ```
5. Verify execution:
   ```bash
   python dispatch.py --run my_task
   ```

### 2. Security Guidelines
- Parameterize all SQL queries.
- Use `subprocess` argument arrays instead of `shell=True`.
- Validate job names with strict identifier patterns (`^[a-zA-Z0-9_-]+$`).
- Restrict log access via `GET /api/runs/{run_id}/logs` using canonical path validation against allowed task directories.
- Avoid inline JavaScript interpolation in the WebUI DOM.

---

## 🧪 Common Testing Commands

```bash
# Initialize / migrate database
python init_db.py

# Run unit and integration tests
pytest

# List configured jobs
python dispatch.py --list

# Manually trigger a job
python dispatch.py --run example_job
```
