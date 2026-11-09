import re
from pathlib import Path

import pytest

from karaqueue import settings
from karaqueue.store import Store

ROOT = Path(__file__).resolve().parents[1]
DOCS = [ROOT / "README.md", ROOT / "CONTRIBUTING.md", ROOT / "SECURITY.md", ROOT / "AGENTS.md", *sorted((ROOT / "docs").glob("*.md"))]


def test_every_setting_is_documented_and_in_the_example_env():
    keys = [s[0] for s in settings.SETTINGS] + [key for key, _ in settings.STARTUP] + ["admin_password"]
    reference = (ROOT / "docs" / "configuration.md").read_text()
    example = (ROOT / ".env.example").read_text()
    missing = [settings.env_name(k) for k in keys if "| `" + settings.env_name(k) + "`" not in reference]
    assert missing == [], "add these to the table in docs/configuration.md"
    assert [k for k in keys if settings.env_name(k) not in example] == [], "add these to .env.example"


def test_readme_puts_a_complete_quick_start_before_the_feature_tour():
    text = (ROOT / "README.md").read_text()
    assert text.index("## Quick start") < text.index("## Configuration") < text.index("## What a night looks like")
    quick_start = text.split("## Quick start", 1)[1].split("## Configuration", 1)[0]
    for step in ("Docker", "Compose v2", "Git", "git clone", "cp .env.example .env",
                 "KARAQUEUE_ADMIN_PASSWORD=", "docker compose up -d", "http://localhost:8000"):
        assert step in quick_start, step
    assert quick_start.index("KARAQUEUE_ADMIN_PASSWORD=") < quick_start.index("docker compose up -d")


def test_compose_supports_older_v2_and_defaults_to_persistent_media_folders():
    text = (ROOT / "compose.yaml").read_text()
    # A copied .env is required; optional-file objects need Compose 2.24+.
    env_file = re.search(r"(?ms)^    env_file:\n(.*?)(?=^    \w|\Z)", text).group(1)
    assert env_file.strip() == "- .env"
    defaults = dict(re.findall(r"^      KARAQUEUE_(\w+): \$\{KARAQUEUE_\1-(.+)\}$", text, re.M))
    assert defaults == {"MEDIA_DIR": "/media", "DOWNLOADS_DIR": "/data/downloads",
                        "RECORDINGS_DIR": "/data/recordings"}
    assert "karaqueue-data:/data" in text
    assert "./songs:/media:ro" in text


@pytest.mark.parametrize("key,default", [("media_dir", "/media"), ("downloads_dir", "/data/downloads"),
                                       ("recordings_dir", "/data/recordings")])
@pytest.mark.parametrize("value", [None, "", "/custom/folder"], ids=["unset", "disabled", "custom"])
def test_compose_folder_defaults_preserve_explicit_values(key, default, value):
    text = (ROOT / "compose.yaml").read_text()
    name = settings.env_name(key)
    expression = re.search(rf"^      {name}: (.+)$", text, re.M).group(1)
    # Assert Compose's unset-only operator, not ':-' which also replaces empty values.
    assert expression == "${" + name + "-" + default + "}"
    env = {} if value is None else {name: value}
    # The '-' contract is dict.get: use the default only when the key is absent.
    resolved = env.get(name, default)
    config = settings.Settings(Store(), env={name: resolved})
    assert config.get(key) == (default if value is None else value)
    if value == "":
        assert not config.get(key)  # An explicitly empty folder disables the feature.


def anchors(doc):
    # GitHub's heading ids: lowercase, punctuation dropped, spaces to hyphens.
    return {re.sub(r"[^\w\- ]", "", line.lstrip("#").strip().lower()).replace(" ", "-")
            for line in doc.read_text().splitlines() if line.startswith("#")}


def test_docs_have_no_em_dashes_and_their_links_go_somewhere():
    for doc in DOCS:
        text = doc.read_text()
        assert "\u2014" not in text, doc
        for target in [part.split(")")[0] for part in text.split("](")[1:]]:
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            path, _, anchor = target.partition("#")
            linked = (doc.parent / path) if path else doc
            assert linked.exists(), (doc.name, target)
            if anchor and linked.suffix == ".md":
                assert anchor in anchors(linked), (doc.name, target)
