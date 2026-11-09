import queue
import threading
import time

import pytest
from fastapi.testclient import TestClient

from karaqueue import app as app_module
from karaqueue import tunnel as tunnel_module
from karaqueue.app import create_app
from karaqueue.store import Store
from karaqueue.tunnel import PROVIDERS, Tunnel, funnel_ready, local_address, named_ready, quick_ready


class FakeProcess:
    """A tunnel program that prints what the test feeds it, and stops when told to or on None."""

    def __init__(self, args, **kwargs):
        self.args, self.env = args, kwargs.get("env", {})
        self.lines = queue.Queue()
        self.code = None
        self.stdout = iter(self.lines.get, None)

    def say(self, line):
        self.lines.put(line + "\n")

    def terminate(self):
        self.code = -15
        self.lines.put(None)

    kill = terminate

    def wait(self, timeout=None):
        return self.code if self.code is not None else 0


class Programs:
    def __init__(self):
        self.started = []
        self.ready = threading.Condition()

    def __call__(self, args, **kwargs):
        proc = FakeProcess(args, **kwargs)
        with self.ready:
            self.started.append(proc)
            self.ready.notify_all()
        return proc

    def nth(self, n, timeout=5):
        with self.ready:
            assert self.ready.wait_for(lambda: len(self.started) >= n, timeout), "the program didn't start"
            return self.started[n - 1]


def wait_for(check, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return
        time.sleep(0.01)
    raise AssertionError("timed out")


@pytest.fixture
def make():
    made = []

    def build(**settings):
        values = {"tunnel": "cloudflare", "tunnel_token": "", "tunnel_hostname": "", "cloudflared": "cloudflared",
                  "tailscale": "tailscale", **settings}
        tun = Tunnel(values.get, local="http://127.0.0.1:8000")
        tun.values = values
        tun.programs = tun.popen = Programs()
        tun.which = lambda name: "/usr/bin/" + name
        tun.restart = 0.01
        made.append(tun)
        return tun
    yield build
    for tun in made:
        tun.stop()


def test_each_provider_spots_its_address():
    assert quick_ready("INF |  https://quiet-river-banana.trycloudflare.com  |", {}) == "https://quiet-river-banana.trycloudflare.com"
    assert quick_ready("INF Requesting new quick Tunnel on trycloudflare.com...", {}) is None
    assert named_ready("INF Registered tunnel connection connIndex=0", {"hostname": "karaoke.example.com"}) == "https://karaoke.example.com"
    assert named_ready("INF Registered tunnel connection connIndex=0", {"hostname": ""}) == ""
    assert named_ready("INF Starting tunnel", {"hostname": "karaoke.example.com"}) is None
    assert funnel_ready("https://party.tail1234.ts.net/", {}) == "https://party.tail1234.ts.net"
    assert funnel_ready("Funnel is not enabled. To enable, visit: https://login.tailscale.com/f/funnel?node=abc", {}) is None
    assert PROVIDERS["tailscale"].args({}, "http://127.0.0.1:8123") == ["funnel", "8123"]


def test_the_local_address_is_this_server_from_the_same_machine():
    assert local_address({}) == "http://127.0.0.1:8000"
    assert local_address({"KARAQUEUE_HOST": "::", "KARAQUEUE_PORT": "9000"}) == "http://127.0.0.1:9000"
    assert local_address({"KARAQUEUE_HOST": "192.168.1.5"}) == "http://192.168.1.5:8000"
    assert local_address({"KARAQUEUE_HOST": "fd00::5"}) == "http://[fd00::5]:8000"


def test_a_quick_tunnel_comes_up_and_restarts_when_it_stops(make):
    tun = make()
    tun.start()
    first = tun.programs.nth(1)
    assert first.args == ["/usr/bin/cloudflared", "tunnel", "--no-autoupdate", "--url", "http://127.0.0.1:8000"]
    assert tun.url() is None
    first.say("INF Requesting new quick Tunnel on trycloudflare.com...")
    first.say("INF |  https://quiet-river-banana.trycloudflare.com  |")
    wait_for(lambda: tun.url() == "https://quiet-river-banana.trycloudflare.com")
    assert tun.status()["qr_svg"].startswith("<svg")

    first.code = 1
    first.lines.put(None)  # it crashed
    second = tun.programs.nth(2)
    assert tun.url() is None
    second.say("INF |  https://another-one.trycloudflare.com  |")
    wait_for(lambda: tun.url() == "https://another-one.trycloudflare.com")

    tun.stop()
    assert second.code == -15 and tun.status()["state"] == "off" and tun.url() is None


def test_changing_the_setting_restarts_or_stops_it(make):
    tun = make()
    tun.start()
    tun.programs.nth(1).say("https://one.trycloudflare.com")
    wait_for(lambda: tun.url())

    tun.values.update(tunnel="cloudflare_named", tunnel_token="t0ken-abc", tunnel_hostname="karaoke.example.com")
    tun.changed()
    named = tun.programs.nth(2)
    assert named.args == ["/usr/bin/cloudflared", "tunnel", "--no-autoupdate", "run"]
    assert named.env["TUNNEL_TOKEN"] == "t0ken-abc"  # in the environment, not where `ps` shows it
    assert "t0ken-abc" not in " ".join(named.args)
    named.say("INF Registered tunnel connection connIndex=0")
    wait_for(lambda: tun.url() == "https://karaoke.example.com")

    tun.values["tunnel"] = "off"
    tun.changed()
    wait_for(lambda: tun.status()["state"] == "off")
    assert named.code == -15 and tun.url() is None


def test_problems_starting_are_shown_not_retried_in_a_hurry(make):
    tun = make(tunnel="cloudflare_named")
    tun.restart = 60
    tun.start()
    wait_for(lambda: tun.status()["state"] == "error")
    assert "token" in tun.status()["message"]

    tun.which = lambda name: None
    tun.values.update(tunnel="tailscale")
    tun.changed()
    wait_for(lambda: tun.status().get("missing"))
    assert tun.status()["message"] == "tailscale isn't installed on this server."
    assert tun.status()["get_it"].startswith("https://")
    assert tun.programs.started == []


def test_tailscale_asking_for_funnel_to_be_allowed_shows_the_link(make):
    tun = make(tunnel="tailscale")
    tun.start()
    proc = tun.programs.nth(1)
    proc.say("Funnel is not enabled on your tailnet.")
    proc.say("To enable, visit:")
    proc.say("         https://login.tailscale.com/f/funnel?node=nABC123")
    wait_for(lambda: tun.status().get("action"))
    assert tun.status()["action"] == "https://login.tailscale.com/f/funnel?node=nABC123"
    assert tun.url() is None
    proc.say("Available on the internet:")
    proc.say("https://party.tail1234.ts.net/")
    wait_for(lambda: tun.url() == "https://party.tail1234.ts.net")


def test_join_links_use_the_tunnel_while_its_up(monkeypatch):
    monkeypatch.setenv("KARAQUEUE_PUBLIC_URL", "https://karaoke.example.com")
    monkeypatch.setenv("KARAQUEUE_TUNNEL", "cloudflare")
    client = TestClient(create_app(Store()))
    code = client.post("/api/rooms", json={"name": "Friday"}).json()["code"]
    assert client.get(f"/api/rooms/{code}/join-info").json()["link"] == f"https://karaoke.example.com/r/{code}"

    tun = client.app.state.tunnel
    tun.state = {"state": "up", "url": "https://quiet-river-banana.trycloudflare.com"}
    assert client.get(f"/api/rooms/{code}/join-info").json()["link"] == f"https://quiet-river-banana.trycloudflare.com/r/{code}"
    tun.state = {"state": "error", "url": "https://quiet-river-banana.trycloudflare.com", "message": "stopped"}
    assert client.get(f"/api/rooms/{code}/join-info").json()["link"] == f"https://karaoke.example.com/r/{code}"


def test_the_admin_page_sees_the_tunnel_and_can_turn_it_on(monkeypatch):
    monkeypatch.setenv("KARAQUEUE_ADMIN_PASSWORD", "correct horse battery")
    client = TestClient(create_app(Store()))
    tok = client.post("/api/admin/login", json={"password": "correct horse battery"}).json()["token"]
    auth = {"Authorization": "Bearer " + tok}
    poked = []
    monkeypatch.setattr(client.app.state.tunnel, "changed", lambda: poked.append(True))
    state = client.get("/api/admin", headers=auth).json()
    assert state["tunnel"]["state"] == "off" and "cloudflare" in state["tunnel"]["providers"]
    assert client.put("/api/admin/settings/tunnel", json={"value": "cloudflare"}, headers=auth).status_code == 200
    assert poked == [True]
    assert client.put("/api/admin/settings/tunnel", json={"value": "ngrok"}, headers=auth).status_code == 400
    assert client.put("/api/admin/settings/cloudflared", json={"value": "/bin/sh"}, headers=auth).status_code == 409
    client.put("/api/admin/settings/tunnel_token", json={"value": "t0ken-abc"}, headers=auth)
    assert "t0ken-abc" not in client.get("/api/admin", headers=auth).text
