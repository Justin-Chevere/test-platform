import logging

from app.throttle import FailureWindow, LoginGuard


def test_key_is_blocked_once_it_reaches_the_limit(clock):
    window = FailureWindow(limit=3, window_seconds=60, clock=clock)

    assert [window.add("alice") for _ in range(3)] == [False, False, True]
    assert window.retry_after("alice") == 60
    assert window.retry_after("bob") == 0


def test_failures_age_out_of_the_window(clock):
    window = FailureWindow(limit=3, window_seconds=60, clock=clock)
    window.add("alice")
    clock.advance(30)
    window.add("alice")
    window.add("alice")

    # Blocked until the first failure is 60 seconds old.
    assert window.retry_after("alice") == 30
    clock.advance(30)
    assert window.retry_after("alice") == 0


def test_clear_forgets_a_key(clock):
    window = FailureWindow(limit=1, window_seconds=60, clock=clock)
    window.add("alice")
    window.clear("alice")

    assert window.retry_after("alice") == 0


def test_stale_keys_are_swept_so_memory_stays_bounded(clock):
    window = FailureWindow(limit=5, window_seconds=60, clock=clock)
    for i in range(1000):
        window.add(f"made-up-{i}")
    assert len(window) == 1000

    clock.advance(61)
    window.add("alice")

    assert len(window) == 1


def test_guard_blocks_an_account_from_any_client(clock):
    guard = LoginGuard(per_account=2, per_client=100, window_seconds=60, clock=clock)
    guard.record_failure("alice", "10.0.0.1")
    guard.record_failure("alice", "10.0.0.2")

    assert guard.retry_after("alice", "10.0.0.3") > 0
    assert guard.retry_after("bob", "10.0.0.1") == 0


def test_guard_blocks_a_client_trying_many_accounts(clock):
    guard = LoginGuard(per_account=100, per_client=3, window_seconds=60, clock=clock)
    for name in ("bob", "carol", "dave"):
        guard.record_failure(name, "10.0.0.9")

    assert guard.retry_after("erin", "10.0.0.9") > 0
    assert guard.retry_after("erin", "10.0.0.1") == 0


def test_blocking_is_logged_once_and_usernames_cannot_forge_log_lines(clock, caplog):
    guard = LoginGuard(per_account=2, per_client=100, window_seconds=60, clock=clock)
    with caplog.at_level(logging.WARNING, logger="app.throttle"):
        for _ in range(3):
            guard.record_failure("eve\nINFO: everything is fine", "10.0.0.1")

    assert len(caplog.records) == 1
    assert "\n" not in caplog.records[0].getMessage()
