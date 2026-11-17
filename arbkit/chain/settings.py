from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from arbkit.chain.spec import ROOT, ChainSpec, load_chain

DEFAULT_CHAIN = "base"


@dataclass
class Settings:
    chain: ChainSpec
    http_url: str
    trace_url: str
    data: Path
    reports: Path

    @classmethod
    def load(
        cls, chain: str | ChainSpec | None = None, data: Path | None = None, reports: Path | None = None
    ):
        load_dotenv(ROOT / ".env", override=False)
        spec = (
            chain
            if isinstance(chain, ChainSpec)
            else load_chain(chain or os.environ.get("ARB_CHAIN") or DEFAULT_CHAIN)
        )
        url = os.environ.get(spec.env_archive) or os.environ.get(spec.env_http)
        if not url:
            raise ValueError(
                f"Set {spec.env_http} (and ideally {spec.env_archive}) in .env (the names are in chains/<chain>/chain.toml [endpoints])."
            )
        return cls(
            spec,
            url,
            os.environ.get(spec.env_trace) or url,
            data or ROOT / "data" / spec.name,
            reports or ROOT / "reports" / spec.name,
        )
