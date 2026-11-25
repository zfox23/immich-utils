#!/usr/bin/env python3
"""
Back up OTHER Immich users' original photos and videos to a local folder.

Access model: each user you want to back up adds you (the Immich account that owns
the API key) as a *Partner* in Immich (Account Settings -> Sharing -> Partner sharing).
The script then reads each partner's timeline with your API key. Your own assets are
never downloaded.

Layout: <BACKUP_ROOT>/<User>/<YYYY>/<YYYY-MM-DD>/<original file name>

Standard library only. Requires Python 3.9+. Works on Windows, macOS and Linux.
Run with --help for options. See README.md for setup.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import logging
import os
import posixpath
import re
import shutil
import socket
import sys
import threading
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root, for the shared package
from shared import config as shared_config  # noqa: E402
from shared.config import ConfigError  # noqa: E402
from shared.immich_client import STOP, Cancelled, ImmichClient, ImmichHTTPError, http_error_hint  # noqa: E402

__version__ = "1.0.0"

SCRIPT_DIR = Path(__file__).resolve().parent
STATE_DIR_NAME = ".immich-backup"
LATEST_REPORT_NAME = "backup-report.html"
MANIFEST_VERSION = 1
REPORTS_TO_KEEP = 10
MIN_FREE_BYTES = 1024 ** 3  # stop downloading when less than 1 GiB is free
CHUNK_SIZE = 1024 * 1024
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")

log = logging.getLogger("immich-backup")


# --------------------------------------------------------------------------------------
# Status labels (shown in logs and the HTML report)
# --------------------------------------------------------------------------------------
ST_NEW = "new"
ST_EXISTING = "existing"
ST_UPDATED = "updated (original changed)"
ST_MOVED = "moved (date changed)"
ST_RESTORED = "restored (local file was missing)"
ST_ADOPTED = "adopted (already on disk)"
ST_FAILED = "failed"
ST_GONE_KEPT = "gone from server: kept"
ST_GONE_REMOVED = "gone from server: removed"
ST_NOT_CHECKED = "not checked this run"


# --------------------------------------------------------------------------------------
# Path helpers (pure functions; unit tested)
# --------------------------------------------------------------------------------------
_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
_RESERVED_TOP_LEVEL = {STATE_DIR_NAME.casefold(), LATEST_REPORT_NAME.casefold()}
MAX_COMPONENT_LEN = 150


def sanitize_component(name: str, fallback: str = "_") -> str:
    """Make a single path component safe on Windows, macOS and Linux."""
    s = _INVALID_CHARS.sub("_", name or "")
    s = s.lstrip(" ").rstrip(" .")
    if not s or s in (".", ".."):
        s = fallback
    if s.split(".")[0].upper() in _WINDOWS_RESERVED:
        s = "_" + s
    if len(s) > MAX_COMPONENT_LEN:
        stem, ext = os.path.splitext(s)
        if len(ext) > 20:
            stem, ext = s, ""
        s = stem[: MAX_COMPONENT_LEN - len(ext)].rstrip(" .") + ext
    return s


def date_part(local_date_time: Optional[str], fallback: Optional[str] = None) -> Optional[str]:
    """Return 'YYYY-MM-DD' from an Immich timestamp string, taken literally (no TZ math)."""
    for value in (local_date_time, fallback):
        if value:
            m = DATE_RE.match(value)
            if m:
                return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


def day_dir(user_folder: str, date_str: Optional[str]) -> str:
    """Relative (posix) directory for a user + capture date."""
    if not date_str:
        return f"{user_folder}/unknown-date"
    return f"{user_folder}/{date_str[:4]}/{date_str}"


def suffixed_name(file_name: str, asset_id: str, length: int = 8) -> str:
    stem, ext = os.path.splitext(file_name)
    return f"{stem}_{asset_id[:length]}{ext}"


def choose_rel_path(
    directory: str,
    file_name: str,
    asset_id: str,
    is_taken: Callable[[str], bool],
) -> str:
    """Pick a relative path for an asset, adding `_<id8>` (then the full id) on collision.

    `is_taken(rel_path)` must return True if the path belongs to a different asset or an
    untracked file. Comparison should be case-insensitive (Windows/macOS filesystems).
    """
    for candidate in (file_name, suffixed_name(file_name, asset_id), suffixed_name(file_name, asset_id, 36)):
        rel = f"{directory}/{candidate}"
        if not is_taken(rel):
            return rel
    raise RuntimeError(f"Could not find a free file name for asset {asset_id} in {directory}")


def choose_user_folder(display_name: str, user_id: str, used_folders: Dict[str, str]) -> str:
    """Pick a folder for a user. `used_folders` maps casefolded folder -> owning user id."""
    base = sanitize_component(display_name, fallback=f"user_{user_id[:8]}")
    if base.casefold() in _RESERVED_TOP_LEVEL:
        base = "_" + base
    for candidate in (base, f"{base}_{user_id[:8]}", f"{base}_{user_id}"):
        owner = used_folders.get(candidate.casefold())
        if owner is None or owner == user_id:
            return candidate
    raise RuntimeError(f"Could not choose a folder for user {user_id}")


def replaced_name(rel_path: str, asset_id: str, when: dt.date) -> str:
    directory, name = posixpath.split(rel_path)
    stem, ext = os.path.splitext(name)
    return f"{directory}/{stem}_{asset_id[:8]}.replaced-{when.strftime('%Y%m%d')}{ext}"


def find_gone_assets(
    manifest_assets: Dict[str, Dict[str, Any]],
    seen_ids: Set[str],
    fully_enumerated_owners: Set[str],
) -> List[str]:
    """Asset ids that are in the manifest, belong to a fully enumerated user, but weren't
    seen on the server this run. Users whose enumeration was incomplete (share removed,
    errors, --user filter) are never reported, so a broken run can't trigger mass deletes."""
    gone = []
    for asset_id, entry in manifest_assets.items():
        if entry.get("ownerId") in fully_enumerated_owners and asset_id not in seen_ids:
            gone.append(asset_id)
    return sorted(gone, key=lambda a: (manifest_assets[a].get("relPath") or "").casefold())


DOWNLOAD_KINDS = ("download", "restore", "replace", "move+replace")


def asset_size(detail: Dict[str, Any]) -> Optional[int]:
    """Original file size from Immich's asset details (exifInfo.fileSizeInByte), if known."""
    value = (detail.get("exifInfo") or {}).get("fileSizeInByte")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return int(value)
    return None


def space_needed(items: Iterable[Tuple[str, Optional[str], Optional[int]]]) -> Dict[str, List[int]]:
    """Sum download sizes per user. `items` are (user name, action kind, size or None).
    Returns {user: [files, known bytes, files of unknown size]}. Kinds that don't download
    (existing, move, adopt, or None) count as zero, but the user still gets a row."""
    out: Dict[str, List[int]] = {}
    for name, kind, size in items:
        row = out.setdefault(name, [0, 0, 0])
        if kind not in DOWNLOAD_KINDS:
            continue
        row[0] += 1
        if size is None:
            row[2] += 1
        else:
            row[1] += size
    return out


def free_space(path: Path) -> Optional[int]:
    """Free bytes on the disk holding `path` (or its nearest existing parent)."""
    p = path
    while not p.exists() and p.parent != p:
        p = p.parent
    try:
        return shutil.disk_usage(to_fs(p)).free
    except OSError:
        return None


def to_fs(path: Path) -> str:
    """Return a filesystem path string, using the Windows long-path prefix when needed."""
    s = os.path.abspath(str(path))
    if os.name == "nt" and len(s) >= 240 and not s.startswith("\\\\?\\"):
        if s.startswith("\\\\"):
            return "\\\\?\\UNC\\" + s[2:]
        return "\\\\?\\" + s
    return s


# --------------------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------------------
def load_config(args: argparse.Namespace) -> Dict[str, Any]:
    cli = {"server_url": args.server_url, "api_key": args.api_key, "backup_root": args.backup_root}
    required = ("server_url", "api_key") if args.probe else ("server_url", "api_key", "backup_root")
    cfg = shared_config.load_config(
        SCRIPT_DIR, args.config, cli, required=required, extra_env={"backup_root": "IMMICH_BACKUP_ROOT"}
    )
    if cfg.get("backup_root"):
        cfg["backup_root"] = Path(os.path.expanduser(str(cfg["backup_root"]))).resolve()
    return cfg


# --------------------------------------------------------------------------------------
# HTTP client (the generic parts live in shared/immich_client.py)
# --------------------------------------------------------------------------------------
class BackupClient(ImmichClient):
    def __init__(self, base_url: str, api_key: str, insecure: bool = False):
        super().__init__(
            base_url, api_key, insecure=insecure, user_agent=f"immich-backup-other-users/{__version__}", logger=log
        )

    def download_original(self, asset_id: str, dest: Path) -> Tuple[int, str]:
        """Stream an asset's original file to `dest`. Returns (bytes, base64 sha1)."""
        path = f"/assets/{asset_id}/original"

        def do():
            sha1 = hashlib.sha1()
            size = 0
            try:
                with self._open("GET", path, {"edited": False}) as resp, open(to_fs(dest), "wb") as out:
                    expected = resp.headers.get("Content-Length")
                    while True:
                        if STOP.is_set():
                            raise Cancelled()
                        chunk = resp.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        out.write(chunk)
                        sha1.update(chunk)
                        size += len(chunk)
                if expected is not None and int(expected) != size:
                    raise ConnectionError(f"incomplete download ({size} of {expected} bytes)")
            except BaseException:
                try:
                    os.remove(to_fs(dest))
                except OSError:
                    pass
                raise
            return size, base64.b64encode(sha1.digest()).decode("ascii")

        return self._with_retries("GET", path, do)

    def probe_original(self, asset_id: str) -> int:
        """Read the first bytes of an original to confirm download permission."""
        with self._open("GET", f"/assets/{asset_id}/original", {"edited": False}) as resp:
            return len(resp.read(1024))


# --------------------------------------------------------------------------------------
# State: lock + manifest
# --------------------------------------------------------------------------------------
def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class RunLock:
    def __init__(self, state_dir: Path):
        self.path = state_dir / "lock"
        self.held = False

    def acquire(self) -> None:
        info = {"pid": os.getpid(), "host": socket.gethostname(), "started": dt.datetime.now().isoformat()}
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    other = json.loads(self.path.read_text(encoding="utf-8"))
                except Exception:
                    other = {}
                same_host = other.get("host") == socket.gethostname()
                if same_host and not pid_alive(int(other.get("pid", 0))):
                    log.warning("Removing stale lock left by process %s.", other.get("pid"))
                    self.path.unlink(missing_ok=True)
                    continue
                raise RuntimeError(
                    f"Another backup appears to be running (lock {self.path}: {other}). "
                    "If you're sure it isn't, delete that file and try again."
                )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(info, f)
            self.held = True
            return
        raise RuntimeError(f"Could not acquire lock {self.path}")

    def release(self) -> None:
        if self.held:
            self.path.unlink(missing_ok=True)
            self.held = False


class Manifest:
    def __init__(self, state_dir: Path):
        self.path = state_dir / "manifest.json"
        self.bak = state_dir / "manifest.json.bak"
        self.lock = threading.RLock()
        self.data: Dict[str, Any] = {"version": MANIFEST_VERSION, "users": {}, "assets": {}}

    @property
    def users(self) -> Dict[str, Dict[str, Any]]:
        return self.data["users"]

    @property
    def assets(self) -> Dict[str, Dict[str, Any]]:
        return self.data["assets"]

    def load(self) -> None:
        for candidate in (self.path, self.bak):
            if not candidate.exists():
                continue
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
                if candidate == self.bak:
                    log.warning("Manifest was unreadable; recovered from %s.", self.bak)
                data.setdefault("users", {})
                data.setdefault("assets", {})
                self.data = data
                return
            except (json.JSONDecodeError, OSError) as e:
                log.warning("Could not read %s: %s", candidate, e)
        if self.path.exists() or self.bak.exists():
            log.warning("Starting with an empty manifest; files already on disk will be re-adopted by checksum.")

    def save(self) -> None:
        with self.lock:
            tmp = self.path.with_suffix(".json.tmp")
            payload = json.dumps(self.data, indent=1, ensure_ascii=False, sort_keys=True)
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            if self.path.exists():
                shutil.copy2(self.path, self.bak)
            os.replace(tmp, self.path)


# --------------------------------------------------------------------------------------
# Data gathering
# --------------------------------------------------------------------------------------
class UserInfo:
    _lock = threading.Lock()

    def __init__(self, user_id: str, name: str, email: str):
        self.id = user_id
        self.name = name
        self.email = email
        self.complete = False  # timeline fully enumerated without errors
        self.counts: Dict[str, int] = {}

    def bump(self, status: str, delta: int = 1) -> None:
        with UserInfo._lock:
            self.counts[status] = self.counts.get(status, 0) + delta


def fmt_bytes(n: Optional[int]) -> str:
    if n is None:
        return "?"
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{n} B"


def enumerate_partner(client: BackupClient, user: UserInfo) -> List[str]:
    """Return ids of the partner's non-trashed timeline assets (all stack members)."""
    params = {"userId": user.id, "visibility": "timeline"}
    buckets = client.get_json("/timeline/buckets", params)
    ids: List[str] = []
    for bucket in buckets:
        data = client.get_json("/timeline/bucket", {**params, "timeBucket": bucket["timeBucket"]})
        for i, asset_id in enumerate(data.get("id", [])):
            owner = (data.get("ownerId") or [None] * (i + 1))[i]
            trashed = (data.get("isTrashed") or [False] * (i + 1))[i]
            if owner == user.id and not trashed:
                ids.append(asset_id)
    return ids


def fetch_details(
    client: BackupClient, ids: Iterable[str], workers: int, label: str
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, str]]:
    """GET /assets/{id} for each id concurrently. Returns (details, errors)."""
    ids = list(dict.fromkeys(ids))
    details: Dict[str, Dict[str, Any]] = {}
    errors: Dict[str, str] = {}
    if not ids:
        return details, errors
    done = 0
    last_report = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(client.get_json, f"/assets/{asset_id}"): asset_id for asset_id in ids}
        try:
            for fut in as_completed(futures):
                asset_id = futures[fut]
                try:
                    details[asset_id] = fut.result()
                except Cancelled:
                    raise
                except Exception as e:
                    errors[asset_id] = str(e)
                done += 1
                if time.monotonic() - last_report > 5:
                    log.info("  %s: fetched details for %d/%d assets...", label, done, len(ids))
                    last_report = time.monotonic()
        except BaseException:
            STOP.set()
            for f in futures:
                f.cancel()
            raise
    return details, errors


# --------------------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------------------
class Action:
    """One thing to do for one asset."""

    def __init__(self, kind: str, asset_id: str, owner: UserInfo, detail: Dict[str, Any], rel_path: str, date_str: Optional[str], live_of: Optional[str]):
        self.kind = kind  # download | replace | move | restore | adopt | none
        self.asset_id = asset_id
        self.owner = owner
        self.detail = detail
        self.rel_path = rel_path
        self.date_str = date_str
        self.live_of = live_of
        self.old_rel_path: Optional[str] = None
        self.status = ""
        self.error = ""


class Backup:
    def __init__(self, cfg: Dict[str, Any], args: argparse.Namespace, client: BackupClient):
        self.cfg = cfg
        self.args = args
        self.client = client
        self.root: Path = cfg["backup_root"]
        self.state_dir = self.root / STATE_DIR_NAME
        self.manifest = Manifest(self.state_dir)
        self.started = dt.datetime.now()
        self.run_stamp = self.started.strftime("%Y%m%d-%H%M%S")
        self.server_version = "?"
        self.me: Dict[str, Any] = {}
        self.users: Dict[str, UserInfo] = {}
        self.claimed: Dict[str, str] = {}  # casefolded rel path -> asset id
        self.seen: Set[str] = set()
        self.actions: List[Action] = []
        self.failed_rows: List[Dict[str, Any]] = []
        self.removed_rows: List[Dict[str, Any]] = []
        self.status_by_id: Dict[str, str] = {}
        self.warnings: List[str] = []
        self.interrupted = False

    # ---- helpers --------------------------------------------------------------------
    def warn(self, msg: str, *a: Any) -> None:
        text = msg % a if a else msg
        self.warnings.append(text)
        log.warning(text)

    def abs(self, rel_path: str) -> Path:
        return self.root.joinpath(*rel_path.split("/"))

    def is_taken_by_other(self, asset_id: str) -> Callable[[str], bool]:
        def check(rel: str) -> bool:
            owner = self.claimed.get(rel.casefold())
            if owner is not None:
                return owner != asset_id
            return os.path.lexists(to_fs(self.abs(rel)))

        return check

    def claim(self, rel: str, asset_id: str) -> None:
        self.claimed[rel.casefold()] = asset_id

    def unclaim(self, rel: Optional[str]) -> None:
        if rel:
            self.claimed.pop(rel.casefold(), None)

    def user_folder(self, user: UserInfo) -> str:
        entry = self.manifest.users.get(user.id)
        if entry and entry.get("folder"):
            entry.update({"name": user.name, "email": user.email})
            return entry["folder"]
        used = {u["folder"].casefold(): uid for uid, u in self.manifest.users.items() if u.get("folder")}
        folder = choose_user_folder(user.name, user.id, used)
        self.manifest.users[user.id] = {"folder": folder, "name": user.name, "email": user.email}
        log.info("User %s will be backed up to folder '%s'.", user.name, folder)
        return folder

    # ---- phases ---------------------------------------------------------------------
    def connect(self) -> None:
        v = self.client.get_json("/server/version")
        self.server_version = f"{v.get('major')}.{v.get('minor')}.{v.get('patch')}"
        self.me = self.client.get_json("/users/me")
        log.info("Connected to %s (Immich %s) as %s <%s>.", self.cfg["server_url"], self.server_version, self.me.get("name"), self.me.get("email"))

    def discover_users(self) -> None:
        partners = self.client.get_json("/partners", {"direction": "shared-with"})
        me_id = self.me["id"]
        for p in partners:
            if p["id"] == me_id:
                continue
            self.users[p["id"]] = UserInfo(p["id"], p.get("name") or p.get("email") or p["id"], p.get("email", ""))
        log.info("%d user(s) share their library with you: %s", len(self.users), ", ".join(sorted(u.name for u in self.users.values())) or "(none)")

        # Admin view: find users who aren't sharing, and explain missing partners.
        admin_users: Optional[List[Dict[str, Any]]] = None
        try:
            admin_users = self.client.get_json("/admin/users", {"withDeleted": True})
        except ImmichHTTPError as e:
            if e.status in (401, 403):
                self.warn("API key can't list all users (needs the adminUser.read permission), so users who haven't shared with you can't be detected.")
            else:
                self.warn("Could not list all users: %s", e)
        if admin_users is not None:
            by_id = {u["id"]: u for u in admin_users}
            for u in admin_users:
                if u["id"] == me_id or u["id"] in self.users or u.get("deletedAt"):
                    continue
                self.warn(
                    "User %s <%s> is NOT sharing with you, so their assets are NOT backed up (%s on server). "
                    "Ask them to add you as a partner.",
                    u.get("name"), u.get("email"), fmt_bytes(u.get("quotaUsageInBytes")),
                )
            for uid, entry in self.manifest.users.items():
                if uid in self.users:
                    continue
                state = "was deleted from Immich" if (by_id.get(uid) or {}).get("deletedAt") or uid not in by_id else "stopped sharing with you"
                count = sum(1 for a in self.manifest.assets.values() if a.get("ownerId") == uid)
                self.warn("Previously backed-up user %s %s; their %d local file(s) are kept and were not checked.", entry.get("name"), state, count)
        else:
            for uid, entry in self.manifest.users.items():
                if uid not in self.users:
                    count = sum(1 for a in self.manifest.assets.values() if a.get("ownerId") == uid)
                    self.warn("Previously backed-up user %s no longer shares with you (or was deleted); their %d local file(s) are kept and were not checked.", entry.get("name"), count)

        if self.args.user:
            wanted = {w.casefold() for w in self.args.user}
            selected = {uid: u for uid, u in self.users.items() if {uid.casefold(), u.name.casefold(), u.email.casefold()} & wanted}
            if not selected:
                raise RuntimeError(f"--user {self.args.user} doesn't match any user sharing with you.")
            self.users = selected
            log.info("Limiting this run to: %s", ", ".join(u.name for u in selected.values()))

    def gather(self) -> Dict[str, Tuple[UserInfo, Dict[str, Any], Optional[str]]]:
        """Returns {asset_id: (owner, detail, live_photo_still_id)} for all reachable assets."""
        found: Dict[str, Tuple[UserInfo, Dict[str, Any], Optional[str]]] = {}
        for user in sorted(self.users.values(), key=lambda u: u.name.casefold()):
            log.info("Scanning %s...", user.name)
            try:
                ids = enumerate_partner(self.client, user)
            except Cancelled:
                raise
            except Exception as e:
                self.warn("Could not list assets for %s: %s. Skipping deletion checks for this user.", user.name, e)
                continue
            details, errors = fetch_details(self.client, ids, self.args.workers, user.name)
            live_ids: Dict[str, str] = {}
            for asset_id, d in details.items():
                if d.get("ownerId") != user.id or d.get("ownerId") == self.me["id"]:
                    continue
                found[asset_id] = (user, d, None)
                if d.get("livePhotoVideoId"):
                    live_ids[d["livePhotoVideoId"]] = asset_id
            live_details, live_errors = fetch_details(self.client, live_ids.keys(), self.args.workers, f"{user.name} (Live Photo videos)")
            for vid, d in live_details.items():
                if d.get("ownerId") == user.id and vid not in found:
                    found[vid] = (user, d, live_ids[vid])
            errors.update(live_errors)
            for asset_id, err in errors.items():
                self.warn("Could not read asset %s of %s: %s", asset_id, user.name, err)
                self.failed_rows.append({"assetId": asset_id, "ownerId": user.id, "originalFileName": "", "error": err})
                self.seen.add(asset_id)  # don't treat as deleted
                user.bump(ST_FAILED)
            user.complete = not errors
            log.info("  %s: %d asset(s) found (%d Live Photo video(s)).", user.name, len(details) + len(live_details), len(live_details))
        self.seen.update(found.keys())
        return found

    def plan(self, found: Dict[str, Tuple[UserInfo, Dict[str, Any], Optional[str]]]) -> None:
        # Claim every known path first so new assets never collide with tracked files.
        for asset_id, entry in self.manifest.assets.items():
            if entry.get("relPath"):
                self.claim(entry["relPath"], asset_id)
            for old in entry.get("replacedVersions", []):
                self.claim(old, asset_id)

        def sort_key(item):
            asset_id, (user, d, live_of) = item
            # Stills before their Live Photo videos so names are stable; then oldest first.
            return (user.name.casefold(), live_of is not None, d.get("fileCreatedAt") or "", asset_id)

        for asset_id, (user, d, live_of) in sorted(found.items(), key=sort_key):
            folder = self.user_folder(user)
            date_source = found[live_of][1] if live_of and live_of in found else d
            date_str = date_part(date_source.get("localDateTime"), date_source.get("fileCreatedAt"))
            directory = day_dir(folder, date_str)
            file_name = sanitize_component(d.get("originalFileName") or asset_id, fallback=asset_id)
            entry = self.manifest.assets.get(asset_id)

            if entry is None:
                # An untracked file with identical content (e.g. the manifest was lost) is adopted.
                rel, kind = None, "download"
                for name in (file_name, suffixed_name(file_name, asset_id)):
                    cand = f"{directory}/{name}"
                    if cand.casefold() not in self.claimed and os.path.isfile(to_fs(self.abs(cand))) and self._content_matches(self.abs(cand), d):
                        rel, kind = cand, "adopt"
                        break
                if rel is None:
                    rel = choose_rel_path(directory, file_name, asset_id, self.is_taken_by_other(asset_id))
                self.claim(rel, asset_id)
                self.actions.append(Action(kind, asset_id, user, d, rel, date_str, live_of))
                continue

            # Known asset.
            if entry.get("state") == "gone_kept":
                log.info("RETURNED  %s/%s is visible on the server again.", user.name, entry.get("relPath"))
                entry.pop("goneSince", None)
            entry["state"] = "present"
            rel = entry["relPath"]
            act = Action("none", asset_id, user, d, rel, date_str, live_of)
            current_dir = posixpath.dirname(rel)
            if current_dir.split("/")[1:] != directory.split("/")[1:]:
                new_rel = choose_rel_path(directory, file_name, asset_id, self.is_taken_by_other(asset_id))
                act.kind, act.old_rel_path, act.rel_path = "move", rel, new_rel
                self.claim(new_rel, asset_id)
            local = self.abs(act.old_rel_path or rel)
            if not os.path.isfile(to_fs(local)):
                act.kind = "restore"
                if act.old_rel_path:
                    self.unclaim(act.old_rel_path)
                    act.old_rel_path = None
            elif entry.get("checksum") and d.get("checksum") and entry["checksum"] != d["checksum"]:
                act.kind = "move+replace" if act.kind == "move" else "replace"
            elif entry.get("size") is not None and os.path.getsize(to_fs(local)) != entry["size"]:
                # Local copy is damaged: download again (a damaged file at the old path is removed).
                act.kind = "restore"
            self.actions.append(act)

    def _content_matches(self, path: Path, d: Dict[str, Any]) -> bool:
        if d.get("libraryId") or not d.get("checksum"):
            return False
        sha1 = hashlib.sha1()
        try:
            with open(to_fs(path), "rb") as f:
                for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
                    sha1.update(chunk)
        except OSError:
            return False
        return base64.b64encode(sha1.digest()).decode("ascii") == d["checksum"]

    # ---- execution ------------------------------------------------------------------
    def record(self, act: Action, size: Optional[int]) -> None:
        d = act.detail
        with self.manifest.lock:
            entry = self.manifest.assets.setdefault(act.asset_id, {"firstBackedUp": self.started.isoformat(timespec="seconds")})
            entry.update(
                {
                    "ownerId": act.owner.id,
                    "originalFileName": d.get("originalFileName"),
                    "relPath": act.rel_path,
                    "checksum": d.get("checksum"),
                    "localDateTime": d.get("localDateTime"),
                    "fileCreatedAt": d.get("fileCreatedAt"),
                    "type": d.get("type"),
                    "livePhotoOf": act.live_of,
                    "state": "present",
                    "lastSeen": self.started.isoformat(timespec="seconds"),
                }
            )
            if size is not None:
                entry["size"] = size
            if d.get("libraryId"):
                entry["externalLibrary"] = True

    def set_mtime(self, path: Path, d: Dict[str, Any]) -> None:
        ts = d.get("fileCreatedAt") or d.get("localDateTime")
        if not ts:
            return
        try:
            when = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=dt.timezone.utc)
            epoch = when.timestamp()
            os.utime(to_fs(path), (epoch, epoch))
        except (ValueError, OSError, OverflowError):
            pass

    def download(self, act: Action) -> int:
        final = self.abs(act.rel_path)
        os.makedirs(to_fs(final.parent), exist_ok=True)
        free = shutil.disk_usage(to_fs(final.parent)).free
        if free < MIN_FREE_BYTES:
            raise RuntimeError(f"less than {fmt_bytes(MIN_FREE_BYTES)} free on the backup disk")
        part = final.with_name(final.name + f".{act.asset_id[:8]}.part")
        size, checksum = self.client.download_original(act.asset_id, part)
        expected = act.detail.get("checksum")
        if expected and not act.detail.get("libraryId") and checksum != expected:
            os.remove(to_fs(part))
            raise RuntimeError(f"checksum mismatch (expected {expected}, got {checksum})")
        os.replace(to_fs(part), to_fs(final))
        self.set_mtime(final, act.detail)
        return size

    def execute_one(self, act: Action) -> None:
        name = f"{act.owner.name}: {act.rel_path}"
        if act.kind == "none":
            act.status = ST_EXISTING
            return
        if act.kind == "adopt":
            self.record(act, os.path.getsize(to_fs(self.abs(act.rel_path))))
            act.status = ST_ADOPTED
            log.info("ADOPTED   %s", name)
            return
        if act.kind in ("move", "move+replace"):
            src, dst = self.abs(act.old_rel_path), self.abs(act.rel_path)
            os.makedirs(to_fs(dst.parent), exist_ok=True)
            os.replace(to_fs(src), to_fs(dst))
            self.unclaim(act.old_rel_path)
            self.remove_empty_dirs(src.parent)
            with self.manifest.lock:
                self.manifest.assets[act.asset_id]["relPath"] = act.rel_path
            log.info("MOVED     %s: %s -> %s", act.owner.name, act.old_rel_path, act.rel_path)
            if act.kind == "move":
                act.status = ST_MOVED
                self.record(act, None)
                return
        if act.kind in ("replace", "move+replace"):
            current = self.abs(act.rel_path)
            old_rel = replaced_name(act.rel_path, act.asset_id, self.started.date())
            os.replace(to_fs(current), to_fs(self.abs(old_rel)))
            with self.manifest.lock:
                entry = self.manifest.assets[act.asset_id]
                entry.setdefault("replacedVersions", []).append(old_rel)
            self.claim(old_rel, act.asset_id)
            size = self.download(act)
            self.record(act, size)
            act.status = ST_UPDATED
            log.info("UPDATED   %s (original changed on server; previous version kept as %s)", name, old_rel)
            return
        size = self.download(act)
        if act.kind == "restore" and act.old_rel_path and act.old_rel_path != act.rel_path:
            try:
                os.remove(to_fs(self.abs(act.old_rel_path)))
                self.remove_empty_dirs(self.abs(act.old_rel_path).parent)
            except OSError:
                pass
            self.unclaim(act.old_rel_path)
        self.record(act, size)
        act.status = ST_RESTORED if act.kind == "restore" else ST_NEW
        log.info("%s %s (%s)", "RESTORED " if act.kind == "restore" else "DOWNLOADED", name, fmt_bytes(size))

    def log_space_needed(self) -> Tuple[int, Optional[int]]:
        """Log per-user and total download sizes plus free space. Returns (known bytes, free)."""
        per_user = space_needed(
            [(u.name, None, None) for u in self.users.values()]
            + [(a.owner.name, a.kind, asset_size(a.detail)) for a in self.actions]
        )
        width = max([len("Total")] + [len(n) for n in per_user])
        log.info("")
        log.info("Disk space this run will use (new and changed files):")
        log.info("  %-*s  %7s  %10s", width, "User", "Files", "Size")
        tot_files = tot_bytes = tot_unknown = 0
        for name in sorted(per_user, key=str.casefold):
            files, size, unknown = per_user[name]
            tot_files, tot_bytes, tot_unknown = tot_files + files, tot_bytes + size, tot_unknown + unknown
            log.info("  %-*s  %7d  %10s%s", width, name, files, fmt_bytes(size), f"  (+{unknown} of unknown size)" if unknown else "")
        log.info("  %-*s  %7d  %10s%s", width, "Total", tot_files, fmt_bytes(tot_bytes), f"  (+{tot_unknown} of unknown size)" if tot_unknown else "")
        free = free_space(self.root)
        if free is not None:
            log.info("  Free on backup disk: %s", fmt_bytes(free))
        log.info("")
        return tot_bytes, free

    def execute(self) -> None:
        todo = [a for a in self.actions if a.kind != "none"]
        for a in self.actions:
            if a.kind == "none":
                a.status = ST_EXISTING
        n_dl = sum(1 for a in todo if a.kind in DOWNLOAD_KINDS)
        log.info("Plan: %d to download, %d to move, %d to adopt, %d already backed up.",
                 n_dl, sum(1 for a in todo if a.kind == "move"), sum(1 for a in todo if a.kind == "adopt"),
                 len(self.actions) - len(todo))
        if self.args.dry_run:
            for a in todo:
                extra = f" (from {a.old_rel_path})" if a.old_rel_path else ""
                size = f" [{fmt_bytes(asset_size(a.detail))}]" if a.kind in DOWNLOAD_KINDS else ""
                log.info("[dry-run] %-12s %s: %s%s%s", a.kind, a.owner.name, a.rel_path, size, extra)
            needed, free = self.log_space_needed()
            if free is not None and needed > free - MIN_FREE_BYTES:
                self.warn("Not enough free space: the real run needs %s but only %s is free (and keeps %s spare).",
                          fmt_bytes(needed), fmt_bytes(free), fmt_bytes(MIN_FREE_BYTES))
            return

        needed, free = self.log_space_needed()
        if free is not None and needed > free - MIN_FREE_BYTES:
            self.warn("This run needs %s but only %s is free; downloads will stop when less than %s is left.",
                      fmt_bytes(needed), fmt_bytes(free), fmt_bytes(MIN_FREE_BYTES))

        # Moves/adopts are quick and touch shared paths: do them serially first.
        serial = [a for a in todo if a.kind in ("move", "adopt", "move+replace")]
        parallel = [a for a in todo if a.kind not in ("move", "adopt", "move+replace")]
        for a in serial:
            self._run_guarded(a)
        self.manifest.save()

        done = 0
        last_save = time.monotonic()
        last_report = time.monotonic()
        disk_full = threading.Event()
        with ThreadPoolExecutor(max_workers=self.args.workers) as pool:
            futures = {pool.submit(self._run_guarded, a, disk_full): a for a in parallel}
            try:
                for _ in as_completed(futures):
                    done += 1
                    now = time.monotonic()
                    if now - last_save > 30:
                        self.manifest.save()
                        last_save = now
                    if now - last_report > 10:
                        log.info("Progress: %d/%d", done, len(parallel))
                        last_report = now
            except KeyboardInterrupt:
                STOP.set()
                for f in futures:
                    f.cancel()
                raise
        self.manifest.save()

    def _run_guarded(self, act: Action, disk_full: Optional[threading.Event] = None) -> None:
        if STOP.is_set():
            act.status, act.error = ST_FAILED, "cancelled"
            return
        if disk_full is not None and disk_full.is_set():
            act.status, act.error = ST_FAILED, "skipped: backup disk is nearly full"
            act.owner.bump(ST_FAILED)
            return
        try:
            self.execute_one(act)
        except Cancelled:
            act.status, act.error = ST_FAILED, "cancelled"
        except Exception as e:
            act.status, act.error = ST_FAILED, str(e)
            if disk_full is not None and "free on the backup disk" in str(e) and not disk_full.is_set():
                disk_full.set()
                self.warn("Backup disk is nearly full; remaining downloads skipped.")
            log.error("FAILED    %s: %s (%s)", act.owner.name, act.rel_path, e)
        act.owner.bump(act.status)

    def remove_empty_dirs(self, directory: Path) -> None:
        root = self.root.resolve()
        d = directory
        while True:
            try:
                d_res = d.resolve()
            except OSError:
                return
            if d_res == root or root not in d_res.parents:
                return
            # Never remove a user's top-level folder.
            if d_res.parent == root:
                return
            try:
                os.rmdir(to_fs(d))
            except OSError:
                return
            d = d.parent

    # ---- deletions ------------------------------------------------------------------
    def handle_gone(self) -> None:
        complete = {u.id for u in self.users.values() if u.complete}
        assets = self.manifest.assets
        gone = find_gone_assets(assets, self.seen, complete)
        for asset_id in gone:
            self.status_by_id[asset_id] = ST_GONE_KEPT
        new_gone = [a for a in gone if assets[a].get("state") != "gone_kept"]
        old_gone = [a for a in gone if assets[a].get("state") == "gone_kept"]

        def uname(asset_id: str) -> str:
            return (self.manifest.users.get(assets[asset_id].get("ownerId")) or {}).get("name", "?")

        for asset_id in new_gone:
            e = assets[asset_id]
            log.info("REMOTE-DELETED user=%s file=%s (original name: %s, Immich id %s) - deleted or trashed on the server.",
                     uname(asset_id), e.get("relPath"), e.get("originalFileName"), asset_id)
        if old_gone:
            log.info("%d file(s) gone from the server were kept in earlier runs and are still kept.", len(old_gone))
        for asset_id in gone:
            owner = self.users.get(assets[asset_id].get("ownerId"))
            if owner:
                owner.bump(ST_GONE_KEPT)
        if not new_gone or self.args.dry_run:
            return

        interactive = not self.args.non_interactive and sys.stdin.isatty()
        to_delete: List[str] = []
        if interactive:
            to_delete = self.prompt_gone(new_gone, uname)
        else:
            log.info("Non-interactive run: keeping %d file(s) that are gone from the server.", len(new_gone))

        now = self.started.isoformat(timespec="seconds")
        for asset_id in new_gone:
            if asset_id in to_delete:
                continue
            assets[asset_id]["state"] = "gone_kept"
            assets[asset_id]["goneSince"] = now
        for asset_id in to_delete:
            e = assets[asset_id]
            paths = [e.get("relPath")] + list(e.get("replacedVersions", []))
            for rel in [p for p in paths if p]:
                try:
                    os.remove(to_fs(self.abs(rel)))
                    self.remove_empty_dirs(self.abs(rel).parent)
                except FileNotFoundError:
                    pass
                except OSError as err:
                    self.warn("Could not delete %s: %s", rel, err)
            log.info("REMOVED   %s: %s", uname(asset_id), e.get("relPath"))
            self.removed_rows.append({**e, "assetId": asset_id})
            self.status_by_id[asset_id] = ST_GONE_REMOVED
            owner = self.users.get(e.get("ownerId"))
            if owner:
                owner.bump(ST_GONE_KEPT, -1)
                owner.bump(ST_GONE_REMOVED)
            del assets[asset_id]
        self.manifest.save()

    def prompt_gone(self, gone: List[str], uname: Callable[[str], str]) -> List[str]:
        assets = self.manifest.assets
        print()
        print("=" * 78)
        print(f"{len(gone)} backed-up file(s) are gone from the Immich server (deleted or trashed):")
        for asset_id in gone:
            print(f"  [{uname(asset_id)}] {assets[asset_id].get('relPath')}")
        print("=" * 78)
        while True:
            choice = input("What should happen to these local copies?  [k]eep all / [d]elete all / [r]eview each: ").strip().lower()
            if choice in ("k", "keep"):
                return []
            if choice in ("d", "delete"):
                confirm = input(f"Permanently delete {len(gone)} local file(s)? Type 'yes' to confirm: ").strip().lower()
                if confirm == "yes":
                    return list(gone)
                continue
            if choice in ("r", "review"):
                break
        selected: List[str] = []
        for i, asset_id in enumerate(gone, 1):
            while True:
                c = input(f"({i}/{len(gone)}) [{uname(asset_id)}] {assets[asset_id].get('relPath')}  [k]eep / [d]elete / [K]eep rest / [D]elete rest: ").strip()
                if c in ("k", "d", "K", "D"):
                    break
            if c == "d":
                selected.append(asset_id)
            elif c == "K":
                return selected
            elif c == "D":
                return selected + gone[i - 1:]
        return selected

    # ---- report ---------------------------------------------------------------------
    def report_rows(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        act_by_id = {a.asset_id: a for a in self.actions}
        users = self.manifest.users

        def uname(owner_id):
            return (users.get(owner_id) or {}).get("name") or (self.users.get(owner_id).name if owner_id in self.users else owner_id)

        for asset_id, e in self.manifest.assets.items():
            act = act_by_id.get(asset_id)
            if asset_id in self.status_by_id:
                status = self.status_by_id[asset_id]
            elif act is not None:
                status = act.status or ST_EXISTING
            else:
                status = ST_NOT_CHECKED
            note = ""
            if act is not None and act.error:
                note = act.error
            if e.get("goneSince"):
                note = f"gone from server since {e['goneSince'][:10]}"
            if e.get("replacedVersions"):
                note = (note + "; " if note else "") + "previous versions: " + ", ".join(e["replacedVersions"])
            rows.append({
                "user": uname(e.get("ownerId")),
                "date": date_part(e.get("localDateTime"), e.get("fileCreatedAt")) or "",
                "local": e.get("relPath") if status != ST_FAILED or os.path.exists(to_fs(self.abs(e.get("relPath", "")))) else "",
                "name": e.get("originalFileName") or "",
                "id": asset_id,
                "checksum": e.get("checksum") or "",
                "status": status,
                "live": bool(e.get("livePhotoOf")),
                "note": note,
            })
        for act in self.actions:
            if act.asset_id not in self.manifest.assets:
                rows.append({
                    "user": act.owner.name, "date": act.date_str or "", "local": "",
                    "name": act.detail.get("originalFileName") or "", "id": act.asset_id,
                    "checksum": act.detail.get("checksum") or "", "status": act.status or ST_FAILED,
                    "live": bool(act.live_of), "note": act.error,
                })
        for f in self.failed_rows:
            rows.append({"user": uname(f["ownerId"]), "date": "", "local": "", "name": f.get("originalFileName", ""),
                         "id": f["assetId"], "checksum": "", "status": ST_FAILED, "live": False, "note": f["error"]})
        for r in self.removed_rows:
            rows.append({"user": uname(r.get("ownerId")), "date": date_part(r.get("localDateTime"), r.get("fileCreatedAt")) or "",
                         "local": "", "name": r.get("originalFileName") or "", "id": r["assetId"], "checksum": r.get("checksum") or "",
                         "status": ST_GONE_REMOVED, "live": bool(r.get("livePhotoOf")), "note": f"was {r.get('relPath')}"})
        rows.sort(key=lambda r: (r["user"].casefold(), r["date"], r["local"] or r["name"]), reverse=False)
        return rows

    def write_report(self) -> Optional[Path]:
        rows = self.report_rows()
        reports_dir = self.state_dir / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        stamped = reports_dir / f"backup-{self.run_stamp}.html"
        meta = {
            "server": self.cfg["server_url"],
            "serverVersion": self.server_version,
            "me": self.me.get("name") or "",
            "started": self.started.isoformat(timespec="seconds"),
            "finished": dt.datetime.now().isoformat(timespec="seconds"),
            "interrupted": self.interrupted,
            "warnings": self.warnings,
            "users": [
                {"name": u.name, "folder": (self.manifest.users.get(u.id) or {}).get("folder", ""), "complete": u.complete, "counts": u.counts}
                for u in sorted(self.users.values(), key=lambda u: u.name.casefold())
            ],
        }
        stamped.write_text(render_report(meta, rows, f"{STATE_DIR_NAME}/reports"), encoding="utf-8")
        (self.root / LATEST_REPORT_NAME).write_text(render_report(meta, rows, ""), encoding="utf-8")
        old = sorted(reports_dir.glob("backup-*.html"))
        for p in old[:-REPORTS_TO_KEEP]:
            try:
                p.unlink()
            except OSError:
                pass
        return self.root / LATEST_REPORT_NAME

    def summary(self) -> None:
        log.info("")
        log.info("Summary:")
        for u in sorted(self.users.values(), key=lambda u: u.name.casefold()):
            parts = ", ".join(f"{v} {k}" for k, v in sorted(u.counts.items()) if v) or "nothing to do"
            flag = "" if u.complete else "  [INCOMPLETE - deletion checks skipped]"
            log.info("  %s: %s%s", u.name, parts, flag)
        if self.warnings:
            log.info("%d warning(s); see above.", len(self.warnings))

    def run(self) -> int:
        self.connect()
        self.discover_users()
        found = self.gather()
        self.plan(found)
        self.execute()
        self.handle_gone()
        failures = sum(1 for a in self.actions if a.status == ST_FAILED) + len(self.failed_rows)
        return 1 if failures else 0


# --------------------------------------------------------------------------------------
# HTML report
# --------------------------------------------------------------------------------------
_REPORT_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Immich Backup Report</title>
<style>
:root { --bg:#fff; --fg:#1d1d1f; --muted:#6e6e73; --line:#e3e3e6; --accent:#4250af; --warn:#9a5b00; --bad:#b3261e; --chip:#f2f2f5; }
@media (prefers-color-scheme: dark) { :root { --bg:#151517; --fg:#ececf0; --muted:#a0a0a8; --line:#2c2c31; --accent:#8f9cff; --warn:#f0b35a; --bad:#ff8a80; --chip:#232328; } }
* { box-sizing: border-box; }
body { margin:0; padding:24px 16px; background:var(--bg); color:var(--fg); font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif; }
main { max-width: 1400px; margin: 0 auto; }
h1 { font-size: 22px; margin: 0 0 4px; }
.meta { color: var(--muted); margin-bottom: 16px; }
.users { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:16px; }
.user { background:var(--chip); border-radius:8px; padding:8px 12px; }
.user b { display:block; }
.user .inc { color: var(--warn); }
.warnings { border-left: 3px solid var(--warn); padding: 4px 12px; margin-bottom: 16px; }
.warnings li { margin: 2px 0; }
.controls { display:flex; flex-wrap:wrap; gap:8px; margin: 12px 0; align-items:center; }
input, select, button { font: inherit; color: var(--fg); background: var(--bg); border:1px solid var(--line); border-radius:6px; padding:6px 8px; }
input[type=search] { flex: 1 1 260px; }
.count { color: var(--muted); }
.wrap { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); vertical-align: top; }
th { position: sticky; top: 0; background: var(--bg); font-weight: 600; }
td.mono { font-family: ui-monospace, Consolas, monospace; font-size: 12px; color: var(--muted); }
a { color: var(--accent); }
.st { white-space: nowrap; }
.st-failed, .st-gone { color: var(--bad); }
.st-new, .st-updated, .st-moved, .st-restored, .st-adopted { color: var(--accent); }
.note { color: var(--muted); font-size: 12px; }
#more { margin: 12px 0; }
</style>
</head>
<body>
<main>
<h1>Immich Backup Report</h1>
<div class="meta" id="meta"></div>
<div class="users" id="users"></div>
<ul class="warnings" id="warnings" hidden></ul>
<div class="controls">
  <input type="search" id="q" placeholder="Filter by file name, path, asset id or note">
  <select id="fuser"><option value="">All users</option></select>
  <select id="fstatus"><option value="">All statuses</option></select>
  <span class="count" id="count"></span>
</div>
<div class="wrap">
<table>
<thead><tr><th>User</th><th>Capture date</th><th>Local file</th><th>Immich</th><th>Status</th><th>Asset id / SHA-1</th></tr></thead>
<tbody id="rows"></tbody>
</table>
</div>
<button id="more" hidden>Show more</button>
</main>
<script type="application/json" id="report-data">__DATA__</script>
<script>
(function () {
  var data = JSON.parse(document.getElementById('report-data').textContent);
  var meta = data.meta, rows = data.rows, prefix = data.prefix;
  function el(tag, attrs, text) {
    var e = document.createElement(tag);
    if (attrs) for (var k in attrs) e.setAttribute(k, attrs[k]);
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }
  function hrefFor(rel) {
    return prefix + rel.split('/').map(encodeURIComponent).join('/');
  }
  var idRe = /^[0-9a-fA-F-]{36}$/;
  document.getElementById('meta').textContent =
    'Server ' + meta.server + ' (Immich ' + meta.serverVersion + ') \\u00b7 backed up as ' + meta.me +
    ' \\u00b7 run ' + meta.started.replace('T', ' ') + ' \\u2013 ' + meta.finished.replace('T', ' ') +
    (meta.interrupted ? ' \\u00b7 INTERRUPTED' : '');
  var ub = document.getElementById('users');
  meta.users.forEach(function (u) {
    var d = el('div', {class: 'user'});
    d.appendChild(el('b', null, u.name + (u.folder ? '  \\u2192 ' + u.folder + '/' : '')));
    var parts = Object.keys(u.counts).filter(function (k) { return u.counts[k]; }).map(function (k) { return u.counts[k] + ' ' + k; });
    d.appendChild(el('span', null, parts.join(', ') || 'nothing to do'));
    if (!u.complete) d.appendChild(el('div', {class: 'inc'}, 'Incomplete scan; deletion checks skipped'));
    ub.appendChild(d);
  });
  if (meta.warnings.length) {
    var w = document.getElementById('warnings'); w.hidden = false;
    meta.warnings.forEach(function (t) { w.appendChild(el('li', null, t)); });
  }
  function fillSelect(id, values) {
    var s = document.getElementById(id);
    values.forEach(function (v) { s.appendChild(el('option', {value: v}, v)); });
  }
  function uniq(key) { var m = {}; rows.forEach(function (r) { m[r[key]] = 1; }); return Object.keys(m).sort(); }
  fillSelect('fuser', uniq('user'));
  fillSelect('fstatus', uniq('status'));
  var PAGE = 500, shown = PAGE, filtered = rows;
  var tbody = document.getElementById('rows');
  function stClass(s) {
    if (s === 'failed') return 'st-failed';
    if (s.indexOf('gone') === 0) return 'st-gone';
    return 'st-' + s.split(' ')[0];
  }
  function render() {
    tbody.textContent = '';
    filtered.slice(0, shown).forEach(function (r) {
      var tr = el('tr');
      tr.appendChild(el('td', null, r.user));
      tr.appendChild(el('td', null, r.date));
      var tdl = el('td');
      if (r.local) { var a = el('a', {href: hrefFor(r.local)}, r.local); tdl.appendChild(a); }
      else tdl.appendChild(el('span', {class: 'note'}, r.name ? '(no local file) ' + r.name : '(no local file)'));
      if (r.live) tdl.appendChild(el('div', {class: 'note'}, 'Live Photo video'));
      if (r.note) tdl.appendChild(el('div', {class: 'note'}, r.note));
      tr.appendChild(tdl);
      var tdi = el('td');
      if (idRe.test(r.id) && r.status.indexOf('gone') !== 0) {
        tdi.appendChild(el('a', {href: meta.server + '/photos/' + r.id, target: '_blank', rel: 'noopener'}, 'Open in Immich'));
      } else tdi.appendChild(el('span', {class: 'note'}, '\\u2014'));
      tr.appendChild(tdi);
      tr.appendChild(el('td', {class: 'st ' + stClass(r.status)}, r.status));
      var tdm = el('td', {class: 'mono'}); tdm.appendChild(el('div', null, r.id)); tdm.appendChild(el('div', null, r.checksum));
      tr.appendChild(tdm);
      tbody.appendChild(tr);
    });
    document.getElementById('count').textContent = Math.min(shown, filtered.length) + ' of ' + filtered.length + ' shown (' + rows.length + ' total)';
    document.getElementById('more').hidden = shown >= filtered.length;
  }
  function apply() {
    var q = document.getElementById('q').value.toLowerCase();
    var fu = document.getElementById('fuser').value, fs = document.getElementById('fstatus').value;
    filtered = rows.filter(function (r) {
      if (fu && r.user !== fu) return false;
      if (fs && r.status !== fs) return false;
      if (!q) return true;
      return (r.local + ' ' + r.name + ' ' + r.id + ' ' + r.checksum + ' ' + r.note + ' ' + r.date).toLowerCase().indexOf(q) !== -1;
    });
    shown = PAGE; render();
  }
  document.getElementById('q').addEventListener('input', apply);
  document.getElementById('fuser').addEventListener('change', apply);
  document.getElementById('fstatus').addEventListener('change', apply);
  document.getElementById('more').addEventListener('click', function () { shown += PAGE * 4; render(); });
  render();
})();
</script>
</body>
</html>
"""


def _json_for_script(obj: Any) -> str:
    """JSON safe to embed inside a <script> element (no `</script>` or comment breakouts)."""
    s = json.dumps(obj, ensure_ascii=False)
    return s.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def render_report(meta: Dict[str, Any], rows: List[Dict[str, Any]], report_dir_rel: str) -> str:
    """Render the report. `report_dir_rel` is the report's directory relative to the backup
    root (posix, '' for the root). Local links are relative so the backup root can move."""
    prefix = "../" * len([p for p in report_dir_rel.split("/") if p])
    data = {"meta": meta, "rows": rows, "prefix": prefix}
    return _REPORT_TEMPLATE.replace("__DATA__", _json_for_script(data))


# --------------------------------------------------------------------------------------
# Probe
# --------------------------------------------------------------------------------------
def run_probe(client: BackupClient) -> int:
    ok = True

    def check(label: str, fn: Callable[[], Any]) -> Any:
        nonlocal ok
        try:
            result = fn()
            print(f"  PASS  {label}")
            return result
        except Exception as e:
            ok = False
            print(f"  FAIL  {label}: {e}")
            return None

    print("Probing Immich API...")
    v = check("GET /server/version", lambda: client.get_json("/server/version"))
    if v:
        print(f"        Immich {v.get('major')}.{v.get('minor')}.{v.get('patch')}")
    me = check("GET /users/me (user.read)", lambda: client.get_json("/users/me"))
    if me:
        print(f"        You are {me.get('name')} <{me.get('email')}>, id {me.get('id')}")
    check("GET /admin/users (adminUser.read, optional)", lambda: client.get_json("/admin/users"))
    partners = check("GET /partners?direction=shared-with (partner.read)", lambda: client.get_json("/partners", {"direction": "shared-with"}))
    if not partners:
        print("  No users share their library with you yet, so asset access can't be tested.")
        return 0 if ok else 1
    p = partners[0]
    print(f"        {len(partners)} partner(s); testing with {p.get('name')}")
    params = {"userId": p["id"], "visibility": "timeline"}
    buckets = check("GET /timeline/buckets for partner (timeline.read)", lambda: client.get_json("/timeline/buckets", params))
    if buckets:
        bucket = check("GET /timeline/bucket for partner", lambda: client.get_json("/timeline/bucket", {**params, "timeBucket": buckets[0]["timeBucket"]}))
        if bucket and bucket.get("id"):
            asset_id = bucket["id"][0]
            detail = check("GET /assets/{id} for a partner asset (asset.read)", lambda: client.get_json(f"/assets/{asset_id}"))
            if detail:
                print(f"        {detail.get('originalFileName')} localDateTime={detail.get('localDateTime')}")
            check("GET /assets/{id}/original for a partner asset (asset.download)", lambda: client.probe_original(asset_id))
            live = next((x for x in (bucket.get("livePhotoVideoId") or []) if x), None)
            if live:
                check("GET /assets/{id} for a hidden Live Photo video", lambda: client.get_json(f"/assets/{live}"))
            else:
                print("  SKIP  Live Photo video access (none in the first time bucket)")
    print("All checks passed." if ok else "Some checks failed; see above.")
    return 0 if ok else 1


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------
def close_logging() -> None:
    for h in list(log.handlers):
        log.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass


def setup_logging(state_dir: Optional[Path], stamp: str, verbose: bool) -> Optional[Path]:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass
    close_logging()
    log.setLevel(logging.DEBUG)
    log.propagate = False
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(console)
    if state_dir is None:
        return None
    logs = state_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    path = logs / f"backup-{stamp}.log"
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    log.addHandler(fh)
    return path


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Back up other Immich users' original photos and videos (shared with you via partner sharing) to a local folder.",
    )
    p.add_argument("--config", help="Path to config JSON (default: config.json next to this script)")
    p.add_argument("--server-url", dest="server_url", help="Immich URL, e.g. https://immich.example.com (env IMMICH_URL)")
    p.add_argument("--api-key", dest="api_key", help="Immich API key (env IMMICH_API_KEY)")
    p.add_argument("--backup-root", dest="backup_root", help="Local backup folder (env IMMICH_BACKUP_ROOT)")
    p.add_argument("--user", action="append", help="Only back up this user (name, email or id). Repeatable.")
    p.add_argument("--dry-run", action="store_true", help="Show what would happen without changing anything")
    p.add_argument("--non-interactive", action="store_true", help="Never prompt; keep files that are gone from the server")
    p.add_argument("--workers", type=int, default=4, help="Parallel requests/downloads (default 4)")
    p.add_argument("--insecure", action="store_true", help="Don't verify the server's TLS certificate")
    p.add_argument("--probe", action="store_true", help="Check API access and permissions, then exit")
    p.add_argument("--verbose", action="store_true", help="More detailed logging")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = p.parse_args(argv)
    args.workers = max(1, min(args.workers, 16))
    return args


def main(argv: Optional[List[str]] = None) -> int:
    try:
        return _main(argv)
    finally:
        close_logging()


def _main(argv: Optional[List[str]] = None) -> int:
    STOP.clear()
    args = parse_args(argv)
    try:
        cfg = load_config(args)
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 2
    client = BackupClient(cfg["server_url"], cfg["api_key"], insecure=args.insecure)

    if args.probe:
        setup_logging(None, "", args.verbose)
        return run_probe(client)

    root: Path = cfg["backup_root"]
    if args.dry_run:
        setup_logging(None, "", args.verbose)
        log.info("DRY RUN: nothing will be downloaded, moved or deleted.")
    else:
        root.mkdir(parents=True, exist_ok=True)
    state_dir = root / STATE_DIR_NAME
    backup = Backup(cfg, args, client)
    lock = RunLock(state_dir)
    if not args.dry_run:
        state_dir.mkdir(parents=True, exist_ok=True)
        log_path = setup_logging(state_dir, backup.run_stamp, args.verbose)
        log.info("Immich backup %s - log: %s", __version__, log_path)
    log.info("Backup folder: %s", root)

    code = 0
    try:
        if not args.dry_run:
            lock.acquire()
        backup.manifest.load()
        code = backup.run()
    except KeyboardInterrupt:
        STOP.set()
        backup.interrupted = True
        log.error("Interrupted. Progress so far has been saved; run again to continue.")
        code = 130
    except ImmichHTTPError as e:
        hint = " Check the API key and its permissions (run with --probe)." if http_error_hint(e) else ""
        log.error("Server error: %s%s", e, hint)
        code = 2
    except (urllib.error.URLError, OSError, RuntimeError) as e:
        log.error("Error: %s", e)
        code = 2
    finally:
        if not args.dry_run and lock.held:
            try:
                backup.manifest.save()
                report = backup.write_report()
                backup.summary()
                log.info("Report: %s", report)
            except Exception as e:  # never let reporting hide the real outcome
                log.error("Could not write manifest/report: %s", e)
            lock.release()
        elif args.dry_run and backup.warnings:
            log.info("%d warning(s); see above.", len(backup.warnings))
    return code


if __name__ == "__main__":
    sys.exit(main())
