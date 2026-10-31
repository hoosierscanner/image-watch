import os, stat, sys, tempfile, unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import apply_compose as ac
from apply_status import Item

COMPOSE = """services:
  node-exporter:
    image: prom/node-exporter:v1.12.0   # pinned
    restart: unless-stopped
  trp-stats:
    image: python:3.13-alpine
"""


class Rewrite(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory(); self.addCleanup(self.d.cleanup)
        self.f = os.path.join(self.d.name, "docker-compose.yml")
        open(self.f, "w").write(COMPOSE); os.chmod(self.f, 0o640)

    def test_image_name(self):
        self.assertEqual(ac.image_name("prom/node-exporter:v1.12.0", "v1.12.0"), "prom/node-exporter")
        self.assertEqual(ac.image_name("localhost:5000/x:v1", "v1"), "localhost:5000/x")

    def test_rewrite_keeps_comment_and_indent(self):
        rw = ac.plan_rewrite([self.f], "prom/node-exporter", "v1.12.0", "v1.12.1")
        self.assertIn("    image: prom/node-exporter:v1.12.1   # pinned\n", rw.new_text)
        self.assertIn("python:3.13-alpine", rw.new_text)
        self.assertIn("-    image: prom/node-exporter:v1.12.0", rw.diff)
        self.assertIn("+    image: prom/node-exporter:v1.12.1", rw.diff)
        self.assertEqual(open(self.f).read(), COMPOSE)  # planning doesn't write

    def test_quoted(self):
        open(self.f, "w").write('services:\n  a:\n    image: "prom/node-exporter:v1.12.0"\n')
        rw = ac.plan_rewrite([self.f], "prom/node-exporter", "v1.12.0", "v1.12.1")
        self.assertIn('image: "prom/node-exporter:v1.12.1"', rw.new_text)

    def test_variable_tag_refused(self):
        open(self.f, "w").write("services:\n  a:\n    image: prom/node-exporter:${TAG:-v1.12.0}\n")
        with self.assertRaisesRegex(ac.RewriteError, "edit the tag by hand"):
            ac.plan_rewrite([self.f], "prom/node-exporter", "v1.12.0", "v1.12.1")

    def test_two_matches_refused(self):
        open(self.f, "w").write("services:\n  a:\n    image: prom/x:v1\n  b:\n    image: prom/x:v1\n")
        with self.assertRaisesRegex(ac.RewriteError, "2 lines use prom/x:v1"):
            ac.plan_rewrite([self.f], "prom/x", "v1", "v2")

    def test_longer_tag_not_matched(self):
        open(self.f, "w").write("services:\n  a:\n    image: prom/x:v1.1\n")
        with self.assertRaises(ac.RewriteError):
            ac.plan_rewrite([self.f], "prom/x", "v1", "v2")

    def test_missing_compose_file(self):
        with self.assertRaisesRegex(ac.RewriteError, "compose file not found: /nope/docker-compose.yml"):
            ac.plan_rewrite(["/nope/docker-compose.yml"], "prom/x", "v1", "v2")

    def test_write_backup_and_restore(self):
        rw = ac.plan_rewrite([self.f], "prom/node-exporter", "v1.12.0", "v1.12.1")
        backup = ac.write_rewrite(rw)
        self.assertTrue(backup.startswith(self.f + ".bak-"))
        self.assertEqual(open(backup).read(), COMPOSE)
        self.assertIn("v1.12.1", open(self.f).read())
        self.assertEqual(stat.S_IMODE(os.stat(self.f).st_mode), 0o640)
        ac.restore(rw, backup)
        self.assertEqual(open(self.f).read(), COMPOSE)

    def test_dry_run_writes_nothing(self):
        rw = ac.plan_rewrite([self.f], "prom/node-exporter", "v1.12.0", "v1.12.1")
        with mock.patch("builtins.print"):
            self.assertIsNone(ac.write_rewrite(rw, dry_run=True))
        self.assertEqual(open(self.f).read(), COMPOSE)
        self.assertEqual(os.listdir(self.d.name), ["docker-compose.yml"])


class Commands(unittest.TestCase):
    def test_compose_cmd(self):
        i = Item(name="image-watch-socket-proxy", service="socket-proxy", workdir="/home/brent/image-watch",
                 config_files=["/a/docker-compose.yml", "/a/override.yml"])
        self.assertEqual(ac.compose_cmd(i, "pull", "socket-proxy"),
                         ["docker", "compose", "-f", "/a/docker-compose.yml", "-f", "/a/override.yml", "pull", "socket-proxy"])

    def test_compose_cmd_pins_project(self):
        i = Item(name="x", service="s", project="custom", workdir="/a", config_files=["/a/c.yml"])
        self.assertEqual(ac.compose_cmd(i, "up"), ["docker", "compose", "-p", "custom", "-f", "/a/c.yml", "up"])

    def test_digest_matches(self):
        ds = ["prom/node-exporter@sha256:aaa", "docker.io/prom/node-exporter@sha256:bbb"]
        self.assertTrue(ac.digest_matches(ds, "sha256:bbb"))
        self.assertFalse(ac.digest_matches(ds, "sha256:ccc"))
        self.assertFalse(ac.digest_matches([], "sha256:aaa"))


if __name__ == "__main__":
    unittest.main()
