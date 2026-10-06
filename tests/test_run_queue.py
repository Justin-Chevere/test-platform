import threading

from sqlalchemy.orm import sessionmaker

from app.db import Base, make_engine
from app.models import Project, Run, RunStatus
from app.run_queue import claim_next_run


def test_empty_queue(session_factory):
    with session_factory() as db:
        assert claim_next_run(db, "w1") is None


def test_claims_the_oldest_run_and_marks_it_running(session_factory, make_project, queue_run):
    project = make_project()
    oldest = queue_run(project)
    queue_run(project)

    with session_factory() as db:
        run = claim_next_run(db, "w1")

    assert run.id == oldest
    assert run.status == RunStatus.RUNNING
    assert run.worker_id == "w1"
    assert run.started_at is not None


def test_a_claimed_run_is_never_handed_out_again(session_factory, make_project, queue_run):
    only = queue_run(make_project())

    with session_factory() as db:
        assert claim_next_run(db, "w1").id == only
        assert claim_next_run(db, "w2") is None


def test_concurrent_workers_never_share_a_run(tmp_path):
    # A database file this time: the point is several connections racing for the same
    # rows, and an in-memory database can't be shared between connections.
    engine = make_engine(f"sqlite:///{tmp_path / 'queue.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    with factory() as db:
        project = Project(
            name="demo",
            repo_url="https://github.com/example/demo",
            default_branch="main",
            setup_command="",
            test_command="pytest",
            report_path="report.xml",
            timeout_seconds=60,
        )
        db.add(project)
        db.flush()
        runs = [Run(project_id=project.id, ref="main") for _ in range(50)]
        db.add_all(runs)
        db.commit()
        queued_ids = sorted(run.id for run in runs)

    workers = [f"w{n}" for n in range(4)]
    claimed: dict[str, list[int]] = {name: [] for name in workers}
    start_together = threading.Barrier(len(workers))

    def work(name: str) -> None:
        start_together.wait()
        with factory() as db:
            while (run := claim_next_run(db, name)) is not None:
                claimed[name].append(run.id)

    threads = [threading.Thread(target=work, args=(name,)) for name in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    engine.dispose()

    every_claim = sorted(run_id for ids in claimed.values() for run_id in ids)
    assert every_claim == queued_ids  # every run claimed, and none of them twice
