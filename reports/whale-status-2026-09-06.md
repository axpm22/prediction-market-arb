# Whale lane — status and P&L decomposition (2026-09-06)

Paper-trading measurement instrument, Polymarket whale-consensus lane (L14). Everything below is
read off the append-only ledger and the tape at 2026-09-06 ~09:00Z; nothing here changes a
pre-registered rule. HEAD eeca475.

## Where the money went (all three sizings, every settled row)

| ended by | rows | stake | P&L | reading |
|---|---|---|---|---|
| our own exits (flip + "whale left") | 225 | $130 | −$5.75 | every `left` exit was a false positive (0 of 150 backed by a sell print) — fixed 2026-09-06 (03e8995, eeca475) |
| held to resolution | 154 | $93 | −$14.77 | 54% won at an average entry of 0.57; break-even after fees ≈ 59% |

Held-to-resolution rows split (no selection made on any of these; n is small and the 64k-position
backtest showed every such cut flipping sign between eras):

| cut | rows | P&L / stake | win |
|---|---|---|---|
| V6 dominance | 126 | −8.9% | 0.56 |
| V1 heads | 10 | −26.9% | 0.60 |
| V4 leaderboard | 9 | −41.9% | 0.44 |
| V5 elite | 9 | −70.5% | 0.22 |
| sizing flat / conf / kelly | 58 / 58 / 38 | −14.5% / −19.2% / −8.7% | 0.53 / 0.53 / 0.55 |
| entry < 0.40 | 14 | −100% | 0.00 |
| entry 0.40–0.70 | 107 | −9% to −27% | 0.41–0.53 |
| entry ≥ 0.70 | 33 | +20.9% | 1.00 |
| baseball (MLB) | 85 | −2.6% | 0.54 |
| tennis | 66 | −37.6% | 0.52 |

Independent outcomes behind the 154 rows: about 50 market-outcomes over two days (each signal
books three sizing rows), all before the 2026-09-06 fixes.

## Do we lose everywhere? Live vs the historical backtest (follow any big wallet, hold)

| sport | live 09-04→06 | backtest train (Feb–mid-Jul) | backtest test (mid-Jul–Sep 4) |
|---|---|---|---|
| baseball MLB | −2.6% (85) | −5.1% (1,972) | +2.5% (9,134) |
| tennis | −37.6% (66) | −8.9% (5,294) | +1.0% (15,184) |
| soccer | — | +1.5% (1,826) | +0.8% (12,649) |
| NBA / WNBA | — | −9.1% / −10.2% | −18% (21) / −4.3% (1,025) |
| MMA | +50% (3) | +16.4% (631) | −2.8% (855) |
| NHL | — | −2.1% (447) | — |
| NFL / NCAAF | — | +32% (13) | −2.5% (409) / +17.9% (171) |

Reading: no sport is positive in every era. The backtest's test-window positives are a sampling
artefact (the wallet universe came off a P&L-ranked leaderboard whose windows sit inside that
period; restricting to wallets with pre-June history takes MLB/tennis to ≈0). Soccer is the only
sport near zero in both historical eras, and it has no live outcomes yet. Nothing here is a
strategy; it is the map of where the instrument has and has not lost.

## What changed on 2026-09-05/06 (instrument, not strategy)

- Trades feed was served from a 300 s edge cache; ~40% of prints were dropped and fills seen up
  to 5 min late (f578bce).
- Whale skill is now graded against what the price band itself paid (569bb59, fa2196f):
  qualifying roster 226 → 145 wallets.
- All 150 "whale left" exits were false; exits now need a disappearance or a sell print, on the
  same basis, never during a partial cache (03e8995, eeca475).
- Supervisor outage 07:06–07:41Z (session-bound process); launchd plists in `ops/launchd/`.

## What decides next

- 2026-09-07: day-7 calibration of `min_points.V2/V3` (the excess-weighted variants), proposed
  to the operator as a dated amendment.
- Pre-registered kill rules (fast kill armed) decide the lane's fate at 100 settled V2/flat
  signals; the historical backtest (reports/backtest-whale-follow-2026-09-05.md) found no
  follow configuration favorable, so the working expectation is a kill, followed by a new
  hypothesis rather than a tuned version of this one.
