# Git Cleanup Runbook (do this before pushing to a public/portfolio repo)

> Why this exists: at audit time the repo had (1) a stale `.git/index.lock`
> blocking all git writes, (2) a large pile of uncommitted changes (the last
> commit `v3.0` is well behind the working tree), and (3) valuable content that
> is **untracked** (`stock/`, `crypto/`, several `docs/`). Follow these steps in
> order. Commands assume you are in the repository root.

## 0. Safety first — confirm no secrets will be committed

```bash
git ls-files | grep -iE 'env|secret|key|token' || echo "none tracked"   # expect only *.example
git check-ignore .env                                                   # expect: .env  (i.e. ignored)
```

`.env` is git-ignored and was never committed (verified). Keep it that way.
**Rotate the Binance testnet API key/secret in your Binance account** if it was
ever shared, then keep the real values only in the local `.env`.

## 1. Clear the stale git lock

First make sure no other git process is running (close any Git GUI, VS Code
Source Control mid-operation, or `git` in another terminal). Then:

```bash
rm -f .git/index.lock
git status            # should work again now
```

## 2. Decide what NOT to track, then extend .gitignore

The `.app` bundles in `stock/apps/` are built artifacts (macOS app wrappers) and
should not be in git. Add:

```bash
cat >> .gitignore <<'EOF'

# macOS app bundles (built artifacts)
*.app/
stock/apps/

# second-engine virtualenv (regenerable)
claude-trade/.venv/
EOF
```

(`.venv/`, `runtime/`, `*.log`, `Futu_OpenD_*/` are already ignored.)

## 3. Bring the valuable untracked content under version control

```bash
# the engineering docs (currently untracked) — these are the portfolio gold
git add docs/                       # ARCHITECTURE notes, learning labs, this runbook, etc.
git add ARCHITECTURE.md README.en.md LICENSE
git add src/taa_futu/strategy_overrides.py tests/test_strategy_overrides.py
git add src/taa_futu/config.py      # the default-off override hook

# the real components that were never tracked
git add stock/tools stock/docs stock/README.md stock/launchers   # screeners, docs, launchers
git add crypto/tools

# resolve the docs/ drift (two files were moved to stock/docs/)
git add -A docs/
```

Review before committing:

```bash
git status
git diff --cached --stat
```

## 4. (Optional) make English the primary README

Standard for an international portfolio repo:

```bash
git mv README.md README.zh-CN.md
git mv README.en.md README.md
```

## 5. Commit and set identity

```bash
git config user.name  "Your Name"
git config user.email "you@example.com"

git add -A
git commit -m "docs+infra: architecture, learning→strategy bridge, repo hygiene

- ARCHITECTURE.md + English README + MIT license
- strategy_overrides.py: human-approved learning→param bridge (default off), tested
- bring stock/ crypto/ screeners, launchers and docs under version control
- ignore built .app bundles and the second-engine venv"
```

## 6. Push to a new GitHub repo

```bash
gh repo create taa-futu --private --source=. --remote=origin   # or create on github.com
git push -u origin HEAD
```

Start `--private`; make it public only after a final secrets review
(`git log -p | grep -iE 'api_secret|password'` should find nothing real).

## 7. Nice-to-have, later

- A GitHub Actions workflow that runs `pytest tests/ -q` on push.
- Split `cli.py` (2,187 lines) into per-domain command modules.
- De-hardcode absolute `/Users/<name>/...` paths in the `.command` launchers (derive the repo root from `$0`). **Done 2026-07-24.**
