#!/usr/bin/env python3
"""Check eviction-success vs. ``DELETE /pods/{uid}`` reconciliation.

Scenario A (run against a live server)::

    python scripts/verify_reconcile.py --base http://127.0.0.1:8000

registers two pods, claims one, reports ``succeeded``, then issues the
inspection delete.  It asserts the pod stays ``evicted`` with its original
``retired_at`` and that the workload's evicted count is not reduced.

Scenario B is the opposite order: the delete commits first, so the open task
is aborted and the late success must be inert.

Legacy upgrade (run with the server stopped, then restart it)::

    python scripts/verify_reconcile.py --seed-legacy --db-path /data/drain.db

rewrites the most recently evicted pod into the shape old builds produced
(``state=removed`` with a ``succeeded`` task) and prints the expected
restored values.  After the next start, ``GET /pods/{uid}`` must show
``evicted`` with the task's settlement time, and the workload counters must
match.

Exit code 0 means every check held.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys

import httpx

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)
        print(f"  FAIL {message}")


def setup(client: httpx.Client, prefix: str) -> tuple[str, dict]:
    """Fresh workload + node per scenario so the runs cannot interfere."""
    workload = f"w-{prefix}"
    node = f"n-{prefix}"
    client.put(f"/workloads/{workload}", json={"ready_floor": 1}).raise_for_status()
    for i in range(2):
        uid = f"{prefix}-{i}"
        client.put(
            f"/pods/{uid}",
            json={"uid": uid, "node": node, "workload": workload, "ready": True},
        ).raise_for_status()
    body = client.post("/drain/claims", json={"node": node, "limit": 1}).json()
    assert body["approved"], body
    assert len(body["approved"]) == 1, body
    return workload, body["approved"][0]


def scenario_success_then_delete(client: httpx.Client, workload: str, task: dict) -> None:
    done = client.post(
        f"/drain/tasks/{task['task_id']}/result", json={"outcome": "succeeded"}
    ).json()
    before = client.get(f"/pods/{task['pod_uid']}").json()
    after = client.delete(f"/pods/{task['pod_uid']}").json()
    wl = client.get(f"/workloads/{workload}").json()
    print(
        "  after success:", before["state"], before["retired_at"],
        "| after delete:", after["state"], after["retired_at"],
    )
    check(done["applied"] is True, "success report was not applied")
    check(before["state"] == "evicted", "success did not mark the pod evicted")
    check(after["state"] == "evicted", "delete reclassified an evicted pod")
    check(after["retired_at"] == before["retired_at"], "delete moved retired_at")
    check(wl["evicted"] == 1, f"evicted count is {wl['evicted']}, expected 1")
    check(wl["removed"] == 0, f"removed count is {wl['removed']}, expected 0")
    check(
        client.get(f"/drain/tasks/{task['task_id']}").json()["status"] == "succeeded",
        "task history changed after the delete",
    )


def scenario_delete_then_success(client: httpx.Client, workload: str, task: dict) -> None:
    removed = client.delete(f"/pods/{task['pod_uid']}").json()
    settled = client.get(f"/drain/tasks/{task['task_id']}").json()
    late = client.post(
        f"/drain/tasks/{task['task_id']}/result", json={"outcome": "succeeded"}
    ).json()
    wl = client.get(f"/workloads/{workload}").json()
    print(
        "  after delete:", removed["state"], "| task:", settled["status"],
        settled["result_note"], "| late success applied:", late["applied"],
    )
    check(removed["state"] == "removed", "manual removal did not take effect")
    check(settled["status"] == "aborted", "open task was not aborted by removal")
    check(settled["result_note"] == "pod_removed", "abort note is not pod_removed")
    check(late["applied"] is False, "late success was applied after removal")
    check(wl["removed"] == 1, f"removed count is {wl['removed']}, expected 1")
    check(wl["evicted"] == 0, f"evicted count is {wl['evicted']}, expected 0")
    check(wl["reserved"] == 0, f"reserved is {wl['reserved']}, expected 0")


def run_live(base: str) -> int:
    with httpx.Client(base_url=base, timeout=30.0) as client:
        print("scenario A: success report first, inspection delete second")
        scenario_success_then_delete(client, *setup(client, "a"))
        print("scenario B: manual removal first, late success second")
        scenario_delete_then_success(client, *setup(client, "b"))
    return 1 if FAILURES else 0


def seed_legacy(db_path: str) -> int:
    """Rewrite the newest evicted pod into the shape old builds produced."""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT p.uid, t.resolved_at"
            "  FROM pods p JOIN eviction_tasks t ON t.pod_uid = p.uid"
            " WHERE p.state = 'evicted' AND t.status = 'succeeded'"
            " ORDER BY t.resolved_at DESC LIMIT 1"
        ).fetchone()
        if row is None:
            print("no evicted pod with a succeeded task to seed", file=sys.stderr)
            return 2
        uid, resolved_at = row
        conn.execute(
            "UPDATE pods SET state = 'removed', ready = 0,"
            " retired_at = '2099-01-01T00:00:00.000Z',"
            " updated_at = '2099-01-01T00:00:00.000Z' WHERE uid = ?",
            (uid,),
        )
        conn.commit()
    finally:
        conn.close()
    print(f"seeded legacy shape for {uid!r}; restart the service, then check:")
    print(f"  GET /pods/{uid}  -> state=evicted retired_at={resolved_at}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--db-path", default=None)
    ap.add_argument("--seed-legacy", action="store_true")
    args = ap.parse_args()
    if args.seed_legacy:
        if not args.db_path:
            ap.error("--seed-legacy requires --db-path")
        return seed_legacy(args.db_path)
    code = run_live(args.base)
    print("\nall reconciliation checks held" if code == 0 else "\nFAILURES")
    return code


if __name__ == "__main__":
    sys.exit(main())
