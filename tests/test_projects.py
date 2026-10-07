from datetime import UTC, datetime

import pytest

from app.models import Run, RunStatus, TestResult
from app.runner import Outcome

NEW_PROJECT = {"name": "demo", "repo_url": "https://github.com/example/demo"}


def test_create_project_fills_in_defaults(client):
    response = client.post("/projects", json=NEW_PROJECT)

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "demo"
    assert body["default_branch"] == "main"
    assert body["test_command"] == "python -m pytest --junitxml=test-report.xml"
    assert body["report_path"] == "test-report.xml"
    assert body["timeout_seconds"] == 600
    assert body["auto_reruns"] == 1
    assert body["created_at"].endswith("Z")  # UTC, never ambiguous local time


def test_project_names_are_unique(client):
    client.post("/projects", json=NEW_PROJECT)

    response = client.post("/projects", json=NEW_PROJECT)

    assert response.status_code == 409


@pytest.mark.parametrize(
    "repo_url",
    [
        "http://github.com/example/demo",  # unencrypted
        "file:///C:/Users/someone/secrets",  # a path on this machine
        "git@github.com:example/demo.git",  # SSH needs keys the worker doesn't have
        "--upload-pack=touch /tmp/pwned",  # an attempt to smuggle in a git option
    ],
)
def test_only_https_repositories_are_accepted(client, repo_url):
    response = client.post("/projects", json=NEW_PROJECT | {"repo_url": repo_url})

    assert response.status_code == 422


@pytest.mark.parametrize("report_path", ["../outside.xml", "/etc/passwd", "C:\\report.xml"])
def test_report_path_must_stay_inside_the_repository(client, report_path):
    response = client.post("/projects", json=NEW_PROJECT | {"report_path": report_path})

    assert response.status_code == 422


@pytest.mark.parametrize("auto_reruns", [-1, 4])
def test_auto_reruns_stay_between_zero_and_three(client, auto_reruns):
    response = client.post("/projects", json=NEW_PROJECT | {"auto_reruns": auto_reruns})

    assert response.status_code == 422


def test_a_branch_name_cannot_look_like_an_option(client):
    response = client.post("/projects", json=NEW_PROJECT | {"default_branch": "--force"})

    assert response.status_code == 422


def test_list_and_get_projects(client):
    created = client.post("/projects", json=NEW_PROJECT).json()

    assert [p["id"] for p in client.get("/projects").json()] == [created["id"]]
    assert client.get(f"/projects/{created['id']}").json() == created
    assert client.get("/projects/999").status_code == 404


def test_trigger_run_queues_the_default_branch(client):
    project = client.post("/projects", json=NEW_PROJECT | {"default_branch": "develop"}).json()

    response = client.post(f"/projects/{project['id']}/runs")

    assert response.status_code == 202
    run = response.json()
    assert run["status"] == "queued"
    assert run["ref"] == "develop"
    assert run["attempt"] == 0  # no worker has claimed it yet
    assert run["started_at"] is None
    assert run["heartbeat_at"] is None
    assert run["duration_seconds"] is None


def test_trigger_run_for_a_given_ref(client):
    project = client.post("/projects", json=NEW_PROJECT).json()

    response = client.post(f"/projects/{project['id']}/runs", json={"ref": "feature/login"})

    assert response.json()["ref"] == "feature/login"


def test_trigger_run_rejects_an_option_like_ref(client):
    project = client.post("/projects", json=NEW_PROJECT).json()

    response = client.post(f"/projects/{project['id']}/runs", json={"ref": "-delete"})

    assert response.status_code == 422


def test_trigger_run_for_a_missing_project(client):
    assert client.post("/projects/999/runs").status_code == 404


def test_list_runs_newest_first(client):
    project = client.post("/projects", json=NEW_PROJECT).json()
    first = client.post(f"/projects/{project['id']}/runs").json()
    second = client.post(f"/projects/{project['id']}/runs").json()

    runs = client.get(f"/projects/{project['id']}/runs").json()

    assert [r["id"] for r in runs] == [second["id"], first["id"]]
    assert client.get(f"/projects/{project['id']}/runs?limit=1").json()[0]["id"] == second["id"]


def test_list_flaky_tests(client, make_project, session_factory):
    project = make_project()
    with session_factory() as db:
        for outcome in (Outcome.FAILED, Outcome.PASSED):
            run = Run(
                project_id=project.id,
                ref="main",
                commit_sha="a" * 40,
                status=RunStatus.PASSED,
                finished_at=datetime(2026, 10, 6, 12, 0, tzinfo=UTC),
            )
            db.add(run)
            db.flush()
            db.add(
                TestResult(
                    run_id=run.id,
                    classname="tests.test_api",
                    name="test_timing",
                    outcome=outcome,
                    duration_seconds=0.1,
                )
            )
        db.commit()

    response = client.get(f"/projects/{project.id}/flaky-tests")

    assert response.status_code == 200
    assert response.json() == [
        {
            "classname": "tests.test_api",
            "name": "test_timing",
            "flaky_commits": 1,
            "last_flaked_at": "2026-10-06T12:00:00Z",
            "latest": {"commit_sha": "a" * 40, "failed_run_id": 1, "passed_run_id": 2},
        }
    ]
    assert client.get("/projects/999/flaky-tests").status_code == 404
