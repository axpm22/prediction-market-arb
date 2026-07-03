# Cowork prompt — audit everything, then write the master improvement plan

Paste everything below this line into a fresh Cowork session with the ArbitrageModel folder
(and the Obsidian vault, if separate) connected.

---

You are doing a full audit-and-planning pass over my prediction-market arbitrage project
(ArbitrageModel). You are NOT writing code in this session. Your deliverable is a
comprehensive plan/guide, plus a set of ready-to-run implementation prompts.

## Step 1 — Read and organize (do this before forming any opinion)

1. Read `CLAUDE.md`, `SPEC.md`, `SPEC_V2.md`, and `MODEL_FOUNDATIONS_FIX_SPEC.md` fully —
   the last one is the current best statement of known weaknesses (six phases, A–F, each
   with a pre-committed decision rule). Treat it as input to improve on, not gospel.
2. Read every other top-level `.md` (there are ~35: audits, weakness sweeps, fix reports,
   preregistrations, build prompts). They are layered — later files amend earlier ones.
   Build a short chronology as you go: what was believed, what broke, what was fixed, what
   was superseded. Flag contradictions between documents; do not silently pick one side.
3. Read `prompts/*.md` — these are queued/completed Claude Code tasks. Determine which have
   actually been implemented (check the code and git log, don't trust the prompt's
   existence) and which are still pending. The difference is your work queue's ground truth.
4. Skim the code (`arb/`, ~40 modules) with the docs as your map — enough to verify claims,
   not a line-by-line review. Verify specifically: which of the 2026-07-02 incident fixes
   (in-play/event-start gating, poll-interleaving dedup, settle tripwire, DB reset/baseline
   cutover from `prompts/incident_recovery_master.md`) are actually in the code and tested.
5. Query `data/arb.sqlite` (and `data/arb-incident-baseline-20260702.sqlite` /
   `data/archive-pre-v2/arb.sqlite` if present) for the empirical state: bets by
   class/sport, settled P&L optimistic vs confirmed, annotation counts, poll cadence
   actuals. Numbers from the DB outrank numbers in any document.
6. If my Obsidian vault is connected, search it for notes on this project (arbitrage,
   Kalshi, Polymarket, prediction markets) and ingest anything relevant — decisions or
   context that never made it into the repo docs. Cite vault notes you used. If no vault
   is reachable, say so once and move on.
7. Research (web) only where the docs are stale or silent — current Kalshi/Polymarket/SX
   fee schedules and API changes since June 2026, settlement-rule pages for the sports in
   scope, and anything Phase B of the foundations spec needs citations for. Cite sources.
   Do not spend Odds API credits; public docs pages only.

## Step 2 — Write the master plan

One document: `MASTER_PLAN.md` at the repo root. Structure:

1. **State of the system** — one page: what works, what's measuring, what's broken, what's
   unknown. Grounded in the DB numbers and the verified-implemented list from Step 1.3–1.5.
2. **Open issues, ranked** — merge MODEL_FOUNDATIONS_FIX_SPEC's phases A–F, unimplemented
   prompt files, contradictions you found, and anything new your audit surfaced. Rank by
   evidentiary leverage (what result would change decisions the most), not effort. For each:
   the assumption at risk, evidence, proposed fix logic, and a pre-committed decision rule
   (if X then Y — keep the foundations spec's discipline).
3. **Improvement opportunities beyond fixes** — where could the model legitimately get
   better (data sources, sports/markets in scope, detection classes, execution modeling)?
   Be skeptical: this project's history is fake edges from data bugs; every proposed
   improvement must state how we'd know it's real (measurement first, betting later).
4. **Sequencing** — dependency-ordered roadmap. What blocks what; what can run in parallel
   worktrees; explicit stop/review gates between stages (the repo's phase discipline).

## Step 3 — Split into implementation prompts

For every work item in the plan, write a self-contained prompt file in `prompts/queue/`,
numbered in execution order (`01-…`, `02-…`). Rules for these prompts — they will be run in
Claude Code by **Opus 4.8**, so calibrate accordingly:

- Assume a strong implementer: state WHAT and WHY, constraints, and acceptance gates; do
  not micro-specify code. But NEVER leave safety-critical details implicit — hard rules
  (paper only, Decimal money, append-only ledgers, no invented endpoints, fixtures-only
  tests, credit budgets) are restated in every prompt, verbatim where they bind.
- Each prompt: context (with the incident history it needs), the task, tests required,
  acceptance commands whose output must be shown, explicit out-of-scope list, and a gate
  ("STOP for review after this" where the phase discipline demands it).
- Each prompt must be runnable cold — no reliance on chat context, only on files in the repo.
- If any prompt would be executed by a model other than Opus 4.8 (e.g. Haiku for runtime
  classification tasks inside the product, or a smaller model for a mechanical migration),
  say so explicitly at the top of that prompt and adjust: more explicit steps, tighter
  guardrails, smaller scope per prompt.
- End with `prompts/queue/README.md`: the run order, what gates sit between prompts, and
  which prompts are parallelizable in separate worktrees.

## Ground rules for the whole session

- Evidence over documents; documents over memory. Where you can check the DB or code, check.
- No code changes, no config changes, no process restarts, and absolutely nothing that
  spends Odds API credits or places paper bets. If the stop flag (`data/stop-all`) is
  present, leave it.
- Where you're uncertain whether something was fixed, say "unverified" — this codebase has
  already been burned twice by believing a fix was live when the running process predated it.
- Ask me before finalizing only if you hit a genuine fork (e.g. two contradictory
  preregistration documents and the choice changes the plan). Otherwise proceed and note
  assumptions inline.
