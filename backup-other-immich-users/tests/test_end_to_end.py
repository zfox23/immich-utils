"""End-to-end tests: run the real script against a fake Immich server."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import backup_immich_users as b  # noqa: E402
from fake_immich import API_KEY, FakeImmich  # noqa: E402


class EndToEnd(unittest.TestCase):
    def setUp(self):
        b.RETRY_BASE_DELAY = 0.01
        self.fake = FakeImmich().start()
        self.tmp = Path(tempfile.mkdtemp(prefix="immich-backup-test-"))
        self.root = self.tmp / "backup"
        self.config = self.tmp / "config.json"
        self.config.write_text(json.dumps({"server_url": self.fake.url, "api_key": API_KEY, "backup_root": str(self.root)}))

        f = self.fake
        self.jane = f.add_user("Jane Doe")
        self.bob = f.add_user("Bob")
        self.carol = f.add_user("Carol", share=False)
        self.mine = f.add_asset(f.me["id"], "MINE.JPG", "2023-01-01T10:00:00.000Z")
        self.j1 = f.add_asset(self.jane, "IMG_0001.JPG", "2023-05-06T14:00:00.000Z")
        self.j2 = f.add_asset(self.jane, "img_0001.jpg", "2023-05-06T15:00:00.000Z")  # case-only collision
        self.jvid = f.add_asset(self.jane, "IMG_0002.MOV", "2023-05-07T09:00:00.000Z", visibility="hidden")
        self.jlive = f.add_asset(self.jane, "IMG_0002.HEIC", "2023-05-07T09:00:00.000Z", live_video=self.jvid)
        self.jarch = f.add_asset(self.jane, "ARCHIVED.JPG", "2023-05-08T09:00:00.000Z", visibility="archive")
        self.jlib = f.add_asset(self.jane, "LIBRARY.JPG", "2022-02-02T09:00:00.000Z", library=True)
        self.b1 = f.add_asset(self.bob, "CON.jpg", "2024-12-31T23:59:00.000Z")
        self.c1 = f.add_asset(self.carol, "CAROL.JPG", "2024-01-01T00:00:00.000Z")

    def tearDown(self):
        self.fake.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_backup(self, *extra):
        return b.main(["--config", str(self.config), "--workers", "3", *extra])

    def manifest(self):
        return json.loads((self.root / ".immich-backup" / "manifest.json").read_text(encoding="utf-8"))

    def files(self):
        out = set()
        for p in self.root.rglob("*"):
            rel = p.relative_to(self.root).as_posix()
            if p.is_file() and not rel.startswith(".immich-backup") and rel != "backup-report.html":
                out.add(rel)
        return out

    def test_full_flow(self):
        f = self.fake
        self.assertEqual(self.run_backup("--non-interactive"), 0)
        files = self.files()
        expected = {
            "Jane Doe/2023/2023-05-06/IMG_0001.JPG",
            f"Jane Doe/2023/2023-05-06/img_0001_{self.j2[:8]}.jpg",
            "Jane Doe/2023/2023-05-07/IMG_0002.HEIC",
            "Jane Doe/2023/2023-05-07/IMG_0002.MOV",
            "Jane Doe/2022/2022-02-02/LIBRARY.JPG",
            "Bob/2024/2024-12-31/_CON.jpg",
        }
        self.assertEqual(files, expected)  # no admin assets, no archived, no non-sharing user
        self.assertEqual((self.root / "Jane Doe/2023/2023-05-06/IMG_0001.JPG").read_bytes(), f.assets[self.j1]["content"])
        m = self.manifest()
        self.assertEqual(m["assets"][self.jvid]["livePhotoOf"], self.jlive)
        self.assertEqual(m["users"][self.jane]["folder"], "Jane Doe")

        report = (self.root / "backup-report.html").read_text(encoding="utf-8")
        self.assertIn(self.j1, report)
        self.assertEqual(len(list((self.root / ".immich-backup" / "reports").glob("backup-*.html"))), 1)
        log = next((self.root / ".immich-backup" / "logs").glob("*.log")).read_text(encoding="utf-8")
        self.assertIn("Carol", log)  # warned: not sharing

        # Second run downloads nothing.
        originals_before = sum(1 for r in f.requests if r.endswith("/original"))
        self.assertEqual(self.run_backup("--non-interactive"), 0)
        self.assertEqual(sum(1 for r in f.requests if r.endswith("/original")), originals_before)

        # Date edit -> move; changed original -> replace (old kept); new asset; deletion -> kept.
        f.assets[self.b1]["localDateTime"] = "2025-01-01T00:01:00.000Z"
        f.set_content(self.j1, b"edited bytes")
        new = f.add_asset(self.bob, "NEW.PNG", "2025-03-03T03:03:03.000Z")
        f.assets[self.jlive]["trashed"] = True  # still + its live video disappear
        self.assertEqual(self.run_backup("--non-interactive"), 0)
        files = self.files()
        self.assertIn("Bob/2025/2025-01-01/_CON.jpg", files)
        self.assertNotIn("Bob/2024/2024-12-31/_CON.jpg", files)
        self.assertFalse((self.root / "Bob/2024").exists())  # empty dirs cleaned
        self.assertEqual((self.root / "Jane Doe/2023/2023-05-06/IMG_0001.JPG").read_bytes(), b"edited bytes")
        self.assertTrue(any(".replaced-" in p for p in files))
        self.assertIn("Bob/2025/2025-03-03/NEW.PNG", files)
        self.assertIn("Jane Doe/2023/2023-05-07/IMG_0002.HEIC", files)  # kept (non-interactive)
        m = self.manifest()
        self.assertEqual(m["assets"][self.jlive]["state"], "gone_kept")
        self.assertEqual(m["assets"][self.jvid]["state"], "gone_kept")
        self.assertIn(new, m["assets"])

        # Interactive: deleting a newly-gone asset removes the file; previously kept ones aren't asked again.
        f.assets[self.j2]["trashed"] = True
        with mock.patch.object(sys.stdin, "isatty", return_value=True), \
             mock.patch("builtins.input", side_effect=["d", "yes"]) as inp:
            self.assertEqual(self.run_backup(), 0)
        self.assertEqual(inp.call_count, 2)
        files = self.files()
        self.assertNotIn(f"Jane Doe/2023/2023-05-06/img_0001_{self.j2[:8]}.jpg", files)
        self.assertIn("Jane Doe/2023/2023-05-07/IMG_0002.HEIC", files)
        self.assertNotIn(self.j2, self.manifest()["assets"])
        report = (self.root / "backup-report.html").read_text(encoding="utf-8")
        self.assertIn("gone from server: removed", report)

    def test_unshared_user_is_never_flagged_deleted(self):
        self.assertEqual(self.run_backup("--non-interactive"), 0)
        self.fake.shared_with_me.discard(self.jane)
        with mock.patch.object(sys.stdin, "isatty", return_value=True), \
             mock.patch("builtins.input", side_effect=AssertionError("should not prompt")):
            self.assertEqual(self.run_backup(), 0)
        m = self.manifest()
        self.assertTrue(all(a.get("state") == "present" for a in m["assets"].values()))
        self.assertIn("Jane Doe/2023/2023-05-06/IMG_0001.JPG", self.files())

    def test_failed_reads_skip_deletion_checks_and_return_error(self):
        self.assertEqual(self.run_backup("--non-interactive"), 0)
        self.fake.fail_asset_ids.add(self.j1)
        self.fake.assets[self.j2]["trashed"] = True
        self.assertEqual(self.run_backup("--non-interactive"), 1)
        m = self.manifest()
        self.assertEqual(m["assets"][self.j2]["state"], "present")  # Jane's scan incomplete -> not flagged

    def test_lost_manifest_adopts_existing_files(self):
        self.assertEqual(self.run_backup("--non-interactive"), 0)
        before = self.files()
        state = self.root / ".immich-backup"
        (state / "manifest.json").unlink()
        (state / "manifest.json.bak").unlink(missing_ok=True)
        originals_before = sum(1 for r in self.fake.requests if r.endswith("/original"))
        self.assertEqual(self.run_backup("--non-interactive"), 0)
        self.assertEqual(self.files(), before | {"Jane Doe/2022/2022-02-02/LIBRARY_%s.JPG" % self.jlib[:8]})
        # Only the external-library asset (path checksum, can't be verified) is downloaded again.
        self.assertEqual(sum(1 for r in self.fake.requests if r.endswith("/original")) - originals_before, 1)

    def test_local_file_deleted_is_restored(self):
        self.assertEqual(self.run_backup("--non-interactive"), 0)
        p = self.root / "Jane Doe/2023/2023-05-06/IMG_0001.JPG"
        p.unlink()
        self.assertEqual(self.run_backup("--non-interactive"), 0)
        self.assertTrue(p.exists())

    def test_dry_run_writes_nothing(self):
        self.assertEqual(self.run_backup("--dry-run"), 0)
        self.assertFalse(self.root.exists())

    def test_user_filter(self):
        self.assertEqual(self.run_backup("--non-interactive", "--user", "bob"), 0)
        self.assertEqual(self.files(), {"Bob/2024/2024-12-31/_CON.jpg"})

    def test_bad_key(self):
        self.config.write_text(json.dumps({"server_url": self.fake.url, "api_key": "nope", "backup_root": str(self.root)}))
        self.assertEqual(self.run_backup("--non-interactive"), 2)

    def test_lock_blocks_second_run(self):
        state = self.root / ".immich-backup"
        state.mkdir(parents=True)
        import socket
        (state / "lock").write_text(json.dumps({"pid": os.getpid(), "host": socket.gethostname()}))
        self.assertEqual(self.run_backup("--non-interactive"), 2)
        (state / "lock").write_text(json.dumps({"pid": 999999, "host": socket.gethostname()}))
        self.assertEqual(self.run_backup("--non-interactive"), 0)  # stale lock removed

    def test_probe(self):
        self.assertEqual(self.run_backup("--probe"), 0)


if __name__ == "__main__":
    unittest.main()
