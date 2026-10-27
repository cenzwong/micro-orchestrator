import json
import os
import sqlite3
import tempfile
import time
import pytest
from fastapi.testclient import TestClient

import dispatch
import init_db
import server
import worker


@pytest.fixture
def tmp_env(tmp_path):
    db_path = str(tmp_path / "test_orchestrator.db")
    config_path = str(tmp_path / "jobs.yaml")
    task_dir = tmp_path / "tasks" / "test_job"
    task_dir.mkdir(parents=True)
    log_dir = task_dir / "logs"
    log_dir.mkdir(parents=True)

    # Create dummy main.py script
    script_path = task_dir / "main.py"
    script_path.write_text("import sys\nprint('hello from test_job')\nsys.exit(0)\n")

    # Write jobs.yaml
    jobs_data = {
        "jobs": [
            {
                "name": "test_job",
                "schedule": "* * * * *",
                "dir": str(task_dir),
                "python_bin": "python3",
                "script": "main.py",
                "concurrency": "forbid",
                "timeout_seconds": 10,
            }
        ]
    }
    with open(config_path, "w") as f:
        import yaml
        yaml.dump(jobs_data, f)

    init_db.init_db(db_path)

    return {
        "db_path": db_path,
        "config_path": config_path,
        "task_dir": str(task_dir),
        "script_path": str(script_path),
        "tmp_path": tmp_path,
    }


def test_init_db_and_migration(tmp_env):
    db_path = tmp_env["db_path"]
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(job_runs);")
    cols = {row[1] for row in cursor.fetchall()}
    assert "run_id" in cols
    assert "status" in cols
    assert "worker_pid" in cols
    conn.close()


def test_claim_and_worker_execution(tmp_env):
    db_path = tmp_env["db_path"]
    config_path = tmp_env["config_path"]

    jobs = dispatch.load_jobs(config_path)
    job = jobs[0]

    ok, run_id, msg = dispatch.claim_and_launch_job(
        job, trigger_type="manual", base_dir=str(tmp_env["tmp_path"]), db_path=db_path
    )
    assert ok is True
    assert run_id != ""

    # Wait briefly for worker to complete
    time.sleep(1.5)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM job_runs WHERE run_id = ?", (run_id,))
    row = dict(cursor.fetchone())
    conn.close()

    assert row["status"] == "SUCCESS"
    assert row["exit_code"] == 0


def test_concurrency_forbid(tmp_env):
    db_path = tmp_env["db_path"]
    config_path = tmp_env["config_path"]

    jobs = dispatch.load_jobs(config_path)
    job = jobs[0]

    # Create a dummy script that sleeps
    script_path = tmp_env["task_dir"] + "/main.py"
    with open(script_path, "w") as f:
        f.write("import time\ntime.sleep(5)\n")

    # Launch first instance
    ok1, run_id1, msg1 = dispatch.claim_and_launch_job(
        job, trigger_type="manual", base_dir=str(tmp_env["tmp_path"]), db_path=db_path
    )
    assert ok1 is True

    # Try launching second instance while first is active
    ok2, run_id2, msg2 = dispatch.claim_and_launch_job(
        job, trigger_type="manual", base_dir=str(tmp_env["tmp_path"]), db_path=db_path
    )
    assert ok2 is False
    assert "already active" in msg2


def test_timeout_execution(tmp_env):
    db_path = tmp_env["db_path"]

    # Short timeout job
    job = {
        "name": "timeout_job",
        "dir": tmp_env["task_dir"],
        "python_bin": "python3",
        "script": "timeout_script.py",
        "timeout_seconds": 1,
    }

    script_path = os.path.join(tmp_env["task_dir"], "timeout_script.py")
    with open(script_path, "w") as f:
        f.write("import time\ntime.sleep(10)\n")

    ok, run_id, msg = dispatch.claim_and_launch_job(
        job, trigger_type="manual", base_dir=str(tmp_env["tmp_path"]), db_path=db_path
    )
    assert ok is True

    time.sleep(2.5)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT status FROM job_runs WHERE run_id = ?", (run_id,))
    row = dict(cursor.fetchone())
    conn.close()

    assert row["status"] == "TIMEOUT"


def test_reconciliation_lost_job(tmp_env):
    db_path = tmp_env["db_path"]

    # Insert a fake job run with dead PID and stale heartbeat
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    stale_time = "2020-01-01 00:00:00"
    cursor.execute(
        """
        INSERT INTO job_runs (run_id, job_name, status, start_time, last_heartbeat, worker_pid)
        VALUES ('stale_run_123', 'test_job', 'RUNNING', ?, ?, 999999)
        """,
        (stale_time, stale_time),
    )
    conn.commit()
    conn.close()

    dispatch.reconcile_stale_runs(db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT status FROM job_runs WHERE run_id = 'stale_run_123'")
    row = dict(cursor.fetchone())
    conn.close()

    assert row["status"] == "LOST"


def test_api_endpoints(tmp_env, monkeypatch):
    monkeypatch.setattr(server, "DB_PATH", tmp_env["db_path"])
    monkeypatch.setattr(server, "CONFIG_PATH", tmp_env["config_path"])
    monkeypatch.setattr(dispatch, "DEFAULT_DB_PATH", tmp_env["db_path"])
    monkeypatch.setattr(dispatch, "DEFAULT_CONFIG_PATH", tmp_env["config_path"])

    client = TestClient(server.app)

    # Test GET /api/config
    res = client.get("/api/config")
    assert res.status_code == 200
    assert "test_job" in res.json()["content"]

    # Test GET /api/jobs
    res = client.get("/api/jobs")
    assert res.status_code == 200
    assert len(res.json()["jobs"]) == 1

    # Test POST /api/run/test_job
    res = client.post("/api/run/test_job")
    assert res.status_code in (202, 409)

    # Test GET /api/runs/{run_id}/logs path security
    res = client.get("/api/runs/non_existent_run_id/logs")
    assert res.status_code == 404


def test_validate_job_config():
    # Valid new command configuration
    valid_cmd_job = {
        "name": "valid_cmd",
        "schedule": "* * * * *",
        "dir": "tasks/valid_cmd",
        "command": ["python3", "main.py", "--arg", "val"],
    }
    dispatch.validate_job_config(valid_cmd_job)

    # Valid legacy configuration
    valid_legacy_job = {
        "name": "valid_legacy",
        "schedule": "* * * * *",
        "dir": "tasks/valid_legacy",
        "python_bin": "python3",
        "script": "main.py",
    }
    dispatch.validate_job_config(valid_legacy_job)

    # Reject job with both command and script
    with pytest.raises(ValueError, match="cannot specify both"):
        dispatch.validate_job_config({
            "name": "conflict_job",
            "schedule": "* * * * *",
            "dir": "tasks/conflict",
            "command": ["python3", "main.py"],
            "script": "main.py",
        })

    # Reject command string instead of list
    with pytest.raises(ValueError, match="must be a non-empty list"):
        dispatch.validate_job_config({
            "name": "str_cmd_job",
            "schedule": "* * * * *",
            "dir": "tasks/str_cmd",
            "command": "python3 main.py",
        })

    # Reject empty command list
    with pytest.raises(ValueError, match="must be a non-empty list"):
        dispatch.validate_job_config({
            "name": "empty_cmd_job",
            "schedule": "* * * * *",
            "dir": "tasks/empty_cmd",
            "command": [],
        })

    # Reject non-string argument in command list
    with pytest.raises(ValueError, match="must be a string"):
        dispatch.validate_job_config({
            "name": "bad_arg_job",
            "schedule": "* * * * *",
            "dir": "tasks/bad_arg",
            "command": ["python3", 123],
        })

    # Reject empty executable
    with pytest.raises(ValueError, match="cannot be empty or whitespace"):
        dispatch.validate_job_config({
            "name": "blank_exec_job",
            "schedule": "* * * * *",
            "dir": "tasks/blank_exec",
            "command": ["   ", "main.py"],
        })


def test_build_command(tmp_path):
    task_dir = str(tmp_path / "task_dir")
    base_dir = str(tmp_path)
    os.makedirs(task_dir, exist_ok=True)

    # Absolute executable
    cmd_abs = dispatch.build_command(
        {"command": ["/usr/bin/python3", "main.py"]}, task_dir, base_dir
    )
    assert cmd_abs == ["/usr/bin/python3", "main.py"]

    # Relative executable with /
    cmd_rel = dispatch.build_command(
        {"command": [".venv/bin/python", "main.py"]}, task_dir, base_dir
    )
    assert cmd_rel == [os.path.abspath(os.path.join(task_dir, ".venv/bin/python")), "main.py"]

    # Bare executable
    cmd_bare = dispatch.build_command(
        {"command": ["python3", "main.py", "arg with spaces", ""]}, task_dir, base_dir
    )
    assert cmd_bare == ["python3", "main.py", "arg with spaces", ""]


def test_generic_command_execution(tmp_env):
    db_path = tmp_env["db_path"]
    task_dir = tmp_env["task_dir"]

    # Create a bash script inside task_dir
    bash_script = os.path.join(task_dir, "run.sh")
    with open(bash_script, "w") as f:
        f.write("#!/bin/bash\necho 'hello from generic bash'\nexit 0\n")
    os.chmod(bash_script, 0o755)

    job = {
        "name": "bash_test_job",
        "schedule": "* * * * *",
        "dir": task_dir,
        "command": ["./run.sh"],
    }

    ok, run_id, msg = dispatch.claim_and_launch_job(
        job, trigger_type="manual", base_dir=str(tmp_env["tmp_path"]), db_path=db_path
    )
    assert ok is True

    time.sleep(1.5)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM job_runs WHERE run_id = ?", (run_id,))
    row = dict(cursor.fetchone())
    conn.close()

    assert row["status"] == "SUCCESS"
    assert row["exit_code"] == 0

    # Verify log output
    abs_log_path = os.path.join(str(tmp_env["tmp_path"]), row["log_file"])
    with open(abs_log_path, "r") as f:
        log_content = f.read()
    assert "hello from generic bash" in log_content


def test_generic_command_failure(tmp_env):
    db_path = tmp_env["db_path"]
    task_dir = tmp_env["task_dir"]

    # Job that exits non-zero
    job = {
        "name": "failed_cmd_job",
        "schedule": "* * * * *",
        "dir": task_dir,
        "command": ["python3", "-c", "import sys; sys.exit(42)"],
    }

    ok, run_id, msg = dispatch.claim_and_launch_job(
        job, trigger_type="manual", base_dir=str(tmp_env["tmp_path"]), db_path=db_path
    )
    assert ok is True

    time.sleep(1.5)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT status, exit_code, error_desc FROM job_runs WHERE run_id = ?", (run_id,))
    row = dict(cursor.fetchone())
    conn.close()

    assert row["status"] == "FAILED"
    assert row["exit_code"] == 42


def test_missing_executable_failure(tmp_env):
    db_path = tmp_env["db_path"]
    task_dir = tmp_env["task_dir"]

    job = {
        "name": "non_existent_exec_job",
        "schedule": "* * * * *",
        "dir": task_dir,
        "command": ["./non_existent_binary_xyz_123"],
    }

    ok, run_id, msg = dispatch.claim_and_launch_job(
        job, trigger_type="manual", base_dir=str(tmp_env["tmp_path"]), db_path=db_path
    )
    assert ok is True

    time.sleep(1.5)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT status, error_desc FROM job_runs WHERE run_id = ?", (run_id,))
    row = dict(cursor.fetchone())
    conn.close()

    assert row["status"] == "FAILED"
    assert "Failed to spawn task process" in row["error_desc"]


def test_generic_command_timeout(tmp_env):
    db_path = tmp_env["db_path"]
    task_dir = tmp_env["task_dir"]

    job = {
        "name": "generic_timeout_job",
        "schedule": "* * * * *",
        "dir": task_dir,
        "command": ["bash", "-c", "sleep 10"],
        "timeout_seconds": 1,
    }

    ok, run_id, msg = dispatch.claim_and_launch_job(
        job, trigger_type="manual", base_dir=str(tmp_env["tmp_path"]), db_path=db_path
    )
    assert ok is True

    time.sleep(2.5)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT status, error_desc FROM job_runs WHERE run_id = ?", (run_id,))
    row = dict(cursor.fetchone())
    conn.close()

    assert row["status"] == "TIMEOUT"
