# Decisions

One line each. Scope changes, threshold changes, cost-model changes. Newest at the bottom.

| when | decision | why | who |
|---|---|---|---|
| 2026-10-16 21:20 | Soundings lives on `ask` and `hand to a person`; slow/hold only under signed standing orders | The brief says a build lives on one or two rungs; these two are where the mechanism (registry + hail) and the handoff live | Kudrat |
| 2026-10-16 21:20 | No separate review pass by default; exit tests + CI are the review. Owner may request one for Phase 3–4 PRs (guards) | 28-hour clock; a full review per PR would cost ~20% of build time; the guard code is the only place a review buys safety | Kudrat |
| 2026-10-16 21:20 | Posterior is naive-Bayes over hand-set likelihood tables, not a learned classifier | Every number is inspectable by a judge and editable by a mentor; a learned model can't be explained in a five-minute pitch | Kudrat |
| 2026-10-16 21:20 | `shutdown_usd_per_min_default: 0` until a mentor gives a figure | We don't invent operational costs; Doyle session Saturday | Kudrat |
| 2026-10-16 21:20 | Detection physics kept to range falloff + noise + gaps + biologic false alarms | The brief says detection is getting easier; sophistication goes into investigation | Kudrat |
