"""Offline tests for the scheduled build's lock, smoke gate and notification."""
import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

ROOT = Path(__file__).resolve().parents[1]


def helper(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "ci" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_lock_hashes_actual_wheels_and_sorts_normalized_names(tmp_path):
    lock = helper("lock")
    for name in ("Z_pkg", "a-package"):
        wheel = tmp_path / (name + ".whl")
        with ZipFile(wheel, "w") as archive:
            archive.writestr(name + ".dist-info/METADATA", f"Name: {name}\nVersion: 1.2.3\n")
    text = lock.wheel_lock(tmp_path)
    lock.check(text)
    assert text.splitlines()[1].startswith("a-package==1.2.3")
    for name, normalized in [("Z_pkg", "z-pkg"), ("a-package", "a-package")]:
        digest = hashlib.sha256((tmp_path / (name + ".whl")).read_bytes()).hexdigest()
        assert f"{normalized}==1.2.3 --hash=sha256:{digest}" in text


@pytest.mark.parametrize("text", ["", "# bootstrap\n", "tool>=1", "tool==1", "--index-url https://example.org",
                                       "tool==1 --hash=sha256:bad"])
def test_lock_rejects_unlocked_or_empty_inputs(text):
    with pytest.raises(ValueError, match="missing hash lock"):
        helper("lock").check(text)


def test_lock_rejects_empty_resolution_and_duplicate_packages(tmp_path):
    lock = helper("lock")
    with pytest.raises(ValueError, match="no wheels"):
        lock.wheel_lock(tmp_path)
    entry = "tool==1 --hash=sha256:" + "0" * 64 + "\n"
    with pytest.raises(ValueError, match="duplicate"):
        lock.check(entry * 2)


def test_resolver_uses_current_project_requirements_and_binary_wheels(tmp_path, monkeypatch):
    pytest.importorskip("tomllib")
    lock = helper("lock")
    monkeypatch.setattr(lock, "ROOT", tmp_path)
    monkeypatch.setattr(lock, "LOCK", tmp_path / "requirements.lock")
    (tmp_path / "pyproject.toml").write_text('[project]\ndependencies = ["fastapi>=0.110"]\n')
    calls = []

    def download(command, **kwargs):
        calls.append((command, kwargs))
        directory = Path(command[command.index("--dest") + 1])
        with ZipFile(directory / "fastapi.whl", "w") as archive:
            archive.writestr("fastapi.dist-info/METADATA", "Name: fastapi\nVersion: 9.9\n")

    monkeypatch.setattr(lock.subprocess, "run", download)
    lock.resolve()
    command, options = calls[0]
    assert "--only-binary=:all:" in command
    assert command[-3:] == ["fastapi>=0.110", "yt-dlp[default,deno]", "setuptools>=68"]
    assert options == {"check": True, "timeout": 240}
    lock.check(lock.LOCK.read_text())
    assert "fastapi==9.9" in lock.LOCK.read_text()


@pytest.mark.parametrize("fetch", [False, True])
def test_smoke_imports_reports_versions_and_bounds_metadata_fetch(fetch, monkeypatch):
    smoke = helper("smoke")
    imports, commands = [], []
    monkeypatch.setattr(smoke.importlib, "import_module", imports.append)
    monkeypatch.setattr(smoke, "version", lambda name: "1.0")

    def run(command, **kwargs):
        commands.append((command, kwargs))
        return SimpleNamespace(stdout=json.dumps({"id": "jNQXAC9IVRw", "title": "test"}))

    monkeypatch.setattr(smoke.subprocess, "run", run)
    smoke.smoke(fetch)
    assert "karaqueue.app" in imports and "yt_dlp" in imports
    assert [command for command, _ in commands[:3]] == [
        ["yt-dlp", "--version"], ["deno", "--version"], ["ffmpeg", "-version"]]
    assert all(options["check"] and options["timeout"] <= 60 for _, options in commands)
    assert len(commands) == (4 if fetch else 3)
    if fetch:
        command = commands[-1][0]
        assert "--skip-download" in command and "--dump-single-json" in command
        assert "--no-playlist" in command and "--flat-playlist" not in command


@pytest.mark.parametrize("result", ["bad-json", '{}', '{"id":"wrong","title":"test"}'])
def test_smoke_rejects_invalid_metadata(result, monkeypatch):
    smoke = helper("smoke")
    monkeypatch.setattr(smoke.importlib, "import_module", lambda name: None)
    monkeypatch.setattr(smoke, "version", lambda name: "1.0")
    monkeypatch.setattr(smoke.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=result))
    with pytest.raises(ValueError):
        smoke.smoke(True)


def test_smoke_propagates_tool_failure(monkeypatch):
    smoke = helper("smoke")
    monkeypatch.setattr(smoke.importlib, "import_module", lambda name: None)
    monkeypatch.setattr(smoke, "version", lambda name: "1.0")

    def failed(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "yt-dlp")

    monkeypatch.setattr(smoke.subprocess, "run", failed)
    with pytest.raises(subprocess.CalledProcessError):
        smoke.smoke(True)


def test_alert_skips_unset_webhook(monkeypatch, capsys):
    alert = helper("alert")
    monkeypatch.delenv("FLEET_ALERT_WEBHOOK", raising=False)
    monkeypatch.setattr(alert, "urlopen", lambda *args, **kwargs: pytest.fail("unexpected network"))
    alert.alert()
    assert "skipping" in capsys.readouterr().out


@pytest.mark.parametrize("failure", [False, True])
def test_alert_posts_one_line_and_does_not_leak_endpoint(failure, monkeypatch, capsys):
    alert = helper("alert")
    monkeypatch.setenv("FLEET_ALERT_WEBHOOK", "https://example.invalid/secret")
    monkeypatch.setenv("CI_PROJECT_PATH", "mikayla/karaqueue")
    monkeypatch.setenv("CI_PIPELINE_URL", "https://example.invalid/pipelines/1")

    def post(request, timeout):
        assert timeout == 15 and request.method == "POST"
        assert json.loads(request.data) == {
            "body": "mikayla/karaqueue: scheduled pipeline failed https://example.invalid/pipelines/1"}
        if failure:
            raise OSError("https://example.invalid/secret")
        return SimpleNamespace(status=200)

    class Response:
        def __enter__(self):
            return SimpleNamespace(status=200)

        def __exit__(self, *args):
            pass

    def open_response(request, timeout):
        post(request, timeout)
        return Response()

    monkeypatch.setattr(alert, "urlopen", open_response)
    if failure:
        with pytest.raises(SystemExit, match="could not be delivered") as error:
            alert.alert()
        assert "secret" not in str(error.value)
    else:
        alert.alert()
    assert "secret" not in capsys.readouterr().out


def test_pipeline_wires_gate_rollback_artifact_and_project_credentials():
    pipeline = (ROOT / ".gitlab-ci.yml").read_text()
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "python ci/lock.py resolve; else python ci/lock.py check" in pipeline
    assert "paths: [requirements.lock]" in pipeline and "expire_in: never" in pipeline
    assert '--label "me.mdbook.karaqueue.requirements=$(cat requirements.lock)"' in pipeline
    assert "--cache=false" in pipeline and "--build-arg EXTRACTOR_SMOKE=1" in pipeline
    assert "$CI_REGISTRY_IMAGE:latest" in pipeline and "$CI_REGISTRY_IMAGE:$(date -u +%Y-%m-%d)" in pipeline
    assert "$CI_REGISTRY_IMAGE:$CI_COMMIT_SHORT_SHA" in pipeline
    assert '"username":"%s","password":"%s"' in pipeline
    assert '"$CI_REGISTRY_USER" "$CI_REGISTRY_PASSWORD"' in pipeline
    assert 'if: $CI_PIPELINE_SOURCE == "schedule"\n      when: on_failure' in pipeline
    assert "--require-hashes -r requirements.lock" in dockerfile
    assert "--no-deps --no-build-isolation ." in dockerfile
    assert "python ci/smoke.py --fetch" in dockerfile
