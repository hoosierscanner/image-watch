# apply.py: pick and apply pending container updates

Date: 2026-10-31 · Status: approved approach (B), awaiting spec review

## Goal

`watch.py` tells Discord when a watched container has a new image build or a new
GitHub release, and never changes anything. `apply.py` is the other half: run it on
the host, get a checklist of watched containers with pending updates ticked, pick,
confirm each one, and it pulls and recreates them with Docker Compose.

Success: on sad2, `./apply.py` shows trunk-pro, node-exporter and
image-watch-socket-proxy with correct live status, and updating a ticked container
leaves it running the image whose summary you approved, or stops and asks if the
registry moved on in between.

## Decisions (from brainstorming)

| Question | Decision |
|---|---|
| Where it runs | On the host as the user (docker group), not in the image-watch container. The watcher keeps its read-only socket proxy. |
| What it lists | Every watched container (same rules as `watch.py`: watch.json or label `image-watch.enable=true`). Pending, announced updates are pre-ticked. Up-to-date rows can be ticked to force a pull and recreate. |
| Source of truth for "pending" | A live check on each run (approach B): registry digest of the running tag, and GitHub's latest release when `releases` is on. |
| Summaries | `watch.py` saves what it posted (summary, warnings, compare URL) in `state.json`. `apply.py` shows it when the live result matches what was announced. |
| Newer than announced | A live digest or release the watcher hasn't announced is shown as "not announced: no summary, PRs not checked" and is **not** pre-ticked. |
| Release updates | Applied by rewriting the image tag in the compose file (backup + diff first), then pull + recreate. |
| Confirmation | After the checklist, a y/N prompt per selected container (default No), showing its summary, warnings, compare URL and any diff. |
| Interface | curses checklist (stdlib). Per-container prompts in plain terminal after curses exits. |
| Dependencies | Python standard library only (host has Python 3.14). Tests use `unittest`. |

## Architecture

```
            Discord ◄── watch.py (container, read-only proxy) ──► state/state.json
                                                                    │  (watcher is the only writer)
                                                                    ▼ read
 you ──► apply.py (host) ── imports watch.py helpers (parse_ref, Registry, github, semver, watched)
            │  reads Docker:  /var/run/docker.sock  (GET only, same API paths watch.py uses)
            │  writes Docker: `docker compose pull` / `up -d --no-deps <service>` (CLI)
            └─ writes files:  compose file image tag (release updates only) + timestamped .bak
```

- `apply.py` never writes `state.json`. "Pending" clears itself: after a pull the
  running digest equals the registry's; after a tag rewrite the running tag equals
  the latest release.
- `apply.py` imports `watch` and points its module globals at host paths
  (`watch.CONFIG`, `watch.STATE`, `watch.SECRETS`) and replaces `watch.docker` with a
  Unix-socket GET client, so `watch.watched()` and image lookups work unchanged.
- Each container's compose location comes from its labels:
  `com.docker.compose.project.working_dir`, `com.docker.compose.project.config_files`
  (comma-separated), `com.docker.compose.service`. A container without them is listed
  but can't be selected ("not a compose container").

## Components

### 1. watch.py changes (small)

- Extract two helpers from `check_one` so both tools share them:
  `image_ref(c, img)` (the ref string) and `running_digest(img, repo)`.
- After a successful Discord post, also save the notice:
  - `state[name]["digest_notice"] = {"digest", "summary", "flags", "url", "at"}`
  - `state[name]["release_notice"] = {"tag", "summary", "url", "at"}`
  - `summary` is the posted description (AI summary or commit list); `at` is local time
    `YYYY-MM-DD HH:MM`. Existing keys `digest` and `release` are unchanged.

### 2. apply.py: discovery and live status

For each watched container, build an `Item`:

- `name, project, service, workdir, config_files, ref, tag, running`
- `kind`: `"release"` if a newer release exists, else `"digest"` if the registry
  digest differs from `running`, else `None` (up to date). A release supersedes a
  digest update (pulling the new tag gets the newest build of it).
- `latest` (registry digest of `tag`), `new_tag` and `expected` (the digest that the
  pull should produce: `latest` for digest/force, registry digest of `new_tag` for a release).
- `announced` (state's `digest`/`release` equals the live value) and `notice` (the
  saved notice when its `digest`/`tag` matches, else `None`).
- `selected` default: `kind is not None and announced`.
- `error`: a failed check or missing compose labels makes the row unselectable.

Checks run in parallel (thread pool), with "Checking N containers…" printed first.

### 3. apply.py: compose tag rewrite (release updates)

- Search each file in `config_files` for lines whose value is exactly
  `<image-without-tag>:<old_tag>` after `image:` (optional quotes, optional trailing
  comment). Exactly one match across all files is required; zero (e.g. the tag comes
  from `${VAR}`) or several → refuse with "edit the tag by hand".
- Show a unified diff in the confirm step. On yes: copy the file to
  `<file>.bak-YYYYmmdd-HHMMSS`, then write the new text (temp file + `os.replace`,
  keeping the file mode).
- If the pull fails, or you decline after a digest mismatch, restore from the backup.

### 4. apply.py: checklist (curses)

- A `Checklist` model (rows, cursor, toggle, all/none) with no curses code, so it
  can be tested; a thin `curses.wrapper` renderer.
- Keys: ↑/↓ or k/j move, space toggles, `a` ticks all pending, `n` clears all,
  Enter continues, `q` quits without changes.
- A row: `[x] name  status`. Status examples: `new build 525e9f1e1e61 → d9b324e2dd1e`,
  `release v1.12.0 → v1.12.1 (tag rewrite)`, `up to date`,
  `not announced: no summary, PRs not checked`, `check failed: …`. A ⚠ marks rows with
  flags. Below the list, the highlighted row's first summary line and its flags.
  Lines are cut to the terminal width.
- Not a TTY → print the table (`--list` format) and exit 2.

### 5. apply.py: per-container confirm and apply

For each selected item, in list order:

1. Print name, action, compare URL, flags in full, summary (or the "not announced"
   note), and the diff for a release.
2. `Update <name>? [y/N/q]`. Anything but `y` skips; `q` skips all remaining.
3. Release: write the tag (with backup).
4. `docker compose -f F1 [-f F2…] pull <service>` with `cwd=workdir` (so `.env`
   interpolation still works), output streamed to the terminal.
5. Verify: the pulled image's `RepoDigests` for the new ref must contain `expected`.
   If not: "pulled X, you approved Y (a newer build landed since)". Ask
   `Recreate anyway? [y/N]`; on no, restore any backup and skip.
6. `docker compose -f … up -d --no-deps <service>`.
7. Inspect the container: report running/not running and the new image ID.

At the end: a summary of updated, skipped and failed items. Exit 0 unless one failed.

### 6. CLI

```
./apply.py             interactive checklist, then per-container prompts
./apply.py --list      print the status table and exit (no prompts, no changes)
./apply.py --dry-run   full interactive flow, but print the file write and docker
                       commands instead of running them
./apply.py --only a,b  skip the checklist and select these containers (they must be
                       selectable); the per-container y/N prompts still run. Works with
                       piped stdin, so the flow can be tested without a terminal.
```

## Error handling

| Case | Behaviour |
|---|---|
| Registry or GitHub call fails for a container | Row shows `check failed: <msg>`, unselectable; other rows unaffected. |
| No compose labels | Row shows `not a compose container`, unselectable. |
| Running image has no registry digest (built locally) | Row shows `running image has no registry digest`, unselectable. |
| New release on GitHub but its tag isn't in the registry yet | Row shows `check failed: registry …: 404` for the new tag, unselectable, until the image is pushed. |
| `state.json` missing or unreadable | Treated as empty: nothing counts as announced. |
| Tag rewrite can't find exactly one line | That item is skipped with the reason; no file touched. |
| Pull fails | Restore backup (release), report, continue with next item. |
| Digest mismatch after pull | Ask; on no, restore backup and skip. |
| `up -d` fails | Report the error and the backup path (if any); continue. |
| Ctrl-C | Exit with "stopped"; an in-flight docker command finishes or dies with it. Nothing else to clean up. |

## Security

- The trust boundary is the host user. `apply.py` has the user's full Docker access
  and writes compose files, so it only runs when you start it, never on a schedule,
  and never acts without a per-container `y`.
- The container side is unchanged: the watcher still sees Docker only through the
  read-only proxy and writes only `state/`.
- `state.json` content (summaries from GitHub commits via an AI) is shown as text only,
  never executed. Image refs and service names go to `docker compose` as separate
  argv items (no shell).
- README's Security section gets a paragraph on this.

## Testing

- `python3 -m unittest discover -s tests -v`, no third-party packages.
- Unit: notice saving in `check_one`/`check_release` (mocked registry, GitHub,
  Discord); status classification; tag rewrite (quotes, comments, `${VAR}`, 0/2
  matches); `Checklist` model; compose argv building; digest verification.
- Manual on sad2: `--list` against the real containers; `--dry-run`; a forced
  recreate of `image-watch-socket-proxy`; a release rewrite on a throwaway compose
  project in the scratchpad (not production).

## Out of scope

Scheduling or unattended updates, Discord messages from `apply.py`, rollback of an
`up -d`, updating containers not on the watch list, pruning old images.
