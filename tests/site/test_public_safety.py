"""Public site safety and honesty: the ops explorer is private, visitors are rate-limited by their real address (only
the local proxy's X-Real-IP is trusted), API responses are noindex, pages carry a CSP that allows exactly their inline
script, robots.txt lets search engines load what pages need, and a window is called "last 7 days" only when true."""

import base64
import hashlib
import re
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from arbkit.live import store as livedb
from arbkit.site import access, seo
from arbkit.site.http import make_handler
from arbkit.site.store import RateLimiter, Store


@pytest.fixture
def public(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Store(tmp_path, public=True)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def private(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Store(tmp_path, public=False)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def get(url, headers=None, token=True):
    """A request as the site's own pages make it (with the page's API token), unless `token` is False."""
    headers = {**({access.HEADER: access.issue()} if token else {}), **(headers or {})}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=5) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_ops_explorer_is_not_public(public):
    for path in ("/explorer", "/explorer.html", "/explorer.js"):
        assert get(public + path)[0] == 404, path


def test_api_is_noindex_and_pages_carry_a_csp_matching_their_inline_script(public):
    status, headers, _ = get(public + "/api/config")
    assert status == 200 and headers["X-Robots-Tag"] == "noindex"
    status, headers, body = get(public + "/contact")
    policy = headers["Content-Security-Policy"]
    inline = re.search(rb"<script>(.*?)</script>", body, re.S).group(1)
    assert f"'sha256-{base64.b64encode(hashlib.sha256(inline).digest()).decode()}'" in policy
    assert "frame-ancestors 'none'" in policy and "unsafe-inline' https://fonts.googleapis.com" in policy
    assert "'unsafe-eval'" not in policy and "script-src 'self' 'sha256-" in policy


def test_rate_limit_keys_on_the_proxys_x_real_ip_only(public, monkeypatch):
    import arbkit.site.store as site_store

    hits = []
    monkeypatch.setattr(site_store.RateLimiter, "allow", lambda self, client: hits.append(client) or True)
    get(public + "/api/config", {"X-Real-IP": "203.0.113.9", "CF-Connecting-IP": "198.51.100.1"})
    assert hits == ["203.0.113.9"]  # the test client connects from 127.0.0.1, like Caddy


def test_rate_limiter_memory_is_bounded():
    limiter = RateLimiter(5, 0.01)
    limiter.MAX_CLIENTS = 100
    for i in range(500):
        limiter.allow(f"10.0.{i // 256}.{i % 256}")
    time.sleep(0.02)
    limiter.allow("10.9.9.9")
    assert len(limiter.hits) <= 101


def test_robots_lets_search_engines_load_the_api_and_names_llms_txt():
    robots = seo.robots_txt()
    assert "Disallow: /api/" not in robots and "llms.txt" in robots
    star = robots.split("\n\n")[0]
    assert "User-agent: *" in star and "Allow: /" in star
    assert "User-agent: GPTBot" in robots and "User-agent: Google-Extended" in robots
    assert "User-agent: Googlebot" not in robots and "OAI-SearchBot" not in robots


def test_llms_txt_lists_pages_without_api_internals():
    class S:
        chains = {"ethereum": 1, "base": 1}

    text = seo.llms_txt(S())
    assert text.startswith("# MEV Finder\n\n> ") and "https://mevfinder.com/base/txs" in text
    assert "/api/" not in text and "info@mevfinder.com" in text


def _block(conn, number, ts, source="live"):
    day = time.strftime("%Y-%m-%d", time.gmtime(ts))
    conn.execute(
        "INSERT INTO blocks(number, hash, timestamp, day, transactions, mev_rows, cycles, losses, candidates, "
        "sandwiches, gross_usd, traced, trace_unavailable, info, source, classified_at) "
        "VALUES(?, ?, ?, ?, 1, 0, 0, 0, 0, 0, 0, 1, 0, '{}', ?, '')",
        (number, f"0x{number:x}", ts, day, source),
    )


def test_coverage_starts_at_the_floor_and_counts_missing_blocks(tmp_path):
    conn = livedb.connect(tmp_path / "c.sqlite")
    now = int(time.time())
    _block(conn, 5, now - 30 * 3600)  # a test block before the follower's start
    for n in range(100, 110):
        if n != 104:  # one block never classified
            _block(conn, n, now - 26 * 3600 + (n - 100) * 600)
    livedb.set_meta(conn, "floor", 100)
    cov = livedb.coverage(conn, livedb.since_day(7))
    assert cov["blocks"] == 9 and cov["first_block"] == 100 and cov["missing_blocks"] == 1
    assert not cov["complete"] and cov["live_since"] == cov["from"]
    assert seo.window({"coverage": cov})[0].startswith("since ")
    assert seo.window({"coverage": {**cov, "complete": True}}) == ("7 days", "in the last 7 days")


def test_head_requests_answer_like_get_without_a_body(public):
    for path in ("/", "/contact", "/robots.txt", "/llms.txt", "/style.css", "/api/config"):
        req = urllib.request.Request(public + path, method="HEAD", headers={access.HEADER: access.issue()})
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.status == 200 and r.read() == b"", path


def test_the_public_api_answers_the_sites_own_pages_only(public):
    """A script calling /api/ directly is refused; a page's token (in its head) opens the API until it expires."""
    status, _, body = get(public + "/api/config", token=False)
    assert status == 403 and b"mevfinder.com pages only" in body
    assert get(public + "/api/config", {access.HEADER: "123.abc"}, token=False)[0] == 403
    expired = access.issue(now=time.time() - access.TOKEN_TTL - 60)
    assert get(public + "/api/config", {access.HEADER: expired}, token=False)[0] == 403
    page = get(public + "/", token=False)[2].decode()
    token = re.search(r'<meta name="mf-token" content="([^"]+)">', page).group(1)
    assert get(public + "/api/config", {access.HEADER: token}, token=False)[0] == 200


def test_the_private_api_needs_no_token(private):
    assert get(private + "/api/config", token=False)[0] == 200
