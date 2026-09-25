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


def branch_commits(owner, repo, base, head, token="", max_commits=10, limit=1500):
    """Lists commits between two revs for PR descriptions (Dependabot-style)."""
    try:
        data = api_get(f"https://api.github.com/repos/{owner}/{repo}/compare/{base}...{head}", token)
        lines = []
        for c in data.get("commits", [])[:max_commits]:
            msg = (c.get("commit", {}).get("message") or "").strip().splitlines()
            if msg:
                lines.append(f"{(c.get('sha') or '')[:7]} {msg[0][:100]}")
        text = "\n".join(lines)
        if len(text) > limit:
            text = text[:limit].rstrip() + "\n…(truncated)"
        total = data.get("total_commits", len(lines))
        if total > len(lines):
            text += f"\n…({total - len(lines)} more commits)"
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


def lock_branch_bumps(pkg_dir):
    """Detects lock-only rev moves (branch pins) from `git diff` on alya.lock.

    Used after `alya update -u`: the manifest keeps `branch = "main"` while
    the lock advances to a new commit. Returns bump entries with kind=branch.
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
    for url, new_rev in new.items():
        old_rev = old.get(url)
        if not old_rev or old_rev == new_rev:
            continue
        base_url = url.split("?", 1)[0]
        mo = re.match(r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?$", base_url)
        owner, repo = (mo.group(1), mo.group(2)) if mo else ("", "")
        name = base_url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        entries.append({
            "name": name, "kind": "branch", "owner": owner, "repo": repo,
            "branch": "", "current": old_rev, "latest": new_rev,
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
    for e in lock_branch_bumps(pkg_dir):
        if not any(b["name"] == e["name"] for b in bumps):
            bumps.append(e)
    if not bumps:
        # A freshly created (untracked) lock is still a change worth a PR:
        # cover it with informational entries instead of reporting up-to-date.
        st = run(["git", "status", "--porcelain", "--", "alya.toml", "alya.lock"], cwd=str(pkg_dir))
        if "alya.lock" in (st.stdout or ""):
            for name, (url, rev) in read_lock_sources(pkg_dir).items():
                mo = re.match(r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?$", url)
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

def main():
    pkg_dir = Path(os.environ.get("INPUT_PACKAGE_DIR", ".")).resolve()
    create_pr = os.environ.get("INPUT_CREATE_PR", "true").lower() in ("true", "1", "yes")
    dry_run = os.environ.get("INPUT_DRY_RUN", "false").lower() in ("true", "1", "yes")
    base = os.environ.get("INPUT_BASE", "main").strip() or "main"
    prefix = os.environ.get("INPUT_BRANCH_PREFIX", "alya-deps").strip() or "alya-deps"
    token = os.environ.get("INPUT_TOKEN", "").strip()

    manifest = pkg_dir / "alya.toml"
    if not manifest.is_file():
        log_error(f"No alya.toml found in {pkg_dir}")
        sys.exit(1)

    bumps, skipped = [], []
    if shutil.which("alya"):
        log("Compiler found on PATH: `alya update -u` will upgrade and re-lock.")
        try:
            bumps, skipped, _ = compiler_bump(pkg_dir, token, dry_run)
        except Exception as e:
            log_error(str(e))
            sys.exit(1)
    else:
        log("No `alya` on PATH: using manifest-only fallback.")
        try:
            bumps, skipped, new_text = fallback_bump(pkg_dir, token, manifest)
        except Exception as e:
            log_error(str(e))
            sys.exit(1)
        if dry_run:
            pass
        elif new_text is not None:
            manifest.write_text(new_text, encoding="utf-8")
            log(f"Updated {manifest}")

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
        write_summary(pkg_dir.name, summary)
        return

    write_outputs(updated=bool(bumps), summary=summary)
    write_summary(pkg_dir.name, summary)

    # Never push straight to the base branch: update (or close) one stable
    # PR per slug, or leave the working tree untouched with create-pr=false.
    # The stable branch name lets repeat runs refresh the same PR instead of
    # piling up duplicates; a clean tree closes a stale PR (Dependabot-style).
    slug = os.environ.get("INPUT_BRANCH_SUFFIX", "").strip().strip("/") or pkg_dir.name
    branch = f"{prefix}/{slug}"
    labels = [l.strip() for l in os.environ.get("INPUT_LABELS", "dependencies").split(",") if l.strip()]
    reviewers = [r.strip() for r in os.environ.get("INPUT_REVIEWERS", "").split(",") if r.strip()]
    gh_env = {"GH_TOKEN": token, "GITHUB_TOKEN": token} if token else None
    existing_pr = open_pr_for_branch(pkg_dir, branch, gh_env) if create_pr else None

    if not bumps or not create_pr:
        if not bumps:
            if existing_pr:
                try:
                    run(["gh", "pr", "close", str(existing_pr), "--comment",
                         "Alya dependencies are up to date; closing."],
                        cwd=str(pkg_dir), env=gh_env, check=True)
                    log(f"Closed stale pull request #{existing_pr}.")
                except Exception as e:
                    log(f"Warning: could not close PR #{existing_pr} ({e}).")
            else:
                log("Everything up to date.")
        else:
            log("create-pr=false: changes left in the working tree.")
        return

    try:
        ensure_labels(pkg_dir, labels, gh_env)
        notes_sections = []
        for b in bumps:
            if b.get("kind") == "branch":
                commits = branch_commits(b["owner"], b["repo"], b["current"], b["latest"], token)
                section = f"#### {b['name']} (lock): {short_rev(b['current'])} -> {short_rev(b['latest'])}"
                notes_sections.append(section + (f"\n\n```text\n{commits}\n```" if commits else ""))
            elif b.get("kind") == "lock-new":
                ctx = recent_commits(b["owner"], b["repo"], b["latest"], token) if b.get("owner") else ""
                section = f"#### {b['name']} (new lock): {short_rev(b['latest'])}"
                notes_sections.append(section + (f"\n\n```text\n{ctx}\n```" if ctx else ""))
            else:
                notes = release_notes(b["owner"], b["repo"], b["latest"], token)
                if notes:
                    notes_sections.append(f"#### {b['name']} {b['latest']}\n\n{notes}")
        run(["git", "checkout", "-B", branch], cwd=str(pkg_dir), check=True)
        run(["git", "config", "user.name", "github-actions[bot]"], cwd=str(pkg_dir), check=True)
        run(
            ["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"],
            cwd=str(pkg_dir),
            check=True,
        )
        run(["git", "add", "alya.toml"], cwd=str(pkg_dir), check=True)
        run(["git", "add", "alya.lock"], cwd=str(pkg_dir))
        # Belt-and-braces: never open an empty PR (e.g. lock-only drift with
        # nothing staged, or a compiler run that normalized nothing).
        staged = run(["git", "status", "--porcelain", "--", "alya.toml", "alya.lock"], cwd=str(pkg_dir))
        if not (staged.stdout or "").strip():
            log("No changes detected; skipping pull request (avoids empty PR).")
            return
        body_lines = []
        for b in bumps:
            if b.get("kind") == "branch":
                body_lines.append(f"- {b['name']} (lock): {short_rev(b['current'])} -> {short_rev(b['latest'])}")
            elif b.get("kind") == "lock-new":
                body_lines.append(f"- {b['name']} (new lock): {short_rev(b['latest'])}")
            else:
                body_lines.append(f"- {b['name']}: {b['current']} -> {b['latest']}")
        run(["git", "commit", "-m", "chore(deps): bump alya dependencies"], cwd=str(pkg_dir), check=True)
        # Push with the caller token when provided: GITHUB_TOKEN honors the
        # repo/org workflow-permissions policy (which may forbid pushes/PRs),
        # while a PAT passed via `token` bypasses it.
        if token:
            remote = run(["git", "remote", "get-url", "origin"], cwd=str(pkg_dir))
            m = re.match(r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?$", (remote.stdout or "").strip())
            if m:
                run(
                    ["git", "remote", "set-url", "origin",
                     f"https://x-access-token:{token}@github.com/{m.group(1)}/{m.group(2)}.git"],
                    cwd=str(pkg_dir),
                    check=True,
                )
        run(["git", "push", "-f", "origin", branch], cwd=str(pkg_dir), check=True)
        pr_body = "Automated Alya dependency bumps by [update-alya](https://github.com/alya-lang/update-alya).\n\n" + "\n".join(body_lines)
        if notes_sections:
            pr_body += "\n\n### Release notes\n\n" + "\n\n".join(notes_sections)
        if existing_pr:
            edit_cmd = ["gh", "pr", "edit", str(existing_pr), "--body", pr_body]
            for label in labels:
                edit_cmd += ["--add-label", label]
            for reviewer in reviewers:
                edit_cmd += ["--add-reviewer", reviewer]
            run(edit_cmd, cwd=str(pkg_dir), env=gh_env, check=True)
            log(f"Updated pull request #{existing_pr}.")
        else:
            pr_cmd = ["gh", "pr", "create", "--base", base, "--head", branch,
                      "--title", "chore(deps): bump alya dependencies", "--body", pr_body]
            for label in labels:
                pr_cmd += ["--label", label]
            for reviewer in reviewers:
                pr_cmd += ["--reviewer", reviewer]
            pr = run(pr_cmd, cwd=str(pkg_dir), env=gh_env, check=True)
            log(f"Opened pull request: {pr.stdout.strip()[:200]}")
    except Exception as e:
        log_error(f"Could not open pull request: {e}")
        sys.exit(1)


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


def recent_commits(owner, repo, rev, token="", max_commits=10, limit=1500):
    """Lists recent commits ending at `rev` (context for lock-new entries)."""
    try:
        data = api_get(f"https://api.github.com/repos/{owner}/{repo}/commits?sha={rev}&per_page={max_commits}", token)
        lines = []
        items = data if isinstance(data, list) else []
        for c in items[:max_commits]:
            msg = (c.get("commit", {}).get("message") or "").strip().splitlines()
            if msg:
                lines.append(f"{(c.get('sha') or '')[:7]} {msg[0][:100]}")
        text = "\n".join(lines)
        if len(text) > limit:
            text = text[:limit].rstrip() + "\n…(truncated)"
        return text
    except Exception:
        return ""


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
