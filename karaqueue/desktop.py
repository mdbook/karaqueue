"""The desktop app: KaraQueue in a window of its own on Windows, macOS and Linux.

It runs the same server as the `karaqueue` command, in this process, with settings picked for
one computer: the database, downloads and recordings in the user's data folder, songs in a
KaraQueue folder in Music, and a free port. The window opens on a dashboard with the guest
address and its QR code, a one-click public tunnel, and buttons into the host and admin pages.

Settings that only apply at startup (the port, say) go in config.env in the data folder, one
KARAQUEUE_NAME=value per line. Real environment variables win over it. Everything else is on
the admin page, as usual.

The built app (see desktop-build/) carries ffmpeg, yt-dlp, deno and cloudflared next to it; they
go first on the PATH, so YouTube and the tunnel work without installing anything.
"""
from __future__ import annotations

import base64
import logging
import logging.handlers
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any, Optional

import segno

log = logging.getLogger("karaqueue.desktop")

NAME = "KaraQueue"
DOCS = "https://github.com/mdbook/karaqueue/blob/main/docs/desktop.md"
PAGE = Path(__file__).parent / "desktop.html"


# ---- where things live ---------------------------------------------------------------------

def data_dir(platform: str = sys.platform, env=os.environ, home: Optional[Path] = None) -> Path:
    """The usual place for an app's own files on each system."""
    home = home or Path.home()
    if platform.startswith("win"):
        return Path(env.get("APPDATA") or home / "AppData" / "Roaming") / NAME
    if platform == "darwin":
        return home / "Library" / "Application Support" / NAME
    return Path(env.get("XDG_DATA_HOME") or home / ".local" / "share") / "karaqueue"


def load_config(path: Path, env=os.environ) -> None:
    """KARAQUEUE_ settings from config.env, where the environment doesn't set them already."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        key, sep, value = line.strip().partition("=")
        key = key.strip()
        if sep and key.startswith("KARAQUEUE_") and not key.startswith("#"):
            env.setdefault(key, value.strip())


def save_config(path: Path, key: str, value: str) -> None:
    """Set one line of config.env, keeping the rest of the file as it is."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = ["# KaraQueue desktop settings, one KARAQUEUE_NAME=value per line.",
                 "# See docs/configuration.md for the names. Restart KaraQueue after changing this file."]
    lines = [line for line in lines if line.partition("=")[0].strip() != key] + [f"{key}={value}"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def free_port(preferred: int, host: str = "0.0.0.0", tries: int = 20) -> int:
    """The preferred port if it's free, else the next free one after it, else any."""
    for port in [*range(preferred, preferred + tries), 0]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if os.name != "nt":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # like uvicorn, so TIME_WAIT doesn't count
            try:
                sock.bind((host if ":" not in host else "0.0.0.0", port))
            except OSError:
                continue
            return sock.getsockname()[1]
    raise OSError("No free port")


def bundled_dirs() -> list:
    """Folders of programs that came with the built app: the app's own folder and the one
    beside it inside the bundle (yt-dlp is in one of them), and tools/ (ffmpeg, deno, cloudflared)."""
    if not getattr(sys, "frozen", False):
        return []
    here = Path(sys.executable).parent
    inside = Path(getattr(sys, "_MEIPASS", here))
    return list(dict.fromkeys(d for d in (here, inside, inside / "tools") if d.is_dir()))


def prepare(env=os.environ, home: Optional[Path] = None, data: Optional[Path] = None) -> Path:
    """Fill in the desktop defaults the user hasn't set. Returns the data folder."""
    home = home or Path.home()
    data = data or data_dir(home=home)
    data.mkdir(parents=True, exist_ok=True)
    load_config(data / "config.env", env)
    songs = home / "Music" / NAME
    defaults = {"KARAQUEUE_DB": data / "karaqueue.db", "KARAQUEUE_DOWNLOADS_DIR": data / "downloads",
                "KARAQUEUE_RECORDINGS_DIR": data / "recordings", "KARAQUEUE_MEDIA_DIR": songs}
    for key, value in defaults.items():
        env.setdefault(key, str(value))
    for folder in env["KARAQUEUE_MEDIA_DIR"].split(os.pathsep):
        if folder.strip():
            Path(folder.strip()).mkdir(parents=True, exist_ok=True)
    # The admin page may point the library anywhere in the user's home, and at the folders above.
    folders = [str(home), str(data), *env["KARAQUEUE_MEDIA_DIR"].split(os.pathsep)]
    env.setdefault("KARAQUEUE_ALLOWED_FOLDERS", os.pathsep.join(f for f in folders if f))
    tools = bundled_dirs()
    if tools:
        env["PATH"] = os.pathsep.join([*map(str, tools), env.get("PATH", "")])
    return data


def open_path(path: Path) -> None:
    """Show a folder or file in the system's file manager or default app."""
    if sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])


def quiet_windows_children() -> None:
    """On Windows, a windowed app's console programs (ffmpeg, yt-dlp, cloudflared) each flash a
    console window. Start them all without one."""
    # ponytail: patches subprocess.Popen for the whole process; fine for this app, which owns it.
    if os.name != "nt":
        return
    flag = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    original = subprocess.Popen

    class Popen(original):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | flag
            super().__init__(*args, **kwargs)
    subprocess.Popen = Popen  # type: ignore[misc]


def setup_logging(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "karaqueue.log"
    handlers: list = [logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    else:  # a windowed build has nowhere to print; anything that tries goes to the log
        sys.stdout = sys.stderr = open(path.with_name("output.log"), "a", buffering=1, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=handlers, force=True)
    return path


# ---- the server, in a thread ---------------------------------------------------------------

class Server:
    def __init__(self) -> None:
        self.app: Any = None
        self.server: Any = None
        self.thread: Optional[threading.Thread] = None
        self.port = 0

    def start(self, timeout: float = 30) -> None:
        import uvicorn

        from .app import create_app, server_options

        self.app = create_app()
        options = server_options()
        self.port = options["port"]
        self.server = uvicorn.Server(uvicorn.Config(self.app, log_config=None, **options))
        self.thread = threading.Thread(target=self.server.run, name="server", daemon=True)
        self.thread.start()
        end = time.monotonic() + timeout
        while not self.server.started:
            if not self.thread.is_alive() or time.monotonic() > end:
                raise RuntimeError(f"The server didn't start on port {self.port}. See the log.")
            time.sleep(0.05)

    def stop(self) -> None:
        if self.server is not None:
            self.server.should_exit = True
        if self.thread is not None:
            self.thread.join(timeout=15)

    @property
    def local(self) -> str:
        return f"http://127.0.0.1:{self.port}"


# ---- what the window's page can ask for ----------------------------------------------------

class Api:
    """Called from desktop.html as window.pywebview.api.<name>(). Names starting with _ stay
    out of the page's reach."""

    def __init__(self, server: Server, data: Path, log_file: Path) -> None:
        self._server, self._data, self._log_file = server, data, log_file
        self._window: Any = None

    def _settings(self) -> Any:
        return self._server.app.state.settings

    def _tunnel(self) -> Any:
        return self._server.app.state.tunnel

    def status(self) -> dict:
        from .app import home_for_guests, is_local

        cfg, tunnel = self._settings(), self._tunnel()
        lan = home_for_guests(None, "", self._server.local)
        guest = home_for_guests(tunnel.url(), cfg.get("public_url"), self._server.local)
        state = tunnel.status()
        return {"guest": guest, "qr_svg": segno.make(guest).svg_inline(border=2, omitsize=True),
                "local": is_local(guest), "lan": lan, "port": self._server.port,
                "tunnel": {k: state.get(k) for k in ("state", "url", "message", "label", "action", "missing", "get_it")},
                "tunnel_setting": cfg.get("tunnel"), "setup": bool(cfg.setup_token),
                "songs": cfg.get("media_dir"), "data": str(self._data)}

    def set_tunnel(self, on: bool) -> dict:
        """Turn the tunnel on (a Cloudflare quick tunnel, unless the admin page picked another) or off."""
        cfg = self._settings()
        current = cfg.get("tunnel")
        cfg.set("tunnel", (current if current != "off" else "cloudflare") if on else "off")
        self._tunnel().changed()
        return self.status()

    def choose_songs(self) -> dict:
        import webview

        picked = self._window.create_file_dialog(webview.FileDialog.FOLDER) if self._window else None
        if picked:
            folder = str(picked[0] if isinstance(picked, (list, tuple)) else picked)
            allowed = os.environ.get("KARAQUEUE_ALLOWED_FOLDERS", "")
            if folder not in allowed.split(os.pathsep):  # a folder outside the home folder, picked here by hand
                os.environ["KARAQUEUE_ALLOWED_FOLDERS"] = os.pathsep.join(filter(None, [allowed, folder]))
            self._settings().set("media_dir", folder)
            config = self._data / "config.env"
            save_config(config, "KARAQUEUE_MEDIA_DIR", folder)
            save_config(config, "KARAQUEUE_ALLOWED_FOLDERS", os.environ["KARAQUEUE_ALLOWED_FOLDERS"])
        return self.status()

    def go(self, path: str) -> None:
        """Show a page of the server in this window: "/" (host a night), "/admin", or the setup link."""
        if not path.startswith("/"):
            return
        if path == "/setup":
            token = self._settings().setup_token
            path = f"/admin#setup={token}" if token else "/admin"
        if self._window:
            self._window.load_url(self._server.local + path)

    def browser(self, path: str = "/") -> None:
        """Open a page in the system's browser, at the address phones on the network use."""
        if path.startswith("/"):
            webbrowser.open(self.status()["lan"] + path)

    def open_link(self, url: str) -> None:
        if url.startswith("https://"):
            webbrowser.open(url)

    def show(self, what: str) -> None:
        cfg = self._settings()
        places = {"songs": (cfg.get("media_dir") or "").split(os.pathsep)[0], "data": str(self._data),
                  "log": str(self._log_file)}
        if places.get(what):
            open_path(Path(places[what]))

    def dashboard(self) -> None:
        if self._window:
            self._window.load_html(dashboard_html(self._server.local))


def dashboard_html(base: str) -> str:
    # The font goes in the page itself: loaded from the server it would be cross-origin, and blocked.
    font = Path(__file__).parent / "static" / "fonts" / "BricolageGrotesque.woff2"
    data = "data:font/woff2;base64," + base64.b64encode(font.read_bytes()).decode()
    return PAGE.read_text(encoding="utf-8").replace("{{base}}", base).replace("{{font}}", data)


# ---- the app ---------------------------------------------------------------------------------

def main() -> None:
    data = prepare()
    log_file = setup_logging(data / "logs")
    quiet_windows_children()
    trust_bundled_tools()
    if sys.platform.startswith("linux") and getattr(sys, "frozen", False):
        # Ubuntu 24.04 and others stop the web view's sandbox from starting, which would leave
        # an empty window. The window only shows this app's own pages.
        os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
    try:
        preferred = int(os.environ.get("KARAQUEUE_PORT") or 8000)
    except ValueError:
        preferred = 8000
    os.environ["KARAQUEUE_PORT"] = str(free_port(preferred, os.environ.get("KARAQUEUE_HOST") or "0.0.0.0"))
    if os.environ["KARAQUEUE_PORT"] != str(preferred):
        log.info("Port %s is taken, using %s", preferred, os.environ["KARAQUEUE_PORT"])
    server = Server()
    server.start()
    log.info("KaraQueue is running at %s (data in %s)", server.local, data)
    api = Api(server, data, log_file)
    try:
        run_window(api, server, data)
    except Exception:  # no GUI toolkit (a bare Linux box, say): carry on in the browser
        log.exception("Couldn't open the window, opening the browser instead")
        webbrowser.open(server.local + ("/admin#setup=" + api._settings().setup_token if api._settings().setup_token else "/"))
        print(f"KaraQueue is running at {server.local}. Press Ctrl+C to stop.")
        try:
            while server.thread and server.thread.is_alive():
                server.thread.join(1)
        except KeyboardInterrupt:
            pass
    finally:
        server.stop()


def window_problem(env=os.environ, platform: str = sys.platform) -> Optional[str]:
    """Why a window can't open here, checked first where trying would crash rather than fail:
    Qt (the Linux build's toolkit) ends the whole process when it can't reach a display."""
    if not platform.startswith("linux"):
        return None
    if not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY") or env.get("QT_QPA_PLATFORM")):
        return "no display"
    if getattr(sys, "frozen", False) and not env.get("WAYLAND_DISPLAY") and not env.get("QT_QPA_PLATFORM"):
        import ctypes.util
        if not ctypes.util.find_library("xcb-cursor"):
            return "the libxcb-cursor0 library is missing (install it for the window)"
    return None


def trust_bundled_tools() -> None:
    """On macOS, a downloaded app's files carry a quarantine flag, and Gatekeeper stops each
    bundled program the first time the app runs it. Once someone has opened the app, its
    programs are trusted the same."""
    if sys.platform != "darwin":
        return
    for folder in bundled_dirs():
        subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(folder)], capture_output=True, check=False)


def run_window(api: Api, server: Server, data: Path) -> None:
    problem = window_problem()
    if problem:
        raise RuntimeError(f"Can't open a window: {problem}")
    import webview
    from webview.menu import Menu, MenuAction, MenuSeparator

    window = webview.create_window(NAME, html=dashboard_html(server.local), js_api=api, width=1100, height=780,
                                   min_size=(720, 560), confirm_close=True, background_color="#211820")
    api._window = window
    menu = [
        Menu(NAME, [MenuAction("Dashboard", api.dashboard), MenuAction("Host a night", lambda: api.go("/")),
                    MenuAction("Admin page", lambda: api.go("/admin")), MenuSeparator(),
                    MenuAction("Open in your browser", lambda: api.browser("/")), MenuSeparator(),
                    MenuAction("Quit", window.destroy)]),
        Menu("Folders", [MenuAction("Songs folder", lambda: api.show("songs")),
                         MenuAction("Data folder", lambda: api.show("data")), MenuAction("Log", lambda: api.show("log"))]),
        Menu("Help", [MenuAction("How the desktop app works", lambda: api.open_link(DOCS))]),
    ]
    webview.start(menu=menu, private_mode=False, storage_path=str(data / "webview"))


if __name__ == "__main__":
    main()
