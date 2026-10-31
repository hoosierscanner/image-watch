import re, sys, unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import watch

AT = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d$")


class Helpers(unittest.TestCase):
    def test_image_ref_uses_container_image(self):
        self.assertEqual(watch.image_ref({"Image": "prom/x:v1"}, {}), "prom/x:v1")

    def test_image_ref_falls_back_to_repotags(self):
        self.assertEqual(watch.image_ref({"Image": "sha256:abc"}, {"RepoTags": ["prom/x:v1"]}), "prom/x:v1")

    def test_running_digest(self):
        img = {"RepoDigests": ["robotastic/trunk-recorder-pro@sha256:aaa"]}
        self.assertEqual(watch.running_digest(img, "robotastic/trunk-recorder-pro"), "sha256:aaa")
        self.assertIsNone(watch.running_digest({"RepoDigests": []}, "robotastic/trunk-recorder-pro"))

    def test_running_digest_library_image(self):
        self.assertEqual(watch.running_digest({"RepoDigests": ["alpine@sha256:bbb"]}, "library/alpine"), "sha256:bbb")

    def test_source_repo(self):
        self.assertEqual(watch.source_repo({"github": "o/r"}, {}), "o/r")
        self.assertEqual(watch.source_repo({}, {"org.opencontainers.image.source": "https://github.com/a/b"}), "a/b")
        self.assertEqual(watch.source_repo({}, {}), "")

    def test_is_newer(self):
        self.assertTrue(watch.is_newer("v1.12.0", "v1.12.1"))
        self.assertFalse(watch.is_newer("v1.12.1", "v1.12.1"))
        self.assertFalse(watch.is_newer("v1.13.0", "v1.12.1"))
        self.assertTrue(watch.is_newer("edge", "nightly"))
        self.assertFalse(watch.is_newer("edge", "edge"))


class DigestNotice(unittest.TestCase):
    def run_check(self, discord_ok=True):
        c = {"Image": "robotastic/trunk-recorder-pro:edge"}
        img = {"RepoDigests": ["robotastic/trunk-recorder-pro@sha256:old"], "Config": {"Labels": {}}}
        opts = {"github": "o/r", "flag_prs": {"104": "call store"}}
        reg = mock.Mock()
        reg.digest.return_value = "sha256:new"
        reg.labels.return_value = {}
        state = {}
        with mock.patch.object(watch, "Registry", return_value=reg), \
             mock.patch.object(watch, "changes", return_value=([("abc", "msg")], "https://gh/compare")), \
             mock.patch.object(watch, "github", return_value={"merged_at": "2026-10-01", "merge_commit_sha": "abc"}), \
             mock.patch.object(watch, "summarize", return_value="• summary"), \
             mock.patch.object(watch, "discord", return_value=discord_ok):
            announced = watch.check_one("trunk-pro", c, opts, state, img)
        return announced, state

    def test_saves_digest_notice(self):
        announced, state = self.run_check()
        self.assertTrue(announced)
        self.assertEqual(state["trunk-pro"]["digest"], "sha256:new")
        n = state["trunk-pro"]["digest_notice"]
        self.assertEqual(n["digest"], "sha256:new")
        self.assertEqual(n["summary"], "• summary")
        self.assertEqual(n["flags"], ["⚠️ Includes PR #104: call store"])
        self.assertEqual(n["url"], "https://gh/compare")
        self.assertRegex(n["at"], AT)

    def test_no_notice_when_discord_fails(self):
        announced, state = self.run_check(discord_ok=False)
        self.assertFalse(announced)
        self.assertEqual(state, {})


class ReleaseNotice(unittest.TestCase):
    def test_saves_release_notice(self):
        img = {"RepoTags": ["prom/node-exporter:v1.12.0"], "Config": {"Labels": {}}}
        opts = {"github": "prometheus/node_exporter", "releases": True}
        state = {}
        with mock.patch.object(watch, "github", return_value={"tag_name": "v1.12.1", "html_url": "https://gh/rel"}), \
             mock.patch.object(watch, "changes", return_value=([], None)), \
             mock.patch.object(watch, "summarize", return_value=None), \
             mock.patch.object(watch, "discord", return_value=True):
            self.assertTrue(watch.check_release("node-exporter", img, opts, state))
        n = state["node-exporter"]["release_notice"]
        self.assertEqual(state["node-exporter"]["release"], "v1.12.1")
        self.assertEqual(n["tag"], "v1.12.1")
        self.assertEqual(n["summary"], "No commit list available.")
        self.assertEqual(n["url"], "https://gh/rel")
        self.assertRegex(n["at"], AT)

    def test_not_newer_posts_nothing(self):
        img = {"RepoTags": ["prom/node-exporter:v1.12.1"], "Config": {"Labels": {}}}
        state = {}
        with mock.patch.object(watch, "github", return_value={"tag_name": "v1.12.1"}), \
             mock.patch.object(watch, "discord") as d:
            self.assertFalse(watch.check_release("node-exporter", img, {"github": "p/n"}, state))
            d.assert_not_called()
        self.assertEqual(state, {})


if __name__ == "__main__":
    unittest.main()
