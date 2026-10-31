"""Compose-side actions for apply.py: tag rewrite, compose commands, digest checks."""
import difflib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass


class RewriteError(Exception):
    pass


@dataclass
class Rewrite:
    path: str
    old_text: str
    new_text: str
    diff: str


def image_name(ref, tag):
    suffix = ":" + tag
    return ref[:-len(suffix)] if ref.endswith(suffix) else ref


def plan_rewrite(files, image, old_tag, new_tag):
    for path in files:
        if not os.path.exists(path):
            raise RewriteError("compose file not found: " + path)
    old_ref = image + ":" + old_tag
    pattern = re.compile(r"^(\s*image:\s*)([\"']?)" + re.escape(old_ref) + r"(\2)(\s*(?:#.*)?)$", re.MULTILINE)
    found = []
    for path in files:
        text = open(path).read()
        found += [(path, text, m) for m in pattern.finditer(text)]
    if not found:
        raise RewriteError("no line 'image: %s' in %s — edit the tag by hand" % (old_ref, ", ".join(files)))
    if len(found) > 1:
        raise RewriteError("%d lines use %s — edit the tag by hand" % (len(found), old_ref))
    path, old, m = found[0]
    new = old[:m.start()] + m.group(1) + m.group(2) + image + ":" + new_tag + m.group(3) + m.group(4) + old[m.end():]
    diff = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), path, path))
    return Rewrite(path, old, new, diff)


def write_rewrite(rw, dry_run=False):
    backup = rw.path + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
    if dry_run:
        print("would write %s (backup %s)" % (rw.path, backup))
        return None
    shutil.copy2(rw.path, backup)
    tmp = rw.path + ".tmp"
    with open(tmp, "w") as f:
        f.write(rw.new_text)
    os.chmod(tmp, os.stat(rw.path).st_mode & 0o7777)
    os.replace(tmp, rw.path)
    return backup


def restore(rw, backup):
    if backup:
        shutil.copy2(backup, rw.path)


def compose_cmd(item, *args):
    argv = ["docker", "compose"]
    for f in item.config_files:
        argv += ["-f", f]
    return argv + list(args)


def digest_matches(repo_digests, expected):
    return any(d.split("@")[-1] == expected for d in repo_digests)


def _out(argv):
    r = subprocess.run(argv, capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def pulled_digests(ref):
    out = _out(["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", ref])
    try:
        return json.loads(out) if out else []
    except ValueError:
        return []


def container_state(name):
    return _out(["docker", "inspect", "--format", "{{.State.Status}} {{.Image}}", name]) or "unknown"


def run_cmd(argv, cwd):
    return subprocess.run(argv, cwd=cwd).returncode
