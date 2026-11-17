"""JSON files of the block cache and reports: written atomically, gzip-compressed when the name ends in .gz."""

from __future__ import annotations

import gzip
import json
from pathlib import Path


def write_json(path: Path, value, level: int = 9):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    if path.suffix == ".gz":
        with gzip.open(tmp, "wt", compresslevel=level) as f:
            json.dump(value, f, separators=(",", ":"))
    else:
        tmp.write_text(json.dumps(value, indent=2) + "\n")
    tmp.replace(path)


def read_json(path):
    if path.suffix == ".gz":
        with gzip.open(path, "rt") as f:
            return json.load(f)
    return json.loads(path.read_text())
