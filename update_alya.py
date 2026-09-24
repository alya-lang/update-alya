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
   alya.toml directly via the GitHub API. `rev`/`branch` pins and non-semver
   tags are reported but never rewritten. When a committed alya.lock exists,
   manifest-only bumps would leave a stale lock (checksum mismatch on
   install), so the fallback refuses and tells the caller to provide alya.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
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
                bumps.append({"name": name, "current": current, "latest": latest})
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
            bumps.append({"name": name, "current": old_tag, "latest": new_tag})
    return bumps, skipped, output


def fallback_bump(pkg_dir, token, manifest):
    """Bumps git+tag pins via the API. Returns (bumps, skipped, new_text|None)."""
    text = manifest.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    if (pkg_dir / "alya.lock").exists():
        return [], ["alya.lock present but no `alya` on PATH: manifest-only bumps would leave a stale lock; add alya-lang/setup-alya before this step"], None
    checked, bumps, skipped = 0, [], []
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
            bumps.append({"name": name, "current": current, "latest": latest})
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
        log(f"{b['name']}: {b['current']} -> {b['latest']}")

    summary_lines = [f"{len(bumps)} bump(s), {len(skipped)} skipped."]
    for b in bumps:
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

    # Never push straight to the base branch: PR (or working tree only).
    if not bumps or not create_pr:
        if not bumps:
            log("Everything up to date.")
        else:
            log("create-pr=false: changes left in the working tree.")
        return

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    branch = f"{prefix}/{stamp}"
    gh_env = {"GH_TOKEN": token, "GITHUB_TOKEN": token} if token else None
    try:
        run(["git", "checkout", "-B", branch], cwd=str(pkg_dir), check=True)
        run(["git", "config", "user.name", "github-actions[bot]"], cwd=str(pkg_dir), check=True)
        run(
            ["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"],
            cwd=str(pkg_dir),
            check=True,
        )
        run(["git", "add", "alya.toml"], cwd=str(pkg_dir), check=True)
        run(["git", "add", "alya.lock"], cwd=str(pkg_dir))
        body_lines = [f"- {b['name']}: {b['current']} -> {b['latest']}" for b in bumps]
        run(["git", "commit", "-m", "chore(deps): bump alya dependencies"], cwd=str(pkg_dir), check=True)
        run(["git", "push", "-u", "origin", branch], cwd=str(pkg_dir), check=True)
        pr_body = "Automated Alya dependency bumps by [update-alya](https://github.com/alya-lang/update-alya).\n\n" + "\n".join(body_lines)
        pr = run(
            ["gh", "pr", "create", "--base", base, "--head", branch,
             "--title", "chore(deps): bump alya dependencies", "--body", pr_body],
            cwd=str(pkg_dir),
            env=gh_env,
            check=True,
        )
        log(f"Opened pull request: {pr.stdout.strip()[:200]}")
    except Exception as e:
        log_error(f"Could not open pull request: {e}")
        sys.exit(1)


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
