# Learning → Strategy Bridge (human-in-the-loop)

## Conclusion first

The strategy learning lab proposes improvements but, by design, never edits live
parameters. This bridge adds the **missing last segment**: a human approves an
eligible candidate, which is written as a small, reversible, audited override
that strategies pick up on the next config load — and **only** when the operator
explicitly turns it on (`STRATEGY_OVERRIDES_ENABLED=true`, default off). It
closes the loop without weakening the anti-overfitting safety design.

## The problem it solves

Before this bridge:

- `stock_learning.py` produced `runtime/strategy_upgrade_candidates.jsonl` and
  `runtime/strategy_promotion_report.json`.
- Nothing read those back. `config.load_settings()` read only `.env`.
- So a learned insight (e.g. *"raise min order value 500 → 750 because 54 trades
  were fee-dominated and net P&L was −423"*) stayed as a suggestion forever
  unless the operator hand-edited `.env`.

The loop was analytically complete but operationally open.

## Design

```
candidate (param + proposed_value, paper_allowed=true)
        │  human runs `promote` and supplies their name
        ▼
runtime/promoted_overrides.json   # audited, reversible
        │  config.load_settings(), only if STRATEGY_OVERRIDES_ENABLED=true
        ▼
Settings (frozen dataclass)  ──►  strategies / auto-trader
```

`src/taa_futu/strategy_overrides.py` implements four operations:

| Operation | What it does |
| --- | --- |
| `promote_candidate(id, approved_by=…)` | Validates the candidate is paper-eligible and whitelisted, then records an override with previous value, approver, timestamp and an evidence digest. |
| `apply_promoted_overrides(settings)` | Returns a copy of `Settings` with whitelisted overrides applied (via `dataclasses.replace`). Generic and side-effect free. |
| `revert_override(candidate_id=… / field=…)` | Removes an override. |
| CLI `status` / `show` | Inspect what is active and whether it is enabled. |

## Safety guarantees

1. **Whitelist only.** `OVERRIDABLE_FIELDS` lists strategy / risk threshold knobs
   only. A `FORBIDDEN_SUBSTRINGS` check additionally blocks anything resembling a
   real-money switch, credential, account id or connection setting
   (`enable_real`, `allow_auto_real`, `unlock`, `password`, `api_key`,
   `api_secret`, `acc_id`, `host`, `port`, `trd_env`, …). Even a tampered
   override file cannot reach a safety switch through `apply`.
2. **Paper gate.** `promote_candidate` refuses unless the promotion report marks
   the candidate `paper_allowed`. Live promotion is never automated here.
3. **Opt-in.** With `STRATEGY_OVERRIDES_ENABLED` unset/false (default), config
   loading is byte-for-byte unchanged.
4. **Fail-safe.** In `load_settings`, the overlay is re-validated and wrapped in
   `try/except`; any error falls back to the base `.env` settings, so a bad
   override file can never break config loading.
5. **Reversible + audited.** Every override records its source candidate id,
   previous value, approver, timestamp and a SHA-256 evidence digest.

## Usage

```bash
# 1. (re)build learning evidence and candidates
.venv/bin/taa-futu stock-learning-build

# 2. see what could be promoted
python -m taa_futu.strategy_overrides status

# 3. approve one eligible candidate (records who approved it)
python -m taa_futu.strategy_overrides promote \
    --candidate-id 676feb79cf00222ee254 --approved-by jiao

# 4. activate overrides (default is off)
#    add to .env:  STRATEGY_OVERRIDES_ENABLED=true
#    then restart the engine

# inspect / undo
python -m taa_futu.strategy_overrides show
python -m taa_futu.strategy_overrides revert --candidate-id 676feb79cf00222ee254
```

Advisory-only candidates (e.g. `review_universe_symbol`, which carry no
`param`/`proposed_value`) are intentionally **not** promotable to a numeric
override; they remain research notes to test in replay.

## Override file schema (`runtime/promoted_overrides.json`, git-ignored)

```json
{
  "schema_version": 1,
  "updated_at": "2026-06-02T...Z",
  "overrides": {
    "auto_trader_min_order_value_usd": {
      "field": "auto_trader_min_order_value_usd",
      "param": "AUTO_TRADER_MIN_ORDER_VALUE_USD",
      "value": 750.0,
      "previous_value": 500.0,
      "source_candidate_id": "676feb79cf00222ee254",
      "action_type": "raise_min_order_value",
      "rationale": "多笔交易被费用主导，建议提高最小订单金额。",
      "scope": "paper",
      "approved_by": "jiao",
      "approved_at": "2026-06-02T...Z",
      "evidence_digest": "…"
    }
  }
}
```

## Tests

`tests/test_strategy_overrides.py` (14 cases) covers the whitelist guard,
promotion eligibility, advisory-only refusal, type coercion, apply/ignore
behavior, revert, a promote→apply round trip, and a guarded integration test
against the real `Settings` dataclass. The `config.load_settings` overlay path is
additionally covered by an end-to-end check (flag off → unchanged; flag on →
applied, safety switch untouched).

## Status & next steps

- Implemented and tested: promote / apply / revert / inspect, wired into
  `load_settings` behind a default-off flag.
- Not yet done (deliberately): a `taa-futu stock-learning-promote` subcommand in
  the main CLI (today it is `python -m taa_futu.strategy_overrides`); a
  paper-canary state machine; and richer per-strategy mapping for
  `tighten_entry_threshold` candidates.
