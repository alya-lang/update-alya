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

      - name: Bump Alya dependencies
        uses: alya-lang/update-alya@v1
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
| `token` | GitHub token for API requests and pull request creation | No | `${{ github.token }}` |

---

## 📤 Outputs

| Output | Description | Example |
|:---|:---|:---|
| `updated` | Whether any pin was bumped (`true`/`false`) | `true` |
| `summary` | Human-readable list of checked, bumped, and skipped pins | `Checked 2 git+tag pin(s)…` |

---

## 📌 Scope

- ✅ `{ git = "<url>", tag = "vX.Y.Z" }` pins are bumped to the upstream latest release tag.
- ⏭️ `rev`/`branch` pins and non-semver tags are reported but never rewritten.
- ⏭️ The package `version` and `alya-version` fields are never touched.

Requires `contents: write` and `pull-requests: write` permissions when `create-pr` is enabled.

---

## 📄 License

This action is licensed under the [MIT License](LICENSE).
