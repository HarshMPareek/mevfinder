"""What the trace layer works with: value movements, venue legs, flash loans and the verdict it returns."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

CLOSURE_TOL = 0.01
EXACT_DUST_SHARE = 1e-8  # receipt rounding (a DAI/USDC pool returning 6e-7 DAI less on a 14,248 DAI leg) does not open a loss
DEAD_ADDRESS = (
    "0x000000000000000000000000000000000000dead"  # the conventional burn sink next to the zero address
)


@dataclass
class Movement:
    token: str
    sender: str
    receiver: str
    amount: int
    order: int  # pre-order index of the frame in the trace
    kind: str  # transfer | transferFrom | native
    via: str | None = None  # declared quote venue (RFQ settlement) whose frame executed this transferFrom


@dataclass
class Leg:
    venue: str
    token_in: str | None = None
    amount_in: int = 0
    token_out: str | None = None
    amount_out: int = 0
    order: int = 0


@dataclass
class TraceVerdict:
    verdict: str  # cycle | single_venue_loop | gain_only | no_venue | no_taker | reverted | empty
    n_movements: int = 0
    venues: list[str] = field(default_factory=list)
    legs: list[Leg] = field(default_factory=list)
    tokens: list[str] = field(default_factory=list)
    taker: str | None = None
    profit_token: str | None = None
    gross: int | None = None  # taker's gain before payouts
    net_after: int | None = None
    payouts: dict[str, int] = field(default_factory=dict)  # recipient -> amount, in the profit token
    payouts_by_token: dict[str, dict[str, int]] = field(default_factory=dict)  # token -> recipient -> amount
    notes: list[str] = field(default_factory=list)
    group: list[str] = field(default_factory=list)  # the operator: callee + helpers/executors
    unclosed_candidates: list[dict[str, Any]] = field(default_factory=list)
    asset_deltas: dict[str, int] = field(default_factory=dict)  # signed venue flows, before payouts
    group_asset_deltas: dict[str, int] = field(default_factory=dict)
    cycle_path: list[dict[str, Any]] = field(default_factory=list)
    venue_terminal_receipts: list[dict[str, Any]] = field(default_factory=list)
    extra_gains: dict[str, int] = field(default_factory=dict)  # other tokens the group ended positive in
    flash_loans: list = field(default_factory=list)  # FlashLoan(lender, token, principal, repaid)
    other_parties: list[dict[str, Any]] = field(default_factory=list)  # closed gains of unrelated operators
    entry_token: str | None = (
        None  # first asset the operator group paid into a venue (None: route not self-funded)
    )
    redirects: list[str] = field(
        default_factory=list
    )  # collectors paid by a venue directly, folded into the group
    protocol_payments: dict[str, dict[str, int]] = field(
        default_factory=dict
    )  # token -> contract -> amount paid out, not proceeds
    # token -> every address the group paid, before the part funded from its own balance was taken off (the audit adds
    # all of them back: a recipient whose whole payment came from inventory is still a payee, not a loss)
    payout_recipients: dict[str, list[str]] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["legs"] = [
            {**lg, "amount_in": str(lg["amount_in"]), "amount_out": str(lg["amount_out"])} for lg in d["legs"]
        ]
        d["gross"] = None if self.gross is None else str(self.gross)
        d["net_after"] = None if self.net_after is None else str(self.net_after)
        d["payouts"] = {k: str(v) for k, v in self.payouts.items()}
        d["payouts_by_token"] = {
            t: {k: str(v) for k, v in d_.items()} for t, d_ in self.payouts_by_token.items()
        }
        d["protocol_payments"] = {
            t: {k: str(v) for k, v in d_.items()} for t, d_ in self.protocol_payments.items()
        }
        d["asset_deltas"] = {k: str(v) for k, v in self.asset_deltas.items()}
        d["group_asset_deltas"] = {k: str(v) for k, v in self.group_asset_deltas.items()}
        d["extra_gains"] = {k: str(v) for k, v in self.extra_gains.items()}
        d["flash_loans"] = [
            {
                "lender": x.lender,
                "token": x.token,
                "principal": str(x.principal),
                "repaid": str(x.repaid),
                "fee": str(x.fee),
            }
            for x in self.flash_loans
        ]
        d["other_parties"] = [
            {
                **o,
                "gross": str(o["gross"]),
                "payouts": {t: {k: str(v) for k, v in d_.items()} for t, d_ in o["payouts"].items()},
            }
            for o in self.other_parties
        ]
        return d


@dataclass
class FlashLoan:
    lender: str
    token: str
    principal: int
    repaid: int

    @property
    def fee(self) -> int:
        return self.repaid - self.principal
