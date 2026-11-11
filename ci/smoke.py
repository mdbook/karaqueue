"""Check installed tools; CI also fetches metadata without downloading media."""
import importlib
import json
import subprocess
import sys
from importlib.metadata import version


def smoke(fetch=False):
    for module, distribution in [
        ("karaqueue.app", "karaqueue"), ("fastapi", "fastapi"),
        ("uvicorn", "uvicorn"), ("pydantic", "pydantic"),
        ("segno", "segno"), ("yt_dlp", "yt-dlp"),
    ]:
        importlib.import_module(module)
        print(f"{distribution} {version(distribution)}", flush=True)
    for command in (["yt-dlp", "--version"], ["deno", "--version"], ["ffmpeg", "-version"]):
        subprocess.run(command, check=True, timeout=15)
    if fetch:
        # "Me at the zoo", YouTube's first upload (stable for 20 years). Do not flatten: exercise the actual extractor.
        result = subprocess.run([
            "yt-dlp", "--ignore-config", "--no-playlist", "--skip-download", "--dump-single-json",
            "--socket-timeout", "10", "--retries", "0", "--extractor-retries", "0",
            "https://www.youtube.com/watch?v=jNQXAC9IVRw",
        ], check=True, timeout=60, capture_output=True, text=True)
        metadata = json.loads(result.stdout)
        if metadata.get("id") != "jNQXAC9IVRw" or not metadata.get("title"):
            raise ValueError("extractor smoke returned no video metadata")
        print("Extractor metadata smoke passed", flush=True)


if __name__ == "__main__":
    smoke(fetch="--fetch" in sys.argv[1:])
