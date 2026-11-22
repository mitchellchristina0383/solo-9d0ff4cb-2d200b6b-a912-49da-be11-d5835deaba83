"""Eviction success vs. inspection ``DELETE /pods/{uid}`` reconciliation."""

from __future__ import annotations

import sqlite3

import pytest

from conftest import add_pod, claim, register_workload, settle


def _remove(client, uid: str) -> dict:
    r = client.delete(f"/pods/{uid}")
    assert r.status_code == 200, r.text
    return r.json()


def test_delete_after_success_keeps_evicted_and_retired_at(client):
    """Node success commits first; the inspection delete is inert."""
    register_workload(client, "web", 1)
    add_pod(client, "web-0", "n1", "web")
    add_pod(client, "web-1", "n1", "web")
    task = claim(client, "n1")["approved"][0]

    done = settle(client, task["task_id"], "succeeded")
    assert done["applied"] is True
    evicted = client.get(f"/pods/{task['pod_uid']}").json()
    assert evicted["state"] == "evicted"
    assert evicted["retired_at"] is not None
    resolved_at = done["task"]["resolved_at"]
    assert evicted["retired_at"] == resolved_at

    removed = _remove(client, task["pod_uid"])
    assert removed["state"] == "evicted"
    assert removed["retired_at"] == evicted["retired_at"]
    assert removed["ready"] is False

    pod = client.get(f"/pods/{task['pod_uid']}").json()
    assert pod["state"] == "evicted"
    assert pod["retired_at"] == evicted["retired_at"]

    wl = client.get("/workloads/web").json()
    assert wl["evicted"] == 1
    assert wl["removed"] == 0

    # Task history is untouched and the settled task stays succeeded.
    assert client.get(f"/drain/tasks/{task['task_id']}").json()["status"] == "succeeded"


def test_delete_is_idempotent_for_evicted_pod(client):
    register_workload(client, "web", 0)
    add_pod(client, "web-0", "n1", "web")
    task = claim(client, "n1")["approved"][0]
    settle(client, task["task_id"], "succeeded")
    first = _remove(client, "web-0")
    second = _remove(client, "web-0")
    assert first["state"] == second["state"] == "evicted"
    assert first["retired_at"] == second["retired_at"]
    assert client.get("/workloads/web").json()["evicted"] == 1


def test_manual_removal_first_still_aborts_task_and_ignores_late_success(client):
    """The pre-existing manual-removal rules are preserved."""
    register_workload(client, "web", 1)
    add_pod(client, "web-0", "n1", "web")
    add_pod(client, "web-1", "n1", "web")
    task = claim(client, "n1")["approved"][0]

    removed = _remove(client, task["pod_uid"])
    assert removed["state"] == "removed"
    assert removed["open_task_id"] is None

    aborted = client.get(f"/drain/tasks/{task['task_id']}").json()
    assert aborted["status"] == "aborted"
    assert aborted["result_note"] == "pod_removed"

    late = settle(client, task["task_id"], "succeeded")
    assert late["applied"] is False
    assert late["ignored_reason"] == "task_already_resolved"

    pod = client.get(f"/pods/{task['pod_uid']}").json()
    assert pod["state"] == "removed"
    wl = client.get("/workloads/web").json()
    assert wl["evicted"] == 0
    assert wl["removed"] == 1
    assert wl["reserved"] == 0


def _seed_legacy_bug(db_file, pod_uid: str, *, deletion_time: str) -> None:
    """Reproduce what the old code wrote: evicted pod relabelled removed."""
    conn = sqlite3.connect(db_file)
    try:
        conn.execute(
            "UPDATE pods SET state = 'removed', ready = 0, retired_at = ?,"
            " updated_at = ? WHERE uid = ?",
            (deletion_time, deletion_time, pod_uid),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def legacy_db(client, monkeypatch):
    """An API-created evicted pod, then mutated into the old bug shape."""
    register_workload(client, "web", 1)
    add_pod(client, "web-0", "n1", "web")
    add_pod(client, "web-1", "n1", "web")
    task = claim(client, "n1")["approved"][0]
    done = settle(client, task["task_id"], "succeeded", report_id="node-success-1")
    resolved_at = done["task"]["resolved_at"]
    # A genuinely removed pod alongside it (no succeeded task).
    add_pod(client, "web-2", "n1", "web")
    client.delete("/pods/web-2")
    db_file = client.db_path
    client.close()
    _seed_legacy_bug(db_file, "web-0", deletion_time="2099-01-01T00:00:00.000Z")
    return db_file, task, resolved_at


def _reopen(tmp_path, monkeypatch):
    import sys

    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    monkeypatch.setenv("DRAIN_DB_PATH", str(tmp_path / "drain.db"))
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def test_old_volume_repair_restores_evicted_and_keeps_history(legacy_db, monkeypatch):
    db_file, task, resolved_at = legacy_db
    with _reopen(db_file.parent, monkeypatch) as fresh:
        pod = fresh.get("/pods/web-0").json()
        assert pod["state"] == "evicted"
        assert pod["ready"] is False
        assert pod["retired_at"] == resolved_at

        # A genuine removal is preserved.
        assert fresh.get("/pods/web-2").json()["state"] == "removed"

        # Workload counters and the state filter reflect the repaired row from
        # the very first query after start.
        wl = fresh.get("/workloads/web").json()
        assert wl["evicted"] == 1
        assert wl["removed"] == 1
        assert wl["effective_ready"] == 1

        evicted = [p["uid"] for p in fresh.get("/pods", params={"state": "evicted"}).json()]
        removed = [p["uid"] for p in fresh.get("/pods", params={"state": "removed"}).json()]
        assert evicted == ["web-0"]
        assert removed == ["web-2"]

        # Task and stored report history is untouched.
        assert fresh.get(f"/drain/tasks/{task['task_id']}").json()["status"] == "succeeded"
        replay = settle(fresh, task["task_id"], "succeeded", report_id="node-success-1")
        assert replay["duplicate"] is True and replay["applied"] is True
        assert fresh.get("/pods/web-0").json()["state"] == "evicted"


def test_old_volume_repair_is_idempotent(legacy_db, monkeypatch):
    db_file, _task, resolved_at = legacy_db
    with _reopen(db_file.parent, monkeypatch) as fresh:
        first = fresh.get("/pods/web-0").json()
    with _reopen(db_file.parent, monkeypatch) as again:
        second = again.get("/pods/web-0").json()
    assert first["state"] == second["state"] == "evicted"
    assert first["retired_at"] == second["retired_at"] == resolved_at


def test_old_volume_repair_is_atomic_and_retryable(legacy_db, monkeypatch):
    from app import db

    db_file, _task, resolved_at = legacy_db
    real = db.repair_misclassified_removals

    def boom(conn):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(db, "repair_misclassified_removals", boom)
    with pytest.raises(RuntimeError):
        db.init_db(db_file)

    # Nothing was partially written: the old bug shape is still present.
    conn = db.connect(db_file)
    try:
        state = conn.execute("SELECT state FROM pods WHERE uid = 'web-0'").fetchone()[0]
    finally:
        conn.close()
    assert state == "removed"

    # A later start (restart) retries and completes the repair.
    monkeypatch.setattr(db, "repair_misclassified_removals", real)
    db.init_db(db_file)
    conn = db.connect(db_file)
    try:
        row = conn.execute(
            "SELECT state, retired_at FROM pods WHERE uid = 'web-0'"
        ).fetchone()
    finally:
        conn.close()
    assert row["state"] == "evicted"
    assert row["retired_at"] == resolved_at


def test_repair_rolls_back_all_rows_on_failure(legacy_db, monkeypatch):
    """Batch semantics: a failure mid-batch restores no rows at all."""
    from app import db

    db_file, _task, _resolved_at = legacy_db
    conn = db.connect(db_file)
    try:
        # A second misclassified pod, so a partially applied batch is visible.
        conn.execute(
            "INSERT INTO pods(uid, node, workload, ready, state, created_at,"
            " updated_at, retired_at)"
            " SELECT 'web-1-copy', node, workload, 0, 'removed', created_at,"
            " updated_at, '2099-01-01T00:00:00.000Z' FROM pods WHERE uid='web-1'"
        )
        conn.execute(
            "INSERT INTO eviction_tasks(id, pod_uid, node, workload, status,"
            " created_at, updated_at, resolved_at)"
            " SELECT 'copy-task', 'web-1-copy', node, workload, 'succeeded',"
            " created_at, updated_at, '2020-01-01T00:00:00.000Z'"
            " FROM eviction_tasks LIMIT 1"
        )
    finally:
        conn.close()

    def fail_after(conn_arg):
        with db.transaction(conn_arg, immediate=True):
            conn_arg.execute(db.REPAIR_MISCLASSIFIED_REMOVALS)
            raise RuntimeError("simulated mid-batch failure")

    monkeypatch.setattr(db, "repair_misclassified_removals", fail_after)
    with pytest.raises(RuntimeError):
        db.init_db(db_file)

    conn = db.connect(db_file)
    try:
        states = {
            r["uid"]: r["state"]
            for r in conn.execute(
                "SELECT uid, state FROM pods WHERE uid IN ('web-0', 'web-1-copy')"
            ).fetchall()
        }
    finally:
        conn.close()
    assert states == {"web-0": "removed", "web-1-copy": "removed"}


def _init_worker(db_path: str, barrier) -> str:
    """Startup path for one worker process sharing the old volume."""
    import os

    os.environ["DRAIN_DB_PATH"] = db_path
    from app import db as worker_db

    barrier.wait(timeout=10)
    worker_db.init_db(db_path)
    conn = worker_db.connect(db_path)
    try:
        row = conn.execute(
            "SELECT state, retired_at FROM pods WHERE uid = 'web-0'"
        ).fetchone()
        wl = conn.execute(
            "SELECT COUNT(*) AS n FROM pods WHERE workload = 'web' AND state = 'evicted'"
        ).fetchone()["n"]
    finally:
        conn.close()
    return f"{row['state']}|{row['retired_at']}|{wl}"


def test_multiple_workers_share_old_volume_and_see_repaired_state(legacy_db, monkeypatch):
    """Every worker observes the repair from its first query after startup."""
    import multiprocessing as mp

    db_file, _task, resolved_at = legacy_db
    ctx = mp.get_context("spawn")
    with ctx.Manager() as manager:
        barrier = manager.Barrier(4)
        with ctx.Pool(4) as pool:
            results = pool.starmap(_init_worker, [(str(db_file), barrier)] * 4)
    assert results == [f"evicted|{resolved_at}|1"] * 4
