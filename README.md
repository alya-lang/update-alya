# update-alya

[![CI](https://github.com/alya-lang/update-alya/actions/workflows/test.yml/badge.svg)](https://github.com/alya-lang/update-alya/actions/workflows/test.yml)
[![License](https://img.shields.io/github/license/alya-lang/update-alya?color=blue&label=License)](LICENSE)

Dependabot-style updater for [Alya](https://github.com/alya-lang/alya) package dependencies. Scans `alya.toml` `[dependencies]` for `git`+`tag` pins, bumps outdated tags to their latest releases, and opens a pull request.

---

## ⚡ Quick Start

Add `alya-lang/update-alya@v1` to a scheduled workflow in your package repository:

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

## 📌 Examples

### PAT-based run (locked workflow policy)

```yaml
jobs:
  update:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v4

      - name: Set up Alya
        uses: alya-lang/setup-alya@v1

      - name: Bump Alya dependencies
        uses: alya-lang/update-alya@v1
        with:
          token: ${{ secrets.UPDATE_ALYA_TOKEN }}
```

### Dry-run report without side effects

```yaml
      - name: Check for outdated pins
        id: check
        uses: alya-lang/update-alya@v1
        with:
          dry-run: 'true'

      - name: Notify on Slack
        if: steps.check.outputs.updated == 'true'
        run: echo "${{ steps.check.outputs.summary }}"
```

---

## ⚙️ Inputs

| Input | Description | Required | Default |
|:---|:---|:---:|:---:|
| `package-dir` | Path to the Alya package directory containing `alya.toml` | No | `'.'` |
| `create-pr` | Open a pull request with the bumps (`false` only updates the working tree) | No | `'true'` |
| `dry-run` | Report outdated pins without changing any files | No | `'false'` |
| `base` | Base branch for the pull request | No | `'main'` |
| `branch-prefix` | Prefix for the generated update branch | No | `'alya-deps'` |
| `branch-suffix` | Slug in the stable branch name (`<prefix>/<suffix>`, defaults to package dir); separates parallel matrix jobs | No | `''` |
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

## 📌 Scope

- ✅ With `alya` on PATH (e.g. via `alya-lang/setup-alya` first): delegates to `alya update -u`, which upgrades `alya.toml` pins **and** re-locks `alya.lock` with correct checksums.
- ✅ Without a compiler: bumps `{ git = "<url>", tag = "vX.Y.Z" }` pins in `alya.toml` directly via the GitHub API — except when a committed `alya.lock` exists, where manifest-only bumps would leave a stale lock (checksum mismatch on install), so the run stops with guidance instead.
- ⏭️ `rev` pins and non-semver tags are reported but never rewritten.
- 🔀 Branch pins: manifest keeps `branch = "…"`, the lock rev is refreshed via the compiler path; PRs list the commits between revs (no release changelog exists for branches). Without a compiler, lock drift is reported read-only.
- 🚫 Empty-PR guard: a pull request opens only when `alya.toml`/`alya.lock` actually changed.
- ⏭️ The package `version` and `alya-version` fields are never touched.
- 🔀 Changes never go straight to the base branch: one stable branch per package (`<prefix>/<suffix>`) plus pull request (or working tree only with `create-pr: 'false'`). Repeat runs refresh the same PR instead of piling up duplicates, and a clean tree closes a stale PR.

Requires `contents: write` and `pull-requests: write` permissions when `create-pr` is enabled. Opened PRs carry the configured labels, requested reviewers, and the upstream release notes of each bumped dependency.

## 🔑 Authentication (two supported paths)

1. **Default token (simplest):** allow GitHub Actions to create pull requests — repository *Settings → Actions → General → Workflow permissions*, or once per organization. Nothing extra to configure; omit `token`.
2. **PAT:** if your organization locks that policy, create a token with `contents` + `pull-requests` access, store it as a secret (e.g. `UPDATE_ALYA_TOKEN`), and pass `token: ${{ secrets.UPDATE_ALYA_TOKEN }}`. It is used for push, labels, and PR creation alike.

---

## 📄 License

This action is licensed under the [MIT License](LICENSE).
