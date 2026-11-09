"""Start a built desktop app and check it works: the server answers, the bundled programs
run, and (where there's a display) the window opened. Used by the desktop CI workflow.

Run: python desktop-build/smoke.py <path to the KaraQueue program> [--window]
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

PORT = 8799


def main() -> int:
    program = Path(sys.argv[1]).resolve()
    want_window = "--window" in sys.argv
    home = Path(tempfile.mkdtemp())
    env = {**os.environ, "KARAQUEUE_PORT": str(PORT), "HOME": str(home), "APPDATA": str(home / "AppData"),
           "XDG_DATA_HOME": str(home / "data"), "USERPROFILE": str(home)}
    app = subprocess.Popen([str(program)], env=env)
    try:
        config = None
        for _ in range(120):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/config", timeout=2) as reply:
                    config = json.load(reply)
                break
            except OSError:
                if app.poll() is not None:
                    print(f"The app quit with exit code {app.returncode}")
                    return 1
                time.sleep(1)
        print("server:", config)
        if not config:
            return 1
        logs = list(home.rglob("karaqueue.log"))
        log = logs[0].read_text(encoding="utf-8", errors="replace") if logs else ""
        window = "Couldn't open the window" not in log
        print("window:", "opened" if window else "fell back to the browser")
        if want_window and not window:
            print(log[-3000:])
            return 1
    finally:
        app.terminate()
        try:
            app.wait(timeout=20)
        except subprocess.TimeoutExpired:
            app.kill()
    here = program.parent
    tools = [p for p in (here / "_internal" / "tools", here.parent / "Resources" / "tools",
                         here.parent / "Frameworks" / "tools") if p.is_dir()][0]
    exe = ".exe" if os.name == "nt" else ""
    ytdlp = next(p for p in (here / f"yt-dlp{exe}", here.parent / "Frameworks" / "yt-dlp", here.parent / "Resources" / "yt-dlp")
                 if p.exists())
    for name, args in ((ytdlp, ["--version"]), (tools / f"ffmpeg{exe}", ["-version"]),
                       (tools / f"deno{exe}", ["--version"]), (tools / f"cloudflared{exe}", ["--version"])):
        out = subprocess.run([str(name), *args], capture_output=True, text=True, timeout=60)
        print(f"{name.name}: {(out.stdout or out.stderr).splitlines()[0] if (out.stdout or out.stderr) else '?'}")
        if out.returncode != 0:
            return 1
    print("smoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
