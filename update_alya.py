#!/usr/bin/env python3
"""
update_alya.py
Core updater for the alya-lang/update-alya GitHub Action (Dependabot-style).

Two paths, best available wins:

1. Compiler path (preferred): when an `alya` binary is on PATH (e.g. via
   alya-lang/setup-alya), runs `alya update -u` in the package directory.
   The compiler upgrades alya.toml pins AND re-locks alya.lock with correct
   checksums. Nothing is ever pushed straight to the base branch: changes go
   to a fresh branch and a pull request (or stay in the working tree with
   create-pr=false).
2. Manifest fallback: without a compiler, bumps `{ git, tag }` pins in
   alya.toml directly via the GitHub API. `rev` pins and non-semver
   tags are reported but never rewritten. Branch pins are read-only too:
   lock drift behind branch HEAD is reported, never edited (checksums need
   the compiler). When a committed alya.lock exists, manifest-only tag
   bumps would leave a stale lock (checksum mismatch on install), so the
   fallback refuses and tells the caller to provide alya.
3. Branch-lock refresh happens through the compiler path: `alya update -u`
   advances lock revs while the manifest keeps `branch = "..."`; the action
   detects those lock-only moves from `git diff`, lists the commits between
   revs in the PR (no release changelog exists for branches), and never
   opens an empty PR (staged-changes guard).
"""

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

# Keep the step summary far below GitHub's 1 MB cap.
SUMMARY_LIMIT = 48 * 1024

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def log(msg):
    print(f"[update-alya] {msg}", flush=True)


def log_error(msg):
    print(f"::error::[update-alya] {msg}", flush=True)


def api_get(url, token=""):
    """GET JSON from the GitHub API (or any JSON URL)."""
    headers = {"User-Agent": "alya-lang-update-alya", "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def latest_release_tag(owner, repo, token=""):
    """Returns the latest release tag, or None when there is no release."""
    try:
        data = api_get(f"https://api.github.com/repos/{owner}/{repo}/releases/latest", token)
        tag = (data.get("tag_name") or "").strip()
        return tag or None
    except Exception:
        return None


def parse_version(tag):
    """Parses 'v1.2.3' into (1, 2, 3); returns None when not plain semver."""
    parts = tag.strip().lstrip("v").split(".")
    if len(parts) != 3:
        return None
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return None


DEP_RE = re.compile(
    r'^(?P<indent>\s*)(?P<name>[A-Za-z0-9_-]+)\s*=\s*\{\s*'
    r'git\s*=\s*"(?P<git>https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^"/]+?)(?:\.git)?)"\s*,\s*'
    r'tag\s*=\s*"(?P<tag>[^"]+)"\s*\}(?P<trail>[ \t]*(?:\r?\n)?)$'
)


def read_pins(manifest):
    """Returns {name: (owner, repo, tag)} for git+tag pins in alya.toml."""
    pins = {}
    for line in manifest.read_text(encoding="utf-8").splitlines(keepends=True):
        m = DEP_RE.match(line)
        if m:
            pins[m.group("name")] = (m.group("owner"), m.group("repo"), m.group("tag"))
    return pins


BRANCH_RE = re.compile(
    r'^(?P<indent>\s*)(?P<name>[A-Za-z0-9_-]+)\s*=\s*\{\s*'
    r'git\s*=\s*"(?P<git>https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^"/]+?)(?:\.git)?)"\s*,\s*'
    r'branch\s*=\s*"(?P<branch>[^"]+)"\s*\}(?P<trail>[ \t]*(?:\r?\n)?)$'
)


def read_branch_pins(manifest):
    """Returns {name: (owner, repo, branch)} for git+branch pins in alya.toml."""
    pins = {}
    for line in manifest.read_text(encoding="utf-8").splitlines(keepends=True):
        m = BRANCH_RE.match(line)
        if m:
            pins[m.group("name")] = (m.group("owner"), m.group("repo"), m.group("branch"))
    return pins


def branch_head_sha(owner, repo, branch, token=""):
    """Resolves a branch to its HEAD commit SHA (None on failure)."""
    try:
        data = api_get(f"https://api.github.com/repos/{owner}/{repo}/commits/{branch}", token)
        return (data.get("sha") or "").strip() or None
    except Exception:
        return None


def changes_lines(owner, repo, base, head, token="", max_commits=30, limit=3000):
    """`* subject by @user (sha)` lines between two revs (release-notes style).

    Mirrors the `What's Changed` section that `generate_release_notes.py`
    produces for package releases. With `base=None`, lists recent commits
    ending at `head` (lock-new context). Returns "" when not retrievable.
    """
    try:
        if base:
            data = api_get(f"https://api.github.com/repos/{owner}/{repo}/compare/{base}...{head}", token)
            items = data.get("commits", [])[:max_commits]
            total = data.get("total_commits", len(items))
        else:
            data = api_get(f"https://api.github.com/repos/{owner}/{repo}/commits?sha={head}&per_page={max_commits}", token)
            items = data if isinstance(data, list) else []
            items = items[:max_commits]
            total = len(items)
        lines = []
        for c in items:
            msg = (c.get("commit", {}).get("message") or "").strip().splitlines()
            if not msg:
                continue
            # Plain username, no @mention: keeps attribution without pinging
            # upstream authors on every automated PR.
            login = ((c.get("author") or {}).get("login") or "").strip()
            author = login if login else ((c.get("commit", {}).get("author", {}).get("name") or "").strip() or "unknown")
            lines.append(f"* {msg[0][:120]} by {author} ({(c.get('sha') or '')[:7]})")
        text = "\n".join(lines)
        if len(text) > limit:
            text = text[:limit].rstrip() + "\n…(truncated)"
        if total > len(lines):
            text += f"\n…({total - len(lines)} more)"
        return text
    except Exception:
        return ""


def read_lock_sources(manifest_dir):
    """Returns {dep_name: (git_url, rev)} from alya.lock [[package]] blocks."""
    lock = Path(manifest_dir) / "alya.lock"
    found = {}
    if not lock.is_file():
        return found
    name, url, rev = None, None, None
    for line in lock.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if s == "[[package]]":
            if name and url and rev:
                found[name] = (url, rev)
            name, url, rev = None, None, None
        elif s.startswith("name"):
            m = re.match(r'name\s*=\s*"([^"]+)"', s)
            if m:
                name = m.group(1)
        elif s.startswith("source"):
            m = re.match(r'source\s*=\s*"git:([^"#]+)#([^"]+)"', s)
            if m:
                url, rev = m.group(1), m.group(2)
    if name and url and rev:
        found[name] = (url, rev)
    return found


def short_rev(rev):
    rev = rev.strip()
    if parse_version(rev):
        return rev
    return rev[:7]


def lock_branch_bumps(pkg_dir, skip_names=()):
    """Detects lock-only rev moves (branch pins) from `git diff` on alya.lock.

    Used after `alya update -u`: the manifest keeps `branch = "main"` while
    the lock advances to a new commit. Returns bump entries with kind=branch.
    Lock moves belonging to tag-bumped deps (`skip_names`) are excluded so
    each manifest+lock pair stays atomic inside its version PR.
    """
    r = run(["git", "diff", "-U0", "--", "alya.lock"], cwd=str(pkg_dir))
    if r.returncode != 0:
        return []
    old, new = {}, {}
    for raw in (r.stdout or "").splitlines():
        if raw.startswith(("---", "+++")) or not raw[:1] in "-+":
            continue
        m = re.match(r'source\s*=\s*"git:([^"#]+)#([^"]+)"\s*$', raw[1:].strip())
        if not m:
            continue
        (old if raw[0] == "-" else new)[m.group(1)] = m.group(2)
    entries = []
    branch_pins = {}
    try:
        for bname, (_, _, bbranch) in read_branch_pins(pkg_dir / "alya.toml").items():
            branch_pins[bname] = bbranch
    except Exception:
        pass
    for url, new_rev in new.items():
        old_rev = old.get(url)
        if not old_rev or old_rev == new_rev:
            continue
        base_url = url.split("?", 1)[0]
        mo = re.match(r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?$", base_url)
        owner, repo = (mo.group(1), mo.group(2)) if mo else ("", "")
        name = base_url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        if name in skip_names:
            continue
        entries.append({
            "name": name, "kind": "branch", "owner": owner, "repo": repo,
            "branch": branch_pins.get(name, ""), "current": old_rev, "latest": new_rev,
        })
    return entries


def branch_drift_notes(pkg_dir, token):
    """Reports branch pins whose lock lags behind branch HEAD (read-only)."""
    notes = []
    branch_pins = read_branch_pins(pkg_dir / "alya.toml")
    if not branch_pins:
        return notes
    locked = read_lock_sources(pkg_dir)
    for name, (owner, repo, branch) in branch_pins.items():
        head = branch_head_sha(owner, repo, branch, token)
        if head is None:
            notes.append(f"{name}: could not resolve branch {branch!r} in {owner}/{repo}")
            continue
        pinned = locked.get(name)
        if pinned is None:
            notes.append(f"{name}: branch {branch!r} not locked yet (HEAD {head[:7]})")
        elif pinned[1] != head:
            notes.append(f"{name}: lock behind branch {branch!r} ({short_rev(pinned[1])} -> {head[:7]})")
    return notes


def release_notes(owner, repo, tag, token="", limit=3000):
    """Fetches an upstream release body for PR descriptions (Dependabot-style)."""
    try:
        data = api_get(f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}", token)
        body = (data.get("body") or "").strip()
    except Exception:
        return ""
    if len(body) > limit:
        body = body[:limit].rstrip() + "\n\n…(truncated)"
    return body


def ensure_labels(pkg_dir, labels, env):
    """Creates missing PR labels (needs push access, already required)."""
    existing = set()
    try:
        res = run(["gh", "label", "list", "--json", "name", "--jq", ".[].name"], cwd=str(pkg_dir), env=env)
        if res.returncode == 0:
            existing = {l.strip() for l in (res.stdout or "").splitlines() if l.strip()}
    except Exception:
        pass
    for label in labels:
        if label not in existing:
            try:
                run(
                    ["gh", "label", "create", label, "--description", "Alya dependency updates",
                     "--color", "0366d6"],
                    cwd=str(pkg_dir),
                    env=env,
                    check=True,
                )
                log(f"Created label: {label}")
            except Exception as e:
                log(f"Warning: could not create label {label!r} ({e}); continuing.")


def run(cmd, cwd=None, env=None, check=False):
    merged = dict(os.environ)
    if env:
        merged.update(env)
    res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=merged)
    if check and res.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {res.stderr.strip()[:300]}")
    return res


def compiler_bump(pkg_dir, token, dry_run):
    """Compares pins against the API (dry run) or runs `alya update -u`.

    Dry runs never invoke the compiler: `alya update -u` always writes, so
    detection uses the same API comparison as the fallback and the real run
    is left to refresh alya.lock. Returns (bumps, skipped, compiler_output).
    """
    before = read_pins(pkg_dir / "alya.toml")
    if dry_run:
        bumps, skipped = [], []
        for name, (owner, repo, current) in before.items():
            latest = latest_release_tag(owner, repo, token)
            if latest is None:
                skipped.append(f"{name}: no published release in {owner}/{repo}")
                continue
            cur_v, new_v = parse_version(current), parse_version(latest)
            if cur_v is not None and new_v is not None and new_v > cur_v:
                bumps.append({"name": name, "kind": "tag", "owner": owner, "repo": repo, "current": current, "latest": latest})
        for note in branch_drift_notes(pkg_dir, token):
            skipped.append(note)
        return bumps, skipped, ""
    res = run(["alya", "update", "-u"], cwd=str(pkg_dir))
    output = (res.stdout or "") + (res.stderr or "")
    if res.returncode != 0:
        raise RuntimeError(f"alya update -u failed:\n{output[:2000]}")
    after = read_pins(pkg_dir / "alya.toml")
    bumps, skipped = [], []
    for name, (owner, repo, old_tag) in before.items():
        new_tag = after.get(name, (None, None, old_tag))[2]
        if new_tag != old_tag:
            bumps.append({"name": name, "kind": "tag", "owner": owner, "repo": repo, "current": old_tag, "latest": new_tag})
    for e in lock_branch_bumps(pkg_dir, set(before)):
        if not any(b["name"] == e["name"] for b in bumps):
            bumps.append(e)
    if not bumps:
        # A freshly created (untracked) lock is still a change worth a PR:
        # cover it with informational entries instead of reporting up-to-date.
        st = run(["git", "status", "--porcelain", "--", "alya.toml", "alya.lock"], cwd=str(pkg_dir))
        if "alya.lock" in (st.stdout or ""):
            for name, (url, rev) in read_lock_sources(pkg_dir).items():
                base_url = url.split("?", 1)[0]
                mo = re.match(r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?$", base_url)
                owner, repo = (mo.group(1), mo.group(2)) if mo else ("", "")
                bumps.append({
                    "name": name, "kind": "lock-new", "owner": owner, "repo": repo,
                    "branch": "", "current": "(absent)", "latest": rev,
                })
    return bumps, skipped, output


def fallback_bump(pkg_dir, token, manifest):
    """Bumps git+tag pins via the API. Returns (bumps, skipped, new_text|None)."""
    text = manifest.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    checked, bumps, skipped = 0, [], []
    for note in branch_drift_notes(pkg_dir, token):
        skipped.append(note)
    if (pkg_dir / "alya.lock").exists():
        skipped.append("alya.lock present but no `alya` on PATH: manifest-only bumps would leave a stale lock; add alya-lang/setup-alya before this step")
        return [], skipped, None
    for i, line in enumerate(lines):
        m = DEP_RE.match(line)
        if not m:
            continue
        name, owner, repo, current = m.group("name"), m.group("owner"), m.group("repo"), m.group("tag")
        checked += 1
        latest = latest_release_tag(owner, repo, token)
        if latest is None:
            skipped.append(f"{name}: no published release in {owner}/{repo}")
            continue
        cur_v, new_v = parse_version(current), parse_version(latest)
        if cur_v is None or new_v is None:
            if latest != current:
                skipped.append(f"{name}: non-semver pin {current!r} (latest {latest!r}), left untouched")
            continue
        if new_v > cur_v:
            bumps.append({"name": name, "kind": "tag", "owner": owner, "repo": repo, "current": current, "latest": latest})
            lines[i] = line.replace(f'tag = "{current}"', f'tag = "{latest}"', 1)
    new_text = "".join(lines) if bumps else None
    return bumps, skipped, new_text

def git_toplevel(start):
    """Returns the enclosing git repo root, or `start` when not in a repo."""
    try:
        r = run(["git", "rev-parse", "--show-toplevel"], cwd=str(start))
        if r.returncode == 0 and (r.stdout or "").strip():
            return Path((r.stdout or "").strip())
    except Exception:
        pass
    return start


def collect_manifests(root):
    """Finds dirs containing alya.toml under `root` (skips caches/VCS)."""
    found = []
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in (".git", ".alya", "target", "node_modules", "__pycache__", ".venv", "vendor")]
        if "alya.toml" in fn:
            found.append(Path(dp))
    return sorted(found)


def dep_branch(prefix, scope, dep, ver):
    """Stable branch per dependency: <prefix>[/<scope>]/<dep>-<ver>."""
    slug = f"{dep}-{ver}".replace("/", "-")
    return f"{prefix}/{scope}/{slug}" if scope else f"{prefix}/{slug}"


def dep_title(dep, entries):
    """Dependabot-style PR title for one dependency group."""
    tags = [e for e in entries if e.get("kind") == "tag"]
    if tags:
        olds = sorted({e["current"] for e in tags})
        new = tags[0]["latest"]
        for e in tags[1:]:
            try:
                if (parse_version(e["latest"]) or (0,)) > (parse_version(new) or (0,)):
                    new = e["latest"]
            except Exception:
                pass
        old_display = olds[0] if len(olds) == 1 else "/".join(olds)
        return f"chore(deps): bump {dep} from {old_display} to {new}"
    for e in entries:
        if e.get("kind") == "branch":
            where = f" ({e['branch']})" if e.get("branch") else ""
            return f"chore(deps): bump {dep}{where}"
    e = entries[0]
    return f"chore(deps): bump {dep}"


def entry_line(repo, e):
    """One human-readable line for a bump entry, prefixed with its manifest."""
    try:
        rel = os.path.relpath(e["dir"], str(repo))
    except Exception:
        rel = e.get("dir", ".")
    if e.get("kind") == "branch":
        return f"- {rel}: {e['name']} (lock): {short_rev(e['current'])} -> {short_rev(e['latest'])}"
    if e.get("kind") == "lock-new":
        return f"- {rel}: {e['name']} (new lock): {short_rev(e['latest'])}"
    return f"- {rel}: {e['name']}: {e['current']} -> {e['latest']}"


def close_scope_stales(repo, prefix, scope, active_deps, gh_env):
    """Closes open updater PRs in scope whose dependency is now clean."""
    head_prefix = f"{prefix}/{scope}/" if scope else f"{prefix}/"
    try:
        r = run(["gh", "pr", "list", "--state", "open", "--json", "number,headRefName",
                 "--jq", ".[].number, .[].headRefName"], cwd=str(repo), env=gh_env)
    except Exception:
        return
    if r.returncode != 0:
        return
    tokens = (r.stdout or "").split()
    numbers, heads = tokens[0::2], tokens[1::2]
    for num, head in zip(numbers, heads):
        if not head.startswith(head_prefix) or not num.isdigit():
            continue
        dep = head[len(head_prefix):].rsplit("-", 1)[0]
        if dep not in active_deps:
            try:
                run(["gh", "pr", "close", num, "--comment",
                     "Alya dependencies are up to date; closing."],
                    cwd=str(repo), env=gh_env, check=True)
                log(f"Closed stale pull request #{num} ({head}).")
            except Exception as e:
                log(f"Warning: could not close PR #{num} ({e}).")


def main():
    scan_root = Path(os.environ.get("INPUT_PACKAGE_DIR", ".")).resolve()
    create_pr = os.environ.get("INPUT_CREATE_PR", "true").lower() in ("true", "1", "yes")
    dry_run = os.environ.get("INPUT_DRY_RUN", "false").lower() in ("true", "1", "yes")
    base = os.environ.get("INPUT_BASE", "main").strip() or "main"
    prefix = os.environ.get("INPUT_BRANCH_PREFIX", "alya-deps").strip() or "alya-deps"
    scope = os.environ.get("INPUT_BRANCH_SUFFIX", "").strip().strip("/")
    token = os.environ.get("INPUT_TOKEN", "").strip()
    labels = [l.strip() for l in os.environ.get("INPUT_LABELS", "dependencies").split(",") if l.strip()]
    reviewers = [r.strip() for r in os.environ.get("INPUT_REVIEWERS", "").split(",") if r.strip()]
    gh_env = {"GH_TOKEN": token, "GITHUB_TOKEN": token} if token else None

    manifests = collect_manifests(scan_root)
    if not manifests:
        log_error(f"No alya.toml found under {scan_root}")
        sys.exit(1)
    repo = git_toplevel(scan_root)
    has_compiler = bool(shutil.which("alya"))
    log(f"Scanning {len(manifests)} manifest(s) under {scan_root} "
        f"({'compiler' if has_compiler else 'manifest-only fallback'}).")

    bumps, skipped = [], []
    for mdir in manifests:
        manifest = mdir / "alya.toml"
        try:
            if has_compiler:
                b, s, _ = compiler_bump(mdir, token, dry_run)
            else:
                b, s, new_text = fallback_bump(mdir, token, manifest)
                if not dry_run and new_text is not None:
                    manifest.write_text(new_text, encoding="utf-8")
                    log(f"Updated {manifest}")
        except Exception as e:
            log_error(f"{mdir}: {e}")
            sys.exit(1)
        for e in b:
            e["dir"] = str(mdir)
        bumps.extend(b)
        skipped.extend(f"{mdir.name}: {s}" for s in s)

    for b in bumps:
        log(f"{b['name']}: {short_rev(b['current'])} -> {short_rev(b['latest'])}")

    summary_lines = [f"{len(bumps)} bump(s), {len(skipped)} skipped."]
    for b in bumps:
        if b.get("kind") == "branch":
            summary_lines.append(f"- {b['name']} (lock): {short_rev(b['current'])} -> {short_rev(b['latest'])}")
        elif b.get("kind") == "lock-new":
            summary_lines.append(f"- {b['name']} (new lock): {short_rev(b['latest'])}")
        else:
            summary_lines.append(f"- {b['name']}: {b['current']} -> {b['latest']}")
    for s in skipped:
        summary_lines.append(f"- skip: {s}")
    summary = "\n".join(summary_lines)

    if dry_run:
        log("Dry run: no files changed.")
        write_outputs(updated=bool(bumps), summary=summary)
        write_summary(scan_root.name, summary)
        return

    write_outputs(updated=bool(bumps), summary=summary)
    write_summary(scan_root.name, summary)

    if not create_pr:
        log("create-pr=false: changes left in the working tree." if bumps else "Everything up to date.")
        return

    # Group by (upstream dependency, change class): version bumps and lock
    # refreshes get separate PRs (Renovate-style lockFileMaintenance split),
    # so mechanical lock updates can merge under a different policy.
    groups = {}
    for b in bumps:
        cls = "version" if b.get("kind") == "tag" else "lock"
        groups.setdefault((b["name"], cls), []).append(b)

    try:
        ensure_labels(repo, labels, gh_env)
        if token:
            remote = run(["git", "remote", "get-url", "origin"], cwd=str(repo))
            m = re.match(r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?$", (remote.stdout or "").strip())
            if m:
                run(
                    ["git", "remote", "set-url", "origin",
                     f"https://x-access-token:{token}@github.com/{m.group(1)}/{m.group(2)}.git"],
                    cwd=str(repo),
                    check=True,
                )
        for (dep, cls), entries in groups.items():
            bump_dep(repo, dep, cls, entries, base, prefix, scope, labels, reviewers, gh_env, token)
        close_scope_stales(repo, prefix, scope, {dep for dep, _ in groups}, gh_env)
        if not groups:
            log("Everything up to date.")
    except Exception as e:
        log_error(f"Could not process pull requests: {e}")
        sys.exit(1)

def bump_dep(repo, dep, cls, entries, base, prefix, scope, labels, reviewers, gh_env, token):
    """Stages, commits, pushes and opens/refreshes one dependency PR."""
    tag_entries = [e for e in entries if e.get("kind") == "tag"]
    if tag_entries:
        ver = tag_entries[0]["latest"]
    elif cls == "lock":
        ver = "lock"
    else:
        ver = short_rev(entries[0]["latest"])
    branch = dep_branch(prefix, scope, dep, ver)
    existing_pr = open_pr_for_branch(repo, branch, gh_env)

    files = set()
    for e in entries:
        files.add(str(Path(e["dir"]) / "alya.toml"))
        files.add(str(Path(e["dir"]) / "alya.lock"))
    notes_sections = []
    for b in entries:
        if b.get("kind") == "branch":
            commits = changes_lines(b["owner"], b["repo"], b["current"], b["latest"], token)
            if commits:
                notes_sections.append(
                    f"#### {b['name']} (lock): {short_rev(b['current'])} -> {short_rev(b['latest'])}"
                    f"\n\n🚀 What's Changed\n\n{commits}")
        elif b.get("kind") == "lock-new":
            commits = changes_lines(b["owner"], b["repo"], None, b["latest"], token) if b.get("owner") else ""
            if commits:
                notes_sections.append(
                    f"#### {b['name']} (new lock): {short_rev(b['latest'])}"
                    f"\n\n🚀 What's Changed\n\n{commits}")
        else:
            commits = changes_lines(b["owner"], b["repo"], b["current"], b["latest"], token)
            if not commits:
                commits = release_notes(b["owner"], b["repo"], b["latest"], token)
            if commits:
                notes_sections.append(f"#### {b['name']} {b['latest']}\n\n🚀 What's Changed\n\n{commits}")
    body_lines = [entry_line(repo, b) for b in entries]
    run(["git", "checkout", "-B", branch], cwd=str(repo), check=True)
    run(["git", "config", "user.name", "github-actions[bot]"], cwd=str(repo), check=True)
    run(
        ["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"],
        cwd=str(repo),
        check=True,
    )
    run(["git", "add", "--"] + sorted(files), cwd=str(repo), check=True)
    # Belt-and-braces: never open an empty PR.
    staged = run(["git", "status", "--porcelain", "--"] + sorted(files), cwd=str(repo))
    if not (staged.stdout or "").strip():
        log(f"[{dep}] No changes detected; skipping pull request (avoids empty PR).")
        return
    title = dep_title(dep, entries)
    run(["git", "commit", "-m", title], cwd=str(repo), check=True)
    run(["git", "push", "-f", "-u", "origin", branch], cwd=str(repo), check=True)
    pr_body = "Automated Alya dependency bumps by [update-alya](https://github.com/alya-lang/update-alya).\n\n" + "\n".join(body_lines)
    if notes_sections:
        pr_body += "\n\n### Changes\n\n" + "\n\n".join(notes_sections)
    if existing_pr:
        edit_cmd = ["gh", "pr", "edit", str(existing_pr), "--title", title, "--body", pr_body]
        for label in labels:
            edit_cmd += ["--add-label", label]
        for reviewer in reviewers:
            edit_cmd += ["--add-reviewer", reviewer]
        run(edit_cmd, cwd=str(repo), env=gh_env, check=True)
        log(f"[{dep}] Updated pull request #{existing_pr}.")
    else:
        pr_cmd = ["gh", "pr", "create", "--base", base, "--head", branch,
                  "--title", title, "--body", pr_body]
        for label in labels:
            pr_cmd += ["--label", label]
        for reviewer in reviewers:
            pr_cmd += ["--reviewer", reviewer]
        pr = run(pr_cmd, cwd=str(repo), env=gh_env, check=True)
        log(f"[{dep}] Opened pull request: {pr.stdout.strip()[:200]}")




def open_pr_for_branch(pkg_dir, branch, env):
    """Returns the open PR number for `branch`, or None."""
    try:
        r = run(["gh", "pr", "list", "--head", branch, "--state", "open",
                 "--json", "number", "--jq", ".[0].number"], cwd=str(pkg_dir), env=env)
        if r.returncode == 0 and (r.stdout or "").strip().isdigit():
            return int((r.stdout or "").strip())
    except Exception:
        pass
    return None


def write_outputs(updated, summary):
    out = os.environ.get("GITHUB_OUTPUT", "")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"updated={'true' if updated else 'false'}\n")
            single = summary.replace("\n", "%0A")
            f.write(f"summary={single}\n")


def write_summary(pkg_name, summary):
    gh_summary = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if not gh_summary:
        return
    body = f"## Update Alya ({pkg_name})\n\n```text\n{summary}\n```\n"[:SUMMARY_LIMIT]
    with open(gh_summary, "a", encoding="utf-8") as f:
        f.write(body)


if __name__ == "__main__":
    main()
