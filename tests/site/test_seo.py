"""MEV Finder pages for search engines: real URLs get their own head and a server-rendered summary; static files and
the API are not pages; detail pages are noindex except bots with counted cycles, which the sitemap lists after the
chain pages on the canonical host."""

import re
from pathlib import Path

from arbkit.site import seo

UI = Path(__file__).resolve().parents[2] / "ui"


class Chain:
    def summary(self, period=None):
        return {
            "cycles": 1234,
            "gross_usd": 5678.9,
            "net_usd": 901.2,
            "sandwiches": 7,
            "period_since": "2026-10-26",
        }


class Store:
    chains = {"ethereum": 1, "base": 1, "robinhood": 1}

    def get(self, name):
        return Chain()


def page(path):
    return seo.render(seo.template(UI), seo.page_for(path, Store()))


def test_chain_page_has_its_own_head_and_numbers():
    html = page("/base")
    assert re.search(r"<title>Base MEV &amp; Arbitrage Tracker \(Live\) \| MEV Finder</title>", html)
    assert '<link rel="canonical" href="https://mevfinder.com/base">' in html
    assert html.count('rel="canonical"') == 1 and html.count("<title>") == 1
    assert "1,234 arbitrage cycles" in html and '"@type":"Dataset"' in html
    assert 'class="ssr"' in html and "<!--ssr-->" not in html


def test_not_pages_and_noindex_details():
    assert seo.page_for("/style.css", Store()) is None
    assert seo.page_for("/api/base/overview", Store()) is None
    assert seo.page_for("/unknown-chain", Store()) is None
    detail = page("/base/tx/0xabc")
    assert '<meta name="robots" content="noindex, follow">' in detail
    assert "index, follow, max-image-preview" not in detail


def test_contact_robots_and_sitemap():
    assert "mailto:info@mevfinder.com" in page("/contact")
    assert "Sitemap: https://mevfinder.com/sitemap.xml" in seo.robots_txt()
    sitemap = seo.sitemap_xml(Store())
    assert (
        "<loc>https://mevfinder.com/robinhood</loc>" in sitemap
        and "<loc>https://mevfinder.com/contact</loc>" in sitemap
    )


def test_assets_are_versioned_for_long_caching():
    html = seo.render(seo.template(UI), seo.page_for("/base", Store()), "123")
    assert '"/app.js?v=123"' in html and '"/style.css?v=123"' in html


def test_summary_is_hidden_from_script_visitors_behind_a_loading_card():
    html = page("/base")
    assert 'classList.add("js")' in html and 'class="card boot"' in html
    css = (UI / "style.css").read_text()
    assert ".js .ssr { display: none; }" in css and ".js .boot { display: block; }" in css


class BotChain(Chain):
    def operators_ranked(self, hours=None, sort="gross"):
        return [
            {
                "address": "0x" + "ab" * 20,
                "cycles": 42,
                "gross_usd": 1500.0,
                "net_usd": 300.4,
                "backruns": 30,
                "top_of_block": 2,
            },
            {
                "address": "0x" + "cd" * 20,
                "cycles": 3,
                "gross_usd": 20.0,
                "net_usd": -5.0,
                "backruns": 0,
                "top_of_block": 0,
            },
            {"address": None, "cycles": 9, "gross_usd": 1.0, "net_usd": 1.0},
        ]


class BotStore(Store):
    def get(self, name):
        return BotChain()


BOTS = BotStore()


def test_active_bot_pages_are_indexed_with_their_numbers_and_listed_in_the_sitemap():
    seo._bot_rankings.clear()
    bot = "0x" + "ab" * 20
    html = seo.render(seo.template(UI), seo.page_for(f"/base/bot/0x{'AB' * 20}", BOTS))
    assert f'<link rel="canonical" href="https://mevfinder.com/base/bot/{bot}">' in html
    assert "noindex" not in html and "index, follow, max-image-preview" in html
    assert "42 arbitrage cycles, $1,500 gross, $300 kept" in html and "ranks 1 of 2 bots" in html
    assert html.count("<title>") == 1 and "Base MEV Bot 0xababab…abab" in html
    loser = seo.render(seo.template(UI), seo.page_for(f"/base/bot/0x{'cd' * 20}", BOTS))
    assert "$5.00 lost after costs" in loser
    sitemap = seo.sitemap_xml(BOTS)
    assert f"<loc>https://mevfinder.com/base/bot/{bot}</loc>" in sitemap
    assert sitemap.count("/bot/") == 2 * len(BOTS.chains)


def test_unknown_bots_transactions_and_blocks_stay_noindex():
    seo._bot_rankings.clear()
    for path in (f"/base/bot/0x{'ef' * 20}", "/base/tx/0xabc", "/base/block/123"):
        assert '<meta name="robots" content="noindex, follow">' in seo.render(
            seo.template(UI), seo.page_for(path, BOTS)
        )
    seo._bot_rankings.clear()
    assert "/bot/" not in seo.sitemap_xml(Store())  # a store without a ranking lists no bots


def test_bot_ranking_is_read_once_per_period_and_survives_a_failing_store():
    seo._bot_rankings.clear()
    calls = []

    class Counting(BotChain):
        def operators_ranked(self, hours=None, sort="gross"):
            calls.append(hours)
            if len(calls) > 1:
                raise RuntimeError("store busy")
            return super().operators_ranked(hours, sort)

    chain = Counting()

    class One(Store):
        def get(self, name):
            return chain

    store = One()
    assert len(seo.bots(store, "base")[0]) == 2 and len(seo.bots(store, "base")[0]) == 2
    assert calls == [seo.WINDOW_DAYS * 24]  # the second read came from the kept ranking
    seo._bot_rankings[(id(store), "base")] = (0.0, *seo._bot_rankings[(id(store), "base")][1:])  # expire it
    assert (
        len(seo.bots(store, "base")[0]) == 2 and len(calls) == 2
    )  # the store failed: the last ranking is served
