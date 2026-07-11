-- Independent recompute of THE headline number (SPEC §14.1): settled P&L for
-- trusted + sports + pregame + clean bets, optimistic basis, gross of carry.
-- Imports nothing from arb/; CI asserts it matches metrics.py on a fixture DB.
WITH costs AS (
  SELECT fb.id,
         SUM(CAST(json_extract(l.value, '$.cost') AS REAL)
             + COALESCE(CAST(json_extract(l.value, '$.fee_cost') AS REAL), 0)) AS c
  FROM flat_bets fb, json_each(fb.legs_json) l
  GROUP BY fb.id
),
settled AS (
  SELECT bet_id, MIN(payload_json) AS payload
  FROM bet_events WHERE kind = 'settled' GROUP BY bet_id
),
excluded AS (
  SELECT DISTINCT CAST(target_id AS INTEGER) AS bet_id FROM annotations
  WHERE target_kind = 'bet' AND kind IN ('settle-tripwire', 'INVALID_AFTER_FINAL')
),
pnl AS (
  SELECT fb.id,
         CASE
           WHEN fb.pnl_optimistic IS NOT NULL THEN CAST(fb.pnl_optimistic AS REAL)
           WHEN json_extract(s.payload, '$.fraction') IS NOT NULL THEN
             CAST(fb.stake_usd AS REAL)
               * (CAST(json_extract(s.payload, '$.fraction') AS REAL) / c.c - 1)
           -- per-leg payoff sum (SPEC §7.2), matching metrics.bet_pnl_optimistic;
           -- subsumes single-leg. contracts = stake/c, equal counts per leg.
           ELSE (
             SELECT COALESCE(SUM(CASE r.value
               WHEN 'WON' THEN (CAST(fb.stake_usd AS REAL) / c.c)
                 * (1 - (CAST(json_extract(fb.legs_json, '$[' || r.key || '].cost') AS REAL)
                        + COALESCE(CAST(json_extract(fb.legs_json,
                            '$[' || r.key || '].fee_cost') AS REAL), 0)))
               WHEN 'LOST' THEN -(CAST(fb.stake_usd AS REAL) / c.c)
                 * (CAST(json_extract(fb.legs_json, '$[' || r.key || '].cost') AS REAL)
                    + COALESCE(CAST(json_extract(fb.legs_json,
                        '$[' || r.key || '].fee_cost') AS REAL), 0))
               ELSE 0 END), 0)
             FROM json_each(json_extract(s.payload, '$.per_leg')) r)
         END AS pnl
  FROM flat_bets fb
  JOIN settled s ON s.bet_id = fb.id
  JOIN costs c ON c.id = fb.id
  WHERE fb.trusted = 1 AND fb.category = 'sports' AND fb.lane = 'clean'
    AND (fb.event_start_utc IS NULL OR fb.created_at < fb.event_start_utc)
    AND fb.id NOT IN (SELECT bet_id FROM excluded)
    -- a leg still unresolved -> whole bet PENDING, never a partial sum
    -- (matches metrics.bet_pnl_optimistic; review 2026-07-10 D4)
    AND (fb.pnl_optimistic IS NOT NULL
         OR json_extract(s.payload, '$.fraction') IS NOT NULL
         OR NOT EXISTS (
           SELECT 1 FROM json_each(json_extract(s.payload, '$.per_leg')) r
           WHERE r.value NOT IN ('WON', 'LOST', 'VOID')))
)
SELECT ROUND(COALESCE(SUM(pnl), 0), 6) AS headline_pnl, COUNT(*) AS n_bets FROM pnl;
