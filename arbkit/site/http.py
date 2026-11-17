"""HTTP: the site's pages (server-rendered heads for search engines), static files from ui/, and the JSON API."""

from __future__ import annotations

import json
import os
from http.server import SimpleHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from arbkit.chain.spec import ROOT
from arbkit.site import access, seo
from arbkit.site.api import Download, answer
from arbkit.site.store import Store

UI_DIR = ROOT / "ui"


def make_handler(store: Store):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(UI_DIR), **kwargs)

        def log_message(self, format, *args):  # noqa: A002 - quiet server
            if os.environ.get("ARB_UI_LOG"):
                super().log_message(format, *args)

        def end_headers(self):
            if not self.path.startswith("/api/"):
                # CDN-friendly caching: versioned scripts/styles (?v=, changed on every deploy by
                # seo.render) never change at a URL, so they cache for a year; images and icons for a day; anything
                # else (unversioned files) revalidates so an edited file never shows stale
                path = urlparse(self.path)
                if "v=" in path.query and path.path.endswith((".js", ".css")):
                    self.send_header("Cache-Control", "public, max-age=31536000, immutable")
                elif path.path.endswith((".png", ".jpg", ".svg", ".ico", ".webmanifest")):
                    self.send_header("Cache-Control", "public, max-age=86400")
                else:
                    self.send_header("Cache-Control", "no-cache")
            super().end_headers()

        def _json(self, payload, status=200):
            body = json.dumps(payload, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "X-Robots-Tag", "noindex"
            )  # pages load these; search engines list the pages, not the API
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _client(self) -> str:
            # behind a reverse proxy on this machine the visitor's address is the X-Real-IP header the proxy sets;
            # the header from anyone else is not trusted
            if self.client_address[0] in ("127.0.0.1", "::1") and self.headers.get("X-Real-IP"):
                return self.headers["X-Real-IP"].strip()
            return self.client_address[0]

        def _limited(self) -> bool:
            return store.public and not store.limiter.allow(self._client())

        def _text(
            self,
            body: str,
            content_type: str,
            cache: str = "public, max-age=300",
            headers: dict | None = None,
        ):
            data = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", cache)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            super().end_headers()  # not self.end_headers: that adds no-cache to every non-API path
            if self.command != "HEAD":
                self.wfile.write(data)

        def _static(self):
            return super().do_HEAD() if self.command == "HEAD" else super().do_GET()

        def do_HEAD(self):  # noqa: N802 - pages, files and the API answer HEAD like GET, without the body
            return self.do_GET()

        def do_GET(self):  # noqa: N802
            url = urlparse(self.path)
            if url.path.startswith("/api/"):
                return self._api(url)
            return self._page(url)

        def _page(self, url):
            """robots.txt / sitemap.xml / llms.txt, a page of the site, or a static file from ui/."""
            if url.path == "/robots.txt":
                return self._text(seo.robots_txt(), "text/plain; charset=utf-8")
            if url.path == "/sitemap.xml":
                return self._text(seo.sitemap_xml(store), "application/xml; charset=utf-8")
            if url.path == "/llms.txt":
                return self._text(seo.llms_txt(store), "text/plain; charset=utf-8")
            if store.public and url.path.startswith(("/explorer", "/explorer.html", "/explorer.js")):
                return self.send_error(404)  # the ops explorer is private-only
            if url.path in ("/explorer", "/explorer/"):
                self.path = "/explorer.html"
                return self._static()
            # a page of the site (/, /base, /base/txs, /contact, /learn): the app's shell with that page's head and
            # a server-rendered summary, so search engines index real URLs with real numbers
            try:
                page = seo.page_for(url.path, store)
            except Exception:  # noqa: BLE001 - a summary that cannot be read never takes the page down
                page = None
            if page is not None:
                if self._limited():
                    return self._json({"error": "rate limited"}, 429)
                # browsers revalidate; a CDN may keep a page 60 s (the live numbers come from the uncached API)
                html = access.with_token(seo.render(seo.template(UI_DIR), page, seo.asset_version(UI_DIR)))
                return self._text(
                    html,
                    "text/html; charset=utf-8",
                    "public, max-age=0, s-maxage=60",
                    {"Content-Security-Policy": seo.csp(html)},
                )
            if url.path in (
                "/",
                "",
            ):  # the app's shell when no page head could be rendered: still with its token
                return self._text(
                    access.with_token(seo.template(UI_DIR)), "text/html; charset=utf-8", "no-cache"
                )
            return self._static()

        def _api(self, url):
            if self._limited():
                return self._json({"error": "rate limited"}, 429)
            if store.public and not access.valid(self.headers.get(access.HEADER)):
                return self._json({"error": "the API serves mevfinder.com pages only"}, 403)
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            parts = [p for p in url.path.split("/") if p][1:]
            result = answer(store, parts, query)
            if isinstance(result, Download):
                return self._download(result)
            return self._json(*result)

        def _download(self, download: Download):
            body = json.dumps(download.payload, indent=1, default=str).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{download.filename}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler
