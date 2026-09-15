# MLManage backend test suite

End-to-end and focused contract/security tests for the MLManage backend API. The cluster suites use Python stdlib plus `kubectl`; focused tests import the API and therefore require its Python dependencies.

## Run

```bash
cd tests_suite
./run.sh
```

The single `run.sh` entry point:

1. **Waits** for required core deployments (`postgres`, `devops-api`, `devops-controller`) and for `vram-webhook` only when it exists (`MLM_WAIT_TIMEOUT`, default 1200s) — so it can run right after `deploy-all.sh`.
2. Ensures the API is reachable (auto-starts a `kubectl port-forward` if needed).
3. Builds + uploads a small custom test image (`mybench`) if not already present (needs `docker`; skip with `--no-image`).
4. Runs all suites in order and prints an aggregated pass/fail summary.
5. Cleans up the port-forward and temp files. Exit code is non-zero if any test failed.

It is also invoked automatically at the end of `devops-backend/scripts/deploy-all.sh` (set `RUN_TESTS=0` to skip).

## Suites

Focused tests import the API module with isolated temporary SQLite databases. Create a dedicated environment from the pinned dependency set, then run the entry point:

```bash
python3 -m venv .venv-focused
.venv-focused/bin/python -m pip install -r requirements-focused.txt
PYTHON="$PWD/.venv-focused/bin/python" ./run_focused.sh
```

If the deployed API already has a virtual environment, it can be reused instead:

```bash
PYTHON=/path/to/backend/.venv/bin/python ./run_focused.sh
```

They cover password hashing, safe login audit logging, rejection of plaintext database values, local-dev task authorization, valid synthetic result archives/deletion, image-scope authorization, scheduled-job field persistence/dispatch, real-versus-simulated GPU inventory, and deployment mode/image configuration.

The cluster end-to-end runner covers:

| Module | Covers |
|--------|--------|
| `test_01_auth_users.py` | JSON-body login, bad credentials, token validation, user CRUD, RBAC, profile update, health, gpu list |
| `test_02_reservations.py` | create/conflict, renewal, **cancellation edge cases**, ownership (403), priority preemption, calendar, iCal |
| `test_03_tasks_results_queue.py` | tasks + RBAC, VRAM injection, **results download after pod deletion**, queue (priority/deps/dispatch), analytics, disk, teams, images, cleanup settings |
| `test_04_isolation.py` | per-user namespace isolation, cross-user 404, input validation edge cases |
| `test_05_admin_resources.py` | group/project/user mutation (PUT/DELETE), GPU availability, activity feed |
| `test_06_audit_log.py` | audit log: actions and sign-in attempts recorded, admin-only access, password redaction, filters and paging |

## Showcase (`showcase.sh`)

After the suite passes, run `./showcase.sh` for a guided, narrated tour of the backend
capabilities covered by the end-to-end suite. It creates sample users/groups/projects, runs tasks, reserves GPUs,
partitions GPUs, schedules queue jobs, downloads results, etc. — printing each `curl` call and its
JSON response via `jq`. It cleans up its demo objects at the end (set `KEEP=1` to keep them,
`PAUSE=1` to step through interactively).

```bash
cd tests_suite && ./showcase.sh
```

## Configuration (env vars)

| Variable | Default | Purpose |
|----------|---------|---------|
| `MLM_BASE` | `http://localhost:8000` | API base URL (set this to skip port-forward) |
| `KUBECTL` | repo `./kubectl` or PATH | kubectl binary |
| `MLM_ADMIN_USER` / `MLM_ADMIN_PASS` | `admin` / `admin` | Bootstrap admin credentials |
| `MLM_TEST_IMAGE` | `localhost:5000/<admin>/mybench:latest` | Image used by task tests |
| `MLM_WAIT_TIMEOUT` | `1200` | Seconds to wait for pods to be Ready |

## Notes

- Tests create temporary users/reservations/tasks with unique suffixes and clean up where practical; leftover terminal tasks are removed by the backend's own cleanup reaper.
- Designed to be idempotent: safe to run repeatedly against the same cluster.
