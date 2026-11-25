"""Tests for reset_person_faces.py: pure helpers, and full runs against a fake Immich."""

import argparse
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import reset_person_faces as r  # noqa: E402
from fake_immich import API_KEY, ML, FakeImmich  # noqa: E402
from shared import config as shared_config  # noqa: E402
from shared import immich_client  # noqa: E402

UTC = dt.timezone.utc


class Helpers(unittest.TestCase):
    def test_parse_date(self):
        self.assertEqual(r.parse_date(" 2026-07-21 "), dt.date(2026, 7, 21))
        with self.assertRaises(ValueError):
            r.parse_date("07/21/2026")

    def test_date_bounds_inclusive(self):
        tz = dt.timezone(dt.timedelta(hours=-7))
        self.assertEqual(r.date_bounds(dt.date(2026, 7, 21), dt.date(2026, 7, 31), tz),
                         ("2026-07-21T00:00:00.000-07:00", "2026-08-01T00:00:00.000-07:00"))

    def test_sort_people(self):
        people = [r.Person("1", "bob", 5), r.Person("2", "Alice", 5), r.Person("3", "Zed", 9)]
        self.assertEqual([p.name for p in r.sort_people(people)], ["Zed", "Alice", "bob"])

    def test_faces_to_delete(self):
        faces = [
            {"id": "a", "sourceType": ML, "person": {"id": "p1"}},
            {"id": "b", "sourceType": "manual", "person": {"id": "p1"}},
            {"id": "c", "sourceType": ML, "person": {"id": "p2"}},
            {"id": "d", "sourceType": ML, "person": None},
            {"id": "e", "sourceType": "exif", "person": {"id": "p3"}},
            {"id": "f", "sourceType": ML, "person": {"id": "p3"}},
        ]
        self.assertEqual([f["id"] for f in r.faces_to_delete(faces, {"p1", "p3"})], ["a", "f"])

    def test_comparison_table(self):
        lines = r.comparison_table({"A": 3, "B": 1}, {"A": 2, "B": 1, r.UNASSIGNED: 1})
        self.assertIn("Before", lines[0])
        self.assertTrue(lines[1].strip().startswith("A"))
        self.assertTrue(lines[-2].strip().startswith(r.UNASSIGNED))
        self.assertTrue(lines[-1].split()[-2:] == ["4", "4"])


class FakeUI:
    def __init__(self, people, start, end=None, album=None, confirm=True):
        self.people, self.start, self.end, self.album, self.answer = people, start, end, album, confirm
        self.confirm_messages = []
        self.waited = False

    def select_people(self, people):
        chosen = [p for p in people if p.name in self.people]
        assert len(chosen) == len(self.people), "unknown person in test"
        return chosen

    def ask_date(self, message, optional, not_before=None):
        return self.end if optional else self.start

    def select_album(self, albums):
        return next((a for a in albums if a["albumName"] == self.album), None)

    def confirm(self, message):
        self.confirm_messages.append(message)
        return self.answer

    def wait_enter(self, message):
        self.waited = True


class EndToEnd(unittest.TestCase):
    def setUp(self):
        immich_client.RETRY_BASE_DELAY = 0.01
        patches = [
            mock.patch.object(r, "POLL_INTERVAL", 0),
            mock.patch.object(shared_config, "ROOT_CONFIG", Path(tempfile.gettempdir()) / "no-such-immich-root-config.json"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        f = self.fake = FakeImmich().start()
        self.addCleanup(f.stop)

        self.a = f.add_person("Alice")
        self.b = f.add_person("Bea")
        self.g = f.add_person("Grandma")
        f.add_person("")  # unnamed: never offered
        # before the range: untouched
        self.old = f.add_asset("2026-07-20T12:00:00.000Z")
        self.old_face = f.add_face(self.old, self.a)
        # in range
        self.p1 = f.add_asset("2026-07-21T00:30:00.000Z", name="first.jpg")
        self.p1_a = f.add_face(self.p1, self.a)
        self.p2 = f.add_asset("2026-08-01T10:00:00.000Z", name="group.jpg")
        self.p2_a = f.add_face(self.p2, self.a)
        self.p2_b = f.add_face(self.p2, self.b)
        self.p2_g = f.add_face(self.p2, self.g)
        self.p2_unassigned = f.add_face(self.p2, None)
        self.p3 = f.add_asset("2026-09-01T10:00:00.000Z", visibility="archive")
        self.p3_b = f.add_face(self.p3, self.b)
        self.manual_only = f.add_asset("2026-09-02T10:00:00.000Z")
        self.manual_face = f.add_face(self.manual_only, self.a, source="manual")
        self.mixed = f.add_asset("2026-09-03T10:00:00.000Z")
        self.mixed_manual = f.add_face(self.mixed, self.b, source="manual")
        self.mixed_ml = f.add_face(self.mixed, self.a)
        self.partner = f.add_asset("2026-09-04T10:00:00.000Z", owner="someone-else")
        self.partner_face = f.add_face(self.partner, self.a)
        self.anchor = f.add_asset("2026-09-05T10:00:00.000Z")
        self.anchor_face = f.add_face(self.anchor, self.b)
        self.locked = f.add_asset("2026-09-06T10:00:00.000Z", visibility="locked")
        self.locked_face = f.add_face(self.locked, self.a)
        # after the range
        self.late = f.add_asset("2026-11-01T00:00:00.000Z")
        self.late_face = f.add_face(self.late, self.a)
        f.add_album("Face anchors", [self.anchor])
        self.output = []

    def run_tool(self, ui, *flags):
        args = argparse.Namespace(dry_run="--dry-run" in flags, no_wait="--no-wait" in flags)
        client = immich_client.ImmichClient(self.fake.url, API_KEY)
        run = r.Run(client, ui, args, out=self.output.append, today=dt.date(2026, 11, 25), tz=UTC)
        return run.execute()

    @property
    def text(self):
        return "".join(self.output)

    def deleted_ids(self):
        return {fid for fid, _ in self.fake.deleted}

    def test_two_people_full_run(self):
        f = self.fake
        ui = FakeUI(["Alice", "Bea"], dt.date(2026, 7, 21), dt.date(2026, 10, 31), album="Face anchors")
        self.assertEqual(self.run_tool(ui), 0)
        self.assertEqual(self.deleted_ids(), {self.p1_a, self.p2_a, self.p2_b, self.p3_b, self.mixed_ml})
        self.assertTrue(all(force for _, force in f.deleted))
        self.assertEqual(len(f.asset_jobs), 1)
        name, ids = f.asset_jobs[0]
        self.assertEqual(name, "refresh-faces")
        self.assertEqual(sorted(ids), sorted([self.p1, self.p2, self.p3, self.mixed]))
        self.assertEqual(ui.confirm_messages, ["Remove facial recognition data (5 faces) from 4 photos?"])
        # one search for both people (personIds.any), one for the album
        flt = f.search_bodies[0]["filter"]
        self.assertEqual(sorted(flt["personIds"]["any"]), sorted([self.a, self.b]))
        self.assertEqual(flt["takenAt"], {"gte": "2026-07-21T00:00:00.000+00:00", "lt": "2026-11-01T00:00:00.000+00:00"})
        out = self.text
        self.assertIn("Alice: 3 faces on 3 photos", out)
        self.assertIn("Bea: 2 faces on 2 photos", out)
        self.assertIn("Skipped 1 photo owned by other users", out)
        self.assertIn("Kept 1 photo from the anchor album", out)
        self.assertIn("Skipped 1 photo where these people only have manual/EXIF faces", out)
        self.assertIn("Queued face detection for 4 photos.", out)
        self.assertIn("NOT the model", out)
        # report: re-detected faces counted, untouched faces (Grandma, unassigned) not double counted
        table = out[out.index("Faces on the processed photos"):]
        self.assertRegex(table, r"Alice\s+3\s+3")
        self.assertRegex(table, r"Bea\s+2\s+2")
        self.assertNotIn("Grandma", table)
        self.assertRegex(table, r"Total\s+5\s+5")

    def test_report_shows_reassignments(self):
        f = self.fake
        f.recognize = lambda aid, old: self.b if aid == self.p1 else (None if aid == self.p2 and old == self.a else old)
        ui = FakeUI(["Alice"], dt.date(2026, 7, 21), None)
        self.assertEqual(self.run_tool(ui), 0)
        # end date defaults to today: the late photo is included
        self.assertIn(self.late_face, self.deleted_ids())
        self.assertNotIn(self.p2_b, self.deleted_ids())  # Bea not selected
        self.assertNotIn(self.old_face, self.deleted_ids())
        table = self.text[self.text.index("Faces on the processed photos"):]
        self.assertRegex(table, r"Alice\s+4\s+2")
        self.assertRegex(table, r"Bea\s+0\s+1")
        self.assertRegex(table, r"\(unassigned\)\s+0\s+1")

    def test_dry_run_changes_nothing(self):
        ui = FakeUI(["Alice", "Bea"], dt.date(2026, 7, 21), dt.date(2026, 10, 31))
        self.assertEqual(self.run_tool(ui, "--dry-run"), 0)
        self.assertEqual(self.fake.deleted, [])
        self.assertEqual(self.fake.asset_jobs, [])
        self.assertEqual(ui.confirm_messages, [])
        self.assertIn("group.jpg", self.text)
        self.assertIn("DRY RUN", self.text)

    def test_declined_confirmation(self):
        ui = FakeUI(["Alice"], dt.date(2026, 7, 21), None, confirm=False)
        self.assertEqual(self.run_tool(ui), 0)
        self.assertEqual(self.fake.deleted, [])
        self.assertEqual(self.fake.asset_jobs, [])

    def test_no_wait_and_cursor_paging(self):
        self.fake.page_size_limit = 2
        ui = FakeUI(["Alice", "Bea"], dt.date(2026, 7, 21), dt.date(2026, 10, 31))
        self.assertEqual(self.run_tool(ui, "--no-wait"), 0)
        self.assertGreater(len(self.fake.search_bodies), 2)
        self.assertIn("cursor", self.fake.search_bodies[1])
        self.assertEqual(len(self.deleted_ids()), 6)  # anchor not excluded this time
        self.assertNotIn("Faces on the processed photos", self.text)
        self.assertNotIn(("GET", "/api/queues/faceDetection"), self.fake.requests)

    def test_queue_permission_missing(self):
        self.fake.queues_forbidden = True
        ui = FakeUI(["Bea"], dt.date(2026, 7, 21), dt.date(2026, 10, 31))
        self.fake.recognize = lambda aid, old: old
        with mock.patch.object(ui, "wait_enter", side_effect=lambda m: self.fake.run_refresh()):
            self.assertEqual(self.run_tool(ui), 0)
        self.assertIn("queue.read", self.text)
        self.assertRegex(self.text, r"Bea\s+3\s+3")

    def test_person_left_without_faces_warning(self):
        ui = FakeUI(["Bea"], dt.date(2026, 7, 21), dt.date(2026, 10, 31))
        self.assertEqual(self.run_tool(ui, "--dry-run"), 0)
        self.assertNotIn("WARNING", self.text)  # Bea has manual and anchor faces too
        ui = FakeUI(["Bea"], dt.date(2026, 7, 21), dt.date(2026, 10, 31), album="Face anchors")
        self.fake.faces[self.mixed_manual]["personId"] = None
        self.fake.faces[self.anchor_face]["deletedAt"] = "x"
        self.assertEqual(self.run_tool(ui, "--dry-run"), 0)
        self.assertIn("WARNING: that's every photo of Bea", self.text)

    def test_main_reads_shared_config(self):
        tmp = Path(tempfile.mkdtemp(prefix="reset-faces-test-"))
        root = tmp / "root.json"
        root.write_text(json.dumps({"server_url": self.fake.url + "/api", "api_key": API_KEY}))
        ui = FakeUI(["Alice"], dt.date(2026, 7, 21), None)
        with mock.patch.object(shared_config, "ROOT_CONFIG", root), \
                mock.patch.object(r, "SCRIPT_DIR", tmp / "no-script-config"):
            code = r.main(["--dry-run"], ui=ui, out=self.output.append)
        self.assertEqual(code, 0)
        self.assertIn("Connected as Admin", self.text)

    def test_main_bad_key(self):
        ui = FakeUI(["Alice"], dt.date(2026, 7, 21), None)
        with mock.patch.object(r, "SCRIPT_DIR", Path(tempfile.gettempdir()) / "nowhere"):
            code = r.main(["--server-url", self.fake.url, "--api-key", "wrong"], ui=ui, out=self.output.append)
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
