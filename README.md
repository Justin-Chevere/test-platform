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
- [x] Local runner: a fresh checkout and virtualenv for every run, a time limit, and no process
      left running afterwards
- [x] JUnit XML reports, so any test tool that writes them works (pytest, Jest, Go, Maven...)
- [x] CI: lint and tests on Linux (Python 3.12 and 3.14) and Windows

## Next steps

1. **Crashed workers:** heartbeats, so a run whose worker died is retried instead of staying
   `running` forever.
2. **Flaky test detection:** flag tests that both pass and fail on the same commit.
3. **Trends:** slowest tests, failure rates, and how both change over time.
4. **Docker runner:** run each job in a throwaway container, so untrusted code can't touch the
   machine.
5. **GitHub integration:** start runs from push webhooks, and report pass or fail on each commit.
6. **Logins**, then a web dashboard.

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

### What a run's status means

| Status | Meaning |
|--------|---------|
| `queued` | Waiting for a worker |
| `running` | A worker has it |
| `passed` | The test command exited with 0 **and** the report shows no failed or errored test |
| `failed` | The tests ran: at least one failed, or the test command exited non-zero |
| `error` | The tests couldn't run or report: unknown branch, failed setup, time limit, no report |

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

uvicorn app.main:app --reload    # terminal 1: the API
python -m app.worker             # terminal 2: a worker (start more for parallel runs)
```

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

`python -m app.worker --once` runs everything queued and exits, which is handy for scripts.

Settings come from environment variables or a `.env` file (see `.env.example`).

## Test

```bash
pytest
ruff check .
```

The runner tests use real git repositories, virtualenvs and processes, including a test that
starts a process tree, lets it hit the time limit, and checks that no child process survived.

## Layout

| Path | Responsibility |
|------|----------------|
| `app/config.py` | Settings from environment variables |
| `app/db.py` | Engine (SQLite in WAL mode), session factory, per-request session dependency |
| `app/models.py` | Database tables: projects, runs, test results |
| `app/schemas.py` | Request and response shapes, validation |
| `app/routers/` | HTTP endpoints: health, projects, runs |
| `app/run_queue.py` | Claims the next queued run, atomically |
| `app/worker.py` | Worker loop: claim a run, execute it, save the outcome |
| `app/runner/base.py` | The `Runner` interface and the data passed in and out of it |
| `app/runner/local.py` | Runs a job on this machine: checkout, virtualenv, commands, cleanup |
| `app/runner/junit.py` | Reads JUnit XML reports |
| `app/main.py` | Application factory |
| `tests/` | API, queue and worker tests on an in-memory database; runner tests end to end |
