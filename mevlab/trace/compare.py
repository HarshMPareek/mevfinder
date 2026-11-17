"""Event layer vs trace layer for one transaction: agree, or say how they disagree."""

from __future__ import annotations

import json
from typing import Any

from mevlab.decode.signatures import NATIVE_ETH
from mevlab.trace.model import CLOSURE_TOL, TraceVerdict


def compare(row: dict[str, Any], tv: TraceVerdict, weth: str) -> dict[str, Any]:
    """Agreement of one label row with the trace verdict (ETH ≡ WETH on both sides)."""
    weth = weth.lower()

    def norm(t: str | None) -> str | None:
        return weth if t and t.lower() == NATIVE_ETH else (t.lower() if t else None)

    label_arb = bool(row.get("is_arb"))
    trace_cycle = tv.verdict == "cycle" or (label_arb and tv.verdict == "gain_only")
    complete = [lg for lg in tv.legs if lg.token_in and lg.token_out]
    gross_label = row.get("gross_trace_raw")
    if gross_label in (None, ""):
        gross_label = row.get("gross_profit_raw")
    tok_label = norm(row.get("profit_token_trace") or row.get("profit_token"))
    try:
        tokens_label = {norm(t) for t in json.loads(row.get("tokens") or "[]")}
    except (TypeError, ValueError):
        tokens_label = set()
    tokens_trace = {norm(t) for t in tv.tokens}
    g_l = int(gross_label) if gross_label not in (None, "") else None
    g_t = tv.gross
    rel = None
    if g_l is not None and g_t is not None and max(abs(g_l), abs(g_t)) > 0:
        rel = abs(g_l - g_t) / max(abs(g_l), abs(g_t))
    ben = (row.get("beneficiary") or "").lower() or None
    return {
        "class_agree": label_arb == trace_cycle,
        "legs_agree": int(row.get("n_legs") or 0) == len(complete),
        "tokens_agree": bool(tokens_label) and tokens_label == tokens_trace,
        "beneficiary_agree": bool(ben) and (ben == tv.taker or ben in tv.group or ben in tv.payouts),
        "profit_token_agree": bool(tok_label) and tok_label == norm(tv.profit_token),
        "gross_rel_diff": rel,
        "gross_agree": rel is not None and rel <= CLOSURE_TOL,
    }
