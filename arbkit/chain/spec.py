"""Chain specifications: everything that differs between chains lives in chains/<name>/chain.toml and dex.toml.

The classifier core (mevlab) is chain-agnostic; it receives the wrapped-native token, the fee family and the
system transaction types from here. Nothing in this module touches the network.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # the repository: arbkit/chain/spec.py -> ../../
CHAINS_DIR = ROOT / "chains"

FAMILIES = {"op-stack", "arbitrum", "ethereum", "bsc"}
ORDERING_POLICIES = {"fee_sorted", "builder", "fcfs"}


@dataclass(frozen=True)
class Token:
    symbol: str
    address: str  # lowercase
    decimals: int
    price: str | None  # "native" | "usd" | Binance symbol such as "BTCUSDT" | None (unpriced)


@dataclass(frozen=True)
class ChainSpec:
    name: str
    chain_id: int
    family: str
    block_time_ms: int
    native_symbol: str
    weth: str  # lowercase wrapped-native token (ETH ≡ WETH equivalence in the classifier)
    ordering_policy: str
    flashblocks_per_block: int
    trigger_window_blocks: int
    system_tx_types: frozenset[int]
    explorer_name: str
    explorer_tx: str
    explorer_address: str
    explorer_block: str
    env_http: str
    env_archive: str
    env_trace: str
    native_price_symbol: str
    tokens: tuple[Token, ...]
    dexes: tuple[dict, ...]
    directory: Path
    known_recipients: dict[str, dict] = None  # address -> {name, kind: builder | kickback | settlement}
    quote_venues: dict[str, dict] = (
        None  # address -> {name, event}: declared RFQ settlement contracts (maker legs count)
    )
    declared_venues: dict[str, dict] = (
        None  # address -> {name}: venues the receipts cannot reveal (a two-contract PSM)
    )
    explorer_pull: dict | None = (
        None  # [explorer] blockscout / blockscout_access / nodereal: where `explorer-pull` fetches copies
    )
    live: dict | None = (
        None  # [live]: follower settings for a local node (log ranges, confirmations, trace window)
    )
    fee_flow: dict | None = (
        None  # [fee_flow]: who receives each fee component on this chain (measured), for the site's labels
    )
    link_original_trigger: bool = (
        False  # [ordering]: same-block trigger search passes own/competing txs (positions.py)
    )

    @property
    def max_log_range(self) -> int | None:
        """Newest-blocks window for a singleton pool lookup by eth_getLogs. None (search from block 0) unless the
        RPC is a local full node (`ARB_LOCAL_NODES=1`), which scans receipts and refuses wide ranges."""
        if not os.environ.get("ARB_LOCAL_NODES"):
            return None
        return int((self.live or {}).get("log_range", 5000))

    def live_setting(self, key: str, default):
        return (
            type(default)((self.live or {}).get(key, default))
            if default is not None
            else (self.live or {}).get(key)
        )

    @property
    def recipient_names(self) -> dict[str, str]:
        return {a: v["name"] for a, v in (self.known_recipients or {}).items()}

    @property
    def venue_aliases(self) -> dict[str, str]:
        """member contract -> declared venue it belongs to (`[declared_venues]` members)."""
        return {m: venue for venue, v in (self.declared_venues or {}).items() for m in v.get("members", [])}

    def recipients_of_kind(self, kind: str) -> set[str]:
        return {a for a, v in (self.known_recipients or {}).items() if v["kind"] == kind}

    def token_symbol(self, address: str | None) -> str | None:
        t = self.token_by_address.get(str(address or "").lower())
        return t.symbol if t else None

    def token_decimals(self, address: str | None) -> int | None:
        t = self.token_by_address.get(str(address or "").lower())
        return t.decimals if t else None

    @property
    def token_by_address(self) -> dict[str, Token]:
        return {t.address: t for t in self.tokens}

    @property
    def factories(self) -> dict[str, str]:
        """factory address (lowercase) -> dex name, for pool -> factory() resolution."""
        return {d["factory"].lower(): d["name"] for d in self.dexes if d.get("factory")}

    @property
    def pool_managers(self) -> dict[str, dict]:
        """singleton pool manager (lowercase) -> registry entry (name, swap_event, init_event)."""
        return {d["pool_manager"].lower(): d for d in self.dexes if d.get("pool_manager")}

    @property
    def blocks_per_day(self) -> int:
        return max(1, round(86_400_000 / self.block_time_ms))

    def explorer_url(self, tx_hash: str) -> str:
        return self.explorer_tx + tx_hash

    def describe(self) -> str:
        return (
            f"{self.name} (chain {self.chain_id}, {self.family}, {self.block_time_ms} ms blocks, "
            f"{self.ordering_policy} ordering, {len(self.dexes)} registry entries)"
        )


def list_chains() -> list[str]:
    if not CHAINS_DIR.exists():
        return []
    return sorted(p.name for p in CHAINS_DIR.iterdir() if (p / "chain.toml").exists())


def _tokens(raw: dict) -> tuple[Token, ...]:
    out = []
    for symbol, value in raw.items():
        if isinstance(value, str):  # legacy "SYMBOL = address" form
            out.append(Token(symbol, value.lower(), 18, None))
            continue
        out.append(
            Token(symbol, str(value["address"]).lower(), int(value.get("decimals", 18)), value.get("price"))
        )
    return tuple(out)


def load_chain(name: str) -> ChainSpec:
    directory = CHAINS_DIR / name
    path = directory / "chain.toml"
    if not path.exists():
        raise ValueError(f"Unknown chain '{name}'. Available: {', '.join(list_chains()) or 'none'}.")
    with path.open("rb") as f:
        cfg = tomllib.load(f)
    chain, ordering = cfg["chain"], cfg.get("ordering", {})
    explorer, endpoints = cfg.get("explorer", {}), cfg.get("endpoints", {})
    family = chain["family"]
    if family not in FAMILIES:
        raise ValueError(f"{name}: unsupported fee family '{family}'; expected one of {sorted(FAMILIES)}.")
    policy = ordering.get("policy", "fee_sorted")
    if policy not in ORDERING_POLICIES:
        raise ValueError(
            f"{name}: unsupported ordering policy '{policy}'; expected one of {sorted(ORDERING_POLICIES)}."
        )
    dexes: tuple[dict, ...] = ()
    dex_path = directory / "dex.toml"
    if dex_path.exists():
        with dex_path.open("rb") as f:
            dexes = tuple(cfg_d for cfg_d in tomllib.load(f).get("dex", []))
    prefix = chain["name"].upper()
    return ChainSpec(
        name=chain["name"],
        chain_id=int(chain["chain_id"]),
        family=family,
        block_time_ms=int(chain.get("block_time_ms", 2000)),
        native_symbol=chain.get("native_symbol", "ETH"),
        weth=str(chain["weth"]).lower(),
        ordering_policy=policy,
        flashblocks_per_block=int(ordering.get("flashblocks_per_block", 0)),
        trigger_window_blocks=int(ordering.get("trigger_window_blocks", 0)),
        link_original_trigger=bool(ordering.get("link_original_trigger", False)),
        system_tx_types=frozenset(int(t) for t in cfg.get("system", {}).get("tx_types", [])),
        explorer_name=explorer.get("name", name),
        explorer_tx=explorer.get("tx", ""),
        explorer_address=explorer.get("address", ""),
        explorer_block=explorer.get("block", ""),
        explorer_pull={
            k: explorer[k] for k in ("blockscout", "blockscout_access", "nodereal", "page") if k in explorer
        },
        env_http=endpoints.get("http", f"{prefix}_HTTP_URL"),
        env_archive=endpoints.get("archive", f"{prefix}_ARCHIVE_URL"),
        env_trace=endpoints.get("trace", f"{prefix}_TRACE_URL"),
        native_price_symbol=cfg.get("pricing", {}).get("native_symbol", "ETHUSDT"),
        tokens=_tokens(cfg.get("tokens", {})),
        dexes=dexes,
        directory=directory,
        known_recipients={
            str(k).lower(): {"name": v.get("name", k), "kind": v.get("kind", "kickback")}
            for k, v in cfg.get("known_recipients", {}).items()
        },
        quote_venues={
            str(k).lower(): {"name": v.get("name", k), "event": v.get("event")}
            for k, v in cfg.get("quote_venues", {}).items()
        },
        declared_venues={
            str(k).lower(): {
                "name": v.get("name", k),
                "members": [str(m).lower() for m in v.get("members", [])],
            }
            for k, v in cfg.get("declared_venues", {}).items()
        },
        live=dict(cfg.get("live", {})),
        fee_flow={str(k): str(v) for k, v in cfg.get("fee_flow", {}).items()},
    )
