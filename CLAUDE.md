# Claude Code Instructions — ha-portfolio-collector

## Version bumping

**Every commit that changes behaviour must bump the version.** Use semver patch (x.y.Z) for bug fixes, minor (x.Y.0) for new features.

Version appears in **4 places** — update all of them together:

| File | Location |
|------|----------|
| `config.yaml` | line 8 — `version: "x.y.z"` |
| `portfolio_collector/config.yaml` | line 8 — `version: "x.y.z"` |
| `portfolio_collector/collector.py` | docstring line 3, `collector_version` assignment, startup log (3 occurrences — use `replace_all: true`) |

After editing, verify with:
```
grep -r "x.y.z" config.yaml portfolio_collector/config.yaml portfolio_collector/collector.py
```
All four (actually five) lines must match before committing.
