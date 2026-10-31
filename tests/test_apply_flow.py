import os, sys, tempfile, unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import apply
from apply_status import Item

COMPOSE = "services:\n  node-exporter:\n    image: prom/node-exporter:v1.12.0\n"


class Flow(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory(); self.addCleanup(self.d.cleanup)
        self.f = os.path.join(self.d.name, "docker-compose.yml"); open(self.f, "w").write(COMPOSE)
        self.calls = []
        self.out = mock.patch("builtins.print"); self.out.start(); self.addCleanup(self.out.stop)

    def run_(self, argv, cwd):
        self.calls.append(argv[-1] if argv[-2] == "pull" else "up"); return self.rc.pop(0)

    def release_item(self):
        return Item(name="node-exporter", service="node-exporter", workdir=self.d.name, config_files=[self.f],
                    ref="prom/node-exporter:v1.12.0", tag="v1.12.0", running="sha256:aaa", latest="sha256:aaa",
                    new_tag="v1.12.1", expected="sha256:ddd", kind="release", announced=True, selected=True)

    def go(self, item, answers, rc=(0, 0), digests=("prom/node-exporter@sha256:ddd",), dry_run=False):
        self.rc = list(rc); answers = list(answers)
        return apply.apply_item(item, ask=lambda p: answers.pop(0), run=self.run_,
                                digests=lambda ref: list(digests), state=lambda n: "running sha256:new", dry_run=dry_run)

    def test_release_happy_path(self):
        self.assertEqual(self.go(self.release_item(), ["y"]), "updated")
        self.assertEqual(self.calls, ["node-exporter", "up"])
        self.assertIn("v1.12.1", open(self.f).read())
        self.assertEqual(len([x for x in os.listdir(self.d.name) if ".bak-" in x]), 1)

    def test_default_is_no(self):
        self.assertEqual(self.go(self.release_item(), [""]), "skipped")
        self.assertEqual(self.calls, []); self.assertEqual(open(self.f).read(), COMPOSE)

    def test_q_quits(self):
        self.assertEqual(self.go(self.release_item(), ["Q"]), "quit")

    def test_pull_failure_restores_compose(self):
        self.assertEqual(self.go(self.release_item(), ["y"], rc=(1,)), "failed: pull exit 1")
        self.assertEqual(open(self.f).read(), COMPOSE); self.assertEqual(self.calls, ["node-exporter"])

    def test_digest_mismatch_declined_restores(self):
        r = self.go(self.release_item(), ["y", ""], digests=("prom/node-exporter@sha256:eee",))
        self.assertEqual(r, "skipped: digest changed")
        self.assertEqual(open(self.f).read(), COMPOSE); self.assertEqual(self.calls, ["node-exporter"])

    def test_digest_mismatch_accepted_recreates(self):
        r = self.go(self.release_item(), ["y", "y"], digests=("prom/node-exporter@sha256:eee",))
        self.assertEqual(r, "updated"); self.assertEqual(self.calls, ["node-exporter", "up"])

    def test_up_failure_reports_backup(self):
        r = self.go(self.release_item(), ["y"], rc=(0, 3))
        self.assertTrue(r.startswith("failed: up exit 3 (compose backup: " + self.f + ".bak-"))

    def test_rewrite_refused_skips_without_asking(self):
        open(self.f, "w").write("services:\n  a:\n    image: prom/node-exporter:${T}\n")
        self.assertTrue(self.go(self.release_item(), []).startswith("skipped: no line"))

    def test_dry_run_changes_nothing(self):
        self.assertEqual(self.go(self.release_item(), ["y"], dry_run=True), "would update")
        self.assertEqual(self.calls, []); self.assertEqual(open(self.f).read(), COMPOSE)
        self.assertEqual(os.listdir(self.d.name), ["docker-compose.yml"])

    def test_forced_pull_with_same_digest_is_up_to_date(self):
        i = Item(name="image-watch-socket-proxy", service="socket-proxy", workdir=self.d.name, config_files=[self.f],
                 ref="tecnativa/docker-socket-proxy:v0.5.0", tag="v0.5.0", running="sha256:aaa", latest="sha256:aaa",
                 expected="sha256:aaa", selected=True)
        r = self.go(i, ["y"], digests=("tecnativa/docker-socket-proxy@sha256:aaa",))
        self.assertEqual(r, "up to date"); self.assertEqual(self.calls, ["socket-proxy"])
        self.assertEqual(open(self.f).read(), COMPOSE)

    def test_run_selected_quit_skips_rest(self):
        a, b = self.release_item(), self.release_item(); b.name = "second"
        answers = ["q"]
        res = apply.run_selected([a, b], lambda p: answers.pop(0), run=self.run_,
                                 digests=lambda r: [], state=lambda n: "")
        self.assertEqual(res, [("node-exporter", "quit"), ("second", "skipped")])

    def test_eof_on_prompt_means_no(self):
        def eof(prompt): raise EOFError
        self.rc = []
        r = apply.apply_item(self.release_item(), ask=eof, run=self.run_, digests=lambda ref: [], state=lambda n: "")
        self.assertEqual(r, "skipped"); self.assertEqual(open(self.f).read(), COMPOSE)

    def test_eof_on_second_prompt_restores_compose(self):
        answers = ["y"]
        def ask(prompt):
            if answers: return answers.pop(0)
            raise EOFError
        self.rc = [0]
        r = apply.apply_item(self.release_item(), ask=ask, run=self.run_,
                             digests=lambda ref: ["prom/node-exporter@sha256:eee"], state=lambda n: "")
        self.assertEqual(r, "skipped: digest changed"); self.assertEqual(open(self.f).read(), COMPOSE)

    def test_run_selected_empty(self):
        self.assertEqual(apply.run_selected([], lambda p: self.fail("asked")), [])


class Describe(unittest.TestCase):
    def test_describe_digest_with_notice(self):
        i = Item(name="trunk-pro", service="trunk-pro", running="sha256:525e9f1e1e61x", expected="sha256:d9b324e2dd1ex",
                 kind="digest", announced=True,
                 notice={"flags": ["⚠️ Includes PR #104: call store"], "summary": "• EDACS", "url": "https://gh/c", "at": "2026-10-31 09:00"})
        text = apply.describe(i, None)
        for part in ["── trunk-pro (trunk-pro)", "pull + recreate: 525e9f1e1e61 → d9b324e2dd1e",
                     "⚠️ Includes PR #104: call store", "announced 2026-10-31 09:00", "https://gh/c", "• EDACS"]:
            self.assertIn(part, text)

    def test_describe_forced(self):
        i = Item(name="x", service="x", running="sha256:a", expected="sha256:a")
        self.assertIn("forced: no update pending", apply.describe(i, None))


if __name__ == "__main__":
    unittest.main()
