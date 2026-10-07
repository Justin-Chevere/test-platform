# test-platform

[![CI](https://github.com/Justin-Chevere/test-platform/actions/workflows/ci.yml/badge.svg)](https://github.com/Justin-Chevere/test-platform/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A self-hosted test runner, like a small CI service: register a Git repository, trigger a run, and
a worker checks out that exact commit, runs its tests in a fresh environment, and records the
result of every single test.

For example, it runs the full test suite of
[cloud-resource-manager](https://github.com/Justin-Chevere/cloud-resource-manager): it clones the
repository, installs it into a new virtualenv, runs 92 tests and stores each result, in about 30
seconds.

## Status

- [x] REST API: register projects, trigger runs, read results, failures and logs
- [x] Run queue stored in the database, safe with any number of workers
- [x] Crash recovery: a dead worker's run goes back to the queue (heartbeats and leases), and a
      worker that was presumed dead can't overwrite its replacement (fencing)
- [x] Flaky test detection: tests that both passed and failed on the same commit, with the evidence,
      found by automatically rerunning failed runs
- [x] Schema migrations (Alembic), checked against the models by a test
- [x] Local runner: a fresh checkout and virtualenv for every run, a time limit, and no process
      left running afterwards
- [x] JUnit XML reports, so any test tool that writes them works (pytest, Jest, Go, Maven...)
- [x] CI: lint and tests on Linux (Python 3.12 and 3.14) and Windows

## Next steps

1. **Quarantine:** let a run pass when its only failures are tests already known to be flaky,
   while still reporting them.
2. **Trends:** slowest tests, failure rates, and how both change over time.
3. **Docker runner:** run each job in a throwaway container, so untrusted code can't touch the
   machine.
4. **GitHub integration:** start runs from push webhooks, and report pass or fail on each commit.
5. **Logins**, then a web dashboard.

## How it works

```
 POST /projects/{id}/runs                     worker 1 ─┐
          │                                   worker 2 ─┼─ each claims the oldest queued run
          ▼                                   worker N ─┘
  runs table (status: queued) ───────────────────▶ runner
                                                    1. fetch exactly that commit
                                                    2. create a fresh virtualenv
                                                    3. run the setup command, then the tests
                                                    4. read the JUnit XML report
                                                         │
  runs + test_results tables ◀────────────────────────────┘
  (status, counts, log, one row per test)
```

The API never runs tests. It records a run as `queued` and answers `202 Accepted` at once. Workers
are separate processes that do the slow part, so they can be scaled and restarted on their own.

### The queue is a table

A queued run is just a row with status `queued`, so there's no Redis or RabbitMQ to operate, and a
run can't be saved but not queued: saving it *is* queuing it. Rails 8's default job queue, Solid
Queue, keeps jobs in the database the same way.

A worker claims a run with **one** `UPDATE` that picks the oldest queued run and marks it `running`,
and only matches a row that's still queued. The database applies writes one at a time, so two
workers can never claim the same run. A test starts four workers racing over 50 runs and checks
each run is claimed exactly once. Swapping in the classic bug (`SELECT` the oldest run, *then*
`UPDATE` it) failed that test in 5 out of 5 tries.

On SQLite, WAL mode lets the API keep reading while a worker writes. On Postgres, the same query
would use `FOR UPDATE SKIP LOCKED`, so workers skip rows another worker has locked instead of
waiting for them.

### Crashed workers

A claim is a lease, not ownership forever:

- **Heartbeats:** while a worker executes a run, a background thread stamps the run's
  `heartbeat_at` every 10 seconds. The worker holds no database transaction open during the run
  itself, only short ones to claim it and to save the result.
- **Leases:** a `running` run with no heartbeat for 60 seconds is presumed abandoned: its worker
  crashed, was killed, or lost its machine. Every worker checks for such runs before claiming
  one, so recovery needs no extra process: as long as one worker is alive, nothing stays stuck.
  The run goes back to the queue, and its `attempt` number shows it's a retry.
- **Attempt limit:** a run is claimed at most 3 times. After that it's marked `error`, so a test
  that kills its worker every time can't go around forever.
- **Fencing:** a worker that only *looked* dead (a frozen process, a laptop that went to sleep)
  can come back after its run was handed to another worker. Each of its writes only applies if the
  run is still on its attempt number, a *fencing token*, so it can't overwrite its replacement's
  heartbeats or results; its own result is thrown away instead.

Tried for real: two workers, with one running cloud-resource-manager's test suite until it was
killed without warning (`TerminateProcess`, the Windows equivalent of `kill -9`). With a 5-second
lease, the other worker took the run over 4.8 seconds after the kill and finished it: 92 tests
passed, on attempt 2.

Known limits:

- A killed worker can't clean up after itself: the command it was running keeps going until it
  ends by itself, with no time limit enforced anymore, and its temporary folder stays behind. A
  container runner (next steps) fixes both, since a whole container can be removed at once.
- Each worker timestamps heartbeats with its own clock, so workers on different machines need
  synchronized clocks (NTP), which keeps them far closer than the 60-second lease.

### What a run's status means

| Status | Meaning |
|--------|---------|
| `queued` | Waiting for a worker, or for another one if its worker died |
| `running` | A worker has it, and keeps sending heartbeats |
| `passed` | The test command exited with 0 **and** the report shows no failed or errored test |
| `failed` | The tests ran: at least one failed, or the test command exited non-zero |
| `error` | The tests couldn't run or report: unknown branch, failed setup, time limit, no report, or every worker that tried it died |

Both signals have to agree for `passed`: a report can be all green while the command fails on
something else (a coverage threshold, a crash after the last test), and the reverse.

### The runner

Each run gets its own temporary folder:

- **Exact commit:** `git fetch --depth=1` downloads only the requested branch, tag or commit, not
  the whole history. The commit SHA that was tested is recorded, even when a branch was asked for.
- **Fresh virtualenv:** no packages carry over between runs, or from the worker's own Python.
- **Time limit:** the setup and test commands share the project's time limit (10 minutes by
  default). When it runs out, the whole *process tree* is killed: `taskkill /T` on Windows, a
  process group signal on Linux and macOS. Killing just the shell would leave the tests running.
- **Never waits for a person:** input is closed, and git is told never to prompt for a login, so a
  private repository fails fast instead of hanging a worker.
- **No stale results:** a report file committed to the repository is deleted before the tests run,
  so it can't pass for this run's results.
- **Cleaned up:** the folder is deleted afterwards, including the read-only files git creates,
  which Windows refuses to delete as they are.

### Reading reports

Reports are JUnit XML, the format nearly every test tool can write and Jenkins and GitLab read.
The report is written by the code under test, so it's parsed with `defusedxml`, which refuses XML
tricks like the "billion laughs" entity bomb (a few hundred bytes that expand to gigabytes).

### Flaky tests

A test is flaky when it both passed and failed **on the same commit**: same code, different
result, so the test itself is unreliable (timing, randomness, test order, state left behind by an
earlier run). Failing on one commit and passing on the next doesn't count: that's a fix.

- **Reruns gather the evidence.** When a run fails, the worker queues the same commit again (once
  by default; each project chooses 0 to 3), in the same transaction that saves the failure.
  `POST /runs/{id}/rerun` does the same on request. A rerun tests the exact commit, not the
  branch, which may have moved on since.
- **One query finds them.** It groups every test result by test and commit, and keeps the groups
  with at least one pass and at least one failure or error (a skip counts as neither). The
  database does the filtering, so only flaky pairs come back. The answer is computed when asked
  rather than stored, so it's never out of date; at a much larger scale, per-commit counts would be
  kept up to date as results arrive instead.
- **Where it shows:** `GET /projects/{id}/flaky-tests` lists each flaky test with how many commits
  it flaked on and the latest evidence: the commit, a run that failed it and a run that passed it.
  In `GET /runs/{id}/tests`, a test that has flaked before carries `"flaky": true`, so its failure
  reads as "probably the usual flake" rather than "the code is broken".

Tried for real, with a test that depends on a file left behind by an earlier run: the first run
failed, the worker queued that commit again, the rerun passed, and the test was reported as flaky
with both runs as evidence.

## Security: trusted repositories only, for now

The local runner keeps runs apart from **each other**, not from **your machine**: the tests run
with the worker's own permissions and could read or delete your files. That's fine for your own
repositories, and it's the same trade-off as GitHub's self-hosted runners, which GitHub recommends
only for private repositories. The Docker runner (next steps) is what makes untrusted code safe.

Meanwhile:

- **Only `https://` repositories** can be registered: no `file://` paths into this machine, and no
  URL or branch name that git could mistake for a command-line option.
- **The API only answers to `localhost`.** It can make this machine run commands and has no logins
  yet, so requests addressed to any other host name get `400`. That blocks DNS rebinding, where a
  web page points its own domain at `127.0.0.1` to reach local services through your browser.
- **Report paths** must stay inside the repository.

## Run it

Requires Python 3.12+ and Git.

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -e ".[dev]"
alembic upgrade head             # create the database, or bring it up to date

uvicorn app.main:app --reload    # terminal 1: the API
python -m app.worker             # terminal 2: a worker (start more for parallel runs)
```

After pulling changes, run `alembic upgrade head` again. The API and workers refuse to start on an
outdated schema, and say which command fixes it.

Then open http://127.0.0.1:8000/docs and try:

1. `POST /projects`:
   ```json
   {
     "name": "cloud-resource-manager",
     "repo_url": "https://github.com/Justin-Chevere/cloud-resource-manager",
     "setup_command": "python -m pip install -e .[dev]"
   }
   ```
   The defaults run `python -m pytest --junitxml=test-report.xml` on the `main` branch.
2. `POST /projects/1/runs` queues a run. Send `{"ref": "<branch, tag or commit>"}` to test
   something other than the default branch.
3. `GET /runs/1` shows the status and counts, `GET /runs/1/tests?outcome=failed` lists the
   failures, and `GET /runs/1/log` returns every command and its output.
4. `POST /runs/1/rerun` tests the same commit again, and `GET /projects/1/flaky-tests` lists any
   test that has both passed and failed on one commit.

`python -m app.worker --once` runs everything queued and exits, which is handy for scripts.

Settings come from environment variables or a `.env` file (see `.env.example`).

## Changing the schema

Every schema change is an Alembic migration in `migrations/versions/`:

1. Change the models in `app/models.py`.
2. `alembic revision --autogenerate -m "what changed"` writes a migration from the difference,
   formatted and linted. Read it before trusting it.
3. `alembic upgrade head` applies it.

Migrations are their own step, never a side effect of starting the API or a worker: processes
starting at the same moment would race to migrate the same database. Instead, each one checks the
schema on startup. A test builds a database from the migrations alone and fails if it differs from
the models in any way, so a model changed without a migration can't slip through.

## Test

```bash
pytest
ruff check .
```

The runner tests use real git repositories, virtualenvs and processes, including a test that
starts a process tree, lets it hit the time limit, and checks that no child process survived. The
worker tests let a run outlast its lease with heartbeats and without, and replace a worker in the
middle of a run to check its late result is thrown away. The migration tests build a database from
the migrations alone, compare it with the models, and undo and redo every migration.

## Layout

| Path | Responsibility |
|------|----------------|
| `app/config.py` | Settings from environment variables |
| `app/db.py` | Engine (SQLite in WAL mode), session factory, per-request session dependency |
| `app/models.py` | Database tables: projects, runs, test results |
| `app/schemas.py` | Request and response shapes, validation |
| `app/routers/` | HTTP endpoints: health, projects, runs |
| `app/run_queue.py` | Claims runs atomically, renews leases, recovers abandoned runs |
| `app/worker.py` | Worker loop: recover, claim, execute with heartbeats, save the outcome if still held, rerun failures |
| `app/flaky.py` | Finds flaky tests: passed and failed on the same commit |
| `app/migrate.py` | Applies migrations from code; the startup schema check |
| `migrations/` | Alembic migrations, one file per schema change |
| `app/runner/base.py` | The `Runner` interface and the data passed in and out of it |
| `app/runner/local.py` | Runs a job on this machine: checkout, virtualenv, commands, cleanup |
| `app/runner/junit.py` | Reads JUnit XML reports |
| `app/main.py` | Application factory |
| `tests/` | API, queue, worker, flaky and migration tests on a fresh database file each; runner tests end to end |
