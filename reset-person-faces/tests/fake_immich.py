"""A tiny fake of the Immich 3.4 endpoints used by reset_person_faces.py.

It follows the real server where it matters for this script:
- DELETE /faces/{id} soft-deletes unless the body has force=true, and refresh-faces
  doesn't re-create a face where a soft-deleted one still sits (IoU match).
- /search/metadata takes the 3.2+ `filter` object and pages with `nextCursor`.
- refresh-faces jobs run when the queues are polled; each re-detected face is assigned
  by `recognize(asset_id, old_person_id)`, which tests can replace.
"""

import datetime as dt
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, for the shared package
from shared.testing import FakeServer  # noqa: E402

API_KEY = "test-key"
ML = "machine-learning"


def new_id() -> str:
    return str(uuid.uuid4())


def parse_ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


class FakeImmich(FakeServer):
    def __init__(self):
        super().__init__()
        self.me = {"id": new_id(), "name": "Admin", "email": "admin@example.com"}
        self.people = {}
        self.assets = {}
        self.faces = {}
        self.albums = {}
        self.deleted = []  # (face_id, force)
        self.asset_jobs = []  # (name, asset_ids)
        self.pending_refresh = []
        self.queues_forbidden = False
        self.search_bodies = []
        self.page_size_limit = None  # force small pages to test cursors
        self.recognize = lambda asset_id, old_person_id: old_person_id

    # --- setup helpers ---------------------------------------------------------------
    def add_person(self, name):
        pid = new_id()
        self.people[pid] = {"id": pid, "name": name, "isHidden": False}
        return pid

    def add_asset(self, taken, owner=None, name=None, visibility="timeline"):
        aid = new_id()
        self.assets[aid] = {"id": aid, "ownerId": owner or self.me["id"], "originalFileName": name or f"{aid[:8]}.jpg",
                            "fileCreatedAt": taken, "localDateTime": taken, "visibility": visibility}
        return aid

    def add_face(self, asset_id, person_id=None, source=ML):
        fid = new_id()
        self.faces[fid] = {"id": fid, "assetId": asset_id, "personId": person_id, "sourceType": source, "deletedAt": None}
        return fid

    def add_album(self, name, asset_ids):
        alid = new_id()
        self.albums[alid] = {"id": alid, "albumName": name, "assetIds": list(asset_ids)}
        return alid

    def live_faces(self, asset_id):
        return [f for f in self.faces.values() if f["assetId"] == asset_id and not f["deletedAt"]]

    def face_dto(self, f):
        p = self.people.get(f["personId"])
        return {"id": f["id"], "sourceType": f["sourceType"], "boundingBoxX1": 0, "boundingBoxY1": 0,
                "boundingBoxX2": 10, "boundingBoxY2": 10, "imageWidth": 100, "imageHeight": 100,
                "person": dict(p) if p else None}

    # --- search ----------------------------------------------------------------------
    def matches(self, a, flt):
        if "personIds" in flt:
            ids = set(flt["personIds"]["any"])
            if not any(f["personId"] in ids for f in self.live_faces(a["id"])):
                return False
        if "albumIds" in flt:
            if not any(a["id"] in self.albums[al]["assetIds"] for al in flt["albumIds"]["any"]):
                return False
        if "takenAt" in flt:
            t = parse_ts(a["fileCreatedAt"])
            if "gte" in flt["takenAt"] and t < parse_ts(flt["takenAt"]["gte"]):
                return False
            if "lt" in flt["takenAt"] and t >= parse_ts(flt["takenAt"]["lt"]):
                return False
        if "visibility" in flt and a["visibility"] not in flt["visibility"]["in"]:
            return False
        return True

    def run_refresh(self):
        for aid in self.pending_refresh:
            for f in list(self.faces.values()):
                if f["assetId"] == aid and f.get("hardDeleted"):
                    del self.faces[f["id"]]
                    nf = self.add_face(aid, self.recognize(aid, f["personId"]))
                    self.faces[nf]["redetected"] = True
        self.pending_refresh = []

    # --- server ------------------------------------------------------------------------
    def handle(self, method, path, q, body, api_key):
        if api_key != API_KEY:
            return 401, {"message": "Invalid API key"}
        parts = path.split("/")[2:]  # after /api
        if method == "GET" and path == "/api/users/me":
            return 200, self.me
        if method == "GET" and path == "/api/people":
            size, page = int(q.get("size", 500)), int(q.get("page", 1))
            people = list(self.people.values())
            return 200, {"people": people[(page - 1) * size: page * size], "total": len(people), "hidden": 0,
                         "hasNextPage": page * size < len(people)}
        if method == "GET" and len(parts) == 3 and parts[0] == "people" and parts[2] == "statistics":
            n = len({f["assetId"] for f in self.faces.values() if f["personId"] == parts[1] and not f["deletedAt"]})
            return 200, {"assets": n}
        if method == "GET" and path == "/api/albums":
            return 200, [{"id": a["id"], "albumName": a["albumName"], "assetCount": len(a["assetIds"])} for a in self.albums.values()]
        if method == "POST" and path == "/api/search/metadata":
            self.search_bodies.append(body)
            unknown = set(body) - {"filter", "size", "cursor"}
            if unknown:
                return 400, {"message": f"unexpected fields {sorted(unknown)}"}
            size = min(body.get("size", 250), self.page_size_limit or 1000)
            offset = int(body.get("cursor") or 0)
            hits = [a for a in self.assets.values() if self.matches(a, body["filter"])]
            items = hits[offset:offset + size]
            nxt = str(offset + size) if offset + size < len(hits) else None
            return 200, {"assets": {"items": [dict(a) for a in items], "count": len(items), "nextCursor": nxt, "nextPage": None},
                         "albums": {"items": []}}
        if method == "GET" and path == "/api/faces":
            return 200, [self.face_dto(f) for f in self.live_faces(q["id"])]
        if method == "DELETE" and len(parts) == 2 and parts[0] == "faces":
            f = self.faces.get(parts[1])
            if not f or f["deletedAt"]:
                return 400, {"message": "Not found"}
            force = bool((body or {}).get("force"))
            self.deleted.append((f["id"], force))
            f["deletedAt"] = "now"
            f["hardDeleted"] = force
            return 204, None
        if method == "POST" and path == "/api/assets/jobs":
            self.asset_jobs.append((body["name"], list(body["assetIds"])))
            if body["name"] == "refresh-faces":
                self.pending_refresh.extend(body["assetIds"])
            return 204, None
        if method == "GET" and len(parts) == 2 and parts[0] == "queues":
            if self.queues_forbidden:
                return 403, {"message": "Missing required permission: queue.read"}
            waiting = len(self.pending_refresh)
            self.run_refresh()
            return 200, {"name": parts[1], "isPaused": False,
                         "statistics": {"waiting": waiting, "active": 0, "delayed": 0, "completed": 0, "failed": 0, "paused": 0}}
        return 404, {"message": f"no route {method} {path}"}
