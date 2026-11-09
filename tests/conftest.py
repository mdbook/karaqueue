import pytest


@pytest.fixture(autouse=True)
def open_rooms(monkeypatch):
    # New rooms need an account by default. Most tests are about what happens in a room,
    # so they let anyone start one; the tests about accounts switch it back.
    monkeypatch.setenv("KARAQUEUE_NEW_ROOMS", "anyone")


@pytest.fixture(autouse=True)
def close_downloads(monkeypatch):
    # Every Downloads starts worker threads. Tests build them directly or through the app and
    # mostly never close them, so close whatever a test made once it is over.
    from karaqueue import downloads

    made = []
    init = downloads.Downloads.__init__

    def tracked(self, *args, **kwargs):
        init(self, *args, **kwargs)
        made.append(self)
    monkeypatch.setattr(downloads.Downloads, "__init__", tracked)
    yield
    for item in made:
        item.close()
