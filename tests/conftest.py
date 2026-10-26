import pytest


@pytest.fixture(autouse=True)
def open_rooms(monkeypatch):
    # New rooms need an account by default. Most tests are about what happens in a room,
    # so they let anyone start one; the tests about accounts switch it back.
    monkeypatch.setenv("KARAQUEUE_NEW_ROOMS", "anyone")
