"""Who may use the JSON API of the public site: the site's own pages, nothing else.

Every page the server renders carries a short-lived token (`<meta name="mf-token">`) signed with a secret only the
server knows; the pages send it back with each API request (`X-MF-Token`). A request without a valid token (a script
calling /api/ directly) is refused. The private site needs none.

The secret is `ARB_API_SECRET` from the environment, so tokens survive a restart; without it each process makes its
own (pages loaded before a restart then need a reload).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time

TOKEN_TTL = 6 * 3600  # seconds a page's token stays valid (a CDN may serve the page for a minute of it)
HEADER = "X-MF-Token"
_secret: bytes | None = None


def _key() -> bytes:
    global _secret
    if _secret is None:
        _secret = (os.environ.get("ARB_API_SECRET") or secrets.token_hex(32)).encode()
    return _secret


def _sign(expiry: str) -> str:
    return hmac.new(_key(), expiry.encode(), hashlib.sha256).hexdigest()[:32]


def issue(now: float | None = None) -> str:
    """A token valid for TOKEN_TTL seconds: `<expiry>.<signature>`."""
    expiry = str(int((now if now is not None else time.time()) + TOKEN_TTL))
    return f"{expiry}.{_sign(expiry)}"


def valid(token: str | None, now: float | None = None) -> bool:
    if not token or "." not in token:
        return False
    expiry, signature = token.split(".", 1)
    if not expiry.isdigit() or int(expiry) < (now if now is not None else time.time()):
        return False
    return hmac.compare_digest(signature, _sign(expiry))


def with_token(html: str) -> str:
    """The page with its API token in the head."""
    return html.replace("<head>", f'<head>\n<meta name="mf-token" content="{issue()}">', 1)
