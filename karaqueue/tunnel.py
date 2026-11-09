"""Tunnels: a public https address for this server with nothing to set up on the router, and
working even behind carrier-grade NAT. The server runs the tunnel program (cloudflared or
tailscale) as a child process, reads the address it prints, and puts it in the join links and
QR codes while the tunnel is up. The program is restarted if it stops, and stopped with the server.

Each kind of tunnel is a `Provider`: how to start the program and how to spot, in what it
prints, that the tunnel is up and at what address. Adding another one is one more entry in
`PROVIDERS`.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
import time
from typing import Any, Callable, NamedTuple, Optional

import segno

from .public import clean_text

log = logging.getLogger("karaqueue.tunnel")

RESTART = 5.0  # first wait before starting a stopped tunnel again, doubling up to RESTART_MAX
RESTART_MAX = 300.0
STEADY = 60.0  # a tunnel that stayed up this long starts again after the shortest wait
QUICK_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
FUNNEL_URL = re.compile(r"https://[a-z0-9.-]+\.ts\.net")
ACTION_URL = re.compile(r"https://login\.tailscale\.com/\S+")


class Provider(NamedTuple):
    label: str
    command: str  # the setting naming the program to run
    args: Callable[[dict, str], list]  # (settings, local address) -> arguments after the program
    env: Callable[[dict], dict]  # extra environment for the program
    # (line it printed, settings) -> the public address once it's up, "" for up at an address we
    # can't tell, or None for not yet.
    ready: Callable[[str, dict], Optional[str]]
    get_it: str  # where to download the program


def quick_ready(line: str, conf: dict) -> Optional[str]:
    found = QUICK_URL.search(line)
    return found.group(0) if found else None


def named_ready(line: str, conf: dict) -> Optional[str]:
    if "Registered tunnel connection" not in line:
        return None
    return f"https://{conf['hostname']}" if conf["hostname"] else ""


def funnel_ready(line: str, conf: dict) -> Optional[str]:
    found = FUNNEL_URL.search(line)
    return found.group(0) if found else None


CLOUDFLARED = "https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
PROVIDERS = {
    # No account: Cloudflare hands out a random https://<words>.trycloudflare.com address each start.
    "cloudflare": Provider("Cloudflare quick tunnel", "cloudflared",
                           lambda conf, local: ["tunnel", "--no-autoupdate", "--url", local],
                           lambda conf: {}, quick_ready, CLOUDFLARED),
    # A tunnel made in the Cloudflare dashboard, on your own domain. The token goes in the environment,
    # not the command line, where anyone on the machine could read it.
    "cloudflare_named": Provider("Cloudflare named tunnel", "cloudflared",
                                 lambda conf, local: ["tunnel", "--no-autoupdate", "run"],
                                 lambda conf: {"TUNNEL_TOKEN": conf["token"]}, named_ready, CLOUDFLARED),
    # Runs in the foreground, so the funnel closes when the program stops.
    "tailscale": Provider("Tailscale Funnel", "tailscale",
                          lambda conf, local: ["funnel", local.rsplit(":", 1)[1]],
                          lambda conf: {}, funnel_ready, "https://tailscale.com/download"),
}


def local_address(env=os.environ) -> str:
    """Where the tunnel program should send visits: this server, as seen from the same machine."""
    host = env.get("KARAQUEUE_HOST") or "0.0.0.0"
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    elif ":" in host:
        host = f"[{host}]"
    try:
        port = int(env.get("KARAQUEUE_PORT") or 8000)
    except ValueError:
        port = 8000
    return f"http://{host}:{port}"


class Tunnel:
    """Runs the chosen tunnel program in a thread of its own while the server runs."""

    def __init__(self, get: Callable[[str], Any], local: Optional[str] = None):
        self.get = get
        self.local = local or local_address()
        self.popen: Callable[..., Any] = subprocess.Popen  # swapped out by tests
        self.which: Callable[[str], Optional[str]] = shutil.which
        self.clock: Callable[[], float] = time.monotonic
        self.restart, self.restart_max = RESTART, RESTART_MAX
        self.lock = threading.Lock()
        self.wake, self.stopping = threading.Event(), threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.proc: Any = None
        self.conf: Optional[dict] = None  # the settings the running program was started with
        self.state: dict = {"state": "off"}

    def settings(self) -> dict:
        provider = self.get("tunnel")
        spec = PROVIDERS.get(provider)
        return {"provider": provider if spec else "off", "token": self.get("tunnel_token"),
                "hostname": self.get("tunnel_hostname"), "command": self.get(spec.command) if spec else ""}

    def url(self) -> Optional[str]:
        """The public address, while the tunnel is up."""
        state = self.state
        return state.get("url") or None if state.get("state") == "up" else None

    def status(self) -> dict:
        state = dict(self.state)
        spec = PROVIDERS.get((self.conf or self.settings())["provider"])
        state.update(label=spec.label if spec else "", get_it=spec.get_it if spec else "",
                     providers={key: p.label for key, p in PROVIDERS.items()})
        if state.get("state") == "up" and state.get("url"):
            state["qr_svg"] = segno.make(state["url"]).svg_inline(border=2, omitsize=True)
        return state

    # -- the thread ---------------------------------------------------------------

    def start(self) -> None:
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name="tunnel", daemon=True)
            self.thread.start()

    def changed(self) -> None:
        """A setting changed: start, stop or restart the program to match."""
        with self.lock:
            if self.proc is not None and self.settings() != self.conf:
                self._end(self.proc)
        self.wake.set()

    def stop(self) -> None:
        self.stopping.set()
        with self.lock:
            if self.proc is not None:
                self._end(self.proc)
        self.wake.set()
        if self.thread is not None:
            self.thread.join(timeout=10)

    def _run(self) -> None:
        delay = self.restart
        while not self.stopping.is_set():
            conf = self.settings()
            if conf["provider"] == "off":
                self.conf, self.state = conf, {"state": "off"}
                self._wait(None)
                continue
            began = self.clock()
            stopped = self._once(conf)
            if self.stopping.is_set():
                break
            if self.settings() != conf:
                delay = self.restart  # a setting changed: start the new one straight away
                continue
            if self.clock() - began >= STEADY:
                delay = self.restart
            if stopped:  # it ran and stopped; it didn't fail to start
                self.state["message"] += f" Starting it again in {delay:g} seconds."
            self._wait(delay)
            delay = min(self.restart_max, delay * 2)
        self.state = {"state": "off"}

    def _wait(self, seconds: Optional[float]) -> None:
        self.wake.wait(seconds)
        self.wake.clear()

    def _once(self, conf: dict) -> bool:
        """Run the program until it stops. False if it couldn't start at all."""
        spec = PROVIDERS[conf["provider"]]
        self.conf = conf
        if conf["provider"] == "cloudflare_named" and not conf["token"]:
            self.state = {"state": "error", "message": "Paste the tunnel token from the Cloudflare dashboard."}
            return False
        program = self.which(conf["command"]) if conf["command"] else None
        if not program:
            self.state = {"state": "error", "missing": True,
                          "message": f"{conf['command'] or spec.command} isn't installed on this server."}
            return False
        self.state = {"state": "starting", "message": f"Starting the {spec.label}."}
        try:
            with self.lock:
                if self.stopping.is_set():
                    return False
                self.proc = proc = self.popen(
                    [program, *spec.args(conf, self.local)], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, errors="replace", env={**os.environ, **spec.env(conf)},
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as exc:
            self.state = {"state": "error", "message": f"Couldn't start {conf['command']} ({type(exc).__name__})."}
            return False
        last = ""
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                last = line
                log.info("%s: %s", conf["command"], clean_text(line, 300))
                if self.state["state"] == "up":
                    continue
                action = ACTION_URL.search(line)
                if action:  # Tailscale waits for someone to turn Funnel on for the tailnet
                    self.state = {"state": "starting", "action": action.group(0),
                                  "message": "Tailscale needs Funnel turned on for this machine. Open the link to allow it."}
                    continue
                url = spec.ready(line, conf)
                if url is not None:
                    self.state = {"state": "up", "url": url, "message": (
                        f"Up at {url}." if url else "Up. Type the tunnel's public hostname to use it in join links.")}
                    log.info("Tunnel up: %s", url or "(no hostname set)")
        finally:
            code = proc.wait()
            with self.lock:
                self.proc = None
        why = f" It said: {clean_text(last)}" if last else ""
        self.state = {"state": "error", "message": f"{conf['command']} stopped (exit code {code}).{why}"}
        return True

    @staticmethod
    def _end(proc: Any) -> None:
        try:
            proc.terminate()
        except OSError:
            pass
        threading.Thread(target=Tunnel._reap, args=(proc,), daemon=True).start()

    @staticmethod
    def _reap(proc: Any) -> None:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
