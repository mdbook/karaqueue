import os
import socket
from pathlib import Path

import pytest

from karaqueue import app as app_module
from karaqueue import desktop
from karaqueue.app import create_app
from karaqueue.store import Store


def test_the_data_folder_is_the_usual_one_on_each_system(tmp_path):
    home = tmp_path
    assert desktop.data_dir("win32", {"APPDATA": r"C:\Users\Sam\AppData\Roaming"}, home) == Path(r"C:\Users\Sam\AppData\Roaming") / "KaraQueue"
    assert desktop.data_dir("darwin", {}, home) == home / "Library" / "Application Support" / "KaraQueue"
    assert desktop.data_dir("linux", {}, home) == home / ".local" / "share" / "karaqueue"
    assert desktop.data_dir("linux", {"XDG_DATA_HOME": "/data"}, home) == Path("/data/karaqueue")


def test_config_env_fills_in_what_the_environment_leaves_out(tmp_path):
    config = tmp_path / "config.env"
    desktop.save_config(config, "KARAQUEUE_PORT", "9000")
    desktop.save_config(config, "KARAQUEUE_MEDIA_DIR", "/songs")
    desktop.save_config(config, "KARAQUEUE_PORT", "9100")  # replaces, doesn't repeat
    text = config.read_text()
    assert text.count("KARAQUEUE_PORT") == 1 and text.startswith("#")
    config.write_text(text + "# KARAQUEUE_HOST=1.2.3.4\nPATH=/evil\nnot a setting\n")
    env = {"KARAQUEUE_MEDIA_DIR": "/from/env"}
    desktop.load_config(config, env)
    assert env == {"KARAQUEUE_MEDIA_DIR": "/from/env", "KARAQUEUE_PORT": "9100"}
    desktop.load_config(tmp_path / "missing.env", env)  # no file is fine


def test_a_taken_port_moves_to_the_next_free_one():
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        assert desktop.free_port(port, "127.0.0.1") != port
    assert desktop.free_port(port, "127.0.0.1") == port


def test_prepare_puts_everything_in_the_users_folders(tmp_path):
    home, env = tmp_path / "home", {"PATH": "/usr/bin"}
    data = desktop.prepare(env, home=home, data=home / "data")
    assert env["KARAQUEUE_DB"] == str(home / "data" / "karaqueue.db")
    assert env["KARAQUEUE_MEDIA_DIR"] == str(home / "Music" / "KaraQueue") and (home / "Music" / "KaraQueue").is_dir()
    assert env["KARAQUEUE_DOWNLOADS_DIR"] == str(data / "downloads")
    assert env["KARAQUEUE_ALLOWED_FOLDERS"].split(os.pathsep)[0] == str(home)
    assert env["PATH"] == "/usr/bin"  # not a built app, so nothing bundled goes first

    env = {"KARAQUEUE_MEDIA_DIR": str(tmp_path / "mine")}
    desktop.prepare(env, home=home, data=home / "data")
    assert env["KARAQUEUE_MEDIA_DIR"] == str(tmp_path / "mine")  # what the user set wins
    assert str(tmp_path / "mine") in env["KARAQUEUE_ALLOWED_FOLDERS"].split(os.pathsep)


class FakeServer:
    def __init__(self, app, port=8123):
        self.app, self.port = app, port
        self.local = f"http://127.0.0.1:{port}"


class FakeWindow:
    def __init__(self):
        self.loaded = []

    def load_url(self, url):
        self.loaded.append(url)


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "in_container", lambda: False)
    monkeypatch.setattr(app_module, "local_ip_for", lambda host: "192.168.1.20")
    store = Store()
    app = create_app(store)
    app.state.tunnel.changed = lambda: None
    result = desktop.Api(FakeServer(app), tmp_path, tmp_path / "karaqueue.log")
    result._window = FakeWindow()
    return result


def test_the_dashboard_shows_the_network_address_then_the_tunnel(api):
    status = api.status()
    assert status["guest"] == status["lan"] == "http://192.168.1.20:8123"
    assert status["qr_svg"].startswith("<svg") and status["local"] is False
    assert status["tunnel"]["state"] == "off" and status["tunnel_setting"] == "off"

    status = api.set_tunnel(True)
    assert status["tunnel_setting"] == "cloudflare"
    api._server.app.state.tunnel.state = {"state": "up", "url": "https://quiet-river-banana.trycloudflare.com"}
    assert api.status()["guest"] == "https://quiet-river-banana.trycloudflare.com"
    assert api.set_tunnel(False)["tunnel_setting"] == "off"


def test_turning_the_tunnel_on_keeps_the_kind_picked_on_the_admin_page(api):
    api._settings().set("tunnel", "tailscale")
    assert api.set_tunnel(True)["tunnel_setting"] == "tailscale"


def test_first_run_goes_to_the_setup_link_and_pages_stay_on_this_server(api):
    api._settings().setup_token = "a" * 32
    assert api.status()["setup"] is True
    api.go("/setup")
    api.go("/admin")
    api.go("https://elsewhere.example")
    assert api._window.loaded == ["http://127.0.0.1:8123/admin#setup=" + "a" * 32, "http://127.0.0.1:8123/admin"]


def test_the_dashboard_page_points_at_the_server():
    page = desktop.dashboard_html("http://127.0.0.1:8123")
    assert 'href="http://127.0.0.1:8123/static/style.css"' in page and "{{base}}" not in page
