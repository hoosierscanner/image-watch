import json, os, sys, tempfile, unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import apply_status as s
import watch

LABELS = {"com.docker.compose.project": "node-exporter",
          "com.docker.compose.service": "node-exporter",
          "com.docker.compose.project.working_dir": "/home/brent/node-exporter",
          "com.docker.compose.project.config_files": "/home/brent/node-exporter/docker-compose.yml"}


def item(**kw):
    base = dict(name="x", running="sha256:aaa", latest="sha256:aaa", tag="edge", ref="o/x:edge")
    base.update(kw)
    return s.Item(**base)


class Classify(unittest.TestCase):
    def test_up_to_date(self):
        i = item(); s.classify(i, {})
        self.assertIsNone(i.kind); self.assertFalse(i.selected)

    def test_announced_digest_is_selected_with_notice(self):
        n = {"digest": "sha256:bbb", "summary": "hi", "flags": [], "url": None, "at": "2026-10-31 10:00"}
        i = item(latest="sha256:bbb"); s.classify(i, {"digest": "sha256:bbb", "digest_notice": n})
        self.assertEqual(i.kind, "digest"); self.assertTrue(i.announced); self.assertTrue(i.selected)
        self.assertEqual(i.notice, n)

    def test_unannounced_digest_not_selected(self):
        old = {"digest": "sha256:bbb", "summary": "old"}
        i = item(latest="sha256:ccc"); s.classify(i, {"digest": "sha256:bbb", "digest_notice": old})
        self.assertEqual(i.kind, "digest"); self.assertFalse(i.announced); self.assertFalse(i.selected)
        self.assertIsNone(i.notice)

    def test_announced_before_notices_were_saved(self):
        i = item(latest="sha256:bbb"); s.classify(i, {"digest": "sha256:bbb"})
        self.assertTrue(i.selected); self.assertIsNone(i.notice)

    def test_release_beats_digest(self):
        n = {"tag": "v1.12.1", "summary": "rel"}
        i = item(tag="v1.12.0", latest="sha256:bbb", new_tag="v1.12.1")
        s.classify(i, {"release": "v1.12.1", "release_notice": n})
        self.assertEqual(i.kind, "release"); self.assertTrue(i.selected); self.assertEqual(i.notice, n)

    def test_no_running_digest_is_error(self):
        i = item(running=None, latest="sha256:bbb"); s.classify(i, {"digest": "sha256:bbb"})
        self.assertEqual(i.error, "running image has no registry digest"); self.assertFalse(i.selected)

    def test_error_row_never_selected(self):
        i = item(error="check failed: boom", latest="sha256:bbb"); s.classify(i, {"digest": "sha256:bbb"})
        self.assertIsNone(i.kind); self.assertFalse(i.selected)


class StatusText(unittest.TestCase):
    def test_texts(self):
        self.assertEqual(s.short("sha256:525e9f1e1e61abcdef"), "525e9f1e1e61")
        self.assertEqual(s.short(None), "?")
        self.assertEqual(s.status_text(item()), "up to date")
        i = item(running="sha256:525e9f1e1e61ff", latest="sha256:d9b324e2dd1eff", kind="digest", announced=True,
                 notice={"flags": ["⚠️ Includes PR #104: x"]})
        self.assertEqual(s.status_text(i), "new build 525e9f1e1e61 → d9b324e2dd1e  ⚠")
        i = item(tag="v1.12.0", new_tag="v1.12.1", kind="release", announced=False)
        self.assertEqual(s.status_text(i),
                         "release v1.12.0 → v1.12.1 (tag rewrite) · not announced: no summary, PRs not checked")
        self.assertEqual(s.status_text(item(error="not a compose container")), "not a compose container")


class LoadState(unittest.TestCase):
    def test_load_state_missing_or_corrupt(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(s.load_state(os.path.join(d, "nope.json")), {})
            p = os.path.join(d, "bad.json"); open(p, "w").write("{half")
            self.assertEqual(s.load_state(p), {})
            open(p, "w").write('{"a": {"digest": "sha256:x"}}')
            self.assertEqual(s.load_state(p), {"a": {"digest": "sha256:x"}})


class CheckContainer(unittest.TestCase):
    def run_check(self, labels=LABELS, digests=None, release=None, opts=None):
        digests = digests or {"v1.12.0": "sha256:aaa"}
        img = {"RepoDigests": ["prom/node-exporter@sha256:aaa"], "RepoTags": ["prom/node-exporter:v1.12.0"],
               "Config": {"Labels": {}}}
        reg = mock.Mock()
        def digest(tag):
            if tag not in digests:
                raise RuntimeError("registry prom/node-exporter:%s: 404" % tag)
            return digests[tag]
        reg.digest.side_effect = digest
        c = {"Image": "prom/node-exporter:v1.12.0", "ImageID": "sha256:img", "Labels": labels}
        with mock.patch.object(watch, "docker", return_value=img), \
             mock.patch.object(watch, "Registry", return_value=reg), \
             mock.patch.object(watch, "github", return_value=release):
            return s.check_container("node-exporter", c, opts if opts is not None else {"github": "prometheus/node_exporter", "releases": True}, {})

    def test_compose_fields_and_up_to_date(self):
        i = self.run_check()
        self.assertEqual((i.project, i.service, i.workdir), ("node-exporter", "node-exporter", "/home/brent/node-exporter"))
        self.assertEqual(i.config_files, ["/home/brent/node-exporter/docker-compose.yml"])
        self.assertEqual((i.ref, i.tag, i.running, i.latest, i.expected), ("prom/node-exporter:v1.12.0", "v1.12.0", "sha256:aaa", "sha256:aaa", "sha256:aaa"))
        self.assertIsNone(i.kind); self.assertIsNone(i.error)

    def test_release_sets_expected_from_new_tag(self):
        i = self.run_check(digests={"v1.12.0": "sha256:aaa", "v1.12.1": "sha256:ddd"}, release={"tag_name": "v1.12.1"})
        self.assertEqual((i.kind, i.new_tag, i.expected), ("release", "v1.12.1", "sha256:ddd"))

    def test_release_tag_missing_from_registry_is_error(self):
        i = self.run_check(release={"tag_name": "v1.12.1"})
        self.assertTrue(i.error.startswith("check failed: registry prom/node-exporter:v1.12.1: 404"))
        self.assertFalse(i.selected)

    def test_not_compose(self):
        i = self.run_check(labels={})
        self.assertEqual(i.error, "not a compose container")

    def test_releases_off_ignores_github(self):
        i = self.run_check(release={"tag_name": "v9.0.0"}, opts={})
        self.assertIsNone(i.new_tag)


class Setup(unittest.TestCase):
    def test_setup_watch_points_at_host_paths(self):
        saved = (watch.CONFIG, watch.STATE, watch.SECRETS, watch.docker)
        try:
            s.setup_watch(Path("/r"))
            self.assertEqual((watch.CONFIG, watch.STATE, watch.SECRETS), ("/r/watch.json", "/r/state/state.json", "/r/secrets"))
            self.assertIs(watch.docker, s.docker_get)
        finally:
            watch.CONFIG, watch.STATE, watch.SECRETS, watch.docker = saved


if __name__ == "__main__":
    unittest.main()
