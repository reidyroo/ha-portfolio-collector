# Contributing & Development Workflow

## Branch protection

The `main` branch is protected — direct pushes are blocked.
**All changes must go through a pull request**, including version bumps,
documentation updates, and hotfixes.

---

## Branch naming

| Type | Pattern | Example |
|---|---|---|
| New feature | `feat/<short-description>` | `feat/phase-presets` |
| Bug fix | `fix/<short-description>` | `fix/t212-sell-order-sign` |
| Documentation | `docs/<short-description>` | `docs/readme-v1.6` |
| Chore / housekeeping | `chore/<short-description>` | `chore/cleanup-branches` |

Keep names lowercase, hyphen-separated, no version numbers in the branch name
(version lives in `config.yaml` and `CHANGELOG.md`).

---

## Standard workflow

```bash
# 1. Start from a clean, up-to-date main
git checkout main
git pull origin main

# 2. Create a feature branch
git checkout -b feat/my-new-thing

# 3. Make changes, commit as you go
git add <files>
git commit -m "feat: describe what and why"

# 4. Push and open a PR
git push -u origin feat/my-new-thing
gh pr create --title "feat: my new thing" --body "..."

# 5. After the PR is merged, clean up locally
git checkout main
git pull origin main
git branch -d feat/my-new-thing
```

---

## Commit message style

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>: <short summary in present tense>

Optional longer body explaining why, not just what.
```

| Type | When to use |
|---|---|
| `feat` | New capability or endpoint |
| `fix` | Bug fix |
| `docs` | README, CHANGELOG, comments only |
| `chore` | Build, tooling, config (no production code change) |
| `refactor` | Code restructure with no behaviour change |

---

## Version bumps

Versions are **semantic** (`MAJOR.MINOR.PATCH`) but in practice:
- **MINOR** bump (`1.x.0`) for any new feature or breaking config change
- **PATCH** bump (`1.6.x`) for bug fixes and docs-only changes

When bumping a version, update all three of these in the same commit:

| File | What to change |
|---|---|
| `config.yaml` | `version:` field |
| `portfolio_collector/collector.py` | `FastAPI(version=...)`, log line, health endpoint string |
| `CHANGELOG.md` | Add a new `## [x.y.z] — YYYY-MM-DD` section |

---

## PR checklist

Before opening a PR:

- [ ] Version bumped if behaviour changed (see above)
- [ ] `CHANGELOG.md` updated with a `## [x.y.z]` section
- [ ] Local files synced to the repo folder
  ```powershell
  Copy-Item "C:\Code\ha_portfolio\addons\portfolio_collector\collector.py" `
            "C:\Code\ha-portfolio-collector\portfolio_collector\collector.py" -Force
  Copy-Item "C:\Code\ha_portfolio\merged\lovelace\dashboard.yaml" `
            "C:\Code\ha-portfolio-collector\lovelace\dashboard.yaml" -Force
  Copy-Item "C:\Code\ha_portfolio\merged\packages\portfolio.yaml" `
            "C:\Code\ha-portfolio-collector\packages\portfolio.yaml" -Force
  ```
- [ ] Add-on tested on HA Green (or at minimum: `python collector.py` starts cleanly)
- [ ] PR title matches the commit type (`feat:`, `fix:`, etc.)

---

## Creating a GitHub release

After a PR is merged and tagged:

```bash
# Tag the merge commit
git checkout main && git pull
git tag -a vX.Y.Z -m "vX.Y.Z — short description"
git push origin vX.Y.Z

# Create the GitHub release
gh release create vX.Y.Z \
  --title "vX.Y.Z — short description" \
  --latest \
  --notes "$(cat <<'EOF'
## What's new
...
EOF
)"
```

---

## Hotfix workflow

For urgent fixes that can't wait for a feature branch cycle:

```bash
git checkout main && git pull
git checkout -b fix/urgent-thing
# make the fix
git commit -m "fix: urgent thing"
git push -u origin fix/urgent-thing
gh pr create --title "fix: urgent thing"
# merge immediately after opening
```

Even hotfixes go through a PR — the protection is there to keep the history clean,
not to slow things down.
