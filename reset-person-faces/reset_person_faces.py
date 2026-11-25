#!/usr/bin/env python3
"""
Remove the machine-learning face data of selected people from your photos in a date range,
then have Immich detect and recognize those faces again.

Use it after changing the facial recognition settings (for example lowering the maximum
recognition distance), to re-recognize look-alike people in part of the library without
re-running face detection on everything.

Why not "Face Detection -> Missing"? That job only picks assets that have never had face
detection run (asset_job_status.facesRecognizedAt IS NULL); deleting faces doesn't reset
that. So this script queues Immich's per-asset "refresh-faces" job instead.

Requires Python 3.9+, the repo's shared/ folder and `questionary` (see requirements.txt).
Run with --help for options. See README.md for setup.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root, for the shared package
from shared.config import ConfigError, load_config  # noqa: E402
from shared.immich_client import STOP, ImmichClient, ImmichHTTPError, http_error_hint  # noqa: E402

__version__ = "1.0.0"

SCRIPT_DIR = Path(__file__).resolve().parent
ML = "machine-learning"
SEARCH_PAGE_SIZE = 1000
PEOPLE_PAGE_SIZE = 1000
REFRESH_CHUNK = 500
WORKERS = 8
POLL_INTERVAL = 5.0  # seconds between job queue checks
IDLE_POLLS = 2  # queues must look idle this many polls in a row
QUEUES = ("faceDetection", "facialRecognition")
VISIBILITIES = ["timeline", "archive"]
UNNAMED = "(new unnamed person)"
UNASSIGNED = "(unassigned)"

log = logging.getLogger("reset-person-faces")


class Aborted(Exception):
    """The user cancelled a prompt (Ctrl-C or Esc)."""


# --------------------------------------------------------------------------------------
# Pure helpers (unit tested)
# --------------------------------------------------------------------------------------
@dataclass
class Person:
    id: str
    name: str
    assets: int = 0


@dataclass
class AssetWork:
    asset: Dict[str, Any]
    faces: List[Dict[str, Any]]  # every face on the asset, before deletion
    delete: List[Dict[str, Any]] = field(default_factory=list)  # faces to delete


def parse_date(text: str) -> dt.date:
    return dt.datetime.strptime(text.strip(), "%Y-%m-%d").date()


def date_bounds(start: dt.date, end: dt.date, tz: Optional[dt.tzinfo] = None) -> Tuple[str, str]:
    """Local-midnight bounds covering start..end inclusive, as ISO timestamps with offsets."""

    def midnight(d: dt.date) -> str:
        naive = dt.datetime.combine(d, dt.time.min)
        aware = naive.replace(tzinfo=tz) if tz else naive.astimezone()
        return aware.isoformat(timespec="milliseconds")

    return midnight(start), midnight(end + dt.timedelta(days=1))


def sort_people(people: Iterable[Person]) -> List[Person]:
    return sorted(people, key=lambda p: (-p.assets, p.name.casefold(), p.id))


def faces_to_delete(faces: Iterable[Dict[str, Any]], person_ids: Set[str]) -> List[Dict[str, Any]]:
    """Machine-learning faces assigned to one of the given people."""
    return [f for f in faces if f.get("sourceType") == ML and ((f.get("person") or {}).get("id") in person_ids)]


def face_label(face: Dict[str, Any]) -> str:
    person = face.get("person")
    if not person:
        return UNASSIGNED
    return person.get("name") or UNNAMED


def tally(faces: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for f in faces:
        key = face_label(f)
        out[key] = out.get(key, 0) + 1
    return out


def comparison_table(before: Dict[str, int], after: Dict[str, int]) -> List[str]:
    special = (UNNAMED, UNASSIGNED)
    names = sorted({*before, *after} - set(special), key=lambda n: (-before.get(n, 0), n.casefold()))
    names += [s for s in special if s in before or s in after]
    width = max([len(n) for n in names] + [len("Person")])
    lines = [f"  {'Person':<{width}}  {'Before':>6}  {'After':>6}"]
    for n in names:
        lines.append(f"  {n:<{width}}  {before.get(n, 0):>6}  {after.get(n, 0):>6}")
    lines.append(f"  {'Total':<{width}}  {sum(before.values()):>6}  {sum(after.values()):>6}")
    return lines


def queue_busy(statistics: Dict[str, Any]) -> int:
    return sum(int(statistics.get(k) or 0) for k in ("waiting", "active", "delayed"))


def plural(n: int, word: str) -> str:
    return f"{n:,} {word}" + ("" if n == 1 else "s")


def chunks(items: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


# --------------------------------------------------------------------------------------
# Prompts (questionary is imported lazily so tests and --help don't need it)
# --------------------------------------------------------------------------------------
class QuestionaryUI:
    def __init__(self):
        try:
            import questionary
        except ImportError:
            raise SystemExit("This script needs the 'questionary' package: pip install -r requirements.txt")
        self.q = questionary

    @staticmethod
    def _answer(value):
        if value is None:
            raise Aborted()
        return value

    def select_people(self, people: List[Person]) -> List[Person]:
        choices = [self.q.Choice(f"{p.name} ({p.assets:,} photos)", value=p) for p in people]
        return self._answer(self.q.checkbox(
            "Select the people whose faces should be reset (type to filter, space to select, enter to finish):",
            choices=choices,
            use_search_filter=True,
            use_jk_keys=False,
            validate=lambda picked: True if picked else "Select at least one person",
        ).ask())

    def ask_date(self, message: str, optional: bool, not_before: Optional[dt.date] = None) -> Optional[dt.date]:
        def validate(text: str):
            if optional and not text.strip():
                return True
            try:
                d = parse_date(text)
            except ValueError:
                return "Use the format YYYY-MM-DD"
            if not_before and d < not_before:
                return f"Must not be before {not_before.isoformat()}"
            return True

        text = self._answer(self.q.text(message, validate=validate).ask())
        return parse_date(text) if text.strip() else None

    def select_album(self, albums: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        # A Choice with value=None would return its title, so use index values; -1 means no album.
        choices = [self.q.Choice("(no anchor album)", value=-1)]
        choices += [
            self.q.Choice(f"{a.get('albumName') or '(untitled)'} ({a.get('assetCount', 0):,} assets)", value=i)
            for i, a in enumerate(albums)
        ]
        index = self._answer(self.q.select(
            "Anchor album: photos in it are left alone (optional):",
            choices=choices,
            use_search_filter=True,
            use_jk_keys=False,
        ).ask())
        return None if index == -1 else albums[index]

    def confirm(self, message: str) -> bool:
        return bool(self._answer(self.q.confirm(message, default=False).ask()))

    def wait_enter(self, message: str) -> None:
        self._answer(self.q.press_any_key_to_continue(message).ask())


# --------------------------------------------------------------------------------------
# Immich queries
# --------------------------------------------------------------------------------------
def parallel_map(fn: Callable[[Any], Any], items: Sequence[Any], label: str, out: Callable[[str], None]) -> List[Any]:
    results: List[Any] = [None] * len(items)
    if not items:
        return results
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(fn, item): i for i, item in enumerate(items)}
        done = 0
        for fut in futures:
            results[futures[fut]] = fut.result()
            done += 1
            if done % 50 == 0 or done == len(items):
                out(f"\r  {label}: {done}/{len(items)}")
    out("\n")
    return results


def load_people(client: ImmichClient, out: Callable[[str], None]) -> List[Person]:
    people: List[Person] = []
    page = 1
    while True:
        resp = client.get_json("/people", {"withHidden": True, "size": PEOPLE_PAGE_SIZE, "page": page})
        for p in resp.get("people") or []:
            if (p.get("name") or "").strip():
                people.append(Person(p["id"], p["name"].strip()))
        if not resp.get("hasNextPage"):
            break
        page += 1
    counts = parallel_map(lambda p: client.get_json(f"/people/{p.id}/statistics").get("assets", 0), people, "Counting photos per person", out)
    for p, c in zip(people, counts):
        p.assets = int(c or 0)
    return sort_people(people)


def search_assets(client: ImmichClient, search_filter: Dict[str, Any]) -> List[Dict[str, Any]]:
    assets: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    cursor: Optional[str] = None
    while True:
        body: Dict[str, Any] = {"filter": search_filter, "size": SEARCH_PAGE_SIZE}
        if cursor:
            body["cursor"] = cursor
        page = client.post_json("/search/metadata", body).get("assets") or {}
        for a in page.get("items") or []:
            if a["id"] not in seen:
                seen.add(a["id"])
                assets.append(a)
        cursor = page.get("nextCursor")
        if not cursor:
            return assets


def get_faces(client: ImmichClient, asset_id: str) -> List[Dict[str, Any]]:
    return client.get_json("/faces", {"id": asset_id}) or []


# --------------------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------------------
class Run:
    def __init__(self, client: ImmichClient, ui: Any, args: argparse.Namespace, out: Callable[[str], None] = None,
                 today: Optional[dt.date] = None, tz: Optional[dt.tzinfo] = None):
        self.client = client
        self.ui = ui
        self.args = args
        self.out = out or (lambda s: (sys.stdout.write(s), sys.stdout.flush()))
        self.today = today or dt.date.today()
        self.tz = tz
        self.failures = 0

    def say(self, line: str = "") -> None:
        self.out(line + "\n")

    def execute(self) -> int:
        me = self.client.get_json("/users/me")
        self.say(f"Connected as {me.get('name')} <{me.get('email')}>.")
        self.say("Loading people...")
        people = load_people(self.client, self.out)
        if not people:
            self.say("No named people found. Name the people in Immich first.")
            return 1

        selected = sort_people(self.ui.select_people(people))
        start = self.ui.ask_date("Start date (YYYY-MM-DD):", optional=False)
        end = self.ui.ask_date(f"End date (YYYY-MM-DD, blank for today {self.today.isoformat()}):", optional=True, not_before=start)
        end = end or self.today
        if end < start:
            self.say("The end date is before the start date.")
            return 2
        albums = self.client.get_json("/albums") or []
        albums.sort(key=lambda a: (a.get("albumName") or "").casefold())
        album = self.ui.select_album(albums)

        work, stats = self.collect(me["id"], selected, start, end, album)
        self.print_summary(selected, start, end, album, work, stats)
        if not work:
            self.say("Nothing to do.")
            return 0
        if self.args.dry_run:
            self.print_assets(work)
            self.say("DRY RUN: nothing was deleted or queued.")
            return 0

        n_faces = sum(len(w.delete) for w in work)
        if not self.ui.confirm(f"Remove facial recognition data ({plural(n_faces, 'face')}) from {plural(len(work), 'photo')}?"):
            self.say("Cancelled. Nothing was changed.")
            return 0

        refresh_ids = self.delete_faces(work)
        queued = self.queue_refresh(refresh_ids)
        if queued and not self.args.no_wait:
            done = [w for w in work if w.asset["id"] in set(queued)]
            self.wait_and_report(done)
        elif queued:
            self.say("Not waiting (--no-wait). Watch Administration -> Jobs -> Face Detection and Facial Recognition.")
        return 1 if self.failures else 0

    # --- collect -------------------------------------------------------------------------
    def collect(self, me_id: str, selected: List[Person], start: dt.date, end: dt.date,
                album: Optional[Dict[str, Any]]) -> Tuple[List[AssetWork], Dict[str, int]]:
        after, before = date_bounds(start, end, self.tz)
        ids = {p.id for p in selected}
        self.say("Searching for photos...")
        found = search_assets(self.client, {
            "personIds": {"any": sorted(ids)},
            "takenAt": {"gte": after, "lt": before},
            "visibility": {"in": VISIBILITIES},
        })
        anchors: Set[str] = set()
        if album:
            anchors = {a["id"] for a in search_assets(self.client, {"albumIds": {"any": [album["id"]]}})}
        own = [a for a in found if a.get("ownerId") == me_id]
        candidates = [a for a in own if a["id"] not in anchors]
        stats = {
            "found": len(found),
            "not_owned": len(found) - len(own),
            "anchors": len(own) - len(candidates),
            "no_ml_faces": 0,
        }
        faces = parallel_map(lambda a: get_faces(self.client, a["id"]), candidates, "Reading faces", self.out)
        work: List[AssetWork] = []
        for asset, asset_faces in zip(candidates, faces):
            w = AssetWork(asset, asset_faces, faces_to_delete(asset_faces, ids))
            if w.delete:
                work.append(w)
            else:
                stats["no_ml_faces"] += 1
        work.sort(key=lambda w: w.asset.get("localDateTime") or w.asset.get("fileCreatedAt") or "")
        return work, stats

    def print_summary(self, selected: List[Person], start: dt.date, end: dt.date, album: Optional[Dict[str, Any]],
                      work: List[AssetWork], stats: Dict[str, int]) -> None:
        self.say()
        self.say(f"Date range: {start.isoformat()} to {end.isoformat()} (inclusive, local time)")
        if album:
            self.say(f"Anchor album: {album.get('albumName')}")
        self.say("Faces to remove:")
        for p in selected:
            photos = sum(1 for w in work if any(f["person"]["id"] == p.id for f in w.delete))
            faces = sum(1 for w in work for f in w.delete if f["person"]["id"] == p.id)
            self.say(f"  {p.name}: {plural(faces, 'face')} on {plural(photos, 'photo')}")
            if photos and photos >= p.assets:
                self.say(f"    WARNING: that's every photo of {p.name} Immich counts. {p.name} may be left with no faces "
                         "(and lose their thumbnail) until recognition assigns new ones.")
        self.say(f"Total: {plural(sum(len(w.delete) for w in work), 'face')} on {plural(len(work), 'photo')}")
        if stats["not_owned"]:
            self.say(f"Skipped {plural(stats['not_owned'], 'photo')} owned by other users (only the owner can delete faces).")
        if stats["anchors"]:
            self.say(f"Kept {plural(stats['anchors'], 'photo')} from the anchor album.")
        if stats["no_ml_faces"]:
            self.say(f"Skipped {plural(stats['no_ml_faces'], 'photo')} where these people only have manual/EXIF faces.")
        self.say()
        self.say("Before continuing, set the new values in Administration -> Settings -> Machine Learning -> "
                 "Facial Recognition (e.g. Maximum recognition distance). Change only the thresholds "
                 "(max recognition distance, min recognized faces, min detection score), NOT the model: "
                 "a new model's faces can't be compared with the existing ones.")
        self.say()

    def print_assets(self, work: List[AssetWork]) -> None:
        for w in work:
            when = (w.asset.get("localDateTime") or w.asset.get("fileCreatedAt") or "")[:16].replace("T", " ")
            names = ", ".join(sorted({f["person"]["name"] for f in w.delete}))
            self.say(f"  {when}  {w.asset.get('originalFileName')}  ({len(w.delete)} faces: {names})")

    # --- change --------------------------------------------------------------------------
    def delete_faces(self, work: List[AssetWork]) -> List[str]:
        """Delete faces one by one. Returns ids of assets where at least one delete was attempted."""
        total = sum(len(w.delete) for w in work)
        touched: List[str] = []
        done = failed = 0
        try:
            for w in work:
                touched.append(w.asset["id"])
                for f in w.delete:
                    try:
                        self.client.delete_json(f"/faces/{f['id']}", {"force": True})
                    except ImmichHTTPError as e:
                        failed += 1
                        log.debug("Could not delete face %s on %s: %s", f["id"], w.asset["id"], e)
                        if e.status in (401, 403):
                            raise
                    done += 1
                    if done % 25 == 0 or done == total:
                        self.out(f"\r  Deleting faces: {done}/{total}")
        except KeyboardInterrupt:
            self.say("\nInterrupted. Queuing face detection for the photos already changed.")
            self.failures += 1
        self.out("\n")
        if failed:
            self.say(f"{failed} face(s) could not be deleted (run with --verbose for details).")
            self.failures += failed
        return touched

    def queue_refresh(self, asset_ids: List[str]) -> List[str]:
        queued: List[str] = []
        for chunk in chunks(asset_ids, REFRESH_CHUNK):
            try:
                self.client.post_json("/assets/jobs", {"name": "refresh-faces", "assetIds": list(chunk)})
                queued.extend(chunk)
            except ImmichHTTPError as e:
                self.failures += 1
                self.say(f"Could not queue face detection for {len(chunk)} photos: {e}{http_error_hint(e)}")
        if queued:
            self.say(f"Queued face detection for {plural(len(queued), 'photo')}.")
        missing = len(asset_ids) - len(queued)
        if missing:
            self.say(f"{missing} photos still need it: open each one in Immich and use the menu's 'Refresh faces'.")
        return queued

    # --- wait + report -------------------------------------------------------------------
    def wait_for_queues(self) -> bool:
        """Poll the face queues until idle. Returns False if the wait was cut short."""
        idle = 0
        try:
            while idle < IDLE_POLLS:
                time.sleep(POLL_INTERVAL)
                busy = {}
                for name in QUEUES:
                    busy[name] = queue_busy((self.client.get_json(f"/queues/{name}") or {}).get("statistics") or {})
                self.out("\r  Waiting for Immich: " + ", ".join(f"{k} {v} pending" for k, v in busy.items()) + "    ")
                idle = idle + 1 if not any(busy.values()) else 0
        except KeyboardInterrupt:
            self.out("\n")
            self.say("Stopped waiting. Immich keeps processing in the background; this report is partial.")
            return False
        except ImmichHTTPError as e:
            self.out("\n")
            if e.status not in (401, 403):
                raise
            self.say("This API key can't read job queues (needs queue.read, an admin permission). "
                     "Watch Administration -> Jobs -> Face Detection and Facial Recognition instead.")
            try:
                self.ui.wait_enter("Press any key when both are finished to see the report...")
            except Aborted:
                return False
            return True
        self.out("\n")
        return True

    def wait_and_report(self, work: List[AssetWork]) -> None:
        self.say("Waiting for face detection and recognition to finish (Ctrl-C to stop waiting)...")
        complete = self.wait_for_queues()
        before_ids = {f["id"] for w in work for f in w.faces}
        after = parallel_map(lambda w: get_faces(self.client, w.asset["id"]), work, "Reading new faces", self.out)
        new_faces = [f for faces in after for f in faces if f["id"] not in before_ids and f.get("sourceType") == ML]
        before = tally(f for w in work for f in w.delete)
        self.say()
        self.say("Faces on the processed photos, before (removed) and after (re-detected):")
        for line in comparison_table(before, tally(new_faces)):
            self.say(line)
        self.say()
        if not complete:
            self.say("Partial: some photos may not have been processed yet.")
        self.say(f"'{UNASSIGNED}' faces didn't match anyone closely enough. Some are retried by Immich's nightly "
                 "facial recognition job, so check again tomorrow. Merge any new unnamed person into the right "
                 "person in the People view.")


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------
def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Remove selected people's machine-learning faces from your photos in a date range, "
                    "then have Immich detect and recognize them again.",
    )
    p.add_argument("--config", help="Path to this script's config JSON (default: config.json next to this script)")
    p.add_argument("--server-url", dest="server_url", help="Immich URL (env IMMICH_URL)")
    p.add_argument("--api-key", dest="api_key", help="Immich API key (env IMMICH_API_KEY)")
    p.add_argument("--insecure", action="store_true", help="Don't verify the server's TLS certificate")
    p.add_argument("--dry-run", action="store_true", help="Show which photos and faces would change, then exit")
    p.add_argument("--no-wait", action="store_true", help="Don't wait for Immich to finish or print the before/after report")
    p.add_argument("--verbose", action="store_true", help="More detailed logging")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None, ui: Any = None, out: Callable[[str], None] = None) -> int:
    STOP.clear()
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, format="%(message)s")
    try:
        cfg = load_config(SCRIPT_DIR, args.config, {"server_url": args.server_url, "api_key": args.api_key})
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 2
    client = ImmichClient(cfg["server_url"], cfg["api_key"], insecure=args.insecure,
                          user_agent=f"immich-reset-person-faces/{__version__}", logger=log)
    try:
        return Run(client, ui or QuestionaryUI(), args, out).execute()
    except (Aborted, KeyboardInterrupt):
        STOP.set()
        print("\nCancelled.", file=sys.stderr)
        return 130
    except ImmichHTTPError as e:
        print(f"Server error: {e}{http_error_hint(e)}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
