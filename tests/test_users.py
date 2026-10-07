import pytest

from app.models import Role

NEW_USER = {"username": "bob", "password": "bob-password-1234", "role": "developer"}


def test_create_and_list_users(admin_client):
    response = admin_client.post("/users", json=NEW_USER)

    assert response.status_code == 201
    created = response.json()
    assert (created["username"], created["role"], created["is_active"]) == (
        "bob",
        "developer",
        True,
    )
    assert "password" not in created and "password_hash" not in created
    usernames = [user["username"] for user in admin_client.get("/users").json()]
    assert usernames == ["admin", "bob"]


def test_usernames_are_unique(admin_client):
    admin_client.post("/users", json=NEW_USER)

    assert admin_client.post("/users", json=NEW_USER).status_code == 409


@pytest.mark.parametrize(
    "change",
    [
        {"password": "too-short"},  # under 12 characters
        {"username": "Bob!"},
        {"role": "superuser"},
    ],
)
def test_new_users_are_validated(admin_client, change):
    assert admin_client.post("/users", json=NEW_USER | change).status_code == 422


def test_change_a_role_and_deactivate(admin_client):
    bob = admin_client.post("/users", json=NEW_USER).json()

    promoted = admin_client.patch(f"/users/{bob['id']}", json={"role": "admin"}).json()
    deactivated = admin_client.patch(f"/users/{bob['id']}", json={"is_active": False}).json()

    assert promoted["role"] == "admin"
    assert deactivated["is_active"] is False


def test_the_last_active_admin_cannot_be_removed(admin_client, make_user):
    me = admin_client.get("/auth/me").json()

    for change in ({"role": "developer"}, {"is_active": False}):
        response = admin_client.patch(f"/users/{me['id']}", json=change)
        assert response.status_code == 409

    # With a second admin around, either change is fine.
    make_user("second-admin", Role.ADMIN)
    assert admin_client.patch(f"/users/{me['id']}", json={"role": "developer"}).status_code == 200


def test_unknown_user(admin_client):
    assert admin_client.patch("/users/999", json={"role": "viewer"}).status_code == 404
