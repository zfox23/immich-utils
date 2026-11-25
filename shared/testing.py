"""Test helper: a tiny threaded HTTP server for fake Immich APIs.

Subclass FakeServer and implement `handle(method, path, query, body, api_key)`. Return
`(status, obj)` to send JSON, or `(status, bytes)` to send raw bytes.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse


class FakeServer:
    def __init__(self):
        self.lock = threading.Lock()
        self.requests = []  # list of (method, path)

    def handle(self, method: str, path: str, query: Dict[str, str], body: Optional[Any], api_key: Optional[str]) -> Tuple[int, Any]:
        raise NotImplementedError

    def start(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _dispatch(self, method):
                u = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw) if raw else None
                with fake.lock:
                    fake.requests.append((method, u.path))
                code, payload = fake.handle(method, u.path, q, body, self.headers.get("x-api-key"))
                if isinstance(payload, (bytes, bytearray)):
                    data, ctype = bytes(payload), "application/octet-stream"
                else:
                    data, ctype = (b"" if payload is None else json.dumps(payload).encode()), "application/json"
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._dispatch("GET")

            def do_POST(self):
                self._dispatch("POST")

            def do_PUT(self):
                self._dispatch("PUT")

            def do_DELETE(self):
                self._dispatch("DELETE")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
