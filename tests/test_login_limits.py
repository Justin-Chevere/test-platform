"""The login guard as wired into POST /auth/token. conftest's guard allows 3 failures per
account and 5 per client in a 60-second window."""

ACCOUNT_LIMIT = 3
CLIENT_LIMIT = 5
WINDOW_SECONDS = 60


def _login(client, username, password):
    return client.post("/auth/token", data={"username": username, "password": password})


def test_an_account_is_paused_even_for_the_right_password(anonymous, make_user):
    make_user("alice", password="alice-password-123")
    for _ in range(ACCOUNT_LIMIT):
        assert _login(anonymous, "alice", "wrong-password").status_code == 401

    response = _login(anonymous, "alice", "alice-password-123")

    assert response.status_code == 429
    assert response.headers["Retry-After"] == str(WINDOW_SECONDS)


def test_the_pause_ends_once_the_window_has_passed(anonymous, make_user, clock):
    make_user("alice", password="alice-password-123")
    for _ in range(ACCOUNT_LIMIT):
        _login(anonymous, "alice", "wrong-password")

    clock.advance(WINDOW_SECONDS)

    assert _login(anonymous, "alice", "alice-password-123").status_code == 200


def test_a_successful_login_resets_the_account_count(anonymous, make_user):
    make_user("alice", password="alice-password-123")
    for _ in range(2):
        for _ in range(ACCOUNT_LIMIT - 1):
            _login(anonymous, "alice", "wrong-password")
        assert _login(anonymous, "alice", "alice-password-123").status_code == 200


def test_unknown_usernames_are_paused_like_real_ones(anonymous):
    # Otherwise a 429 would confirm that an account exists.
    for _ in range(ACCOUNT_LIMIT):
        _login(anonymous, "ghost", "wrong-password")

    assert _login(anonymous, "ghost", "wrong-password").status_code == 429


def test_one_client_trying_many_accounts_is_paused(anonymous, make_user):
    make_user("zoe", password="zoe-password-1234")
    for i in range(CLIENT_LIMIT):
        assert _login(anonymous, f"user-{i}", "wrong-password").status_code == 401

    assert _login(anonymous, "zoe", "zoe-password-1234").status_code == 429


def test_logging_into_your_own_account_does_not_reset_the_client_count(anonymous, make_user):
    make_user("mallory", password="mallory-password-1")
    for i in range(CLIENT_LIMIT - 1):
        _login(anonymous, f"victim-{i}", "guess")
    assert _login(anonymous, "mallory", "mallory-password-1").status_code == 200

    _login(anonymous, "victim-x", "guess")

    assert _login(anonymous, "victim-y", "guess").status_code == 429
