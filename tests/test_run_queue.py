import threading
from datetime import UTC, datetime, timedelta

from app.models import Run, RunStatus
from app.run_queue import Claim, claim_next_run, recover_abandoned_runs, send_heartbeat

LEASE_SECONDS = 60


def _get(session_factory, run_id) -> Run:
    with session_factory() as db:
        return db.get_one(Run, run_id)


def _go_silent(session_factory, run_id, seconds=LEASE_SECONDS + 1):
    """Act as if the run's worker sent its last heartbeat `seconds` ago."""
    with session_factory() as db:
        db.get_one(Run, run_id).heartbeat_at = datetime.now(UTC) - timedelta(seconds=seconds)
        db.commit()


def _recover(session_factory, max_attempts=3) -> int:
    with session_factory() as db:
        return recover_abandoned_runs(
            db, lease_seconds=LEASE_SECONDS, max_attempts=max_attempts
        )


def _claim(session_factory, worker_id) -> Claim | None:
    with session_factory() as db:
        return claim_next_run(db, worker_id)


def test_empty_queue(session_factory):
    assert _claim(session_factory, "w1") is None


def test_claims_the_oldest_run_and_starts_its_lease(session_factory, make_project, queue_run):
    project = make_project()
    oldest = queue_run(project)
    queue_run(project)

    claim = _claim(session_factory, "w1")

    assert claim == Claim(run_id=oldest, attempt=1)
    run = _get(session_factory, oldest)
    assert run.status == RunStatus.RUNNING
    assert run.worker_id == "w1"
    assert run.started_at is not None
    assert run.heartbeat_at == run.started_at


def test_a_claimed_run_is_never_handed_out_again(session_factory, make_project, queue_run):
    only = queue_run(make_project())

    assert _claim(session_factory, "w1").run_id == only
    assert _claim(session_factory, "w2") is None


def test_concurrent_workers_never_share_a_run(session_factory, make_project, queue_run):
    project = make_project()
    queued_ids = [queue_run(project) for _ in range(50)]
    workers = [f"w{n}" for n in range(4)]
    claimed: dict[str, list[int]] = {name: [] for name in workers}
    start_together = threading.Barrier(len(workers))

    def work(name: str) -> None:
        start_together.wait()
        while (claim := _claim(session_factory, name)) is not None:
            claimed[name].append(claim.run_id)

    threads = [threading.Thread(target=work, args=(name,)) for name in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    every_claim = sorted(run_id for ids in claimed.values() for run_id in ids)
    assert every_claim == queued_ids  # every run claimed, and none of them twice


def test_a_heartbeat_renews_the_lease(session_factory, make_project, queue_run):
    run_id = queue_run(make_project())
    claim = _claim(session_factory, "w1")
    _go_silent(session_factory, run_id, seconds=30)

    with session_factory() as db:
        assert send_heartbeat(db, claim) is True

    assert _get(session_factory, run_id).heartbeat_at > datetime.now(UTC) - timedelta(seconds=5)


def test_a_run_with_a_recent_heartbeat_is_left_alone(session_factory, make_project, queue_run):
    run_id = queue_run(make_project())
    _claim(session_factory, "w1")

    assert _recover(session_factory) == 0
    assert _get(session_factory, run_id).worker_id == "w1"


def test_an_abandoned_run_goes_back_to_the_queue(session_factory, make_project, queue_run):
    run_id = queue_run(make_project())
    _claim(session_factory, "w1")
    _go_silent(session_factory, run_id)

    assert _recover(session_factory) == 1
    run = _get(session_factory, run_id)
    assert run.status == RunStatus.QUEUED
    assert (run.worker_id, run.started_at, run.heartbeat_at) == (None, None, None)
    # The next claim is the run's second attempt.
    assert _claim(session_factory, "w2") == Claim(run_id=run_id, attempt=2)


def test_a_returning_worker_cannot_renew_a_run_it_lost(session_factory, make_project, queue_run):
    run_id = queue_run(make_project())
    first = _claim(session_factory, "w1")
    _go_silent(session_factory, run_id)
    _recover(session_factory)
    second = _claim(session_factory, "w2")

    with session_factory() as db:
        assert send_heartbeat(db, first) is False  # w1 was presumed dead, and is too late
        assert send_heartbeat(db, second) is True


def test_a_run_abandoned_on_every_attempt_is_failed(session_factory, make_project, queue_run):
    run_id = queue_run(make_project())
    for worker in ("w1", "w2"):
        _claim(session_factory, worker)
        _go_silent(session_factory, run_id)
        _recover(session_factory, max_attempts=2)

    run = _get(session_factory, run_id)
    assert run.status == RunStatus.ERROR
    assert run.error == "gave up after 2 attempts: every worker that ran it stopped responding"
    assert run.finished_at is not None
    assert _claim(session_factory, "w3") is None
