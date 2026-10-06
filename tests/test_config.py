import pytest
from pydantic import ValidationError

from app.config import Settings


def test_the_defaults_allow_several_heartbeats_per_lease():
    settings = Settings()

    assert settings.lease_seconds >= 3 * settings.heartbeat_seconds


def test_a_lease_shorter_than_three_heartbeats_is_refused():
    # Two heartbeats per lease: a single late one would make a live worker look dead.
    with pytest.raises(ValidationError, match="at least 3 times"):
        Settings(heartbeat_seconds=30, lease_seconds=60)
