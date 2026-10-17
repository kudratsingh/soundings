## Work order
WO-___ — link to `docs/work-orders/WO-___.md`

## What this does
One paragraph.

## Checklist
- [ ] Exit tests from the WO pass locally with `MODEL_MODE=replay` (paste the pytest summary line below)
- [ ] `make check` green
- [ ] Ladder rung(s) touched: watch / ask / slow / hold / hand to a person
- [ ] Hard rules exercised (CLAUDE.md §2): list numbers and the tests that prove them
- [ ] No paid runs, or a line added to `docs/log/PAID-RUNS.md`
- [ ] `docs/STATE.md` updated
- [ ] No new denylist words (`bash scripts/denylist-check.sh`)
- [ ] Screenshot attached if the WO has a manual check

## Test summary
```
<pytest summary line>
```

## Determinism proof (if the WO touches sim, scorer, guards, or eval)
```
<diff of two runs, expected empty>
```
