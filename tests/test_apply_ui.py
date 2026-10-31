import curses, sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from apply_status import Item
from apply_ui import Checklist, row_line, detail_lines


def rows():
    return [Item(name="image-watch-socket-proxy"),                       # up to date
            Item(name="node-exporter", kind="release", tag="v1.12.0", new_tag="v1.12.1", announced=False),
            Item(name="trunk-pro", kind="digest", announced=True, selected=True, running="sha256:a", latest="sha256:b",
                 notice={"flags": ["⚠️ Includes PR #104: call store"], "summary": "\n• EDACS added\n• more", "at": "2026-10-31 09:00"}),
            Item(name="broken", error="check failed: boom")]


class Model(unittest.TestCase):
    def test_move_clamps(self):
        cl = Checklist(rows()); cl.move(-1); self.assertEqual(cl.cursor, 0)
        cl.move(10); self.assertEqual(cl.cursor, 3)

    def test_toggle_and_error_rows(self):
        cl = Checklist(rows()); cl.toggle()
        self.assertTrue(cl.items[0].selected)
        cl.cursor = 3; cl.toggle(); self.assertFalse(cl.items[3].selected)

    def test_select_pending_and_clear(self):
        cl = Checklist(rows()); cl.select_pending()
        self.assertEqual([i.name for i in cl.chosen()], ["node-exporter", "trunk-pro"])
        cl.clear(); self.assertEqual(cl.chosen(), [])

    def test_keys(self):
        cl = Checklist(rows())
        self.assertIsNone(cl.handle_key(curses.KEY_DOWN)); self.assertEqual(cl.cursor, 1)
        cl.handle_key(ord("k")); self.assertEqual(cl.cursor, 0)
        cl.handle_key(ord(" ")); self.assertTrue(cl.items[0].selected)
        self.assertEqual(cl.handle_key(10), "done")
        self.assertEqual(cl.handle_key(ord("q")), "quit")
        self.assertIsNone(cl.handle_key(ord("z")))


class Render(unittest.TestCase):
    def test_row_line(self):
        r = rows()[2]
        self.assertEqual(row_line(r, True, 200), "> [x] " + "trunk-pro".ljust(26) + " new build a → b  ⚠")

    def test_row_line_truncates_to_width(self):
        self.assertEqual(len(row_line(rows()[1], False, 40)), 39)
        self.assertEqual(row_line(rows()[1], False, 0), "")

    def test_detail_lines(self):
        self.assertEqual(detail_lines(rows()[2]), ["⚠️ Includes PR #104: call store", "• EDACS added", "announced 2026-10-31 09:00"])
        self.assertEqual(detail_lines(rows()[1]), ["not announced: no summary, PRs not checked"])
        self.assertEqual(detail_lines(rows()[3]), ["check failed: boom"])
        self.assertEqual(detail_lines(rows()[0]), [])


if __name__ == "__main__":
    unittest.main()
