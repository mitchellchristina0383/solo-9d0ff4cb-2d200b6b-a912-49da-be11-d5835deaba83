# Self-test record

Environment used for verification: Linux, Python 3.11.2, uvicorn (4 workers for
the concurrency/restart runs, 2 for the container-entrypoint run), SQLite in
WAL mode. Docker is **not installed** in this sandbox, so the Compose stack
itself was not executed here; everything else was run against a real HTTP
server with the pinned dependency versions from `requirements.txt`
(FastAPI 0.115.14, Pydantic 2.11.7, uvicorn 0.34.3).

## 1. Test suite

```
$ python -m pytest -q
23 passed
```

Coverage of the requested behaviours:

| Requirement | Test |
| --- | --- |
| Approved evictions never drop a workload below its ready floor | `test_claim_never_takes_workload_below_floor` |
| Concurrent claims (different nodes) share one budget | `test_concurrent_claims_across_nodes_share_one_budget` |
| Waiting pods are returned with a reason | `test_limit_is_honoured_and_rest_reported_as_waiting` |
| Not-ready pods do not consume ready budget | `test_not_ready_pods_do_not_consume_the_ready_budget` |
| Dispatched pods count unavailable even while reporting ready | `test_dispatched_pod_counts_unavailable_even_if_it_reports_ready` |
| Success needs a ready replacement to restore capacity | `test_floor_blocks_claims_until_a_replacement_is_ready`, `test_success_does_not_count_until_replacement_reports_ready` |
| Abort restores actual readiness (not stale state) | `test_aborted_eviction_restores_actual_readiness`, `test_aborted_eviction_restores_ready_count_when_still_ready` |
| Stale reports cannot touch later tasks | `test_stale_report_on_settled_task_changes_nothing`, `test_late_report_cannot_touch_a_newer_task_for_the_same_pod` |
| Removed pods cannot be resurrected | `test_late_readiness_report_cannot_resurrect_a_removed_pod`, `test_late_success_cannot_resurrect_a_removed_pod`, `test_readiness_report_on_evicted_pod_is_ignored` |
| Idempotent reports | `test_duplicate_result_report_is_idempotent`, `test_readiness_report_idempotency`, `test_replay_reports_current_state_not_a_stale_snapshot` |
| Unresolved evictions survive restart | `test_open_evictions_still_reserve_after_restart` |
| Settled results survive restart | `test_result_already_applied_survives_restart`, `test_abort_after_restart_uses_actual_readiness` |
| Inventory survives restart | `test_inventory_survives_restart` |

## 2. Concurrent dispatch, real server (4 workers)

`scripts/verify_concurrency.py` floods the API from 20 threads across 5 nodes
(50 ready pods, floor 4), then settles every eviction and adds a replacement.

```
$ python scripts/verify_concurrency.py --base http://127.0.0.1:8020 \
      --nodes 5 --pods-per-node 10 --floor 4 --workers 20 --restart-cmd ...

workload web: 50 ready pods, floor=4, capacity=46
approved 46 evictions in parallel
restarting server with unresolved evictions ...

all invariants held
```

Checked: exactly `50 − 4 = 46` evictions approved (no overspend, no duplicates
task/pod), `effective_ready == floor` after dispatch, and — after killing and
restarting all 4 workers mid-flight — `reserved == 46`, no new approvals, a
dispatched pod still reporting ready stays unavailable, all 46 pre-restart task
ids settle, and capacity returns only with a ready replacement.

## 3. Sustained concurrency (stress)

`scripts/stress_claims.py` runs 24 threads for 15 s mixing claims, `succeeded`,
`aborted`, and replacement registration, while a sampler polls the invariant.

```
$ python scripts/stress_claims.py --base http://127.0.0.1:8020 --seconds 15

no invariant violations under sustained concurrency
```

Over the run the sampler never saw `effective_ready < floor`, never saw
`reserved > active_total` (which would mean one pod was handed out twice), and
the final state had `reserved == 0` with all 953 tasks settled. Server log:
0 tracebacks, 0 `database is locked`, 0 HTTP 500.

## 4. End-to-end flow (the `/docs` walkthrough)

`scripts/demo_flow.py` registers a workload + pods, claims from a node,
simulates a node reporting readiness, then `succeeded` / `aborted` / a stale
replay. All responses were as expected (approved list + waiting reasons,
effective readiness moving 4 → 2 → 2 → 3, stale replay `applied:false`).

## 6. Container entrypoint

The image starts the service via `python -m app.main` (2 uvicorn workers,
non-root user, `/data` volume). The same entrypoint was run natively with
`DRAIN_WORKERS=2`:

```
$ DRAIN_WORKERS=2 python -m app.main
{"status":"ok","database":"/tmp/entry.db"}
docs=200
2 x "Application startup complete"
```

This exercises the multi-process start path (including concurrent schema
initialisation) that the container uses, even though the image itself was not
built here.

## 7. Static checks

* `pyflakes app tests scripts` — clean.
* `/docs` and `/openapi.json` render (13 paths); waiting-reason enum documented.

## 8. Review notes / deliberate decisions

* Reservations are **derived** from `dispatched` task rows, never a separate
  counter that could drift or reset on restart.
* Claims and every mutation use `BEGIN IMMEDIATE`, which is what serialises
  competing claims; the ready count is recomputed inside that transaction.
* Pod uids are treated as immutable (Kubernetes semantics): a retired uid
  cannot be re-registered, and a pod cannot move nodes. Replacements use new
  uids. This is what lets a late report be ignored safely.
* `report_id` gives idempotent retries for both readiness and eviction reports.

## 9. Not verified here / remaining work

* **`docker compose up` was not executed** (no Docker daemon in the sandbox).
  The Dockerfile/compose file are written and the YAML parses; the image build
  and healthcheck need a run on a Docker host.
* No authentication/authorisation — the API assumes a trusted internal network.
* SQLite is a single-writer design. Fine for a coordinator at this scale; for
  higher write throughput, move to Postgres and replace `BEGIN IMMEDIATE` with
  `SELECT … FOR UPDATE` / advisory locks.
* No background reconciliation: if a node agent dies after claiming, its tasks
  stay `dispatched` (correctly counted as unavailable) until an operator
  reports the result or a future reaper times them out. A timeout-based reaper
  is the obvious next addition.
* No pagination on list endpoints.

## 10. Eviction success vs. `DELETE /pods/{uid}` reconciliation (revision)

Requirement: the node agent's success report and an inspection job's
`DELETE /pods/{uid}` race on the same pod. Verified against a real
`python -m app.main` server (2 uvicorn workers, shared SQLite file) and in the
unit suite. New tests are in `tests/test_reconcile.py`.

### 10.1 Success commits first, delete arrives later

```
after success: evicted 2026-11-22T12:51:54.137Z task succeeded
after DELETE:  evicted 2026-11-22T12:51:54.137Z
workload: {'evicted': 1, 'removed': 0, 'effective_ready': 1}
PASS
```

`state` stays `evicted`, `retired_at` keeps the task settlement time, the task
stays `succeeded`, and the workload's evicted count stays 1 (previously the
delete rewrote the pod to `removed`, moved `retired_at` and dropped the count).

### 10.2 Delete commits first, late success arrives

```
after DELETE: removed | task: aborted pod_removed
late success: applied=False ignored_reason=task_already_resolved
workload: {'evicted': 0, 'removed': 1, 'reserved': 0}
PASS
```

Unchanged pre-existing behaviour: the open task is aborted with
`result_note=pod_removed`, and the late success is acknowledged but inert.

### 10.3 Old-volume upgrade

An API-built evicted pod was mutated into the old bug shape
(`state=removed`, `retired_at=2099-01-01…`), alongside a genuine manual
removal and a normal eviction. Restarting two workers on that file:

```
old-volume state before upgrade: {'c1-0': 'removed', 'c1-1': 'active',
                                   'm-0': 'removed', 'o-0': 'evicted'}
docs: 200
repaired pod: evicted 2026-11-22T12:51:58.347Z expected 2026-11-22T12:51:58.347Z
workloads: {'web': (1, 0), 'manual': (0, 1), 'other': (1, 0)}
state=evicted: ['c1-0', 'o-0']
state=removed: ['m-0']
genuine-removal task: aborted pod_removed
PASS
```

The misclassified pod is `evicted` again with the success task's settlement
time; the genuine removal stays `removed`; task and `report_events` history is
untouched; `/pods/{uid}`, `?state=` and the workload counters all reflect the
repair from the first request.

### 10.4 Batch atomicity, retry and multi-worker start

Covered by pytest:

* `test_repair_rolls_back_all_rows_on_failure` — a failure injected after the
  update statement but before commit leaves **both** affected rows untouched.
* `test_old_volume_repair_is_atomic_and_retryable` — after a failed start the
  data is unchanged; the next `init_db` repairs it.
* `test_multiple_workers_share_old_volume_and_see_repaired_state` — four
  concurrent spawned processes call `init_db` on one old volume behind a
  barrier; each observes `evicted` + the correct `retired_at` + count 1.
* `test_old_volume_repair_is_idempotent` — a second start changes nothing.

### 10.5 Suite

```
$ python -m pytest -q
31 passed
$ python -m pyflakes app tests scripts
(clean)
```

Docker is still not installed in this sandbox, so `docker compose up` was not
executed here; the same entrypoint (`python -m app.main`, 2 workers, shared
`DRAIN_DB_PATH`, `/docs` served) was run natively as above. On the Compose
host the same checks run against the exposed port:

```bash
docker compose up --build -d
python scripts/verify_reconcile.py --base http://127.0.0.1:8000
docker compose stop
python scripts/verify_reconcile.py --seed-legacy --db-path <host path to drain.db>
docker compose start
curl -s localhost:8000/pods/<uid>      # evicted + original retired_at
```

A repaired start logs `restored N misclassified removed pod(s) to evicted`
through uvicorn's logger (visible with `docker compose logs drain-api`).
