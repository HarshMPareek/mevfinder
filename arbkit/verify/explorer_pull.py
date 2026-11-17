"""Pull explorer copies of classified transactions into chains/<chain>/validation/explorer/<hash>.json.

The copy is the compact shape `explorer.parse_blockscout` reads ({"compact": {"s", "b", "f", "from", "to", "t", "i",
"more"}}, raw integer amounts), whatever the explorer behind it:

- Blockscout API v2 (`[explorer] blockscout`): /api/v2/transactions/<hash>, /token-transfers and
  /internal-transactions, every page followed (`next_page_params`) so `more` is false. `blockscout_access = "direct"`
  is plain HTTPS; `"browser"` runs the same requests from a same-origin headless Chrome tab (`cdp_fetch.mjs`) for the
  hosts that answer plain clients with a Cloudflare JS challenge. A challenge that does not clear by itself is
  recorded as `challenge`, never clicked.
- NodeReal (`[explorer] nodereal`, the API behind bsctrace.com): tx/getDetail (status, fee, counterparties, the ERC-20
  transfer list) and tx/getInternalTxList (every page of 100). Unlike the rendered page there is no 20-row cap and
  the amounts are raw, so a BSC copy can be exact; a page-text copy of the same hash is moved to explorer/superseded/.

Which hashes: the rows of one or more hand-review sheets (`--sheet`), explicit hashes, `--refetch-partial` (copies
whose last `explorer-check` was partial because the explorer had not indexed internal transfers yet) and
`--upgrade-text` (page-text copies to re-pull as API copies). Nothing here chooses rows: a copy makes a row "covered"
for `handreview`, so copies are pulled after a sheet is drawn, never before.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from arbkit.chain.spec import ChainSpec

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
DEFAULT_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
REPULLABLE = re.compile(
    r"not indexed internal transfers|lists \d+ of \d+ internal transfers|first \d+ of \d+|share-based token",
    re.I,
)
HASH = re.compile(r"^0x[0-9a-f]{64}$")


# --------------------------------------------------------------------------------------------- fetchers


class DirectFetcher:
    """Plain HTTPS with a browser user agent; follows redirects (optimism.blockscout.com -> explorer.optimism.io)."""

    name = "direct"

    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout

    def get(self, url: str) -> tuple[int, str]:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace") if exc.fp else ""
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return 0, str(exc)

    def close(self) -> None:
        pass


class BrowserFetcher:
    """Same-origin fetches from a headless Chrome tab driven over the DevTools protocol by `cdp_fetch.mjs`."""

    name = "browser"

    def __init__(self, origin: str, chrome: str | None = None, node: str = "node"):
        helper = Path(__file__).with_name("cdp_fetch.mjs")
        self.origin = origin
        self.proc = subprocess.Popen(
            [node, str(helper), origin, chrome or DEFAULT_CHROME],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        first = self.proc.stdout.readline()
        self.hello = (
            json.loads(first) if first.strip() else {"ready": False, "probe": "helper produced no output"}
        )
        self.ready = bool(self.hello.get("ready"))
        self._id = 0

    def get(self, url: str) -> tuple[int, str]:
        if not self.ready or self.proc.poll() is not None:
            return 0, "browser tab not ready"
        self._id += 1
        self.proc.stdin.write(json.dumps({"id": self._id, "url": url}) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            return 0, "helper closed"
        reply = json.loads(line)
        return int(reply.get("status") or 0), reply.get("body") or ""

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=15)
        except Exception:
            self.proc.kill()


def _json(status: int, body: str) -> dict | None:
    if status != 200:
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


# --------------------------------------------------------------------------------------------- Blockscout


def blockscout_pages(get, url: str, pause: float = 0.0) -> tuple[list[dict], bool, int]:
    """Every item of a paginated Blockscout list endpoint; (items, complete, last status)."""
    items: list[dict] = []
    params: dict | None = None
    status = 0
    for _ in range(50):
        page_url = url + (
            "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}) if params else ""
        )
        status, body = get(page_url)
        data = _json(status, body)
        if data is None:
            return items, False, status
        items.extend(data.get("items") or [])
        params = data.get("next_page_params")
        if not params:
            return items, True, status
        if pause:
            time.sleep(pause)
    return items, False, status


def compact_blockscout(tx: dict, transfers: list[dict], internal: list[dict], complete: bool) -> dict:
    t = []
    for x in transfers:
        total, token = x.get("total") or {}, x.get("token") or {}
        if total.get("value") is None:
            continue
        t.append(
            [
                (x.get("from") or {}).get("hash"),
                (x.get("to") or {}).get("hash"),
                token.get("address_hash") or token.get("address"),
                token.get("symbol"),
                str(total.get("decimals") or token.get("decimals") or "18"),
                str(total["value"]),
                x.get("type"),
            ]
        )
    i = []
    for x in internal:
        try:
            value = int(x.get("value") or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0 and not x.get("error"):
            i.append([(x.get("from") or {}).get("hash"), (x.get("to") or {}).get("hash"), str(value)])
    fee = tx.get("fee") or {}
    return {
        "s": tx.get("status"),
        "b": tx.get("block_number", tx.get("block")),
        "f": str(fee["value"]) if fee.get("value") is not None else None,
        "from": (tx.get("from") or {}).get("hash"),
        "to": (tx.get("to") or {}).get("hash"),
        "t": t,
        "i": i,
        "more": not complete,
        "internal_items": len(internal),
    }


def pull_blockscout(get, base: str, tx_hash: str, pause: float = 0.0) -> tuple[dict | None, str | None]:
    """(compact copy, None) or (None, reason)."""
    root = f"{base.rstrip('/')}/api/v2/transactions/{tx_hash}"
    status, body = get(root)
    if status == 403 and re.search(r"just a moment|challenge", body, re.I):
        return None, "challenge"
    tx = _json(status, body)
    if tx is None:
        return None, f"transaction endpoint http {status}"
    if pause:
        time.sleep(pause)
    transfers, complete_t, st = blockscout_pages(get, root + "/token-transfers", pause)
    if not complete_t and not transfers:
        return None, f"token-transfers http {st}"
    if pause:
        time.sleep(pause)
    internal, complete_i, st = blockscout_pages(get, root + "/internal-transactions", pause)
    if not complete_i and not internal:
        return None, f"internal-transactions http {st}"
    return compact_blockscout(tx, transfers, internal, complete_t and complete_i), None


# --------------------------------------------------------------------------------------------- NodeReal (BSCTrace)


def compact_nodereal(detail: dict, internal: list[dict], complete: bool) -> dict:
    spec = detail.get("ethereumSpecific") or {}
    sender, callee = (detail.get("vin") or {}).get("address"), (detail.get("vout") or {}).get("address")
    t = []
    for x in detail.get("tokenTransfers") or []:
        if str(x.get("category")) != "20" or x.get("value") is None:
            continue
        t.append(
            [
                x.get("from"),
                x.get("to"),
                x.get("token"),
                x.get("symbol") or x.get("tokenName"),
                str(x.get("decimal") if x.get("decimal") is not None else "18"),
                str(int(x["value"])),
                "token_transfer",
            ]
        )
    own_value = int(detail.get("value") or 0)
    i = []
    for x in internal:
        raw = x.get("value") or "0"
        value = int(raw, 16) if isinstance(raw, str) and raw.startswith("0x") else int(raw)
        if value <= 0:
            continue
        if (
            own_value
            and str(x.get("from", "")).lower() == str(sender or "").lower()
            and str(x.get("to", "")).lower() == str(callee or "").lower()
            and value == own_value
            and x.get("traceIndex") in (0, None)
        ):
            continue  # the transaction's own value, shown as "Value" by explorers
        i.append([x.get("from"), x.get("to"), str(value)])
    return {
        "s": "ok" if spec.get("receiptsStatus") == 1 else "error",
        "b": detail.get("blockNumber"),
        "f": str(int(detail["fees"])) if detail.get("fees") is not None else None,
        "from": sender,
        "to": callee,
        "t": t,
        "i": i,
        "more": not complete,
        "internal_items": len(internal),
    }


def pull_nodereal(
    get, base: str, tx_hash: str, pause: float = 0.0, page_size: int = 100
) -> tuple[dict | None, str | None]:
    status, body = get(f"{base.rstrip('/')}/tx/getDetail?hash={tx_hash}")
    data = _json(status, body)
    if data is None or data.get("code") not in (0, None) or not data.get("data"):
        return None, f"getDetail http {status} code {data.get('code') if data else None}"
    detail = data["data"]
    internal: list[dict] = []
    complete = False
    for page in range(1, 60):
        if pause:
            time.sleep(pause)
        status, body = get(
            f"{base.rstrip('/')}/tx/getInternalTxList?page={page}&pageSize={page_size}&hash={tx_hash}"
        )
        data = _json(status, body)
        if data is None or data.get("code") not in (0, None):
            return None, f"getInternalTxList http {status} code {data.get('code') if data else None}"
        chunk = (data.get("data") or {}).get("list") or []
        internal.extend(chunk)
        total = (data.get("data") or {}).get("total")
        if len(chunk) < page_size or (total is not None and len(internal) >= int(total)):
            complete = True
            break
    return compact_nodereal(detail, internal, complete), None


# --------------------------------------------------------------------------------------------- selection and saving


def sheet_hashes(sheet: Path) -> list[str]:
    data = json.loads(Path(sheet).read_text())
    entries = data["entries"] if isinstance(data, dict) else data
    return [str(e["tx_hash"]).lower() for e in entries if isinstance(e, dict) and e.get("tx_hash")]


def partial_hashes(validation_dir: Path) -> list[str]:
    """Hashes whose newest explorer check was partial for a reason another pull can fix."""
    checks = sorted(validation_dir.glob("explorer_check_*.json"))
    if not checks:
        return []
    try:
        data = json.loads(checks[-1].read_text())
    except ValueError:
        return []
    out = []
    for r in data.get("results", []):
        if r.get("verdict") == "partial" and any(REPULLABLE.search(p) for p in r.get("partial", [])):
            out.append(str(r["tx_hash"]).lower())
    return out


def text_copy_hashes(explorer_dir: Path) -> list[str]:
    return sorted(p.stem.lower() for p in explorer_dir.glob("0x*.txt"))


def save_copy(explorer_dir: Path, tx_hash: str, compact: dict, source: str) -> Path:
    explorer_dir.mkdir(parents=True, exist_ok=True)
    path = explorer_dir / f"{tx_hash}.json"
    path.write_text(
        json.dumps(
            {"compact": compact, "pulled_at": datetime.now(timezone.utc).isoformat(), "source": source}
        )
    )
    text = explorer_dir / f"{tx_hash}.txt"
    if text.exists():
        superseded = explorer_dir / "superseded"
        superseded.mkdir(exist_ok=True)
        shutil.move(str(text), str(superseded / text.name))
    return path


def make_fetcher(spec: ChainSpec, access: str | None = None, chrome: str | None = None):
    pull = spec.explorer_pull or {}
    if pull.get("nodereal"):
        return DirectFetcher(), "nodereal", pull["nodereal"]
    if pull.get("blockscout"):
        mode = access or pull.get("blockscout_access") or "direct"
        if mode == "browser":
            return BrowserFetcher(pull["blockscout"], chrome), "blockscout", pull["blockscout"]
        return DirectFetcher(), "blockscout", pull["blockscout"]
    raise ValueError(
        f"{spec.name}: no explorer API declared in chain.toml [explorer] (blockscout = ... or nodereal = ...)."
    )


def pull(
    spec: ChainSpec,
    hashes: list[str],
    explorer_dir: Path | None = None,
    access: str | None = None,
    pause: float = 0.3,
    log=print,
    fetcher=None,
    kind: str | None = None,
    base: str | None = None,
) -> dict:
    """Pull every hash; returns {"pulled": [...], "failed": {hash: reason}, "source": ...}."""
    explorer_dir = explorer_dir or spec.directory / "validation" / "explorer"
    if fetcher is None:
        fetcher, kind, base = make_fetcher(spec, access)
    source = f"{base} ({kind}, {fetcher.name})"
    result = {"source": source, "pulled": [], "failed": {}, "ready": getattr(fetcher, "ready", True)}
    try:
        if not result["ready"]:
            hello = getattr(fetcher, "hello", {})
            reason = "challenge" if hello.get("challenge") else f"browser tab not ready: {hello.get('probe')}"
            result["failed"] = {h: reason for h in hashes}
            log(f"{spec.name}: {reason}; nothing pulled")
            return result
        for n, h in enumerate(hashes, 1):
            h = h.lower()
            if not HASH.match(h):
                result["failed"][h] = "not a transaction hash"
                continue
            if kind == "nodereal":
                compact, reason = pull_nodereal(fetcher.get, base, h, pause)
            else:
                compact, reason = pull_blockscout(fetcher.get, base, h, pause)
            if compact is None:
                result["failed"][h] = reason
                log(f"  {n}/{len(hashes)} {h[:12]}… NOT FETCHED: {reason}")
                if reason == "challenge":
                    for rest in hashes[n:]:
                        result["failed"][rest.lower()] = "challenge"
                    break
            else:
                save_copy(explorer_dir, h, compact, source)
                result["pulled"].append(h)
                log(
                    f"  {n}/{len(hashes)} {h[:12]}… transfers {len(compact['t'])} internal {len(compact['i'])}{' (more pages not saved)' if compact.get('more') else ''}"
                )
            if pause:
                time.sleep(pause)
    finally:
        fetcher.close()
    return result
