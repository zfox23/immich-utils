"""A small Immich API client: JSON requests with retries for 429/5xx and network errors."""

from __future__ import annotations

import json
import logging
import socket
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Optional

RETRY_BASE_DELAY = 2.0  # seconds; doubles on each retry

# Set this to make in-flight requests and retry waits stop with Cancelled.
STOP = threading.Event()


class ImmichHTTPError(Exception):
    def __init__(self, status: int, method: str, path: str, body: str):
        super().__init__(f"HTTP {status} for {method} {path}: {body[:300]}")
        self.status = status


class Cancelled(Exception):
    pass


def http_error_hint(e: ImmichHTTPError) -> str:
    """Extra advice to append to an error message, for permission problems."""
    if e.status in (401, 403):
        return " Check the API key and its permissions (see the README)."
    return ""


class ImmichClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        insecure: bool = False,
        timeout: float = 60.0,
        retries: int = 3,
        user_agent: str = "immich-utils",
        logger: Optional[logging.Logger] = None,
    ):
        self.base_url = base_url
        self.api_key = api_key
        self.timeout = timeout
        self.retries = retries
        self.user_agent = user_agent
        self.log = logger or logging.getLogger("immich-utils")
        ctx = ssl.create_default_context()
        if insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        self._opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))

    def _url(self, path: str, params: Optional[Dict[str, Any]] = None) -> str:
        url = f"{self.base_url}/api{path}"
        if params:
            clean = {k: (str(v).lower() if isinstance(v, bool) else v) for k, v in params.items() if v is not None}
            url += "?" + urllib.parse.urlencode(clean)
        return url

    def _open(self, method: str, path: str, params=None, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self._url(path, params), data=data, method=method)
        req.add_header("x-api-key", self.api_key)
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", self.user_agent)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        return self._opener.open(req, timeout=self.timeout)

    def _with_retries(self, method: str, path: str, fn: Callable[[], Any]) -> Any:
        delay = RETRY_BASE_DELAY
        for attempt in range(self.retries + 1):
            if STOP.is_set():
                raise Cancelled()
            try:
                return fn()
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8", "replace")
                except Exception:
                    pass
                finally:
                    e.close()
                retryable = e.code == 429 or e.code >= 500
                if not retryable or attempt == self.retries:
                    raise ImmichHTTPError(e.code, method, path, body) from None
                self.log.debug("HTTP %s for %s %s, retrying in %.0fs", e.code, method, path, delay)
            except (urllib.error.URLError, socket.timeout, ConnectionError, TimeoutError, OSError) as e:
                if isinstance(e, Cancelled):
                    raise
                if attempt == self.retries:
                    raise
                self.log.debug("Network error for %s %s (%s), retrying in %.0fs", method, path, e, delay)
            STOP.wait(delay)
            delay *= 2
        raise AssertionError("unreachable")

    def request_json(self, method: str, path: str, params: Optional[Dict[str, Any]] = None, body: Any = None) -> Any:
        """Send a request and return the decoded JSON response (None for an empty body)."""

        def do():
            with self._open(method, path, params, body) as resp:
                raw = resp.read()
                return json.loads(raw.decode("utf-8")) if raw.strip() else None

        return self._with_retries(method, path, do)

    def get_json(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        return self.request_json("GET", path, params)

    def post_json(self, path: str, body: Any, params: Optional[Dict[str, Any]] = None) -> Any:
        return self.request_json("POST", path, params, body)

    def delete_json(self, path: str, body: Any = None, params: Optional[Dict[str, Any]] = None) -> Any:
        return self.request_json("DELETE", path, params, body)
