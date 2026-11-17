"""arbkit: the application around the classifier core (mevlab).

chain/    one chain's settings (chains/<chain>/*.toml), RPC client, registry checks, measured chain facts
scan/     classify blocks: download, event layer + trace layer per transaction, venues, sandwiches, families
value/    prices (Binance 1-minute), fees, proceeds, builder economics, per-day accounting
live/     the follower that classifies every new block into a SQLite store, and the store itself
site/     the website and JSON API (mevfinder.com) over the live stores or saved reports
verify/   checks of the classifier: hand labels and review, audits, recall screens, explorer copies, evidence
cli/      the `arb` command line
"""
