"""A tiny in-process fake of the Immich 3.4 endpoints used by the backup script.

It enforces the same partner rules as the real server: you can only see users who
share with you, only their TIMELINE assets appear in timeline buckets, and HIDDEN
assets (Live Photo videos) are readable by id.
"""

import base64
import hashlib
import json
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

API_KEY = "test-key"


def new_id() -> str:
    return str(uuid.uuid4())


class FakeImmich:
    def __init__(self):
        self.me = {"id": new_id(), "name": "Admin", "email": "admin@example.com"}
        self.users = {self.me["id"]: dict(self.me, deletedAt=None, quotaUsageInBytes=0)}
        self.shared_with_me = set()  # user ids that share with me
        self.assets = {}
        self.fail_asset_ids = set()
        self.requests = []
        self.lock = threading.Lock()

    # --- setup helpers ---------------------------------------------------------------
    def add_user(self, name, share=True):
        uid = new_id()
        self.users[uid] = {"id": uid, "name": name, "email": f"{name.lower().replace(' ', '.')}@example.com",
                           "deletedAt": None, "quotaUsageInBytes": 1000}
        if share:
            self.shared_with_me.add(uid)
        return uid

    def add_asset(self, owner, name, local_dt, content=None, visibility="timeline", live_video=None, library=False):
        aid = new_id()
        content = content if content is not None else f"content-of-{aid}".encode()
        self.assets[aid] = {
            "id": aid, "ownerId": owner, "originalFileName": name, "localDateTime": local_dt,
            "fileCreatedAt": local_dt, "content": content, "visibility": visibility,
            "trashed": False, "livePhotoVideoId": live_video, "libraryId": new_id() if library else None,
        }
        return aid

    def set_content(self, aid, content):
        self.assets[aid]["content"] = content

    @staticmethod
    def checksum(content):
        return base64.b64encode(hashlib.sha1(content).digest()).decode()

    def detail(self, a):
        checksum = self.checksum(("path:" + a["id"]).encode()) if a["libraryId"] else self.checksum(a["content"])
        return {
            "id": a["id"], "ownerId": a["ownerId"], "originalFileName": a["originalFileName"],
            "localDateTime": a["localDateTime"], "fileCreatedAt": a["fileCreatedAt"], "checksum": checksum,
            "livePhotoVideoId": a["livePhotoVideoId"], "visibility": a["visibility"], "isTrashed": a["trashed"],
            "type": "IMAGE", "libraryId": a["libraryId"],
            "exifInfo": {} if a.get("no_size") else {"fileSizeInByte": len(a["content"])},
        }

    def can_read_asset(self, a):
        if a["ownerId"] == self.me["id"]:
            return True
        return a["ownerId"] in self.shared_with_me and not a["trashed"] and a["visibility"] in ("timeline", "hidden")

    # --- server ------------------------------------------------------------------------
    def start(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send_json(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                u = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                with fake.lock:
                    fake.requests.append(u.path)
                if u.path == "/api/server/version":
                    return self.send_json(200, {"major": 3, "minor": 4, "patch": 2})
                if self.headers.get("x-api-key") != API_KEY:
                    return self.send_json(401, {"message": "Invalid API key"})
                if u.path == "/api/users/me":
                    return self.send_json(200, fake.me)
                if u.path == "/api/admin/users":
                    return self.send_json(200, list(fake.users.values()))
                if u.path == "/api/partners":
                    return self.send_json(200, [
                        {"id": uid, "name": fake.users[uid]["name"], "email": fake.users[uid]["email"]}
                        for uid in fake.shared_with_me
                    ])
                if u.path.startswith("/api/timeline/"):
                    uid = q.get("userId")
                    if uid != fake.me["id"] and uid not in fake.shared_with_me:
                        return self.send_json(400, {"message": "Not found or no timeline.read access"})
                    assets = [a for a in fake.assets.values() if a["ownerId"] == uid and a["visibility"] == "timeline" and not a["trashed"]]
                    if u.path == "/api/timeline/buckets":
                        months = {}
                        for a in assets:
                            key = a["fileCreatedAt"][:7] + "-01"
                            months[key] = months.get(key, 0) + 1
                        return self.send_json(200, [{"timeBucket": k, "count": v} for k, v in sorted(months.items())])
                    if u.path == "/api/timeline/bucket":
                        tb = q["timeBucket"][:7]
                        sel = [a for a in assets if a["fileCreatedAt"][:7] == tb]
                        return self.send_json(200, {
                            "id": [a["id"] for a in sel], "ownerId": [a["ownerId"] for a in sel],
                            "isTrashed": [False for _ in sel], "livePhotoVideoId": [a["livePhotoVideoId"] for a in sel],
                        })
                m = re.match(r"^/api/assets/([0-9a-f-]{36})(/original)?$", u.path)
                if m:
                    a = fake.assets.get(m.group(1))
                    if a is None or not fake.can_read_asset(a):
                        return self.send_json(400, {"message": "Not found or no asset.read access"})
                    if m.group(1) in fake.fail_asset_ids:
                        return self.send_json(500, {"message": "boom"})
                    if m.group(2):
                        self.send_response(200)
                        self.send_header("Content-Type", "application/octet-stream")
                        self.send_header("Content-Length", str(len(a["content"])))
                        self.end_headers()
                        self.wfile.write(a["content"])
                        return
                    return self.send_json(200, fake.detail(a))
                return self.send_json(404, {"message": "no route"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
