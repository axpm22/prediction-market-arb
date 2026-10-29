# PREREG_MLB_CORR_v2: amendment to v1 (frozen 2026-10-29 ET, before the confirmation file is read)

v1 (`PREREG_MLB_CORR_v1.md`, sha256 c2f1b6ac…a04431) stands except for the changes below. **Primary confirmation scoring uses v2.** v1 is also run on the confirmation set and reported as secondary; it is not gating.

## What was seen before this freeze
- **§8 outcome calibration** (`calibration.json`, 1,964 discovery-range games, no prices). The v1 simulator ran hot on run level and hits, e.g. HIT 1+ 0.640 predicted vs 0.607 observed. It also under-captured the SP-K ↔ opponent-runs link.
- **v2 grid fit** (`fit_v2.json`). This is outcome-only Brier on 300 discovery-range games.
- **Discovery prices:** the first 6 discovery games (pipeline debugging), 22 signals and 4 fills. These are too few to inform any choice. Every change below comes from the outcome calibration, not from P&L.

## Changes
1. **Simulator** (`config.yaml mlb.sim_v2`):
   - `form_mu = -0.05`. This is a mean shift of the batting side's log-odds on BB and hits (K the other way): a global run-level correction.
   - `starter_share` per slot: the probability that the starter, not a substitute, takes the slot's 4th and later PA. It comes from 2026-04-01..08-29 boxscores, where the starter's share of slot PAs runs from 0.970 (slot 1) to 0.918 (slot 9). Props credit only the starter's PAs.
   - The day-form latents (`form_sd`) were tested and add no Brier skill, so they stay at 0. The under-captured K↔runs coupling therefore remains and is disclosed as a known model weakness for KS props.
2. **§5 interval, clarified (no change of intent):** the "±1/√(eff. n)" term uses the ESS of the tilted weights summed over all 20 chunks.
3. **Discovery set:** discovery candles arrive at about 9 games/hour in a fixed random game order. Discovery is descriptive only. REPORT.md gives the n available when the confirmation file is opened, and any later discovery games are reported separately.
4. **Code bug fixed** (not a rule change): `lane.spec` read a NaN `team` field as truthy for props, so no prop matched a team. Found on the first discovery game.
5. **Null N1 in discovery:** the random discovery draw has few same-date games, so N1 is only informative on the confirmation set, where every game on a date is present.

Everything else is unchanged: universe, exclusions, decision time, signal rule, fill proxy, fees, scoring, k = 3, verdict ceiling SOFT.
