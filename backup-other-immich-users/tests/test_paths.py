import datetime as dt
import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backup_immich_users as b  # noqa: E402


class SanitizeTests(unittest.TestCase):
    def test_invalid_characters_replaced(self):
        self.assertEqual(b.sanitize_component('a<b>c:d"e/f\\g|h?i*j'), "a_b_c_d_e_f_g_h_i_j")

    def test_control_characters_replaced(self):
        self.assertEqual(b.sanitize_component("a\x00b\x1fc"), "a_b_c")

    def test_trailing_dots_and_spaces_stripped(self):
        self.assertEqual(b.sanitize_component(" name. . "), "name")

    def test_empty_and_dot_names_use_fallback(self):
        self.assertEqual(b.sanitize_component("", "fb"), "fb")
        self.assertEqual(b.sanitize_component("..", "fb"), "fb")
        self.assertEqual(b.sanitize_component("...", "fb"), "fb")

    def test_windows_reserved_names(self):
        self.assertEqual(b.sanitize_component("CON"), "_CON")
        self.assertEqual(b.sanitize_component("nul.jpg"), "_nul.jpg")
        self.assertEqual(b.sanitize_component("CONSOLE.jpg"), "CONSOLE.jpg")

    def test_long_names_truncated_keeping_extension(self):
        s = b.sanitize_component("x" * 400 + ".jpeg")
        self.assertLessEqual(len(s), b.MAX_COMPONENT_LEN)
        self.assertTrue(s.endswith(".jpeg"))

    def test_unicode_kept(self):
        self.assertEqual(b.sanitize_component("Zoë 写真.HEIC"), "Zoë 写真.HEIC")


class DateTests(unittest.TestCase):
    def test_local_date_time_used_literally(self):
        # Immich marks localDateTime with Z even though it is local wall-clock time.
        self.assertEqual(b.date_part("2023-12-31T23:30:00.000Z"), "2023-12-31")

    def test_fallback(self):
        self.assertEqual(b.date_part(None, "2020-01-02T03:04:05Z"), "2020-01-02")
        self.assertIsNone(b.date_part(None, None))
        self.assertIsNone(b.date_part("garbage"))

    def test_day_dir(self):
        self.assertEqual(b.day_dir("Jane", "2023-05-06"), "Jane/2023/2023-05-06")
        self.assertEqual(b.day_dir("Jane", None), "Jane/unknown-date")


class CollisionTests(unittest.TestCase):
    AID = "abcdef12-3456-4789-8abc-def012345678"

    def test_no_collision(self):
        rel = b.choose_rel_path("J/2023/2023-01-01", "IMG_1.JPG", self.AID, lambda r: False)
        self.assertEqual(rel, "J/2023/2023-01-01/IMG_1.JPG")

    def test_collision_adds_short_id(self):
        taken = {"j/2023/2023-01-01/img_1.jpg"}
        rel = b.choose_rel_path("J/2023/2023-01-01", "IMG_1.JPG", self.AID, lambda r: r.casefold() in taken)
        self.assertEqual(rel, "J/2023/2023-01-01/IMG_1_abcdef12.JPG")

    def test_double_collision_uses_full_id(self):
        taken = {"d/img_1.jpg", "d/img_1_abcdef12.jpg"}
        rel = b.choose_rel_path("d", "IMG_1.JPG", self.AID, lambda r: r.casefold() in taken)
        self.assertEqual(rel, f"d/IMG_1_{self.AID}.JPG")

    def test_user_folder_unique_and_stable(self):
        used = {}
        f1 = b.choose_user_folder("Jane Doe", "11111111-0000-4000-8000-000000000000", used)
        used[f1.casefold()] = "11111111-0000-4000-8000-000000000000"
        f2 = b.choose_user_folder("jane doe", "22222222-0000-4000-8000-000000000000", used)
        self.assertEqual(f1, "Jane Doe")
        self.assertEqual(f2, "jane doe_22222222")
        # Same user again gets the same folder.
        self.assertEqual(b.choose_user_folder("Jane Doe", "11111111-0000-4000-8000-000000000000", used), "Jane Doe")

    def test_user_folder_avoids_state_dir(self):
        self.assertEqual(b.choose_user_folder(".immich-backup", "33333333-0000-4000-8000-000000000000", {}), "_.immich-backup")

    def test_replaced_name(self):
        self.assertEqual(
            b.replaced_name("J/2023/2023-01-01/IMG_1.JPG", self.AID, dt.date(2026, 11, 24)),
            "J/2023/2023-01-01/IMG_1_abcdef12.replaced-20261124.JPG",
        )


class GoneTests(unittest.TestCase):
    def test_only_fully_enumerated_owners(self):
        assets = {
            "a1": {"ownerId": "u1", "relPath": "U1/x"},
            "a2": {"ownerId": "u1", "relPath": "U1/y"},
            "b1": {"ownerId": "u2", "relPath": "U2/z"},
        }
        # u2 stopped sharing (or failed to enumerate): never reported as gone.
        self.assertEqual(b.find_gone_assets(assets, {"a1"}, {"u1"}), ["a2"])
        self.assertEqual(b.find_gone_assets(assets, set(), set()), [])


class SpaceTests(unittest.TestCase):
    def test_asset_size(self):
        self.assertEqual(b.asset_size({"exifInfo": {"fileSizeInByte": 1234}}), 1234)
        self.assertIsNone(b.asset_size({"exifInfo": {}}))
        self.assertIsNone(b.asset_size({"exifInfo": None}))
        self.assertIsNone(b.asset_size({}))
        self.assertIsNone(b.asset_size({"exifInfo": {"fileSizeInByte": True}}))

    def test_space_needed(self):
        result = b.space_needed([
            ("Jane", None, None),          # user with nothing to do still gets a row
            ("Bob", None, None),
            ("Jane", "download", 100),
            ("Jane", "replace", 50),
            ("Jane", "download", None),    # unknown size
            ("Jane", "none", 999),         # already backed up: free
            ("Jane", "move", 999),         # moves don't need space
            ("Jane", "adopt", 999),
        ])
        self.assertEqual(result, {"Jane": [3, 150, 1], "Bob": [0, 0, 0]})


class ReportTests(unittest.TestCase):
    def _data(self, html_text):
        m = re.search(r'<script type="application/json" id="report-data">(.*?)</script>', html_text, re.S)
        self.assertIsNotNone(m)
        return json.loads(m.group(1))

    def test_malicious_values_cannot_break_out(self):
        evil = '</script><script>alert(1)</script><!--'
        meta = {"server": "https://x", "serverVersion": "1", "me": evil, "started": "s", "finished": "f",
                "interrupted": False, "warnings": [evil], "users": []}
        rows = [{"user": evil, "date": "", "local": f"U/{evil}.jpg", "name": evil, "id": "x", "checksum": "",
                 "status": "new", "live": False, "note": evil}]
        out = b.render_report(meta, rows, "")
        # Exactly the two script tags from the template; the payload adds none.
        self.assertEqual(out.count("<script"), 2)
        self.assertEqual(out.count("</script>"), 2)
        self.assertEqual(self._data(out)["rows"][0]["name"], evil)

    def test_relative_prefix(self):
        meta = {"server": "s", "serverVersion": "1", "me": "", "started": "", "finished": "", "interrupted": False, "warnings": [], "users": []}
        self.assertEqual(self._data(b.render_report(meta, [], ""))["prefix"], "")
        self.assertEqual(self._data(b.render_report(meta, [], ".immich-backup/reports"))["prefix"], "../../")


if __name__ == "__main__":
    unittest.main()
