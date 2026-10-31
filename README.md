# image-watch

A Discord message when a container you run has a new image build, with a short
AI summary of what changed. Like [Watchtower](https://github.com/containrrr/watchtower)
in monitor-only mode: the watcher never pulls or restarts anything. When you decide to update,
run `./apply.py` on the host (see [Applying updates](#applying-updates)): a checklist, one y/N per container.

Every hour, for each watched container, it:

1. reads the digest of the image the container runs (through a read-only Docker socket proxy);
2. asks the registry (Docker Hub, GHCR, …) for the current digest of the same tag;
3. if they differ and that build hasn't been announced yet, gets the commits between the
   two builds from GitHub, has an AI summarize them, and posts one Discord message with the
   summary, a link to the full comparison, and any warnings you asked for.

A notice looks like this:

> **New edge for trunk-pro** (links to the GitHub comparison)
>
> * EDACS support has been added, including configuration and decoding of EDACS systems.
> * The bladeRF radio is now supported as a native source.
> * Live gain adjustments are now possible for RTL-SDR, Airspy, and USRP/SoapySDR radios.
> * …
>
> **Running** `525e9f1e1e61` · **New** `d9b324e2dd1e` · **Commits** 188
> *robotastic/trunk-recorder-pro:edge · summary by cloudflare @cf/meta/llama-3.3-70b-instruct-fp8-fast*

The commits come from the images' standard OCI labels (`org.opencontainers.image.source` and
`org.opencontainers.image.revision`), which most images built with GitHub Actions carry.

## Install

Needs Linux (or anything running Docker Engine) with Docker Compose v2, and a Discord channel
you can add a webhook to. Then:

```sh
git clone https://github.com/hoosierscanner/image-watch.git
cd image-watch
./install.sh
```

The installer asks for:

1. **A Discord webhook**: Server Settings → Integrations → Webhooks → New Webhook.
2. **A GitHub token** (optional): only raises GitHub's rate limit (60 requests an hour without one).
3. **Who writes the summaries**, with short setup steps for each:

| Choice | Cost | Needs |
|---|---|---|
| Cloudflare Workers AI | free daily allowance | an API token from the "Workers AI" template, and your account ID |
| OpenAI API | pay as you go | an API key |
| Claude Code | your Claude Pro or Max plan | a token from `claude setup-token` |
| Claude API | pay as you go | an API key |
| None | free | nothing: the message lists the commits instead |

It then builds the container, sends a test message (which checks that the AI answers), and
starts the watcher. Run `./install.sh` again any time to change a setting; Enter keeps the
current value. Secrets are typed at hidden prompts and saved one per file in `secrets/`
(readable only by you); nothing secret is in `.env` or the compose file.

If a summary fails, the message is still sent, with the latest commit titles.

## Choosing containers

List them in `watch.json` (the installer makes one from `watch.json.example`):

```json
{
  "containers": {
    "my-container": {},
    "another-container": {
      "github": "owner/repo",
      "flag_prs": {"123": "a change to read about before updating"}
    }
  }
}
```

Or put a label on the container itself, Watchtower style:

```yaml
labels:
  image-watch.enable: "true"
  image-watch.flag_prs: '{"123": "a change to read about before updating"}'
```

### How it finds what changed

It watches **containers**, not repositories. For each one:

1. **Image and tag** come from what the container runs (say `robotastic/trunk-recorder-pro:edge`);
   that tag is what it checks the registry for.
2. **The repository** comes from the image's `org.opencontainers.image.source` label
   (`https://github.com/TrunkRecorder/trunk-recorder-pro`), unless you set `github` yourself.
3. **The commits** are the ones between the running image's and the new image's
   `org.opencontainers.image.revision` labels, the exact commit each was built from.

If an image has no such labels and you haven't set `github`, you still get the notice that a
new build exists, without the commit list or the summary. Only GitHub repositories are supported
for commits; images can come from any registry that speaks the standard v2 API.

### Pinned to a version tag? Set `releases: true`

The digest check above only ever asks the registry about the tag you're already running. If
that tag is a version like `v1.9.1`, a new release ships under a *different* tag (`v1.9.2`) that
the digest check never queries — so you'd see silence forever, not "no updates available". This
is invisible with a floating tag like `edge` or `latest`, where every new build lands under the
same tag you're watching.

Set `"releases": true` on a container pinned to an exact version tag, and image-watch also asks
GitHub for that repository's latest release and compares version numbers. When it's newer, you
get a notice in the same style (commits, AI summary, compare link) titled "New release". This is
independent of the digest check, so you may occasionally get both for the same cycle — that's
two different, both-true facts (a new build of your tag, and a newer tag exists), not a
duplicate.

Don't set it on a container tracking a floating tag: there's no version number to compare, so
image-watch falls back to "any different tag is new", which is noisy and not the point.

### Options

Per container:

- `github`: the repository to take commits from, when the image doesn't say (its source label).
- `flag_prs`: pull requests to watch for. When an update contains one of them (merged), the
  message turns orange and says so. Handy for a big change you want to prepare for.
- `releases`: also check GitHub's latest release against the running version tag (see above).
  Only makes sense for a container pinned to an exact version, not a floating tag.

## Commands

```sh
docker compose logs -f image-watch                                  # what it's doing
docker compose run --rm image-watch python3 /app/watch.py --once    # check now
docker compose run --rm image-watch python3 /app/watch.py --test    # test message
```

`INTERVAL` in `docker-compose.yml` sets how often it checks (seconds, default 3600).

## Applying updates

`./apply.py` pulls and recreates the containers that have an update. It runs on the host, not in
the container. It needs Python 3 and membership in the `docker` group.

```sh
./apply.py              # checklist, then y/N for each container you ticked
./apply.py --list       # just print the status table
./apply.py --dry-run    # go through the prompts, show what would run, change nothing
./apply.py --only NAME  # skip the checklist for one or more containers (comma-separated)
```

The checklist shows every watched container with its live status. Keys: arrows (or `j`/`k`) move,
space ticks, `a` ticks everything pending, `n` clears, Enter continues, `q` quits. Updates that
were already announced come pre-ticked. Ones marked "not announced" don't, because there is no
summary yet and the PR flags haven't been checked. The panel below the list shows the summary
and any PR warnings for the highlighted row.

Each ticked container gets its own y/N prompt, and the default is No. For a new release, apply.py
first rewrites the image tag in the compose file and keeps a `.bak-<time>` copy next to it. Then it
runs `docker compose pull` and `up -d --no-deps` for that service only. The pull is checked against
the digest you approved: if a newer build landed in between, it asks again before recreating.
If the pull fails, the compose file is put back.

## Security

- Docker is reached only through [docker-socket-proxy](https://github.com/Tecnativa/docker-socket-proxy),
  which allows listing containers and images and nothing else (no starting, stopping, pulling or
  exec). The proxy sits on an internal network that only image-watch can reach.
- image-watch runs as a normal user with a read-only filesystem; it writes only `state/`.
- `.env`, `secrets/`, `state/` and `watch.json` are in `.gitignore`.
- `apply.py` is the only part that changes anything. It runs on the host with your own Docker access, only
  when you start it, and acts only on containers you confirm one by one. The container side stays read-only.

## Files

| File | What |
|---|---|
| `watch.py` | the watcher (Python standard library, plus the Anthropic SDK for the Claude API) |
| `apply.py`, `apply_status.py`, `apply_compose.py`, `apply_ui.py` | the update tool (runs on the host) |
| `tests/` | `python3 -m unittest discover -s tests` |
| `install.sh` | interactive setup |
| `docker-compose.yml`, `Dockerfile` | the containers; the `claude-code` build adds the Claude Code CLI |
| `watch.json.example` | the watch list's format |
| `LICENSE` | MIT |

## Why not Watchtower?

Watchtower's repository was archived in December 2025, and its default Docker API version is
older than Docker 29 accepts. It also needs full access to the Docker socket to do its job of
pulling and restarting. image-watch only has to look, so it gets a read-only view, and leaves
updating to you.

## License

[MIT](LICENSE)
