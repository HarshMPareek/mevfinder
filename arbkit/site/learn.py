"""The Learn page: articles are Markdown files in content/learn/ (front matter: title, date, summary), so adding
one is a pull request."""

from __future__ import annotations

import re
from pathlib import Path

from arbkit.chain.spec import ROOT

LEARN_DIR = ROOT / "content" / "learn"
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")


def learn_articles(directory: Path | None = None) -> list[dict]:
    """Articles for the Learn page: content/learn/<slug>.md, each with a front matter block of `key: value` lines
    (title, date, summary) between `---` lines. Newest date first. Only plain slugs are served (no paths)."""
    directory = directory or LEARN_DIR
    out = []
    for path in sorted(directory.glob("*.md")) if directory.exists() else []:
        if not SLUG.match(path.stem):
            continue
        text = path.read_text()
        meta: dict[str, str] = {}
        if text.startswith("---\n") and "\n---" in text[4:]:
            head, text = text[4:].split("\n---", 1)
            text = text.lstrip("-").lstrip("\n")
            for line in head.splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    meta[key.strip().lower()] = value.strip()
        out.append(
            {
                "slug": path.stem,
                "title": meta.get("title", path.stem.replace("-", " ").capitalize()),
                "date": meta.get("date", ""),
                "summary": meta.get("summary", ""),
                "markdown": text,
            }
        )
    return sorted(out, key=lambda a: a["date"], reverse=True)
