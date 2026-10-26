import re
from pathlib import Path

from karaqueue import settings

ROOT = Path(__file__).resolve().parents[1]
DOCS = [ROOT / "README.md", ROOT / "CONTRIBUTING.md", ROOT / "SECURITY.md", *sorted((ROOT / "docs").glob("*.md"))]


def test_every_setting_is_documented_and_in_the_example_env():
    keys = [s[0] for s in settings.SETTINGS] + [key for key, _ in settings.STARTUP] + ["admin_password"]
    reference = (ROOT / "docs" / "configuration.md").read_text()
    example = (ROOT / ".env.example").read_text()
    missing = [settings.env_name(k) for k in keys if "| `" + settings.env_name(k) + "`" not in reference]
    assert missing == [], "add these to the table in docs/configuration.md"
    assert [k for k in keys if settings.env_name(k) not in example] == [], "add these to .env.example"


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
