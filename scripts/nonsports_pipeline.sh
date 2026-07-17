#!/usr/bin/env bash
# =============================================================================
# nonsports_pipeline.sh — the politics / econ / weather paper-bet pipeline
# =============================================================================
# One entry point for the whole non-sports program (SPEC §12, plan Phases 1-2),
# in dependency order. Every `arb` command invoked here is idempotent by design
# (§14.1: "every command is safe to run twice"), so re-running this script is
# always safe. Paper trading only (hard rule 1); nothing here places orders.
#
#   Stage 0  Preflight     git/venv/tests sanity + per-venue reachability probe,
#                          including detection of the ANJ regulator DNS block
#                          on Polymarket (first seen 2026-07-16).
#   Stage 1  Matcher       `arb nonsports` — deterministic blocking-key matcher
#                          (concept ∩ date-window ∩ strike agreement, NO fuzzy
#                          text). Writes data/nonsports_candidates.yaml.
#                          Append-only merge: curation verdicts are never
#                          touched. Needs BOTH venues; skipped while PM blocked.
#   Stage 2  Curation      status of the human queue (rule 11: manual-FIRST).
#                          Humans adjudicate per data/nonsports_curation_protocol.md;
#                          confirmed pairs enter data/nonsports_map.yaml by hand.
#   Stage 3  Haiku         `arb nonsports --eval` — the AI matcher (rule 11:
#                          Haiku-SECOND). v3 architecture: proposition SHAPE is
#                          decided in code (match.shape_relation); Haiku answers
#                          only same-source/series/entity/period, via structured
#                          output (json_schema), verdicts cached append-only in
#                          the `verdicts` table keyed (pair, prompt-sha, model),
#                          spend capped by ai_matcher.max_new_pairs_per_day.
#                          Cached pairs cost $0. Prints the §18.5 precision
#                          score vs human ground truth. `--sweep` additionally
#                          classifies every uncached candidate.
#                          NOTE ai_matcher.enabled stays FALSE until the
#                          pre-registered §18.5 bar passes on POST-freeze pairs
#                          (precision >= 0.95, zero FP, >= 80 verdicted incl.
#                          >= 50 same). Flipping it is a dated PREREGISTRATION
#                          amendment, never a script action.
#   Stage 4  Lanes         one poll of each nonsports lane, ONLY if the
#                          supervisor is not already running them (rule 13:
#                          loops belong to the supervisor):
#                            political    PM-only intra-market YES+NO<1 (locked)
#                            negrisk-scan mutually-exclusive baskets, both venues
#                            mapped       adjudicated cross-venue pairs from
#                                         nonsports_map.yaml (XSPREAD/REVIEW)
#   Stage 5  Report        `arb status` + today's daily report.
#
# Usage:
#   scripts/nonsports_pipeline.sh              # normal run
#   scripts/nonsports_pipeline.sh --sweep      # + Haiku sweep of uncached pairs
#   scripts/nonsports_pipeline.sh --skip-lanes # never poll lanes (report only)
#
# Requires: .venv with the project installed; ANTHROPIC_API_KEY in .env only
# if uncached candidates exist (otherwise the Haiku stage is a cache read).
# The Odds API is NEVER touched here (hard rule 2 — nonsports uses Kalshi and
# Polymarket public endpoints exclusively).
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
SWEEP=0; SKIP_LANES=0
for a in "$@"; do
  case "$a" in
    --sweep) SWEEP=1 ;;
    --skip-lanes) SKIP_LANES=1 ;;
    *) echo "unknown flag: $a" >&2; exit 2 ;;
  esac
done

say() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

# ---------------------------------------------------------------- Stage 0 ----
say "Stage 0: preflight"
[ -x "$PY" ] || { echo "FATAL: .venv missing — python -m venv .venv && pip install -e ."; exit 1; }
git diff --quiet && git diff --cached --quiet \
  || echo "WARN: dirty git tree — fine for a batch run, but loops only ever run committed HEAD (rule 13)"
"$PY" -m pytest -q >/dev/null 2>&1 \
  || { echo "FATAL: pytest not green — fix before running anything live (rule 13)"; exit 1; }
echo "pytest: green"

# Venue probes. Kalshi: expect HTTP 200. Polymarket: since 2026-07-16 French
# ISP resolvers poison gamma-api/clob.polymarket.com to the ANJ (Autorité
# Nationale des Jeux) block server, whose TLS cert is CN=*.anj.fr — every PM
# fetch then dies with CERTIFICATE_VERIFY_FAILED. That is a regulator block at
# the network layer, not a code bug: detect it, say so once, and degrade to
# Kalshi-only rather than retry-storm.
KALSHI_OK=0; PM_OK=0
# probe uses the verified events listing (data/verified_facts.md 2026-07-14),
# limit=1 — rule 3 forbids inventing a status endpoint
[ "$(curl -s -o /dev/null -w '%{http_code}' -m 10 \
     'https://api.elections.kalshi.com/trade-api/v2/events?status=open&limit=1')" = 200 ] \
  && KALSHI_OK=1 || echo "WARN: kalshi unreachable — kalshi stages will fail this run"
PM_CN=$(echo | openssl s_client -connect gamma-api.polymarket.com:443 \
        -servername gamma-api.polymarket.com 2>/dev/null \
        | openssl x509 -noout -subject 2>/dev/null || true)
if [ "$(curl -s -o /dev/null -w '%{http_code}' -m 10 \
       'https://gamma-api.polymarket.com/markets?limit=1')" = 200 ]; then
  PM_OK=1
elif echo "$PM_CN" | grep -q 'anj\.fr'; then
  echo "BLOCKED: Polymarket is DNS-poisoned to the ANJ block server ($PM_CN)."
  echo "         PM-dependent stages (matcher, political lane, mapped PM legs)"
  echo "         are skipped. This is a regulatory/network condition — code"
  echo "         cannot fix it; see reports/nonsports-pipeline-review for options."
else
  echo "WARN: Polymarket unreachable (cert: ${PM_CN:-none}) — PM stages skipped"
fi
echo "venues: kalshi=$KALSHI_OK polymarket=$PM_OK"

# ---------------------------------------------------------------- Stage 1 ----
say "Stage 1: blocking-key matcher -> candidates"
if [ "$PM_OK" = 1 ] && [ "$KALSHI_OK" = 1 ]; then
  "$PY" -m arb nonsports          # writes/merges data/nonsports_candidates.yaml
else
  echo "skipped: needs both venues (kalshi=$KALSHI_OK pm=$PM_OK)"
fi

# ---------------------------------------------------------------- Stage 2 ----
say "Stage 2: human curation queue (rule 11: manual-first)"
TOTAL=$(grep -c '^[^ ]' data/nonsports_candidates.yaml || echo 0)
CURATED=$(grep -cE '^  verdict:' data/nonsports_candidates.yaml || echo 0)
echo "candidates on file: $TOTAL; human-verdicted: $CURATED; awaiting: $((TOTAL - CURATED))"
echo "protocol: data/nonsports_curation_protocol.md -> confirmed pairs go to data/nonsports_map.yaml"

# ---------------------------------------------------------------- Stage 3 ----
say "Stage 3: Haiku matcher (rule 11: Haiku-second) + §18.5 score"
# eval mode classifies curated-but-uncached pairs (cached = free) and prints
# precision vs human ground truth — the evidence that would one day flip
# ai_matcher.enabled via a dated amendment. Runs fine while enabled=false.
"$PY" -m arb nonsports --eval
[ "$SWEEP" = 1 ] && "$PY" -m arb nonsports --sweep

# ---------------------------------------------------------------- Stage 4 ----
say "Stage 4: lane polls"
if [ "$SKIP_LANES" = 1 ]; then
  echo "skipped (--skip-lanes)"
elif pgrep -f "arb supervisor" >/dev/null 2>&1; then
  echo "skipped: supervisor is running and already owns these loops (rule 13);"
  echo "         config supervisor.processes has political/negrisk/mapped enabled"
else
  [ "$PM_OK" = 1 ] && "$PY" -m arb political --once \
    || echo "political: skipped (PM-only lane, PM blocked)"
  [ "$KALSHI_OK" = 1 ] && "$PY" -m arb negrisk-scan --once \
    || echo "negrisk: skipped (kalshi down)"
  [ "$KALSHI_OK" = 1 ] && "$PY" -m arb mapped --once \
    || echo "mapped: skipped (kalshi down)"   # PM legs degrade per-pair on their own
fi

# ---------------------------------------------------------------- Stage 5 ----
say "Stage 5: status + daily report"
"$PY" -m arb status || true
"$PY" -m arb report --daily "$(date +%F)" || true
say "pipeline done"
