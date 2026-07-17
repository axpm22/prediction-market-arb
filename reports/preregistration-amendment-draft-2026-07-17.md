# DRAFT — PREREGISTRATION amendment: PM/ANJ outage window (operator signs and commits)

Drafted by agent 2026-07-17 per PROMPT_OPS_SPEED_FIXES operator checklist #2.
Not effective until the operator appends the row below to the Amendment log in
`PREREGISTRATION.md` and commits it. Evidence: reports/nonsports-pipeline-review-2026-07-17.md.

## Proposed Amendment-log row

| Date | Change | Reason |
|---|---|---|
| 2026-07-17 | **PM/ANJ outage window declared, from 2026-07-16 until the block lifts (end date to be recorded in a follow-up row).** `gamma-api.polymarket.com` and `clob.polymarket.com` resolve via the ISP resolver to 145.239.225.117 serving a `CN=*.anj.fr` certificate (Autorité Nationale des Jeux block server); every PM fetch fails TLS (225 failures 07-16, 277 on 07-17). For the duration of the window: (1) days inside the window are EXCLUDED from the 3-week lane kill-gates and flag-rate metrics for the `political`, `mapped`, and PM-side `negrisk` lanes — a venue that cannot be quoted is not evidence about the strategy; Kalshi-only lanes (`kalshi_events` baskets, LIVE, sports) are unaffected and keep accumulating normally. (2) The §18.5 matcher evidence stream is frozen in place — no new post-freeze candidate pairs can be curated while the matcher lacks the PM side; the ≥50-positives clock simply pauses, no bar is changed. (3) Open bet `flat_bets id 42` (NEGRISK, `nonsports:pm:101856`, opened 2026-07-12) cannot be reconciled against PM and will hit `reconcile_give_up_days: 14` → UNRESOLVABLE around 2026-07-30; the operator must decide BEFORE the timer fires: (a) let it record UNRESOLVABLE (honest instrument reading — the venue was unreachable), or (b) manually settle from a court-of-record source per the settlement protocol, with an annotation naming the source. Decision to be recorded here when made. No threshold, gate bar, or hypothesis changes; this row only marks which days the gates may judge | Regulatory/network outage outside the instrument; excluding blocked-venue days keeps kill-gates from scoring a dead feed as strategy failure |

## Operator sign-off

- [ ] Wording reviewed (edit freely — the substance to keep: window start
      2026-07-16, gate-day exclusion for PM-dependent lanes, §18.5 clock pause,
      the bet-42 decision deadline before ~2026-07-30)
- [ ] Row appended to PREREGISTRATION.md Amendment log
- [ ] This draft file deleted or marked superseded in the same commit
