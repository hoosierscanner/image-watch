"""Live update status for the containers watch.py watches (host side, read-only)."""
import concurrent.futures
import http.client
import json
import socket
from dataclasses import dataclass, field
from pathlib import Path

import watch

ROOT = Path(__file__).resolve().parent


@dataclass
class Item:
    name: str
    project: str | None = None
    service: str | None = None
    workdir: str | None = None
    config_files: list[str] = field(default_factory=list)
    ref: str = ""
    tag: str = ""
    running: str | None = None
    latest: str | None = None
    new_tag: str | None = None
    expected: str | None = None
    kind: str | None = None
    announced: bool = False
    notice: dict | None = None
    error: str | None = None
    selected: bool = False


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


def setup_watch(root=ROOT):
    """Point watch.py at the host's files and a read-only Docker connection."""
    watch.CONFIG = str(root / "watch.json")
    watch.STATE = str(root / "state" / "state.json")
    watch.SECRETS = str(root / "secrets")
    watch.docker = docker_get


def load_state(path):
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return {}


def classify(item, entry):
    if item.error:
        item.kind, item.selected = None, False
        return
    if item.running is None:
        item.error, item.selected = "running image has no registry digest", False
        return
    if item.new_tag:
        item.kind = "release"
        item.announced = entry.get("release") == item.new_tag
        n = entry.get("release_notice")
        item.notice = n if n and n.get("tag") == item.new_tag else None
    elif item.latest != item.running:
        item.kind = "digest"
        item.announced = entry.get("digest") == item.latest
        n = entry.get("digest_notice")
        item.notice = n if n and n.get("digest") == item.latest else None
    else:
        item.kind = None
    item.selected = item.kind is not None and item.announced


def check_container(name, c, opts, state):
    item = Item(name=name)
    labels = c.get("Labels") or {}
    item.project = labels.get("com.docker.compose.project")
    item.service = labels.get("com.docker.compose.service")
    item.workdir = labels.get("com.docker.compose.project.working_dir")
    files = labels.get("com.docker.compose.project.config_files")
    item.config_files = files.split(",") if files else []
    if not (item.workdir and item.service and item.config_files):
        item.error = "not a compose container"
        classify(item, {})
        return item
    try:
        img = watch.docker("/images/%s/json" % c["ImageID"])
        item.ref = watch.image_ref(c, img)
        registry, repo, item.tag = watch.parse_ref(item.ref)
        item.running = watch.running_digest(img, repo)
        reg = watch.Registry(registry, repo)
        item.latest = reg.digest(item.tag)
        if opts.get("releases"):
            src = watch.source_repo(opts, img.get("Config", {}).get("Labels") or {})
            if src:
                rel = watch.github("/repos/%s/releases/latest" % src)
                if rel and rel.get("tag_name") and watch.is_newer(item.tag, rel["tag_name"]):
                    item.new_tag = rel["tag_name"]
        item.expected = reg.digest(item.new_tag) if item.new_tag else item.latest
        classify(item, state.get(name, {}))
    except Exception as e:
        item.error = "check failed: " + str(e)[:100]
        classify(item, {})
    return item


def collect():
    state = load_state(watch.STATE)
    pairs = watch.watched()
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        items = list(ex.map(lambda kv: check_container(kv[0], kv[1][0], kv[1][1], state), pairs.items()))
    return sorted(items, key=lambda i: i.name)


def short(digest):
    return "?" if digest is None else digest.split(":", 1)[-1][:12]


def status_text(item):
    if item.error:
        return item.error
    if item.kind is None:
        return "up to date"
    if item.kind == "digest":
        text = "new build " + short(item.running) + " → " + short(item.latest)
    else:
        text = "release " + item.tag + " → " + item.new_tag + " (tag rewrite)"
    if item.notice and item.notice.get("flags"):
        text += "  ⚠"
    if not item.announced:
        text += " · not announced: no summary, PRs not checked"
    return text
