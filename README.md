# update-alya

[![CI](https://github.com/alya-lang/update-alya/actions/workflows/test.yml/badge.svg)](https://github.com/alya-lang/update-alya/actions/workflows/test.yml)
[![License](https://img.shields.io/github/license/alya-lang/update-alya?color=blue&label=License)](LICENSE)

Dependabot-style updater for [Alya](https://github.com/alya-lang/alya) package dependencies. Scans `alya.toml` files, checks for outdated git tags and branch revisions, updates locks, and opens automated pull requests.

---

## ⚡ Quick Start

Create `.github/workflows/update-deps.yml` in your repository:

```yaml
name: Update Alya dependencies

on:
  schedule:
    - cron: '0 3 * * 1' # Weekly on Monday at 03:00 UTC
  workflow_dispatch:

permissions:
  contents: write
  pull-requests: write

jobs:
  update:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Set up Alya
        uses: alya-lang/setup-alya@v1

      - name: Bump Alya dependencies
        uses: alya-lang/update-alya@v1
```

---

## 🧹 Automatic Branch Cleanup (Recommended)

When pull requests are merged or closed without merging, GitHub does not delete unmerged branches by default. Add `.github/workflows/cleanup-deps.yml` to automatically delete dependency branches whenever PRs are closed:

```yaml
name: Cleanup Dependency Branches

on:
  pull_request:
    types: [closed]

permissions:
  contents: write

jobs:
  cleanup:
    name: Delete PR branch
    runs-on: ubuntu-latest
    if: |
      github.event.pull_request.head.repo.full_name == github.repository &&
      startsWith(github.head_ref, 'alya-deps/')
    steps:
      - name: Delete merged or closed branch
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          BRANCH: ${{ github.head_ref }}
        run: |
          gh api --method DELETE "repos/${{ github.repository }}/git/refs/heads/${BRANCH}" || true
```

> **Tip:** You can also enable **"Automatically delete head branches"** in repository *Settings → General → Pull Requests* to let GitHub natively prune merged branches.

---

## ⚙️ Inputs

| Input | Description | Required | Default |
|:---|:---|:---:|:---:|
| `package-dir` | Scan root: every `alya.toml` below it is checked | No | `'.'` |
| `create-pr` | Open a pull request with the bumps (`false` only updates the working tree) | No | `'true'` |
| `dry-run` | Report outdated pins without changing any files | No | `'false'` |
| `base` | Base branch for the pull request | No | `'main'` |
| `branch-prefix` | Prefix for the generated update branch | No | `'alya-deps'` |
| `branch-suffix` | Scope segment in branch names (`<prefix>/<suffix>/<dep>-<ver>`); separates parallel matrix jobs | No | `''` |
| `labels` | Comma-separated labels attached to the PR (created if missing) | No | `'dependencies'` |
| `reviewers` | Comma-separated GitHub usernames to request review from | No | `''` |
| `token` | GitHub token for API requests and pull request creation | No | `${{ github.token }}` |

---

## 📤 Outputs

| Output | Description | Example |
|:---|:---|:---|
| `updated` | Whether any pin was bumped (`true`/`false`) | `true` |
| `summary` | Human-readable list of checked, bumped, and skipped pins | `Checked 2 git+tag pin(s)…` |

---

## ✨ Features & Behavior

- **Compiler Integration:** When `alya` is installed (via `alya-lang/setup-alya`), runs `alya update -u` to update `alya.toml` and synchronize `alya.lock` with correct checksums.
- **Manifest Fallback:** Without a compiler, updates `{ git, tag }` pins directly via the GitHub API (requires no committed lockfile).
- **One PR Per Dependency:** Groups bumps by upstream dependency (e.g. `chore(deps): bump rand from v0.0.0 to v0.1.0`).
- **No Duplicate PRs:** Re-running the action updates the existing branch and PR instead of creating duplicates.
- **Empty-PR Guard:** Never opens a PR if `alya.toml` and `alya.lock` haven't changed.
- **Detailed Changelogs:** Pull request descriptions include commit lists and release notes comparing old and new revisions.
- **Superseded PR Cleanup:** When a newer version of a dependency opens a new PR, older open PRs for that dependency are automatically closed with a reference comment (`Superseded by #...`), triggering the repository's branch cleanup workflow.

---

## 🔑 Authentication

1. **Default `GITHUB_TOKEN` (Recommended):**
   Ensure GitHub Actions has permission to create pull requests in repository **Settings → Actions → General → Workflow permissions** (enable *"Allow GitHub Actions to create and approve pull requests"*).
2. **Personal Access Token (PAT):**
   If workflow permissions are locked, create a PAT with `contents: write` and `pull-requests: write`, save it as a repository secret (e.g., `UPDATE_ALYA_TOKEN`), and pass:
   ```yaml
   with:
     token: ${{ secrets.UPDATE_ALYA_TOKEN }}
   ```

---

## 📄 License

This action is licensed under the [MIT License](LICENSE).
