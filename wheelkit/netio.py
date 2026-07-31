"""HTTP helpers with a CA bundle that actually works on this machine.

Python installed from python.org ships without a populated CA store, so every
``urlopen`` against an HTTPS host raises ``SSLCertVerificationError``. The old
scanner swallowed that as a generic ``OSError`` and silently degraded to "no
data". Build the context from ``certifi`` instead and let failures be loud.
"""

from __future__ import annotations

import gzip
import json
import ssl
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlencode

_DEFAULT_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) weekly-wheel-scan/2"


class FetchError(RuntimeError):
    """A network call failed after exhausting retries."""


def _build_ssl_context() -> ssl.SSLContext:
    try:
        import certifi
    except ImportError as exc:  # pragma: no cover - install-time problem
        raise FetchError(
            "certifi is not installed. Run: pip install -r requirements.txt\n"
            "Without it this Python build cannot verify any HTTPS certificate."
        ) from exc
    return ssl.create_default_context(cafile=certifi.where())


_SSL_CONTEXT: ssl.SSLContext | None = None


def ssl_context() -> ssl.SSLContext:
    global _SSL_CONTEXT
    if _SSL_CONTEXT is None:
        _SSL_CONTEXT = _build_ssl_context()
    return _SSL_CONTEXT


def get_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
    retries: int = 3,
    backoff: float = 1.5,
) -> Any:
    """GET a URL and decode JSON, retrying transient failures.

    Raises ``FetchError`` on 4xx responses that will not improve with a retry,
    and after the final attempt for everything else.
    """
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        url = f"{url}?{urlencode(clean)}"

    request_headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "gzip",
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": _DEFAULT_UA,
    }
    if headers:
        request_headers.update(headers)

    last_error: Exception | None = None
    for attempt in range(retries):
        request = urllib.request.Request(url, headers=request_headers)
        try:
            with urllib.request.urlopen(
                request, timeout=timeout, context=ssl_context()
            ) as response:
                raw = response.read()
                if response.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_error = exc
            # Auth, permission and not-found errors are permanent.
            if exc.code in {400, 401, 403, 404, 422}:
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", "replace")[:300]
                except Exception:
                    pass
                raise FetchError(f"HTTP {exc.code} for {url}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            last_error = exc
        if attempt < retries - 1:
            time.sleep(backoff * (2**attempt))

    raise FetchError(f"Could not fetch {url}: {last_error}")
