"""image-watch: tell Discord when a watched container's image has a new build.

Like Watchtower in monitor-only mode, without pulling or restarting anything.
Every INTERVAL seconds, for each watched container:

  1. its running image's digest, from Docker (through a read-only socket proxy);
  2. the registry's current digest for the same tag;
  3. if they differ and this build wasn't announced yet: the new build's
     commit (OCI label org.opencontainers.image.revision), the commits since
     the running one from GitHub, a short summary from the AI provider in .env
     (install.sh sets it up), and one Discord message.

A container is watched when it has the label image-watch.enable=true or is
listed in watch.json. Per container (labels "image-watch.<key>" or keys in
watch.json): github (owner/repo, default: the image's source label),
flag_prs ({"104": "call store"}: say so when one of those PRs is in the update),
and releases (true: for a container pinned to an exact version tag, also
check GitHub's latest release against that tag -- the digest check above
only catches a new build of the SAME tag, never a new version under a
different one).

  python3 watch.py           run forever
  python3 watch.py --once    check once and exit
  python3 watch.py --test    send a test message to Discord (with an AI check) and exit
"""
import json, os, re, sys, time, urllib.error, urllib.parse, urllib.request

DOCKER = os.environ.get("DOCKER_URL", "http://socket-proxy:2375")
INTERVAL = int(os.environ.get("INTERVAL", "3600"))
CONFIG = os.environ.get("CONFIG", "/config/watch.json")
STATE = os.environ.get("STATE", "/state/state.json")
SECRETS = os.environ.get("SECRETS", "/secrets")
AI = os.environ.get("AI_PROVIDER", "none")   # cloudflare | openai | claude-api | claude-code | none
MODEL = os.environ.get("AI_MODEL", "")
INDEX_TYPES = ", ".join([
    "application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json"])


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def secret(name):
    try:
        return open(os.path.join(SECRETS, name)).read().strip()
    except OSError:
        return ""


def http(url, data=None, headers=None, method=None, timeout=30):
    """Return (status, headers, body bytes); HTTP errors are returned, not raised."""
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def docker(path):
    status, _, body = http(DOCKER + path)
    if status != 200:
        raise RuntimeError("docker %s: %d" % (path, status))
    return json.loads(body)


# ---- registry

def parse_ref(ref):
    """'robotastic/x:edge' -> ('registry-1.docker.io', 'robotastic/x', 'edge')."""
    ref = ref.split("@")[0]
    name, _, tag = ref.rpartition(":") if ":" in ref.split("/")[-1] else (ref, "", "latest")
    first = name.split("/")[0]
    if "." in first or ":" in first or first == "localhost":
        registry, repo = first, name.split("/", 1)[1]
    else:
        registry, repo = "registry-1.docker.io", name if "/" in name else "library/" + name
    return registry, repo, tag or "latest"


class Registry:
    def __init__(self, registry, repo):
        self.base, self.repo, self.token = "https://%s/v2/%s" % (registry, repo), repo, None

    def get(self, path, accept, method="GET"):
        for _ in range(2):
            h = {"Accept": accept}
            if self.token:
                h["Authorization"] = "Bearer " + self.token
            status, headers, body = http(self.base + path, headers=h, method=method)
            if status != 401:
                return status, headers, body
            m = dict(re.findall(r'(\w+)="([^"]*)"', headers.get("WWW-Authenticate", "")))
            q = urllib.parse.urlencode({k: m[k] for k in ("service", "scope") if k in m})
            _, _, tok = http(m["realm"] + "?" + q)
            self.token = json.loads(tok).get("token") or json.loads(tok).get("access_token")
        return status, headers, body

    def digest(self, tag):
        status, headers, _ = self.get("/manifests/" + tag, INDEX_TYPES, method="HEAD")
        if status != 200:
            raise RuntimeError("registry %s:%s: %d" % (self.repo, tag, status))
        return headers["Docker-Content-Digest"]

    def labels(self, digest, arch="amd64"):
        """The image config's labels for linux/<arch> under an index (or a single manifest)."""
        _, _, body = self.get("/manifests/" + digest, INDEX_TYPES)
        m = json.loads(body)
        if "manifests" in m:
            pick = [x for x in m["manifests"] if x.get("platform", {}).get("architecture") == arch
                    and x.get("platform", {}).get("os") == "linux"]
            _, _, body = self.get("/manifests/" + pick[0]["digest"], INDEX_TYPES)
            m = json.loads(body)
        _, _, cfg = self.get("/blobs/" + m["config"]["digest"], "*/*")
        return json.loads(cfg).get("config", {}).get("Labels") or {}


# ---- GitHub

def github(path):
    h = {"Accept": "application/vnd.github+json"}
    if secret("github-token"):
        h["Authorization"] = "Bearer " + secret("github-token")
    status, _, body = http("https://api.github.com" + path, headers=h)
    return json.loads(body) if status == 200 else None


def semver(tag):
    """'v1.9.1' -> (1, 9, 1); None if tag doesn't start with a version number."""
    m = re.match(r"^[vV]?(\d+)(?:\.(\d+))?(?:\.(\d+))?", tag)
    if not m:
        return None
    return tuple(int(g) if g else 0 for g in m.groups())


def changes(repo, old, new):
    """Commits old..new as (sha, headline), newest last, and the compare URL."""
    c = github("/repos/%s/compare/%s...%s" % (repo, old, new)) if old and new else None
    if not c:
        return [], None
    return [(x["sha"], x["commit"]["message"].split("\n")[0]) for x in c.get("commits", [])], c.get("html_url")


PROMPT = ("These are the commit messages in a new build of %s, a self-hosted service the reader runs "
          "at home. In at most 6 short bullet points, say what changed that the reader would notice or "
          "should act on. Plain language, no preamble.\n\n%s")


def ask_ai(prompt):
    """One prompt in, the model's text out, from the provider install.sh chose (AI_PROVIDER)."""
    key = secret("ai-key")
    if AI == "cloudflare":
        status, _, out = http("https://api.cloudflare.com/client/v4/accounts/%s/ai/run/%s" % (os.environ["CF_ACCOUNT_ID"], MODEL),
                              data=json.dumps({"messages": [{"role": "user", "content": prompt}], "max_tokens": 600}).encode(),
                              headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"}, timeout=90)
        if status != 200:
            raise RuntimeError("HTTP %d %s" % (status, out[:200]))
        return json.loads(out)["result"]["response"].strip()
    if AI == "openai":
        status, _, out = http("https://api.openai.com/v1/chat/completions",
                              data=json.dumps({"model": MODEL, "messages": [{"role": "user", "content": prompt}],
                                               "max_completion_tokens": 2000}).encode(),
                              headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"}, timeout=90)
        if status != 200:
            raise RuntimeError("HTTP %d %s" % (status, out[:200]))
        return json.loads(out)["choices"][0]["message"]["content"].strip()
    if AI == "claude-api":
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        # On a policy decline the API re-runs the request on a fallback model (server-side fallbacks).
        r = client.beta.messages.create(model=MODEL, max_tokens=4000, output_config={"effort": "low"},
                                        betas=["server-side-fallback-2026-07-01"], fallbacks="default",
                                        messages=[{"role": "user", "content": prompt}])
        if r.stop_reason == "refusal":
            raise RuntimeError("declined: %s" % (r.stop_details.category if r.stop_details else "?"))
        return "".join(b.text for b in r.content if b.type == "text").strip()
    if AI == "claude-code":
        import subprocess
        r = subprocess.run(["claude", "-p", prompt, "--output-format", "text", "--model", MODEL],
                           capture_output=True, text=True, timeout=180,
                           env={**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": key, "HOME": "/tmp"})
        if r.returncode:
            raise RuntimeError(r.stderr.strip()[:200] or "exit %d" % r.returncode)
        return r.stdout.strip()
    return None


def summarize(name, commits):
    if AI in ("", "none") or not commits:
        return None
    try:
        return ask_ai(PROMPT % (name, "\n".join("- " + c for c in commits[-150:]))) or None
    except Exception as e:
        log("summary failed (%s):" % AI, str(e)[:300])
        return None


# ---- Discord

def discord(embed):
    url = secret("discord-webhook")
    if not url:
        log("no Discord webhook in", SECRETS)
        return False
    status, _, body = http(url, data=json.dumps({"username": "image-watch", "embeds": [embed]}).encode(),
                           headers={"Content-Type": "application/json", "User-Agent": "image-watch"})
    if status not in (200, 204):
        log("discord failed:", status, body[:200])
    return status in (200, 204)


# ---- the check

def watched():
    try:
        conf = json.load(open(CONFIG))
    except OSError:
        conf = {}
    out = {}
    for c in docker("/containers/json?all=1"):
        name, labels = c["Names"][0].lstrip("/"), c.get("Labels") or {}
        opts = dict(conf.get("containers", {}).get(name, {}))
        if labels.get("image-watch.enable") == "true":
            opts.update({k[len("image-watch."):]: v for k, v in labels.items() if k.startswith("image-watch.")})
        elif name not in conf.get("containers", {}):
            continue
        if isinstance(opts.get("flag_prs"), str):
            opts["flag_prs"] = json.loads(opts["flag_prs"])
        if isinstance(opts.get("releases"), str):
            opts["releases"] = opts["releases"].lower() == "true"
        out[name] = (c, opts)
    return out


def check_one(name, c, opts, state, img):
    ref = c["Image"] if not c["Image"].startswith("sha256:") else None
    ref = ref or (img.get("RepoTags") or [None])[0]
    registry, repo, tag = parse_ref(ref)
    running = next((d.split("@")[1] for d in img.get("RepoDigests", []) if d.split("@")[0].endswith(repo.split("library/")[-1])), None)
    reg = Registry(registry, repo)
    latest = reg.digest(tag)
    if latest == running or state.get(name, {}).get("digest") == latest:
        return False
    old_labels = img.get("Config", {}).get("Labels") or {}
    new_labels = reg.labels(latest)
    src = opts.get("github") or re.sub(r"^https://github.com/", "", new_labels.get("org.opencontainers.image.source", ""))
    old_rev, new_rev = old_labels.get("org.opencontainers.image.revision"), new_labels.get("org.opencontainers.image.revision")
    shas_commits, compare = changes(src, old_rev, new_rev) if src else ([], None)
    shas, commits = {s for s, _ in shas_commits}, [m for _, m in shas_commits]
    flags = []
    for pr, what in (opts.get("flag_prs") or {}).items():
        p = github("/repos/%s/pulls/%s" % (src, pr)) if src else None
        if p and p.get("merged_at") and p.get("merge_commit_sha") in shas:
            flags.append("⚠️ Includes PR #%s: %s" % (pr, what))
    summary = summarize(ref, commits)
    lines = commits[-15:]
    desc = (summary or "\n".join("• " + x for x in lines) or "No commit list available.")[:3500]
    fields = [{"name": "Running", "value": "`%s`" % (old_rev or running or "?")[:12], "inline": True},
              {"name": "New", "value": "`%s`" % (new_rev or latest)[:12], "inline": True},
              {"name": "Commits", "value": str(len(commits)), "inline": True}]
    if flags:
        fields.insert(0, {"name": "Heads up", "value": "\n".join(flags), "inline": False})
    embed = {"title": "New %s for %s" % (tag, name), "description": desc, "fields": fields,
             "color": 0xEB6834 if flags else 0x2A78D6,
             "footer": {"text": ref + (" · summary by %s %s" % (AI, MODEL) if summary else "")}}
    if compare:
        embed["url"] = compare
    if discord(embed):
        state.setdefault(name, {})["digest"] = latest
        log(name, "announced", latest[:19], len(commits), "commits", flags)
        return True
    return False


def check_release(name, img, opts, state):
    """For a container pinned to an exact version tag: is GitHub's latest release newer?"""
    ref = (img.get("RepoTags") or [None])[0]
    if not ref:
        return False
    _, _, tag = parse_ref(ref)
    labels = img.get("Config", {}).get("Labels") or {}
    src = opts.get("github") or re.sub(r"^https://github.com/", "", labels.get("org.opencontainers.image.source", ""))
    if not src:
        return False
    rel = github("/repos/%s/releases/latest" % src)
    new_tag = rel.get("tag_name") if rel else None
    if not new_tag:
        return False
    cur_v, new_v = semver(tag), semver(new_tag)
    if (new_v <= cur_v) if (cur_v is not None and new_v is not None) else (tag == new_tag):
        return False
    if state.get(name, {}).get("release") == new_tag:
        return False
    shas_commits, compare = changes(src, tag, new_tag)
    commits = [m for _, m in shas_commits]
    summary = summarize(ref, commits)
    desc = (summary or "\n".join("• " + x for x in commits[-15:]) or "No commit list available.")[:3500]
    fields = [{"name": "Running", "value": "`%s`" % tag, "inline": True},
              {"name": "New release", "value": "`%s`" % new_tag, "inline": True},
              {"name": "Commits", "value": str(len(commits)), "inline": True}]
    embed = {"title": "New release %s for %s" % (new_tag, name), "description": desc, "fields": fields,
             "color": 0x8E44AD,
             "footer": {"text": ref + (" · summary by %s %s" % (AI, MODEL) if summary else "")}}
    embed["url"] = compare or rel.get("html_url")
    if discord(embed):
        state.setdefault(name, {})["release"] = new_tag
        log(name, "new release", new_tag, len(commits), "commits")
        return True
    return False


def run_once():
    try:
        state = json.load(open(STATE))
    except (OSError, ValueError):
        state = {}
    for name, (c, opts) in watched().items():
        try:
            img = docker("/images/%s/json" % c["ImageID"])
            announced = check_one(name, c, opts, state, img)
            if opts.get("releases") and check_release(name, img, opts, state):
                announced = True
            if announced:
                with open(STATE + ".tmp", "w") as f:
                    json.dump(state, f, indent=1)
                os.replace(STATE + ".tmp", STATE)
            else:
                log(name, "up to date")
        except Exception as e:
            log(name, "check failed:", e)


if __name__ == "__main__":
    if "--test" in sys.argv:
        names = ", ".join(watched()) or "none"
        ai = "off"
        if AI not in ("", "none"):
            try:
                ai = "%s (%s) answered: %s" % (AI, MODEL, ask_ai("Reply with exactly: summaries are working")[:80])
            except Exception as e:
                ai = "%s (%s) FAILED: %s" % (AI, MODEL, str(e)[:200])
        print("AI:", ai)
        ok = discord({"title": "image-watch is connected", "color": 0x1BAF7A,
                      "description": "Watching: %s. Checks every %d minutes.\nAI summaries: %s" % (names, INTERVAL // 60, ai)})
        sys.exit(0 if ok and "FAILED" not in ai else 1)
    while True:
        run_once()
        if "--once" in sys.argv:
            break
        time.sleep(INTERVAL)
