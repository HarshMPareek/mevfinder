"""Search-engine side of the site (MEV Finder): real page URLs (/base, /base/txs, /contact) served as the app's
index.html with that page's title, description, canonical URL, social tags and structured data in the head, and a short
server-rendered summary with the live numbers in <main> (crawlers read it; the app replaces it on load). Plus
robots.txt and sitemap.xml. Of the detail pages only bots with counted cycles in the window are indexed and listed in
the sitemap (each has its own numbers); transactions, blocks and unknown addresses stay noindex. The canonical host is ARB_SITE_URL (default https://mevfinder.com) whatever host served
the request, so the bare IP never competes with the domain."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from html import escape
from pathlib import Path

CONTACT_EMAIL = "info@mevfinder.com"
NAMES = {
    "ethereum": "Ethereum",
    "base": "Base",
    "bsc": "BSC",
    "arbitrum": "Arbitrum",
    "optimism": "Optimism",
    "robinhood": "Robinhood Chain",
}
LONG_NAMES = {
    "bsc": "BNB Smart Chain (BSC)",
    "optimism": "Optimism (OP Mainnet)",
    "robinhood": "Robinhood Chain",
}
SUBPAGES = {
    "txs": (
        "MEV Transactions: Arbitrage & Sandwich Feed",
        "MEV transactions: every arbitrage cycle, sandwich, JIT bundle and liquidation",
    ),
    "blocks": (
        "Blocks with MEV",
        "blocks with MEV: which transactions extracted value and who built each block",
    ),
    "bots": (
        "MEV Bots: Top Arbitrage Searchers",
        "MEV bots: the searchers and contracts behind the arbitrage, ranked by profit",
    ),
}
DETAIL = {"tx", "block", "bot"}
WINDOW_DAYS = 7
BOT_TTL = 900.0  # seconds a chain's bot ranking is reused for bot pages and the sitemap
BOTS_IN_SITEMAP = 250  # per chain: the window's top bots by gross
ADDRESS = re.compile(r"0x[0-9a-f]{40}")
_bot_rankings: dict[tuple[int, str], tuple[float, list[dict], dict[str, tuple[int, dict]]]] = {}


def site_url() -> str:
    return os.environ.get("ARB_SITE_URL", "https://mevfinder.com").rstrip("/")


def name(chain: str) -> str:
    return NAMES.get(chain, chain.replace("-", " ").title())


def _money(x) -> str:
    return f"${x:,.0f}" if x is not None and abs(x) >= 100 else f"${(x or 0):,.2f}"


def _summary(store, chain: str) -> dict:
    data = store.get(chain)
    try:
        return data.summary(WINDOW_DAYS)  # live store: the rolling window
    except TypeError:
        return data.summary()  # saved reports


def window(s: dict) -> tuple[str, str]:
    """(short, long) wording of what the figures cover: "7 days" / "in the last 7 days" only when the live data really
    reaches back that far with nothing missing, otherwise when it starts ("since Oct 31" / "since Oct 31 20:55 UTC")."""
    cov = s.get("coverage") or {}
    if cov.get("complete") or not cov.get("live_since"):
        return f"{WINDOW_DAYS} days", f"in the last {WINDOW_DAYS} days"
    t = datetime.strptime(cov["from"], "%Y-%m-%dT%H:%M:%SZ")
    return f"since {t:%b} {t.day}", f"since {t:%b} {t.day} {t:%H:%M} UTC"


def bots(store, chain: str) -> tuple[list[dict], dict[str, tuple[int, dict]]]:
    """The chain's bots with counted cycles in the window, best first, and the same by address as (rank, bot). It is the
    ranking the bots page shows, read at most once per BOT_TTL per chain here, so a crawler walking thousands of bot
    pages costs one ranking query per chain per quarter hour. The last ranking is kept when the store cannot be read."""
    key = (id(store), chain)
    hit = _bot_rankings.get(key)
    now = time.monotonic()
    if hit and hit[0] > now:
        return hit[1], hit[2]
    data = store.get(chain)
    try:
        if hasattr(data, "operators_ranked"):  # live store: the rolling window
            rows = data.operators_ranked(WINDOW_DAYS * 24)
        elif hasattr(data, "operators"):  # saved reports
            rows = data.operators(limit=10**6)
        else:
            rows = []
    except Exception:  # noqa: BLE001 - a ranking that cannot be read never takes a page or the sitemap down
        if hit:
            return hit[1], hit[2]
        rows = []
    ranked = [
        r
        for r in rows
        if ADDRESS.fullmatch(str(r.get("address") or "").lower()) and (r.get("cycles") or 0) > 0
    ]
    by_address = {r["address"].lower(): (i, r) for i, r in enumerate(ranked, 1)}
    _bot_rankings[key] = (now + BOT_TTL, ranked, by_address)
    return ranked, by_address


def _org() -> dict:
    return {
        "@type": "Organization",
        "@id": f"{site_url()}/#org",
        "name": "MEV Finder",
        "url": f"{site_url()}/",
        "logo": f"{site_url()}/logo-256.png",
        "email": CONTACT_EMAIL,
        "contactPoint": {"@type": "ContactPoint", "email": CONTACT_EMAIL, "contactType": "customer support"},
    }


def _chain_line(store, chain: str) -> str:
    s = _summary(store, chain)
    return (
        f'<li><a href="/{chain}">{escape(name(chain))} MEV</a>: {int(s.get("cycles") or 0):,} arbitrage cycles, '
        f"{_money(s.get('gross_usd'))} gross, {_money(s.get('net_usd'))} kept by searchers, "
        f"{int(s.get('sandwiches') or 0):,} sandwiches ({window(s)[1]})</li>"
    )


def page_for(path: str, store) -> dict | None:
    """The page a URL path names, or None when it is not a page (static files, API)."""
    parts = [p for p in path.split("/") if p]
    order = list(NAMES)
    chains = sorted(
        store.chains, key=lambda c: order.index(c) if c in order else len(order)
    )  # Ethereum first
    base = site_url()
    if not parts:
        names = ", ".join(name(c) for c in chains[:-1]) + (
            f" and {name(chains[-1])}" if len(chains) > 1 else ""
        )
        ssr = (
            f'<section class="ssr"><h1>MEV Finder: live MEV and arbitrage on {escape(names)}</h1>'
            f"<p>MEV Finder classifies every new block on six chains as it is made: atomic arbitrage, sandwiches, JIT "
            f"liquidity, liquidations and inventory arbitrage. It shows who extracted the value, what they paid the "
            f"block builder or sequencer, and what they kept, with every arbitrage cycle checked against the raw "
            f"receipts. Figures below cover the last {WINDOW_DAYS} days, or the time since that chain's live "
            f"data began if that is shorter.</p><ul>"
            + "".join(_chain_line(store, c) for c in chains)
            + '</ul><p><a href="/contact">Contact</a></p></section>'
        )
        return {
            "title": "MEV Finder: Live MEV & Arbitrage Tracker on 6 Chains",
            "description": (
                f"Live MEV on {names}: arbitrage, sandwiches, JIT, liquidations, "
                "who made what and what builders got."
            ),
            "url": f"{base}/",
            "ssr": ssr,
            "jsonld": [
                _org(),
                {
                    "@type": "WebSite",
                    "@id": f"{base}/#site",
                    "name": "MEV Finder",
                    "alternateName": ["mevfinder", "mevfinder.com", "MEV Finder: live MEV tracker"],
                    "url": f"{base}/",
                    "publisher": {"@id": f"{base}/#org"},
                },
            ],
        }
    if parts == ["contact"]:
        ssr = (
            f'<section class="ssr"><h1>Contact MEV Finder</h1><p>Questions about the data, API or research access, '
            f"partnerships, or a transaction you think we classified wrong: write to "
            f'<a href="mailto:{CONTACT_EMAIL}">{CONTACT_EMAIL}</a>.</p></section>'
        )
        return {
            "title": "Contact MEV Finder",
            "description": f"Contact MEV Finder at {CONTACT_EMAIL}: MEV and arbitrage "
            "data, API and research access, partnerships, corrections.",
            "url": f"{base}/contact",
            "ssr": ssr,
            "jsonld": [
                _org(),
                {
                    "@type": "ContactPage",
                    "name": "Contact MEV Finder",
                    "url": f"{base}/contact",
                    "about": {"@id": f"{base}/#org"},
                },
            ],
        }
    if parts[0] == "learn":
        return {
            "title": "Learn: MEV and Arbitrage Explained | MEV Finder",
            "description": "How MEV, arbitrage, sandwiches "
            "and block building work on Ethereum, Base, BSC, Arbitrum, Optimism and Robinhood Chain.",
            "url": f"{base}/{'/'.join(parts[:2])}",
            "ssr": '<section class="ssr"><h1>Learn</h1></section>',
            "jsonld": [_org()],
            "robots": "noindex, follow",
        }  # no articles yet: an empty page is kept out of the index until they exist
    chain = parts[0]
    if chain not in chains:
        return None
    sub = parts[1] if len(parts) > 1 else ""
    if sub and sub not in SUBPAGES and sub not in DETAIL:
        return None
    n, long = name(chain), LONG_NAMES.get(chain, name(chain))
    s = _summary(store, chain)
    cycles, gross, net = int(s.get("cycles") or 0), s.get("gross_usd"), s.get("net_usd")
    since = s.get("period_since") or ""
    span_short, span_long = window(s)
    figures = (
        f"{cycles:,} arbitrage cycles, {_money(gross)} gross and {_money(net)} kept by searchers {span_long}"
    )
    short = f"{cycles:,} arbitrage cycles, {_money(gross)} gross, {_money(net)} kept ({span_short})"
    others = "".join(
        f'<li><a href="/{c}">{escape(name(c))} MEV tracker</a></li>' for c in chains if c != chain
    )
    links = (
        f'<ul><li><a href="/{chain}/txs">{escape(n)} MEV transactions</a></li><li><a href="/{chain}/bots">'
        f'{escape(n)} MEV bots</a></li><li><a href="/{chain}/blocks">{escape(n)} blocks</a></li>{others}'
        f'<li><a href="/contact">Contact</a></li></ul>'
    )
    crumbs = {
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "MEV Finder", "item": f"{base}/"},
            {"@type": "ListItem", "position": 2, "name": f"{n} MEV", "item": f"{base}/{chain}"},
        ],
    }
    if sub in DETAIL:
        ident = parts[2] if len(parts) > 2 else ""
        found = bots(store, chain)[1].get(ident.lower()) if sub == "bot" else None
        if found:  # a bot with counted cycles in the window: a page of its own with its numbers, indexed
            rank, bot = found
            address, total = ident.lower(), len(bots(store, chain)[0])
            b_cycles, b_gross, b_net = (
                int(bot.get("cycles") or 0),
                bot.get("gross_usd") or 0.0,
                bot.get("net_usd") or 0.0,
            )
            kept = f"{_money(b_net)} kept" if b_net >= 0 else f"{_money(-b_net)} lost after costs"
            numbers = f"{b_cycles:,} arbitrage cycles, {_money(b_gross)} gross, {kept}"
            crumbs["itemListElement"] += [
                {"@type": "ListItem", "position": 3, "name": f"{n} MEV bots", "item": f"{base}/{chain}/bots"},
                {
                    "@type": "ListItem",
                    "position": 4,
                    "name": address,
                    "item": f"{base}/{chain}/bot/{address}",
                },
            ]
            return {
                "title": f"{n} MEV Bot {address[:8]}…{address[-4:]}: Profit & Trades | MEV Finder",
                "description": f"MEV bot {address} on {long}: {numbers} {span_long}.",
                "url": f"{base}/{chain}/bot/{address}",
                "jsonld": [crumbs],
                "ssr": f'<section class="ssr"><h1>{escape(n)} MEV bot {escape(address)}</h1><p>This bot made '
                f"{escape(numbers)} on {escape(long)} {escape(span_long)}: {int(bot.get('backruns') or 0):,} "
                f"backruns and {int(bot.get('top_of_block') or 0):,} top-of-block trades. It ranks {rank:,} of "
                f"{total:,} bots on {escape(n)} by gross.</p>{links}</section>",
            }
        what = {"tx": "Transaction", "block": "Block", "bot": "Bot"}[sub]
        return {
            "title": f"{n} {what} {ident[:14]} | MEV Finder",
            "description": f"{what} {ident} on {long}: MEV, arbitrage and fees.",
            "url": f"{base}/{chain}/{sub}/{ident}",
            "robots": "noindex, follow",
            "jsonld": [crumbs],
            "ssr": f'<section class="ssr"><h1>{escape(n)} {what.lower()} {escape(ident)}</h1>{links}</section>',
        }
    if sub:
        headline, about = SUBPAGES[sub]
        crumbs["itemListElement"].append(
            {"@type": "ListItem", "position": 3, "name": headline, "item": f"{base}/{chain}/{sub}"}
        )
        return {
            "title": f"{n} {headline} | MEV Finder",
            "description": f"{n} {about}. {short}.",
            "url": f"{base}/{chain}/{sub}",
            "jsonld": [crumbs],
            "ssr": f'<section class="ssr"><h1>{escape(n)} {escape(headline)}</h1><p>{escape(n)} {escape(about)}. '
            f"{escape(figures)}.</p>{links}</section>",
        }
    dataset = {
        "@type": "Dataset",
        "name": f"{n} MEV and arbitrage data",
        "url": f"{base}/{chain}",
        "description": (
            f"Live MEV on {long}: every atomic arbitrage cycle with its route, profit, fees and builder "
            "payment, plus sandwiches, JIT liquidity, liquidations and inventory arbitrage, classified "
            "block by block from the chain's own node."
        ),
        "creator": {"@id": f"{base}/#org"},
        "isAccessibleForFree": True,
        "keywords": [
            f"{n} MEV",
            f"{n} arbitrage",
            "MEV",
            "arbitrage",
            "sandwich attacks",
            "MEV bots",
            "searchers",
            "block builders",
            "JIT liquidity",
            "liquidations",
        ],
        "variableMeasured": [
            "arbitrage cycles",
            "gross arbitrage profit (USD)",
            "profit kept by searchers (USD)",
            "fees and builder payments (USD)",
            "sandwich attacks",
            "JIT liquidity",
            "liquidations",
        ],
    }
    live_since = (s.get("coverage") or {}).get("live_since")
    if live_since or since:
        dataset["temporalCoverage"] = f"{live_since or since}/.."
    return {
        "title": f"{n} MEV & Arbitrage Tracker (Live) | MEV Finder",
        "description": f"Live {n} MEV: {short}. Sandwiches, JIT, liquidations and bots, block by block.",
        "url": f"{base}/{chain}",
        "jsonld": [_org(), crumbs, dataset],
        "ssr": f'<section class="ssr"><h1>{escape(n)} MEV and arbitrage, live</h1><p>{escape(figures)}, '
        f"{int(s.get('sandwiches') or 0):,} sandwiches.</p>{links}</section>",
    }


def head(page: dict) -> str:
    t, d, u = escape(page["title"]), escape(page["description"]), escape(page["url"])
    image = escape(f"{site_url()}/og-image.jpg")
    tags = [
        f"<title>{t}</title>",
        f'<meta name="description" content="{d}">',
        f'<link rel="canonical" href="{u}">',
        '<meta property="og:type" content="website">',
        '<meta property="og:site_name" content="MEV Finder">',
        f'<meta property="og:title" content="{t}">',
        f'<meta property="og:description" content="{d}">',
        f'<meta property="og:url" content="{u}">',
        f'<meta property="og:image" content="{image}">',
        '<meta property="og:image:width" content="1200">',
        '<meta property="og:image:height" content="630">',
        '<meta property="og:image:alt" content="MEV Finder: live MEV and arbitrage tracker">',
        '<meta name="twitter:card" content="summary_large_image">',
        f'<meta name="twitter:title" content="{t}">',
        f'<meta name="twitter:description" content="{d}">',
        f'<meta name="twitter:image" content="{image}">',
    ]
    if page.get("robots"):
        tags.append(f'<meta name="robots" content="{escape(page["robots"])}">')
    if page.get("jsonld"):
        graph = json.dumps(
            {"@context": "https://schema.org", "@graph": page["jsonld"]}, separators=(",", ":")
        )
        tags.append('<script type="application/ld+json">' + graph.replace("</", "<\\/") + "</script>")
    return "\n".join(tags)


ASSETS = ("style.css", "app.js", "txmap.js", "buddy.js", "md.js", "game.js")


def asset_version(ui_dir: Path) -> str:
    """A version for the page's scripts and styles that changes whenever any of them changes (newest modification time),
    so they can be cached for a year by browsers and the CDN and still update on every deploy."""
    return str(max(int((ui_dir / a).stat().st_mtime) for a in ASSETS if (ui_dir / a).exists()))


def render(template: str, page: dict, version: str | None = None) -> str:
    out = re.sub(r"<!--seo:start-->.*?<!--seo:end-->", lambda _: head(page), template, count=1, flags=re.S)
    if version:
        for asset in ASSETS:
            out = out.replace(f'"/{asset}"', f'"/{asset}?v={version}"')
    if page.get("robots"):  # the page's own robots tag replaces the site default
        out = out.replace('<meta name="robots" content="index, follow, max-image-preview:large">\n', "", 1)
    return out.replace("<!--ssr-->", page.get("ssr", ""), 1)


# crawlers that only collect training data for AI models (search engines and AI search/answer bots stay allowed)
TRAINING_BOTS = (
    "GPTBot",
    "Google-Extended",
    "CCBot",
    "ClaudeBot",
    "anthropic-ai",
    "Applebot-Extended",
    "Bytespider",
    "meta-externalagent",
    "cohere-training-data-crawler",
    "Diffbot",
    "Omgilibot",
    "Timpibot",
)


def robots_txt() -> str:
    """Everything a visitor sees is crawlable, including the /api/ calls the pages load their numbers from (Google
    renders the app and needs them; API responses carry X-Robots-Tag: noindex so they are not listed themselves).
    Training-only AI crawlers are asked to stay out (Cloudflare also blocks them)."""
    training = "".join(f"User-agent: {b}\n" for b in TRAINING_BOTS)
    return (
        f"# MEV Finder: live MEV and arbitrage on six chains. A plain guide to the site: {site_url()}/llms.txt\n"
        f"User-agent: *\nAllow: /\nDisallow: /explorer\n\n{training}Disallow: /\n\nSitemap: {site_url()}/sitemap.xml\n"
    )


def llms_txt(store) -> str:
    """https://llmstxt.org: what the site is and how to find things on it, for AI assistants and agents. Pages and what
    they show only, no internals."""
    base = site_url()
    order = list(NAMES)
    chains = sorted(store.chains, key=lambda c: order.index(c) if c in order else len(order))
    lines = [
        "# MEV Finder",
        "",
        "> MEV Finder tracks MEV (maximal extractable value) live on Ethereum, Base, BNB Smart Chain, Arbitrum, "
        "Optimism and Robinhood Chain. Every new block is classified as it is produced: atomic arbitrage, "
        "sandwiches, JIT liquidity, liquidations and inventory arbitrage, with who extracted the value, what they "
        "paid the block builder or sequencer, and what they kept.",
        "",
        "Figures are in US dollars at the time of each block. A chain's overview covers the last 7 days, or the "
        "time since its live data began when that is shorter; the page says which. Losing bots are shown "
        "separately and never added to the totals.",
        "",
        "## Chains",
        "",
    ]
    for c in chains:
        n = name(c)
        lines += [
            f"- [{n} overview]({base}/{c}): totals, what was paid to builders, what searchers kept, top bots, "
            "latest blocks",
            f"- [{n} MEV transactions]({base}/{c}/txs): every arbitrage cycle, sandwich, JIT bundle and "
            "liquidation, newest first",
            f"- [{n} MEV bots]({base}/{c}/bots): the searchers behind the arbitrage, ranked by profit",
            f"- [{n} blocks]({base}/{c}/blocks): recent blocks and the MEV in each",
        ]
    lines += [
        "",
        "## Page addresses",
        "",
        f"- A transaction: `{base}/<chain>/tx/<transaction hash>`: what it did, its route through the pools, "
        "profit, fees and builder payment",
        f"- A block: `{base}/<chain>/block/<number>`: every transaction in execution order, with the MEV marked",
        f"- A bot: `{base}/<chain>/bot/<address>`: one searcher's cycles and results",
        "- `<chain>` is one of: " + ", ".join(f"`{c}`" for c in chains),
        "- The search box at the top of every page takes a block number or a transaction hash.",
        "",
        "## Terms",
        "",
        "- Gross: the value an arbitrage or other MEV transaction extracted before costs.",
        "- Paid to block builders: priority fees and direct tips to the block's builder or sequencer.",
        "- Base fee: the protocol fee, burned.",
        "- Kept by searchers: gross minus everything paid, for winning transactions.",
        "",
        "## Other",
        "",
        f"- [Learn]({base}/learn): how MEV and arbitrage work",
        f"- [Contact]({base}/contact): data and API access, research, partnerships, corrections: {CONTACT_EMAIL}",
        f"- [Sitemap]({base}/sitemap.xml)",
        "",
    ]
    return "\n".join(lines)


CSP_BASE = (
    "default-src 'self'; script-src 'self' {hashes} https://static.cloudflareinsights.com; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com data:; "
    "img-src 'self' data:; connect-src 'self' https://cloudflareinsights.com; frame-ancestors 'none'; "
    "base-uri 'self'; form-action 'self'; object-src 'none'; upgrade-insecure-requests"
)


def csp(html: str) -> str:
    """Content-Security-Policy for a page: scripts only from this site (plus Cloudflare's analytics beacon), each inline
    script allowed by its hash, so an injected script cannot run; the page cannot be framed."""
    hashes = " ".join(
        f"'sha256-{base64.b64encode(hashlib.sha256(m.encode()).digest()).decode()}'"
        for m in re.findall(r"<script>(.*?)</script>", html, flags=re.S)
    )
    return CSP_BASE.format(hashes=hashes)


def sitemap_xml(store) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    urls = [("/", "hourly", "1.0"), ("/contact", "monthly", "0.4")]
    for c in store.chains:
        urls += [
            (f"/{c}", "hourly", "0.9"),
            (f"/{c}/txs", "hourly", "0.7"),
            (f"/{c}/bots", "daily", "0.6"),
            (f"/{c}/blocks", "hourly", "0.5"),
        ]
    for c in store.chains:  # the window's top bots: the only detail pages that are indexed
        urls += [
            (f"/{c}/bot/{b['address'].lower()}", "daily", "0.3") for b in bots(store, c)[0][:BOTS_IN_SITEMAP]
        ]
    body = "".join(
        f"<url><loc>{escape(site_url() + p)}</loc><lastmod>{today}</lastmod><changefreq>{f}</changefreq>"
        f"<priority>{pr}</priority></url>"
        for p, f, pr in urls
    )
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>\n'


def template(ui_dir: Path) -> str:
    return (ui_dir / "index.html").read_text()
