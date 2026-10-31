#!/usr/bin/env python3
"""Pull and recreate the containers image-watch says are out of date. Runs on the host, asks per container."""
import argparse
import sys

import watch
from apply_compose import (RewriteError, compose_cmd, container_state, digest_matches, image_name, plan_rewrite,
                           pulled_digests, restore, run_cmd, write_rewrite)
from apply_status import collect, setup_watch, short, status_text
from apply_ui import detail_lines, run_checklist


def describe(item, rw):
    lines = ["── %s (%s)" % (item.name, item.service)]
    if item.kind == "release" and rw:
        lines.append("release %s → %s: rewrite %s, pull + recreate" % (item.tag, item.new_tag, rw.path))
    else:
        lines.append("pull + recreate: %s → %s" % (short(item.running), short(item.expected)))
    n = item.notice
    if n:
        lines += list(n.get("flags") or [])
        if n.get("at"):
            lines.append("announced " + n["at"])
        if n.get("url"):
            lines.append(n["url"])
        if n.get("summary"):
            lines.append(n["summary"])
    elif item.kind:
        lines += detail_lines(item)
    else:
        lines.append("forced: no update pending")
    if rw:
        lines.append(rw.diff.rstrip("\n"))
    return "\n".join(x for x in lines if x)


def apply_item(item, ask, run=run_cmd, digests=pulled_digests, state=container_state, dry_run=False):
    p = print
    rw = None
    if item.kind == "release":
        try:
            rw = plan_rewrite(item.config_files, image_name(item.ref, item.tag), item.tag, item.new_tag)
        except RewriteError as e:
            p(str(e))
            return "skipped: " + str(e)
    p(describe(item, rw))
    a = ask("Update %s? [y/N/q] " % item.name).strip().lower()
    if a == "q":
        return "quit"
    if a != "y":
        return "skipped"
    backup = write_rewrite(rw, dry_run) if rw else None
    pull = compose_cmd(item, "pull", item.service)
    up = compose_cmd(item, "up", "-d", "--no-deps", item.service)
    if dry_run:
        for argv in (pull, up):
            p("would run (in %s): %s" % (item.workdir, " ".join(argv)))
        return "updated"
    rc = run(pull, item.workdir)
    if rc:
        if rw:
            restore(rw, backup)
        return "failed: pull exit %d" % rc
    ref = image_name(item.ref, item.tag) + ":" + (item.new_tag or item.tag)
    if item.kind is None and digest_matches(digests(ref), item.expected):
        p(item.name + ": already up to date, nothing to recreate")
        return "up to date"
    if not digest_matches(digests(ref), item.expected):
        p("pulled %s, but you approved %s (a newer build landed since)" % (
            ", ".join(short(d.split("@")[-1]) for d in digests(ref)) or "nothing", short(item.expected)))
        if ask("Recreate anyway? [y/N] ").strip().lower() != "y":
            if rw:
                restore(rw, backup)
            return "skipped: digest changed"
    rc = run(up, item.workdir)
    if rc:
        return "failed: up exit %d" % rc + (" (compose backup: %s)" % backup if backup else "")
    p(item.name + ": " + state(item.name))
    return "updated"


def run_selected(items, ask, **kw):
    results, quit_ = [], False
    for item in items:
        if quit_:
            results.append((item.name, "skipped"))
            continue
        r = apply_item(item, ask, **kw)
        quit_ = r == "quit"
        results.append((item.name, r))
    return results


def print_table(items):
    for i in items:
        print(("[x] " if i.selected else "[ ] ") + i.name.ljust(26) + " " + status_text(i))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Pull and recreate containers with pending updates.")
    ap.add_argument("--list", action="store_true", help="show the status table and exit")
    ap.add_argument("--dry-run", action="store_true", help="show what would run, change nothing")
    ap.add_argument("--only", help="comma-separated container names; skips the checklist, still asks y/N")
    args = ap.parse_args(argv)
    setup_watch()
    print("Checking %d containers…" % len(watch.watched()), flush=True)
    items = collect()
    if args.list:
        print_table(items)
        return 0
    if args.only:
        by_name = {i.name: i for i in items}
        chosen = []
        for name in [n.strip() for n in args.only.split(",") if n.strip()]:
            if name not in by_name:
                print("not watched: " + name)
                return 2
            if by_name[name].error:
                print("%s: %s" % (name, by_name[name].error))
                return 2
            by_name[name].selected = True
            chosen.append(by_name[name])
    elif not sys.stdin.isatty():
        print_table(items)
        print("not a terminal: use --list, or --only NAME")
        return 2
    else:
        chosen = run_checklist(items)
        if chosen is None:
            print("No changes.")
            return 0
        if not chosen:
            print("Nothing selected.")
            return 0
    results = run_selected(chosen, input, dry_run=args.dry_run)
    for name, r in results:
        print("%-26s %s" % (name, r))
    return 1 if any(r.startswith("failed") for _, r in results) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nstopped")
        sys.exit(130)
