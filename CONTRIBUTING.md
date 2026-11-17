# Contributing

mevfinder.com is built from this repository: a merged pull request goes live by itself once the tests and the
maintainers' checks pass. Thank you for helping.

## What you can change, and where

| You want to | Change | Check with |
|---|---|---|
| add a DEX / venue the classifier does not know | `chains/<chain>/dex.toml` (factory or pool manager, swap event signature, probe call) | `./arb -c <chain> verify-registry` |
| add a priced token or a known payee (builder, kickback, settlement) | `chains/<chain>/chain.toml` | `./arb -c <chain> scan <block>` on a block that has it |
| decode a new swap event | `mevlab/decode/swaps.py` (+ its signature in `signatures.py`) | a test in `tests/mevlab/test_decode.py` |
| fix a classification rule | the rule where it lives (`mevlab/receipt`, `mevlab/trace`, `arbkit/scan`) and `docs/METHOD.md` | a test built from the transaction's receipt and trace, in `tests/` |
| write a Learn article | `content/learn/<slug>.md` (see [its README](content/learn/README.md)) | `./arb ui`, then `/learn/<slug>` |
| change a page of the site | `ui/` (plain JS and CSS, no build step) and `arbkit/site/` | `./arb ui` |

## Rules for classifier changes

- **Fix the rule, not the row.** A wrong label is fixed where the rule is defined, in a way that holds whatever the
  token or protocol does. No per-transaction exceptions, no tags added on the output.
- **Order fills and settlements never count.** A transaction that executes someone else's order is not an arbitrage,
  whatever the filler keeps.
- **Exact amounts.** Gross comes from the receipt's transfers and the trace; it must be reproducible from raw receipts.
- **No false positives over recall.** When a case is unclear, the row is held for review, not counted.
- **Every fix comes with a test** that reproduces the transaction from its recorded receipt and trace (`tests/fixtures/`).

Maintainers also re-run every hand-verified transaction on all six chains before a classifier change is merged.

## Development

```sh
uv sync
uv run ruff check . && uv run ruff format --check .
uv run pytest
```

Code style: `ruff format` (line length 110), absolute imports, one job per module, a docstring that says what a module
reads and what it produces. Comments explain why a rule exists, with the transaction that taught it.

## Pull requests

- One topic per pull request; describe what changes on the site, if anything.
- Classifier changes: list the transactions whose label changes, before and after.
- Never commit RPC URLs, keys or `.env` files.

## For maintainers

`main` is protected by the ruleset in [`.github/rulesets/protect-main.json`](.github/rulesets/protect-main.json): no
force pushes, no deletion, every change through a pull request, CI (`test`) green on an up-to-date branch, review
threads resolved. Apply or update it with
`gh api -X POST repos/HarshMPareek/mevfinder/rulesets --input .github/rulesets/protect-main.json`
(rulesets need a public repository or GitHub Pro).
