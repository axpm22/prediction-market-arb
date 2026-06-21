#!/bin/bash
# One-shot status across every bet class + EV CLV verdict + health checks.
# Usage: ./status.sh          (full report)
#        ./status.sh 414      (only count bets after baseline id 414, e.g. a test window)
cd "$(dirname "$0")"
BASE="${1:-0}"
PY=.venv/bin/python

echo "================ ARBITRAGE MODEL STATUS  ($(date -u +%H:%M:%SZ)) ================"

# Health: exactly one of each process, no duplicates (duplicates = double-counted bets)
echo "--- processes (each should appear once) ---"
for p in "arb daemon" "arbnext focus" "arbnext verify" "arbnext hot" "arbnext political"; do
  n=$(ps -Ao command | grep -- "$p" | grep -v grep | grep -v caffeinate | wc -l | tr -d ' ')
  flag=""; [ "$n" -gt 1 ] && flag="  <-- DUPLICATE, double-counting!"
  [ "$n" -eq 0 ] && flag="  <-- not running"
  printf "  %-18s x%s%s\n" "$p" "$n" "$flag"
done

sqlite3 data/arb.sqlite "
SELECT '';
SELECT '--- ARB (flat \$1/bet) ---';
SELECT '  flagged OPEN:    ' || COUNT(*) || '   sum edge \$' || COALESCE(ROUND(SUM(CAST(edge AS REAL)),4),0)
  FROM flat_bets WHERE kind='ARB' AND status='OPEN' AND id>$BASE;
SELECT '  VERIFIED:        ' || COUNT(*) || '   sum edge \$' || COALESCE(ROUND(SUM(CAST(edge AS REAL)),4),0)
  FROM flat_bets WHERE kind='ARB' AND status='VERIFIED' AND id>$BASE;
SELECT '  VANISHED:        ' || COUNT(*) || '   (died on re-quote)' FROM flat_bets WHERE status='VANISHED' AND id>$BASE;
SELECT '  UNVERIFIABLE:    ' || COUNT(*) || '   (SX legs, cannot confirm)' FROM flat_bets WHERE status='UNVERIFIABLE' AND id>$BASE;
SELECT '  hot lifetimes:   ' || COUNT(*) || '   (1s-resolution survival tracks)' FROM flat_bets WHERE status='LIFETIME' AND id>$BASE;
SELECT '';
SELECT '--- POLITICAL (intra-market YES+NO<\$1, trusted) ---';
SELECT '  open arbs:       ' || COUNT(*) FROM flat_bets WHERE category IN ('politics','economics','climate') AND status='OPEN';
SELECT '';
SELECT '--- EV (MEASUREMENT ONLY - never bet) ---';
SELECT '  flags recorded:  ' || COUNT(*) FROM flat_bets WHERE kind='EV';
SELECT '  below e_min:     ' || COUNT(*) FROM flat_bets WHERE kind='EV' AND notes LIKE '%below-e-min%';
SELECT '';
SELECT '--- DIVERGENCE (logged, \$0 stake) ---';
SELECT '  open:            ' || COUNT(*) FROM flat_bets WHERE kind='DIVERGENCE' AND status='OPEN';
SELECT '';
SELECT '--- SETTLED (realized P&L) ---';
SELECT '  ' || result || ': ' || COUNT(*) || '  \$' || COALESCE(ROUND(SUM(CAST(pnl_optimistic AS REAL)),2),0)
  FROM flat_bets WHERE status='SETTLED' GROUP BY result;
SELECT '';
SELECT '--- ARCHIVE / HEALTH ---';
SELECT '  quotes: ' || (SELECT COUNT(*) FROM quotes) || '   polls: ' || (SELECT COUNT(*) FROM polls)
       || '   quarantined matches: ' || (SELECT COUNT(*) FROM suspect_matches);
SELECT '  last scan credits remaining: ' || COALESCE((SELECT credits_remaining FROM polls WHERE kind='scan' AND credits_remaining IS NOT NULL ORDER BY id DESC LIMIT 1),'n/a');
"

echo ""
echo "--- EV THESIS (CLV verdict) ---"
$PY -m arbnext.ev.clv --min-depth 50 2>&1 | sed 's/^/  /'
echo "=============================================================================="
