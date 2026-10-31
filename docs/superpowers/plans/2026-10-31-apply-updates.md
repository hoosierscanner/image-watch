# apply.py Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task (single agent, native execution). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A host-side `./apply.py` that lists every watched container with live update status, lets the user tick containers in a curses checklist, confirms each one y/N, and pulls and recreates it with Docker Compose. For new releases it first rewrites the image tag in the compose file.

**Architecture:** `watch.py` (the existing watcher, running in a container) gains a few small shared helpers and saves each Discord notice it posts into `state/state.json`. Three new root-level modules sit beside it: `apply_status.py` (discovery + live check, reusing `watch.py` by import), `apply_compose.py` (tag rewrite, compose argv, digest checks) and `apply_ui.py` (checklist model + curses). `apply.py` is the CLI that ties them together. Reads go to Docker over `/var/run/docker.sock` (GET only). Writes go through the `docker compose` CLI.

**Tech Stack:** Python standard library only (host: Python 3.14; the watcher container: Python 3.13, so `watch.py` must stay 3.13-compatible). Tests: `unittest` + `unittest.mock`. pytest is NOT installed; don't install it.

**Spec:** `docs/superpowers/specs/2026-10-31-apply-updates-design.md`. Read it before Task 1.

## Global Constraints

- Work in `/home/brent/image-watch` on branch `main` (the user's repo; commit after each task, never push).
- No third-party packages, no `pip install`, no new apt packages. Host runs Python 3.14; `watch.py` also runs in `python:3.13-slim`.
- Run all tests with: `cd ~/image-watch && python3 -m unittest discover -s tests -v`
- Each test file starts with `sys.path.insert(0, str(Path(__file__).resolve().parent.parent))` so `import watch` / `import apply_status` work. No `tests/__init__.py`.
- `apply.py` never writes `state/state.json`. Only the watcher writes it.
- Every subprocess call uses an argv list, never `shell=True`.
- Every compose write and every `docker compose pull`/`up` happens only after the user typed `y` for that container.
- Default answer to every prompt is No. Only a lone `y` (case-insensitive, stripped) means yes.
- Times shown to the user are local time (`time.strftime`), never UTC.
- **Never pull or recreate `trunk-pro` during this build.** It is a live radio recorder. Read-only commands (`--list`, `docker inspect`) are fine.
- Commit messages end with the two attribution lines:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01YUy4EP4VNh4Smcb9ZXroFg
  ```
  (If you are a different model, use your own model name in `Co-Authored-By`.)

## Facts about this host (verified 2026-10-31)

- User `brent` is in the `docker` group; `/var/run/docker.sock` is `root:docker 660`.
- Docker 29 uses the containerd image store: an image's `RepoDigests` entry (`repo@sha256:…`) equals the registry's `Docker-Content-Digest` for that tag (the index digest). So "pulled digest == registry digest" is a direct string compare.
- Compose labels on the watched containers:

  | container | project | service | working_dir | config_files | image |
  |---|---|---|---|---|---|
  | trunk-pro | trunk-pro | trunk-pro | /home/brent/trunk-pro | /home/brent/trunk-pro/docker-compose.yml | robotastic/trunk-recorder-pro:edge |
  | node-exporter | node-exporter | node-exporter | /home/brent/node-exporter | /home/brent/node-exporter/docker-compose.yml | prom/node-exporter:v1.12.1 |
  | image-watch-socket-proxy | image-watch | socket-proxy | /home/brent/image-watch | /home/brent/image-watch/docker-compose.yml | tecnativa/docker-socket-proxy:v0.5.0 |

- `watch.json` watches those three; node-exporter and image-watch-socket-proxy have `"releases": true`.
- The image-watch container mounts `./watch.py` read-only, so a `watch.py` change takes effect after `docker compose restart image-watch`.

## Review Focus

1. **A GitHub release published before its image is in the registry.** Expect: the row shows `check failed: registry …: 404`, can't be selected, and other rows are unaffected. Test: `test_release_tag_missing_from_registry_is_error` (Task 2).
2. **`state.json` missing or corrupt** (fresh install, or a half-written file). Expect: treated as `{}`, nothing announced, no crash. Test: `test_load_state_missing_or_corrupt` (Task 2).
3. **A running image with no registry digest** (built locally, or `RepoDigests` empty). Expect: unselectable row `running image has no registry digest`. Test: `test_no_running_digest_is_error` (Task 2).
4. **Compose file moved or deleted since the container was created.** Expect: release rewrite refuses with `compose file not found: <path>`, and nothing is written. Test: `test_missing_compose_file` (Task 3).
5. **A narrow terminal** (SSH from a phone). Expect: rows truncated to the width, no curses exception. Test: `test_row_line_truncates_to_width` (Task 4).

---

## File Structure

| File | Responsibility |
|---|---|
| `watch.py` (modify) | Add `image_ref`, `running_digest`, `source_repo`, `is_newer` helpers; save `digest_notice` / `release_notice` after posting. |
| `apply_status.py` (create) | `Item` dataclass, Docker socket GET client, `setup_watch`, `load_state`, `classify`, `check_container`, `collect`, `status_text`, `short`. |
| `apply_compose.py` (create) | `Rewrite`, `RewriteError`, `plan_rewrite`, `write_rewrite`, `restore`, `compose_cmd`, `digest_matches`, `pulled_digests`, `container_state`, `run_cmd`. |
| `apply_ui.py` (create) | `Checklist` model, `row_line`, `detail_lines`, `run_checklist` (curses). |
| `apply.py` (create, executable) | `describe`, `apply_item`, `run_selected`, `print_table`, `main`. |
| `tests/test_watch_notices.py` | Task 1 tests. |
| `tests/test_apply_status.py` | Task 2 tests. |
| `tests/test_apply_compose.py` | Task 3 tests. |
| `tests/test_apply_ui.py` | Task 4 tests. |
| `tests/test_apply_flow.py` | Task 5 tests. |
| `README.md`, `.gitignore` (modify) | Task 6. |

---

### Task 1: watch.py shared helpers + saved notices

**Files:**
- Modify: `watch.py` (`check_one` at lines 236-273, `check_release` at lines 276-310)
- Test: `tests/test_watch_notices.py` (create)

**Interfaces:**
- Produces (module-level functions in `watch.py`, used by Task 2):
  - `image_ref(c: dict, img: dict) -> str | None`: `c["Image"]` unless it starts with `"sha256:"`, else `(img.get("RepoTags") or [None])[0]`. (Same as current lines 237-238.)
  - `running_digest(img: dict, repo: str) -> str | None`: same expression as current line 240.
  - `source_repo(opts: dict, labels: dict) -> str`: `opts.get("github")` or the `org.opencontainers.image.source` label with `https://github.com/` stripped, else `""`. (Same as lines 247 and 283.)
  - `is_newer(cur_tag: str, new_tag: str) -> bool`: True when `new_tag` is a newer release than `cur_tag`. If both parse with `semver`, compare tuples (`new > cur`); otherwise `new_tag != cur_tag`. (This is the negation of line 291's condition.)
  - State shape after a successful post (existing keys unchanged):
    ```json
    {"trunk-pro": {"digest": "sha256:…",
                   "digest_notice": {"digest": "sha256:…", "summary": "…", "flags": ["⚠️ Includes PR #104: …"], "url": "https://github.com/…/compare/…", "at": "2026-10-31 10:15"}},
     "node-exporter": {"release": "v1.12.1",
                       "release_notice": {"tag": "v1.12.1", "summary": "…", "url": "https://…", "at": "2026-10-31 10:15"}}}
    ```
    `summary` is the exact `desc` string posted to Discord. `url` is `compare` (may be `None`) for digests, and `embed["url"]` for releases. `at` is `time.strftime("%Y-%m-%d %H:%M")`.

- [ ] **Step 1: Write the failing tests** in `tests/test_watch_notices.py`:

```python
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
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `cd ~/image-watch && python3 -m unittest tests.test_watch_notices -v 2>&1 | tail -5` (if module-style import fails, use `python3 -m unittest discover -s tests -p 'test_watch_notices.py' -v`)
Expected: errors such as `AttributeError: module 'watch' has no attribute 'image_ref'` and a `KeyError: 'digest_notice'`.

- [ ] **Step 3: Implement in `watch.py`**

  - Add the four helpers in the `# ---- the check` section, above `watched()`. Put `is_newer` right after `semver` in the GitHub section.
  - In `check_one`, replace lines 237-238 with `ref = image_ref(c, img)`, line 240 with `running = running_digest(img, repo)`, and line 247 with `src = source_repo(opts, new_labels)`.
  - In `check_one`, after a successful `discord(embed)`, write `digest` (as now) and `digest_notice` (shape above; `flags` is the existing `flags` list, `url` is `compare`).
  - In `check_release`, replace line 283 with `src = source_repo(opts, labels)`, and replace the `if (new_v <= cur_v) ...` line (291) with `if not is_newer(tag, new_tag): return False`. Remove the now-unused `cur_v, new_v` line.
  - In `check_release`, after a successful post, write `release` (as now) and `release_notice` (`url` = `embed["url"]`).
  - Use `st = state.setdefault(name, {})` once and set both keys on `st`.
  - Don't change anything else, and no new imports (`time` is already imported).

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `python3 -m unittest discover -s tests -v`
Expected: `Ran 10 tests … OK`.

- [ ] **Step 5: Reload the live watcher and check it still runs**

Run:
```bash
cd ~/image-watch && python3 -m py_compile watch.py && docker compose restart image-watch && sleep 20 && docker compose logs --tail 8 image-watch
```
Expected: log lines like `… trunk-pro up to date` / `… node-exporter up to date` and no `Traceback`. (`check failed:` for a network blip is OK; a `NameError`/`AttributeError` is not. Fix it before continuing.)

- [ ] **Step 6: Commit**

```bash
git add watch.py tests/test_watch_notices.py
git commit -m "watch.py: save each posted notice in state.json; share ref/digest/source/version helpers"   # plus the attribution lines
```

---

### Task 2: apply_status.py — discovery and live status

**Files:**
- Create: `apply_status.py`
- Test: `tests/test_apply_status.py`

**Interfaces:**
- Consumes (from `watch`): `watched()`, `docker`, `parse_ref`, `Registry`, `github`, `image_ref`, `running_digest`, `source_repo`, `is_newer`, and module globals `CONFIG`, `STATE`, `SECRETS`.
- Produces:
  ```python
  @dataclass
  class Item:
      name: str
      project: str | None = None
      service: str | None = None
      workdir: str | None = None
      config_files: list[str] = field(default_factory=list)
      ref: str = ""              # e.g. "prom/node-exporter:v1.12.0"
      tag: str = ""              # e.g. "v1.12.0"
      running: str | None = None # running image's registry digest
      latest: str | None = None  # registry digest of `tag` now
      new_tag: str | None = None # set only when a newer release exists
      expected: str | None = None# digest the pull must produce: latest, or registry digest of new_tag
      kind: str | None = None    # "digest" | "release" | None (up to date)
      announced: bool = False
      notice: dict | None = None # the saved digest_notice / release_notice, when it matches
      error: str | None = None   # set => row unselectable
      selected: bool = False
  ```
  - `ROOT: Path`: `Path(__file__).resolve().parent`
  - `docker_get(path: str) -> dict | list`: GET over `/var/run/docker.sock`, raising `RuntimeError("docker <path>: <status>")` unless status is 200.
  - `setup_watch(root: Path = ROOT) -> None`: sets `watch.CONFIG = str(root/"watch.json")`, `watch.STATE = str(root/"state"/"state.json")`, `watch.SECRETS = str(root/"secrets")`, `watch.docker = docker_get`.
  - `load_state(path: str) -> dict`: parsed JSON, or `{}` on `OSError`/`ValueError`.
  - `classify(item: Item, entry: dict) -> None`: fills `kind`, `announced`, `notice`, `selected`, `error` from `running/latest/new_tag` and the container's state entry.
  - `check_container(name: str, c: dict, opts: dict, state: dict) -> Item`: never raises.
  - `collect() -> list[Item]`: all watched containers, sorted by name, checked in parallel.
  - `short(digest: str | None) -> str`: `"?"` for None, else the part after `:` cut to 12 chars.
  - `status_text(item: Item) -> str`

**The rules `classify` implements** (in this order):
1. If `item.error` is set: `kind=None`, `selected=False`, return.
2. If `item.running is None`: `error = "running image has no registry digest"`, `selected=False`, return.
3. If `item.new_tag`: `kind="release"`, `announced = entry.get("release") == new_tag`, `notice = entry.get("release_notice")` only if its `"tag"` equals `new_tag`.
4. Else if `item.latest != item.running`: `kind="digest"`, `announced = entry.get("digest") == latest`, `notice = entry.get("digest_notice")` only if its `"digest"` equals `latest`.
5. Else `kind=None`.
6. `selected = kind is not None and announced`.

**`check_container` steps** (wrap everything after reading the labels in `try/except Exception as e: item.error = "check failed: " + str(e)[:100]`):
1. `labels = c.get("Labels") or {}`. Fill `project` / `service` / `workdir` from `com.docker.compose.project`, `com.docker.compose.service`, `com.docker.compose.project.working_dir`; `config_files = labels["com.docker.compose.project.config_files"].split(",")`. If `working_dir`, `service` or `config_files` is missing: `error = "not a compose container"`, call `classify(item, {})`, return.
2. `img = watch.docker("/images/%s/json" % c["ImageID"])`; `item.ref = watch.image_ref(c, img)`; `registry, repo, item.tag = watch.parse_ref(item.ref)`; `item.running = watch.running_digest(img, repo)`.
3. `reg = watch.Registry(registry, repo)`; `item.latest = reg.digest(item.tag)`.
4. If `opts.get("releases")`: `src = watch.source_repo(opts, img.get("Config", {}).get("Labels") or {})`; if `src`, `rel = watch.github("/repos/%s/releases/latest" % src)`; if `rel` and `rel.get("tag_name")` and `watch.is_newer(item.tag, rel["tag_name"])`: `item.new_tag = rel["tag_name"]`.
5. `item.expected = reg.digest(item.new_tag) if item.new_tag else item.latest` (this raises for a release whose image isn't pushed yet → error row; Review Focus 1).
6. `classify(item, state.get(name, {}))`; return.

**`status_text` format** (exact strings, which the tests pin):
- error → the error text itself
- `kind None` → `"up to date"`
- digest → `"new build " + short(running) + " → " + short(latest)`
- release → `"release " + tag + " → " + new_tag + " (tag rewrite)"`
- then, if `notice` has non-empty `"flags"`, append `"  ⚠"`
- then, if `kind` is set and not `announced`, append `" · not announced: no summary, PRs not checked"`

**`docker_get` body** (the only non-obvious code in this task):
```python
class _UnixConn(http.client.HTTPConnection):
    def __init__(self, sock_path):
        super().__init__("localhost", timeout=30)
        self.sock_path = sock_path
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.sock_path)

def docker_get(path):
    conn = _UnixConn("/var/run/docker.sock")
    try:
        conn.request("GET", path)
        r = conn.getresponse()
        body = r.read()
    finally:
        conn.close()
    if r.status != 200:
        raise RuntimeError("docker %s: %d" % (path, r.status))
    return json.loads(body)
```

**`collect`:** `setup_watch()` must already have been called. `state = load_state(watch.STATE)`. `pairs = watch.watched()`. Use `concurrent.futures.ThreadPoolExecutor(max_workers=6)` to map `check_container(name, c, opts, state)` over `pairs.items()`, and return the result sorted by `item.name`.

- [ ] **Step 1: Write the failing tests** in `tests/test_apply_status.py`:

```python
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
            return s.check_container("node-exporter", c, opts or {"github": "prometheus/node_exporter", "releases": True}, {})

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
```

- [ ] **Step 2: Run and confirm the tests fail**

Run: `python3 -m unittest discover -s tests -p 'test_apply_status.py' -v 2>&1 | tail -3`
Expected: `ModuleNotFoundError: No module named 'apply_status'`.

- [ ] **Step 3: Implement `apply_status.py`** following the interfaces, rules and step lists above. Imports: `concurrent.futures, http.client, json, socket` from the stdlib, `dataclass, field` from `dataclasses`, `Path` from `pathlib`, and `import watch`.

- [ ] **Step 4: Run all tests and confirm they pass**

Run: `python3 -m unittest discover -s tests -v`
Expected: all pass (Task 1's 10 + Task 2's tests).

- [ ] **Step 5: Smoke test against real Docker (read-only)**

Run:
```bash
cd ~/image-watch && python3 -c "
import apply_status as s; s.setup_watch()
for i in s.collect(): print(i.name, '|', i.service, '|', s.status_text(i))"
```
Expected: three lines, for `image-watch-socket-proxy | socket-proxy | …`, `node-exporter | node-exporter | …` and `trunk-pro | trunk-pro | …`, each with a real status (`up to date`, `new build …`, or `release …`). If one shows `check failed: …`, read the message: a GitHub rate limit or network error is acceptable; a Python error isn't, so fix it.

- [ ] **Step 6: Commit** `apply_status.py tests/test_apply_status.py` with message `apply_status: live update status for watched containers` (plus the attribution lines).

---

### Task 3: apply_compose.py — tag rewrite, compose commands, digest check

**Files:**
- Create: `apply_compose.py`
- Test: `tests/test_apply_compose.py`

**Interfaces:**
- Consumes: `Item` (Task 2) fields `name, service, workdir, config_files, ref, tag, new_tag`.
- Produces:
  - `class RewriteError(Exception)`
  - `@dataclass class Rewrite: path: str; old_text: str; new_text: str; diff: str`
  - `image_name(ref: str, tag: str) -> str`: `ref` without its trailing `":" + tag`.
  - `plan_rewrite(files: list[str], image: str, old_tag: str, new_tag: str) -> Rewrite`: reads files, matches nothing on disk else.
  - `write_rewrite(rw: Rewrite, dry_run: bool = False) -> str | None`: returns the backup path (None in dry run).
  - `restore(rw: Rewrite, backup: str | None) -> None`: copies the backup back over `rw.path` (no-op if `backup` is None).
  - `compose_cmd(item, *args: str) -> list[str]`: `["docker", "compose", "-f", f1, "-f", f2, …, *args]`.
  - `digest_matches(repo_digests: list[str], expected: str) -> bool`
  - `pulled_digests(ref: str) -> list[str]`: `docker image inspect --format '{{json .RepoDigests}}' <ref>` parsed; `[]` if the command fails.
  - `container_state(name: str) -> str`: `docker inspect --format '{{.State.Status}} {{.Image}}' <name>` stdout stripped, or `"unknown"` on failure.
  - `run_cmd(argv: list[str], cwd: str | None) -> int`: `subprocess.run(argv, cwd=cwd).returncode`, so output streams to the terminal.

**`plan_rewrite` rules:**
- For each path: if it doesn't exist, `raise RewriteError("compose file not found: " + path)`.
- The pattern (per line, `re.MULTILINE`): `^(\s*image:\s*)(["']?)` + `re.escape(image + ":" + old_tag)` + `(\2)(\s*(?:#.*)?)$`
- Count matches across all files. 0 → `RewriteError("no line 'image: %s:%s' in %s — edit the tag by hand" % (image, old_tag, ", ".join(files)))`. More than 1 → `RewriteError("%d lines use %s:%s — edit the tag by hand" % (n, image, old_tag))`.
- Replace only that match's `image:old_tag` part with `image:new_tag`, keeping the indent, quotes and comment.
- `diff` = `"".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), path, path))`.

**`write_rewrite`:** in dry run, print `would write <path> (backup <path>.bak-YYYYmmdd-HHMMSS)` and return None. Otherwise: `backup = path + ".bak-" + time.strftime("%Y%m%d-%H%M%S")`, `shutil.copy2(path, backup)`, write `new_text` to `path + ".tmp"`, `os.chmod` it to the original file's mode, `os.replace` it onto `path`, and return `backup`.

- [ ] **Step 1: Write the failing tests** in `tests/test_apply_compose.py`:

```python
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

    def test_digest_matches(self):
        ds = ["prom/node-exporter@sha256:aaa", "docker.io/prom/node-exporter@sha256:bbb"]
        self.assertTrue(ac.digest_matches(ds, "sha256:bbb"))
        self.assertFalse(ac.digest_matches(ds, "sha256:ccc"))
        self.assertFalse(ac.digest_matches([], "sha256:aaa"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run and confirm the tests fail** (`ModuleNotFoundError: No module named 'apply_compose'`).

Run: `python3 -m unittest discover -s tests -p 'test_apply_compose.py' -v 2>&1 | tail -3`

- [ ] **Step 3: Implement `apply_compose.py`** per the interfaces and rules above. Imports: `difflib, json, os, re, shutil, subprocess, time`, `dataclass`.

- [ ] **Step 4: Run all tests and confirm they pass**

Run: `python3 -m unittest discover -s tests -v` → `OK`.

- [ ] **Step 5: Smoke test the read-only helpers against real Docker**

Run:
```bash
cd ~/image-watch && python3 -c "
import apply_compose as ac
print(ac.pulled_digests('prom/node-exporter:v1.12.1'))
print(ac.container_state('node-exporter'))
print(ac.pulled_digests('no/such-image:x'))"
```
Expected: a list with one `prom/node-exporter@sha256:…`, then `running sha256:…`, then `[]`.

- [ ] **Step 6: Commit** `apply_compose.py tests/test_apply_compose.py` with message `apply_compose: compose tag rewrite with backup, compose argv, pulled-digest check` (plus the attribution lines).

---

### Task 4: apply_ui.py — checklist model and curses screen

**Files:**
- Create: `apply_ui.py`
- Test: `tests/test_apply_ui.py`

**Interfaces:**
- Consumes: `Item`, `status_text` from `apply_status`.
- Produces:
  - `class Checklist`, built as `Checklist(items: list[Item])`, with attributes `items` and `cursor: int` (starts at 0) and these methods:
    - `move(delta: int) -> None`: clamps to `[0, len-1]` (no wrap).
    - `toggle() -> None`: flips `selected` on the cursor row, only if its `error is None`.
    - `select_pending() -> None`: for every row without an error, `selected = kind is not None`.
    - `clear() -> None`: all `selected = False`.
    - `chosen() -> list[Item]`: selected rows in list order.
    - `handle_key(key: int) -> str | None`: returns `"done"` for Enter (`10`, `13`, `curses.KEY_ENTER`), `"quit"` for `ord("q")`, and None otherwise, after acting:
      - `curses.KEY_UP` / `ord("k")` → move(-1)
      - `curses.KEY_DOWN` / `ord("j")` → move(1)
      - `ord(" ")` → toggle
      - `ord("a")` → select_pending
      - `ord("n")` → clear
  - `row_line(item: Item, current: bool, width: int) -> str`: `("> " if current else "  ") + ("[x] " if item.selected else "[ ] ") + item.name.ljust(26) + " " + status_text(item)`, cut to `max(width - 1, 0)` characters.
  - `detail_lines(item: Item) -> list[str]`: up to 4 lines for the bottom panel:
    - each flag in `notice["flags"]`
    - the first non-empty line of `notice["summary"]` (if a notice exists)
    - `"announced " + notice["at"]` if the notice has `"at"`
    - if `kind` is set and `notice is None`: `"no saved summary"` when `announced`, otherwise `"not announced: no summary, PRs not checked"`
    - if `item.error`: `[item.error]`
  - `run_checklist(items: list[Item]) -> list[Item] | None`: `curses.wrapper` around the loop below; returns `chosen()` on `"done"` and None on `"quit"`.

**Screen layout** (in `_draw(stdscr, cl)`; wrap each `addstr` call in `try/except curses.error: pass`, because writing to the bottom-right cell raises):
- Line 0: `"image-watch apply — " + time.strftime("%H:%M")` in bold.
- Lines 2…: one `row_line` per item. The cursor row is `curses.A_REVERSE`. A row with an error is `curses.A_DIM`. A row with `kind` set is `curses.A_BOLD`.
- Two lines below the list: `detail_lines(cl.items[cl.cursor])`, each cut to the width.
- The last screen line: `"↑↓ move  space toggle  a all pending  n none  enter continue  q quit"`, cut to the width.
- If the screen has fewer rows than needed, skip the lines that don't fit: draw only while `y < height - 1`.
- Loop: `curses.curs_set(0)` (wrapped in try), then repeatedly `stdscr.erase()`, `_draw`, `stdscr.refresh()`, `key = stdscr.getch()`, `r = cl.handle_key(key)`, and return on `"done"`/`"quit"`. On `curses.KEY_RESIZE`, just redraw.

- [ ] **Step 1: Write the failing tests** in `tests/test_apply_ui.py`:

```python
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
```

- [ ] **Step 2: Run and confirm the tests fail** (`ModuleNotFoundError: No module named 'apply_ui'`).

- [ ] **Step 3: Implement `apply_ui.py`** per the interfaces and layout above. Imports: `curses, time`, and `from apply_status import Item, status_text`.

- [ ] **Step 4: Run all tests and confirm they pass**: `python3 -m unittest discover -s tests -v` → `OK`.

- [ ] **Step 5: Commit** `apply_ui.py tests/test_apply_ui.py` with message `apply_ui: checklist model and curses screen` (plus the attribution lines).

(The curses screen itself is checked by the user in Task 6. You can't operate it from a non-interactive shell.)

---

### Task 5: apply.py — confirm, apply, CLI

**Files:**
- Create: `apply.py` (shebang `#!/usr/bin/env python3`; `chmod +x apply.py`)
- Test: `tests/test_apply_flow.py`

**Interfaces:**
- Consumes: everything in the Produces blocks of Tasks 2-4.
- Produces:
  - `describe(item: Item, rw: Rewrite | None) -> str`: the block printed before the prompt. Lines, in this order, skipping any that are empty:
    1. `"── " + item.name + " (" + item.service + ")"`
    2. Action: `"pull + recreate: " + short(running) + " → " + short(expected)` for digest or forced, or `"release " + tag + " → " + new_tag + ": rewrite " + rw.path + ", pull + recreate"` for a release.
    3. Each flag from `notice["flags"]`, as is.
    4. `"announced " + notice["at"]` and `notice["url"]` (each when present).
    5. `notice["summary"]` (all of it), or the `detail_lines` note when there's no notice and `kind` is set, or `"forced: no update pending"` when `kind is None`.
    6. `rw.diff` for a release.
  - `apply_item(item, ask, run=run_cmd, digests=pulled_digests, state=container_state, dry_run=False) -> str`: returns one of `"updated"`, `"skipped"`, `"skipped: <reason>"`, `"failed: <reason>"`, `"quit"`.
  - `run_selected(items: list[Item], ask, **kw) -> list[tuple[str, str]]`: `(name, result)` for each item. After `"quit"`, the rest get `"skipped"` without being asked. An empty list returns `[]` and asks nothing.
  - `print_table(items: list[Item]) -> None`: one line per item, `("[x] " if selected else "[ ] ") + name.ljust(26) + " " + status_text(item)`.
  - `main(argv: list[str] | None = None) -> int`

**`apply_item` steps** (`p = print`; `ask(prompt) -> str` is `input` in production):
1. `rw = None`. If `item.kind == "release"`: `rw = plan_rewrite(item.config_files, image_name(item.ref, item.tag), item.tag, item.new_tag)`. On `RewriteError as e`: print it and return `"skipped: " + str(e)`.
2. `p(describe(item, rw))`. `a = ask("Update %s? [y/N/q] " % item.name).strip().lower()`. `"q"` → return `"quit"`. Not `"y"` → return `"skipped"`.
3. `backup = write_rewrite(rw, dry_run) if rw else None`.
4. Build `pull = compose_cmd(item, "pull", item.service)` and `up = compose_cmd(item, "up", "-d", "--no-deps", item.service)`. In dry run, print `"would run (in " + workdir + "): " + " ".join(argv)` for both and return `"updated"` (skip steps 5-8).
5. `rc = run(pull, item.workdir)`. If non-zero: `restore(rw, backup)` when `rw`, and return `"failed: pull exit %d" % rc`.
6. `ref = image_name(item.ref, item.tag) + ":" + (item.new_tag or item.tag)`. If `not digest_matches(digests(ref), item.expected)`:
   - print `"pulled %s, but you approved %s (a newer build landed since)" % (", ".join(short(d.split("@")[-1]) for d in digests(ref)) or "nothing", short(item.expected))`
   - if `ask("Recreate anyway? [y/N] ").strip().lower() != "y"`: restore (when `rw`), and return `"skipped: digest changed"`.
7. `rc = run(up, item.workdir)`. If non-zero, return `"failed: up exit %d" % rc + (" (compose backup: %s)" % backup if backup else "")`.
8. Print `item.name + ": " + state(item.name)` and return `"updated"`.

**`main` behaviour:**
- `argparse` options: `--list`, `--dry-run`, and `--only` (a comma-separated string).
- `setup_watch()`. Print `"Checking %d containers…" % len(watch.watched())` with `flush=True`, then `items = collect()`.
- `--list` → `print_table(items)` and return 0.
- `--only a,b`:
  - Unknown names → print `"not watched: x"` and return 2.
  - A named item with an error → print `"x: <error>"` and return 2.
  - Otherwise `chosen = [those items]` in the order given, with `selected = True`.
  - Skips the checklist and works when stdin isn't a TTY.
- Otherwise, if `not sys.stdin.isatty()`: `print_table(items)`, print `"not a terminal: use --list, or --only NAME"`, and return 2.
- Otherwise `chosen = run_checklist(items)`. None → print `"No changes."` and return 0. `[]` → print `"Nothing selected."` and return 0.
- `results = run_selected(chosen, input, dry_run=args.dry_run)`.
- Print a summary line per result: `"%-26s %s" % (name, result)`.
- Return 1 if any result starts with `"failed"`, else 0.
- Guard: `if __name__ == "__main__": try: sys.exit(main()) except KeyboardInterrupt: print("\nstopped"); sys.exit(130)`.

- [ ] **Step 1: Write the failing tests** in `tests/test_apply_flow.py`:

```python
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
        self.assertEqual(self.go(self.release_item(), ["y"], dry_run=True), "updated")
        self.assertEqual(self.calls, []); self.assertEqual(open(self.f).read(), COMPOSE)
        self.assertEqual(os.listdir(self.d.name), ["docker-compose.yml"])

    def test_forced_digest_pull(self):
        i = Item(name="image-watch-socket-proxy", service="socket-proxy", workdir=self.d.name, config_files=[self.f],
                 ref="tecnativa/docker-socket-proxy:v0.5.0", tag="v0.5.0", running="sha256:aaa", latest="sha256:aaa",
                 expected="sha256:aaa", selected=True)
        r = self.go(i, ["y"], digests=("tecnativa/docker-socket-proxy@sha256:aaa",))
        self.assertEqual(r, "updated"); self.assertEqual(self.calls, ["socket-proxy", "up"])
        self.assertEqual(open(self.f).read(), COMPOSE)

    def test_run_selected_quit_skips_rest(self):
        a, b = self.release_item(), self.release_item(); b.name = "second"
        answers = ["q"]
        res = apply.run_selected([a, b], lambda p: answers.pop(0), run=self.run_,
                                 digests=lambda r: [], state=lambda n: "")
        self.assertEqual(res, [("node-exporter", "quit"), ("second", "skipped")])

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
```

- [ ] **Step 2: Run and confirm the tests fail** (`ModuleNotFoundError: No module named 'apply'`).

- [ ] **Step 3: Implement `apply.py`** per the interfaces and steps above, then `chmod +x apply.py`.

- [ ] **Step 4: Run all tests and confirm they pass**: `python3 -m unittest discover -s tests -v` → `OK`.

- [ ] **Step 5: Read-only CLI checks against real Docker**

Run: `cd ~/image-watch && ./apply.py --list; echo "exit $?"`
Expected: `Checking 3 containers…`, then three table rows, then `exit 0`.

Run: `./apply.py < /dev/null; echo "exit $?"`
Expected: the table, then `not a terminal: use --list, or --only NAME`, then `exit 2`.

Run: `./apply.py --only nosuch; echo "exit $?"`
Expected: `not watched: nosuch`, then `exit 2`.

Run: `printf 'n\n' | ./apply.py --only node-exporter; echo "exit $?"`
Expected: the describe block for node-exporter, the prompt, `node-exporter … skipped`, then `exit 0`. Nothing pulled.

- [ ] **Step 6: Commit** `apply.py tests/test_apply_flow.py` with message `apply.py: per-container confirm, pull with digest check, recreate; --list/--dry-run/--only` (plus the attribution lines).

---

### Task 6: Docs, then end-to-end checks on sad2

**Files:**
- Modify: `README.md` (new section after "Commands"; Security; Files table)
- Modify: `.gitignore`

- [ ] **Step 1: `.gitignore`**: add a line `*.bak-*` (release rewrites of image-watch's own `docker-compose.yml` leave a backup in the repo).

- [ ] **Step 2: README.** Add a section `## Applying updates` after `## Commands`. Write it in the README's existing style (short sentences, `sh` code blocks), covering:
  - `./apply.py` runs on the host, not in the container. It needs Python 3 and membership in the `docker` group.
  - What the checklist shows, the keys, and that announced updates come pre-ticked while "not announced" ones don't.
  - The y/N per container, and that the default is No.
  - Release updates rewrite the compose file's image tag and keep a `.bak-<time>` copy.
  - The pull is checked against the digest you approved before anything is recreated.
  - `--list`, `--dry-run`, `--only NAME`.

  In **Security**, add a bullet: `apply.py` is the only part that changes anything. It runs on the host with your own Docker access, only when you start it, and acts only on containers you confirm one by one. The container side stays read-only.

  In the **Files** table, add these rows:
  - `apply.py`, `apply_status.py`, `apply_compose.py`, `apply_ui.py` | the update tool (runs on the host)
  - `tests/` | `python3 -m unittest discover -s tests`

- [ ] **Step 3: Commit** `README.md .gitignore` with message `README: applying updates with apply.py` (plus the attribution lines).

- [ ] **Step 4: End-to-end release rewrite on a throwaway container (not production)**

```bash
curl -s 'https://api.github.com/repos/prometheus/node_exporter/releases?per_page=5' | grep '"tag_name"'
```
Take the **second-newest** tag (OLD) from that list. Then create `~/iw-apply-test/docker-compose.yml`:

```yaml
services:
  probe:
    image: prom/node-exporter:OLD   # replace OLD with the tag
    container_name: iw-apply-test
    labels:
      image-watch.enable: "true"
      image-watch.releases: "true"
      image-watch.github: "prometheus/node_exporter"
```

Run:
```bash
cd ~/iw-apply-test && docker compose up -d && cd ~/image-watch && ./apply.py --list
```
Expected: an `iw-apply-test` row reading `release OLD → <newest> (tag rewrite) · not announced: no summary, PRs not checked`, with `[ ]` (not pre-ticked).

Run: `printf 'y\n' | ./apply.py --only iw-apply-test; echo "exit $?"`
Expected:
- the describe block with the diff
- the pull output
- `iw-apply-test: running sha256:…`
- `iw-apply-test … updated`
- `exit 0`

Then check:
```bash
docker inspect iw-apply-test --format '{{.Config.Image}}'   # → prom/node-exporter:<newest>
ls ~/iw-apply-test/                                          # → docker-compose.yml and docker-compose.yml.bak-…
./apply.py --list | grep iw-apply-test                       # → up to date
```

Clean up:
```bash
cd ~/iw-apply-test && docker compose down && cd ~ && rm -r ~/iw-apply-test
docker image rm prom/node-exporter:OLD   # only the OLD tag; never the tag node-exporter runs
```

If the hourly watcher ran during the test, Discord may have a "New release … for iw-apply-test" notice. That's expected; mention it to the user.

- [ ] **Step 5: Hand the interactive checks to the user.** Ask them to run these in the session (the `!` prefix runs in their terminal), and report what they see:
  1. `! cd ~/image-watch && ./apply.py --dry-run`
     - The curses checklist appears with three rows.
     - Arrows, space, `a` and `n` behave.
     - The bottom panel shows the highlighted row's details.
     - `q` prints `No changes.`
     - Run it again, tick `image-watch-socket-proxy`, press Enter and answer `y`: `would run …` lines, nothing changed.
  2. **Only with the user's OK:** `! ./apply.py --only image-watch-socket-proxy`, answer `y`. This force-pulls and recreates the proxy, which is low risk; the watcher reconnects on its next check. Expected: `updated`, and `docker ps` shows the proxy running.

  Do not run anything against `trunk-pro`.

- [ ] **Step 6: Final full test run and status**

Run: `python3 -m unittest discover -s tests -v && git status --short && git log --oneline -8`
Expected: all tests `OK`, a clean tree, and the six task commits on top of `22b8fee`.
