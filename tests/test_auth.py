import re
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.main import app
from app.models import Role
from app.security import create_access_token

# Every other route must turn away a caller without a token. Adding a route here is a
# deliberate decision to make it public.
PUBLIC_ROUTES = {
    ("GET", "/health"),
    ("POST", "/auth/token"),
}
NEW_PROJECT = {"name": "demo", "repo_url": "https://github.com/example/demo"}


def _login(client, username, password):
    return client.post("/auth/token", data={"username": username, "password": password})


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _claims(user):
    # Every claim a real token has, so a forgery fails only on what's forged.
    expires = datetime.now(UTC) + timedelta(minutes=5)
    return {"sub": str(user.id), "ver": user.token_version, "exp": expires}


def test_login_returns_a_token_that_identifies_the_user(anonymous, make_user):
    make_user("alice", Role.DEVELOPER, password="alice-password-123")

    response = _login(anonymous, "alice", "alice-password-123")

    assert response.status_code == 200
    body = response.json()
    assert (body["token_type"], body["expires_in"]) == ("bearer", 30 * 60)
    me = anonymous.get("/auth/me", headers=_bearer(body["access_token"])).json()
    assert (me["username"], me["role"]) == ("alice", "developer")
    assert "password_hash" not in me


def test_wrong_password_and_unknown_user_get_the_same_answer(anonymous, make_user):
    make_user("alice", password="alice-password-123")

    wrong_password = _login(anonymous, "alice", "not-the-password")
    unknown_user = _login(anonymous, "mallory", "alice-password-123")

    assert wrong_password.status_code == unknown_user.status_code == 401
    assert wrong_password.json() == unknown_user.json()


def test_a_deactivated_user_cannot_log_in(anonymous, make_user):
    make_user("alice", password="alice-password-123", is_active=False)

    assert _login(anonymous, "alice", "alice-password-123").status_code == 401


def test_every_non_public_route_rejects_anonymous_requests(anonymous):
    # The OpenAPI schema is the public list of every endpoint the app serves, so a new
    # route that forgets its role check fails here.
    checked = []
    for route_path, operations in app.openapi()["paths"].items():
        for method in operations:
            if (method.upper(), route_path) in PUBLIC_ROUTES:
                continue
            path = re.sub(r"\{[^}]+\}", "1", route_path)
            response = anonymous.request(method, path)
            assert response.status_code == 401, f"{method} {route_path} answered anonymously"
            checked.append((method, route_path))
    # Guards against the loop silently finding nothing and passing vacuously.
    assert len(checked) >= 20


@pytest.mark.parametrize(
    "forge",
    [
        lambda user: "not-a-jwt",
        lambda user: create_access_token(user, expires_in=timedelta(seconds=-1)),
        lambda user: jwt.encode(_claims(user), "an-attacker-chosen-secret-long-enough-x"),
        # The classic JWT forgery: declare "alg": "none" and send no signature at all.
        lambda user: jwt.encode(_claims(user), None, algorithm="none"),
    ],
    ids=["garbage", "expired", "wrong-secret", "unsigned"],
)
def test_bad_tokens_are_rejected(anonymous, make_user, forge):
    token = forge(make_user("alice"))

    assert anonymous.get("/auth/me", headers=_bearer(token)).status_code == 401


def test_a_viewer_can_read_but_not_change_anything(viewer_client, make_project):
    project = make_project()

    assert viewer_client.get("/projects").status_code == 200
    assert viewer_client.get(f"/projects/{project.id}/trends/daily").status_code == 200
    assert viewer_client.post("/projects", json=NEW_PROJECT | {"name": "x"}).status_code == 403
    assert viewer_client.post(f"/projects/{project.id}/runs").status_code == 403
    assert viewer_client.get("/users").status_code == 403


def test_a_developer_can_run_tests_but_not_register_projects(developer_client, make_project):
    project = make_project()

    assert developer_client.post(f"/projects/{project.id}/runs").status_code == 202
    assert developer_client.post("/projects", json=NEW_PROJECT | {"name": "x"}).status_code == 403
    assert developer_client.get("/users").status_code == 403


def test_an_admin_can_register_projects_and_manage_users(admin_client):
    assert admin_client.post("/projects", json=NEW_PROJECT).status_code == 201
    assert admin_client.get("/users").status_code == 200


def test_runs_record_who_triggered_them(client_as, make_project):
    project = make_project()
    alice = client_as(Role.DEVELOPER, "alice")

    run = alice.post(f"/projects/{project.id}/runs").json()

    assert run["triggered_by"] == "alice"


def test_deactivation_revokes_tokens_already_issued(admin_client, viewer_client):
    assert viewer_client.get("/projects").status_code == 200
    user_id = viewer_client.get("/auth/me").json()["id"]

    admin_client.patch(f"/users/{user_id}", json={"is_active": False})

    assert viewer_client.get("/projects").status_code == 401


def test_role_changes_apply_to_tokens_already_issued(admin_client, viewer_client, make_project):
    project = make_project()
    assert viewer_client.post(f"/projects/{project.id}/runs").status_code == 403
    user_id = viewer_client.get("/auth/me").json()["id"]

    admin_client.patch(f"/users/{user_id}", json={"role": "developer"})

    assert viewer_client.post(f"/projects/{project.id}/runs").status_code == 202


def test_changing_your_password_ends_every_other_session(anonymous, make_user):
    make_user("alice", password="alice-password-123")
    first = _login(anonymous, "alice", "alice-password-123").json()["access_token"]
    second = _login(anonymous, "alice", "alice-password-123").json()["access_token"]
    change = {"current_password": "alice-password-123", "new_password": "a-brand-new-password"}

    response = anonymous.post("/auth/password", json=change, headers=_bearer(first))

    assert response.status_code == 200
    fresh = response.json()["access_token"]
    assert anonymous.get("/auth/me", headers=_bearer(fresh)).status_code == 200
    for old in (first, second):
        assert anonymous.get("/auth/me", headers=_bearer(old)).status_code == 401
    assert _login(anonymous, "alice", "a-brand-new-password").status_code == 200


def test_changing_your_password_needs_the_current_one(anonymous, make_user):
    make_user("alice", password="alice-password-123")
    token = _login(anonymous, "alice", "alice-password-123").json()["access_token"]
    guess = {"current_password": "a-wrong-guess", "new_password": "a-brand-new-password"}

    assert anonymous.post("/auth/password", json=guess, headers=_bearer(token)).status_code == 403
    assert _login(anonymous, "alice", "alice-password-123").status_code == 200  # unchanged


def test_password_change_guesses_are_limited_like_logins(anonymous, make_user):
    # Otherwise a stolen token could guess the current password here without limit.
    make_user("alice", password="alice-password-123")
    token = _login(anonymous, "alice", "alice-password-123").json()["access_token"]
    guess = {"current_password": "a-wrong-guess", "new_password": "a-brand-new-password"}
    for _ in range(3):  # the test guard's per-account limit
        anonymous.post("/auth/password", json=guess, headers=_bearer(token))

    right = guess | {"current_password": "alice-password-123"}
    response = anonymous.post("/auth/password", json=right, headers=_bearer(token))

    assert response.status_code == 429
