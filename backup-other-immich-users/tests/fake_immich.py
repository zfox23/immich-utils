"""A tiny in-process fake of the Immich 3.4 endpoints used by the backup script.

It enforces the same partner rules as the real server: you can only see users who
share with you, only their TIMELINE assets appear in timeline buckets, and HIDDEN
assets (Live Photo videos) are readable by id.
"""

import base64
import hashlib
import re
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, for the shared package
from shared.testing import FakeServer  # noqa: E402

API_KEY = "test-key"


def new_id() -> str:
    return str(uuid.uuid4())


class FakeImmich(FakeServer):
    def __init__(self):
        super().__init__()
        self.me = {"id": new_id(), "name": "Admin", "email": "admin@example.com"}
        self.users = {self.me["id"]: dict(self.me, deletedAt=None, quotaUsageInBytes=0)}
        self.shared_with_me = set()  # user ids that share with me
        self.assets = {}
        self.fail_asset_ids = set()

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
    def handle(self, method, path, q, body, api_key):
        if method != "GET":
            return 404, {"message": "no route"}
        if path == "/api/server/version":
            return 200, {"major": 3, "minor": 4, "patch": 2}
        if api_key != API_KEY:
            return 401, {"message": "Invalid API key"}
        if path == "/api/users/me":
            return 200, self.me
        if path == "/api/admin/users":
            return 200, list(self.users.values())
        if path == "/api/partners":
            return 200, [
                {"id": uid, "name": self.users[uid]["name"], "email": self.users[uid]["email"]}
                for uid in self.shared_with_me
            ]
        if path.startswith("/api/timeline/"):
            uid = q.get("userId")
            if uid != self.me["id"] and uid not in self.shared_with_me:
                return 400, {"message": "Not found or no timeline.read access"}
            assets = [a for a in self.assets.values() if a["ownerId"] == uid and a["visibility"] == "timeline" and not a["trashed"]]
            if path == "/api/timeline/buckets":
                months = {}
                for a in assets:
                    key = a["fileCreatedAt"][:7] + "-01"
                    months[key] = months.get(key, 0) + 1
                return 200, [{"timeBucket": k, "count": v} for k, v in sorted(months.items())]
            if path == "/api/timeline/bucket":
                tb = q["timeBucket"][:7]
                sel = [a for a in assets if a["fileCreatedAt"][:7] == tb]
                return 200, {
                    "id": [a["id"] for a in sel], "ownerId": [a["ownerId"] for a in sel],
                    "isTrashed": [False for _ in sel], "livePhotoVideoId": [a["livePhotoVideoId"] for a in sel],
                }
        m = re.match(r"^/api/assets/([0-9a-f-]{36})(/original)?$", path)
        if m:
            a = self.assets.get(m.group(1))
            if a is None or not self.can_read_asset(a):
                return 400, {"message": "Not found or no asset.read access"}
            if m.group(1) in self.fail_asset_ids:
                return 500, {"message": "boom"}
            if m.group(2):
                return 200, a["content"]
            return 200, self.detail(a)
        return 404, {"message": "no route"}
