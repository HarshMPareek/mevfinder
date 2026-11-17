from __future__ import annotations


def to_int(x: str | int | None) -> int:
    if x is None:
        return 0
    if isinstance(x, int):
        return x
    return int(x, 16) if x.startswith("0x") else int(x)


def addr(x: str) -> str:
    """Normalise to lowercase 0x-prefixed 20-byte address (accepts 32-byte topic form)."""
    h = x.lower()
    if h.startswith("0x"):
        h = h[2:]
    return "0x" + h[-40:].rjust(40, "0")
