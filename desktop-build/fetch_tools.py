"""Download the programs the desktop app carries, for the system this runs on, into
desktop-build/build/tools: ffmpeg (from the imageio-ffmpeg wheel), deno (from the deno wheel,
for yt-dlp's YouTube support) and cloudflared (from Cloudflare's releases). yt-dlp itself is
built into the app as a second program, see karaqueue.spec.

Versions are pinned here; bump them in one place. Run: python desktop-build/fetch_tools.py
"""
from __future__ import annotations

import io
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

DENO = "deno==2.9.8"
FFMPEG = "imageio-ffmpeg==0.8.1"
CLOUDFLARED = "2026.10.7"
OUT = Path(__file__).parent / "build" / "tools"
EXE = ".exe" if sys.platform.startswith("win") else ""


def cloudflared_asset() -> str:
    arm = platform.machine().lower() in ("arm64", "aarch64")
    if sys.platform.startswith("win"):
        return "cloudflared-windows-amd64.exe"
    if sys.platform == "darwin":
        return f"cloudflared-darwin-{'arm64' if arm else 'amd64'}.tgz"
    return f"cloudflared-linux-{'arm64' if arm else 'amd64'}"


def wheel_member(requirement: str, pick) -> bytes:
    """One file out of the wheel pip picks for this system."""
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run([sys.executable, "-m", "pip", "download", "-q", "--no-deps", "--only-binary", ":all:",
                        "-d", tmp, requirement], check=True)
        wheel = next(Path(tmp).glob("*.whl"))
        with zipfile.ZipFile(wheel) as z:
            name = next(n for n in z.namelist() if pick(n))
            return z.read(name)


def save(name: str, data: bytes) -> None:
    path = OUT / (name + EXE)
    path.write_bytes(data)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f"{path.name}: {len(data) // 1024 // 1024} MB")


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    save("ffmpeg", wheel_member(FFMPEG, lambda n: "/binaries/ffmpeg-" in n))
    save("deno", wheel_member(DENO, lambda n: ".data/scripts/deno" in n))
    asset = cloudflared_asset()
    url = f"https://github.com/cloudflare/cloudflared/releases/download/{CLOUDFLARED}/{asset}"
    with urllib.request.urlopen(url, timeout=120) as reply:
        data = reply.read()
    if asset.endswith(".tgz"):
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            data = tar.extractfile("cloudflared").read()
    save("cloudflared", data)


if __name__ == "__main__":
    main()
