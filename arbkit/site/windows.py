"""The site's time windows (24 hours, 3 days, 7 days) and the per-chain summary over the default window."""

from __future__ import annotations

LIVE_WINDOW_DAYS = 7  # the site's default rolling window over the live stores, in days
WINDOWS = {
    "24h": 24,
    "3d": 72,
    "7d": 168,
}  # the windows a visitor can pick (rolling, from the top of an hour)


def window_hours(query: dict, default: int | None = LIVE_WINDOW_DAYS * 24) -> int | None:
    """window=24h|3d|7d -> hours; days=N -> N x 24 (older links); window=all or nothing given -> `default`."""
    w = query.get("window", "")
    if w in WINDOWS:
        return WINDOWS[w]
    if w == "all":
        return None
    raw = query.get("days", "")
    if raw == "all":
        return None
    if raw.isdigit() and 0 < int(raw) <= 31:
        return int(raw) * 24
    return default


def chain_summary(chain) -> dict:
    """A chain's summary for the chain list: the site's rolling window for a live store, everything for saved reports."""
    return chain.summary(hours=LIVE_WINDOW_DAYS * 24) if hasattr(chain, "daily_for") else chain.summary()
