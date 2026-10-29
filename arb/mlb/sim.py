"""Plate-appearance Monte Carlo for one MLB game (prereg PREREG_MLB_CORR_v1 §5).

simulate(inp, n_sims, n_chunks, seed) -> SimResult. Vectorized across sims: one loop
step = one PA in every unfinished game.

Model:
- Outcomes, fixed order OUTCOMES = (K, BB, 1B, 2B, 3B, HR, OUT); BB includes HBP,
  OUT = out in play.
- Parameter uncertainty: per chunk, every batter / SP / pen rate vector is drawn
  once from Dirichlet(alpha); the chunk's n_sims/n_chunks games use those rates.
- Matchup (odds-ratio / log5): p_k ∝ b_k * q_k / L_k, then 1B/2B/3B/HR x park
  factor, renormalized. Park factors apply to both teams (home park).
- Starter: faces batters until his BF reaches a per-sim limit
  max(3, Poisson(exp_bf)); then the pitching side's pen alpha for the rest of the game.
  sp_k counts only K recorded by the SP.
- Baserunning table (constants below), fixed for every game:
  BB: forces only. 1B: runner on 3rd scores; on 2nd scores w.p. P_1B_2_SCORES else
  holds at 3rd; on 1st goes to 3rd w.p. P_1B_1_TO_3 if 3rd is free, else to 2nd.
  2B: runners on 2nd/3rd score; on 1st scores w.p. P_2B_1_SCORES else to 3rd.
  3B/HR: bases clear. OUT in play: runner on 1st with <2 outs -> double play
  w.p. P_DP (batter + lead-forced runner on 1st out, others hold, no run). Else one
  out; with <2 outs before the PA a runner on 3rd scores (sac fly / groundout)
  w.p. P_SF; then (if the inning is not over) runner on 2nd advances to a free 3rd
  w.p. P_OUT_2_TO_3, and runner on 1st to a free 2nd w.p. P_OUT_1_TO_2.
  Reached on error: an OUT becomes a 1B for baserunning w.p. P_ROE (no H, TB or
  RBI credited). No SB / WP / PB; the advancement rates absorb them.
- RBI: every run scoring on the batter's PA (no runs ever score on a DP here).
- Game: 9 innings; bottom 9 skipped if home leads; walk-off ends the game at once.
  Walk-off on a non-HR PA counts runs only up to the winning run (HR counts all).
  Extras: regular_season -> automatic runner on 2nd every half from the 10th
  (unearned, no RBI to anyone unless driven in); postseason: none.
  Cap: still tied after MAX_INNINGS -> one run added to a coin-flip side (no RBI).

v2 options (PREREG_MLB_CORR_v2; defaults reproduce v1 exactly):
- form_sd=(sp, off): per-sim day-form latents. The SP's z ~ N(0, sp) multiplies the K
  odds by e^z and BB/hit odds by e^-z while he pitches; the batting side's e ~ N(0, off)
  multiplies BB/hit odds by e^e and K odds by e^-e for the whole game; form_mu is
  e's mean (a global run-level correction, calibrated with the two sds). This adds the
  game-level over-dispersion and the K <-> runs coupling the v1 calibration lacked.
- starter_share: per-slot prob the starter (not a sub) takes the slot's 4th+ PA;
  bat_* props count only the starter's PAs (team runs are unaffected).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

OUTCOMES = ("K", "BB", "1B", "2B", "3B", "HR", "OUT")
K, BB, S1, S2, S3, HR, OUT = range(7)
TB = np.array([0, 0, 1, 2, 3, 4, 0])

# Baserunning constants (chosen so 2025-like league-average inputs give ~4.5 R/team/G
# and ~0.7 GIDP/team/G; see tests/test_mlb_sim.py for the calibration check).
P_1B_2_SCORES = 0.65   # runner on 2nd scores on a single
P_1B_1_TO_3 = 0.30     # runner on 1st reaches 3rd on a single (if 3rd free)
P_2B_1_SCORES = 0.45   # runner on 1st scores on a double
P_DP = 0.18            # DP | out in play, runner on 1st, <2 outs
P_SF = 0.60            # runner on 3rd scores on an out in play, <2 outs
P_OUT_2_TO_3 = 0.40    # runner on 2nd advances to 3rd on an out in play
P_OUT_1_TO_2 = 0.30    # runner on 1st advances to 2nd on a non-DP out in play
P_ROE = 0.025          # out in play -> batter reaches on error (~1.2% of PA)
MAX_INNINGS = 20
SP_MIN_BF = 3


@dataclass
class SimResult:
    runs: np.ndarray        # (n, 2) [away, home]
    bat_hr: np.ndarray      # (n, 2, 9) by batting-order slot
    bat_h: np.ndarray
    bat_rbi: np.ndarray
    bat_tb: np.ndarray
    sp_k: np.ndarray        # (n, 2) starter strikeouts
    chunk: np.ndarray       # (n,) chunk id
    innings: np.ndarray     # (n,) innings started (>9 = extra innings)
    side_index: dict = field(default_factory=lambda: {"away": 0, "home": 1})


def _dirichlet(rng, alpha):
    g = rng.standard_gamma(np.asarray(alpha, float))
    return g / g.sum(-1, keepdims=True)


def _matchup_cdf(inp, n_chunks, rng):
    """(C, bat side, slot, pitcher 0=SP 1=pen, 7) cumulative PA-outcome probs."""
    L = np.asarray(inp["league"], float)
    park = np.ones(7)
    for o in ("1B", "2B", "3B", "HR"):
        park[OUTCOMES.index(o)] = inp["park"][o]
    sides = [inp["away"], inp["home"]]
    out = np.empty((n_chunks, 2, 9, 2, 7))
    for c in range(n_chunks):
        bats = [_dirichlet(rng, [b["alpha"] for b in s["lineup"]]) for s in sides]
        pits = [_dirichlet(rng, [s["sp"]["alpha"], s["pen"]["alpha"]]) for s in sides]
        for s in range(2):
            p = bats[s][:, None, :] * pits[1 - s][None, :, :] / L * park
            out[c, s] = p / p.sum(-1, keepdims=True)
    return np.cumsum(out, -1)


# sign of each outcome's log-odds shift under a positive pitcher / offense latent
SGN_P = np.array([1, -1, -1, -1, -1, -1, 0.0])
SGN_O = -SGN_P


def simulate(inp: dict, n_sims: int = 20000, n_chunks: int = 20, seed: int = 0,
             form_sd=(0.0, 0.0), form_mu=0.0, starter_share=None) -> SimResult:
    assert n_sims % n_chunks == 0, "n_sims must be a multiple of n_chunks"
    rng = np.random.default_rng(seed)
    cdf = _matchup_cdf(inp, n_chunks, rng)
    n = n_sims
    chunk = np.repeat(np.arange(n_chunks), n // n_chunks)
    ghost = bool(inp["regular_season"])
    exp_bf = np.array([inp["away"]["sp"]["exp_bf"], inp["home"]["sp"]["exp_bf"]])
    sp_lim = np.maximum(SP_MIN_BF, rng.poisson(exp_bf, size=(n, 2)))
    form = any(form_sd) or form_mu != 0.0
    if form:
        pr = np.diff(cdf, axis=-1, prepend=0.0)
        zp = rng.normal(0, form_sd[0], (n, 2))   # by pitching side
        zo = rng.normal(form_mu, form_sd[1], (n, 2))   # by batting side
    share = None if starter_share is None else np.asarray(starter_share, float)
    slot_pa = np.zeros((n, 2, 9), int)

    runs = np.zeros((n, 2), int)
    bat_hr, bat_h, bat_rbi, bat_tb = (np.zeros((n, 2, 9), int) for _ in range(4))
    sp_k = np.zeros((n, 2), int)
    sp_bf = np.zeros((n, 2), int)         # indexed by pitching side
    slot = np.zeros((n, 2), int)          # next batter, by batting side
    inning = np.ones(n, int)
    half = np.zeros(n, int)               # 0 top (away bats), 1 bottom
    outs = np.zeros(n, int)
    b1, b2, b3 = (np.zeros(n, bool) for _ in range(3))
    live = np.ones(n, bool)

    while live.any():
        a = np.flatnonzero(live)
        h, inn = half[a], inning[a]
        pit = 1 - h
        s = slot[a, h]
        use_pen = (sp_bf[a, pit] >= sp_lim[a, pit]).astype(int)
        u = rng.random(len(a))
        if form:
            lat = np.where(use_pen == 0, zp[a, pit], 0.0)[:, None] * SGN_P + zo[a, h][:, None] * SGN_O
            p = pr[chunk[a], h, s, use_pen] * np.exp(lat)
            c = np.cumsum(p / p.sum(1, keepdims=True), 1)
        else:
            c = cdf[chunk[a], h, s, use_pen]
        o = (u[:, None] > c).sum(1).clip(max=6)
        r1, r2, r3 = rng.random((3, len(a)))
        x1, x2, x3, nout = b1[a], b2[a], b3[a], outs[a]
        roe = (o == OUT) & (r3 >= 1 - P_ROE)   # reached on error: runs like a 1B
        o = np.where(roe, S1, o)

        # --- outcome -> runs scored, new bases, new outs (vectorized table) ---
        n1, n2, n3 = x1.copy(), x2.copy(), x3.copy()
        sc = np.zeros(len(a), int)
        m = o == BB
        sc[m] = (x1 & x2 & x3)[m]
        n3[m] = (x3 | (x1 & x2))[m]
        n2[m] = (x2 | x1)[m]
        n1[m] = True
        m = o == S1
        two_scores = x2 & (r2 < P_1B_2_SCORES)
        third_free = ~(x2 & ~two_scores)
        one_to3 = x1 & third_free & (r1 < P_1B_1_TO_3)
        sc[m] = (x3.astype(int) + two_scores)[m]
        n3[m] = ((x2 & ~two_scores) | one_to3)[m]
        n2[m] = (x1 & ~one_to3)[m]
        n1[m] = True
        m = o == S2
        one_scores = x1 & (r1 < P_2B_1_SCORES)
        sc[m] = (x3.astype(int) + x2 + one_scores)[m]
        n3[m] = (x1 & ~one_scores)[m]
        n2[m], n1[m] = True, False
        m = (o == S3) | (o == HR)
        sc[m] = (x1.astype(int) + x2 + x3 + (o == HR))[m]
        n1[m], n2[m] = False, False
        n3[m] = (o == S3)[m]
        nout = nout + (o == K)
        m = o == OUT
        dp = m & x1 & (outs[a] < 2) & (r1 < P_DP)
        n1[dp] = False
        nout = nout + m + dp
        sf = m & ~dp & x3 & (outs[a] < 2) & (r3 < P_SF)
        sc[sf] += 1
        n3[sf] = False
        adv2 = m & ~dp & x2 & ~n3 & (r2 < P_OUT_2_TO_3) & (nout < 3)
        n3[adv2], n2[adv2] = True, False
        adv1 = m & ~dp & x1 & ~n2 & (r1 >= 1 - P_OUT_1_TO_2) & (nout < 3)
        n2[adv1], n1[adv1] = True, False

        # walk-off on a non-HR: only runs up to the winning run count
        bot_late = (h == 1) & (inn >= 9)
        cap = runs[a, 0] - runs[a, 1] + 1
        sc = np.where(bot_late & (o != HR), np.minimum(sc, cap), sc)

        # --- bookkeeping ---
        runs[a, h] += sc
        slot_pa[a, h, s] += 1
        cr = np.ones(len(a), bool) if share is None else \
            (slot_pa[a, h, s] <= 3) | (rng.random(len(a)) < share[s])
        bat_rbi[a, h, s] += np.where(roe | ~cr, 0, sc)
        bat_hr[a, h, s] += (o == HR) & cr
        bat_h[a, h, s] += (o >= S1) & (o <= HR) & ~roe & cr
        bat_tb[a, h, s] += np.where(roe | ~cr, 0, TB[o])
        sp_k[a, pit] += (o == K) & (use_pen == 0)
        sp_bf[a, pit] += 1
        slot[a, h] = (s + 1) % 9
        b1[a], b2[a], b3[a], outs[a] = n1, n2, n3, nout

        # --- end of half / game ---
        ra, rh = runs[a, 0], runs[a, 1]
        walkoff = bot_late & (rh > ra)
        end_half = (nout >= 3) & ~walkoff
        over = walkoff | (end_half & (h == 0) & (inn >= 9) & (rh > ra)) \
                       | (end_half & (h == 1) & (inn >= 9) & (rh != ra))
        cap_hit = end_half & (h == 1) & (inn >= MAX_INNINGS) & (rh == ra) & ~over
        if cap_hit.any():
            ci = a[cap_hit]
            runs[ci, rng.integers(0, 2, len(ci))] += 1
            over |= cap_hit
        live[a[over]] = False
        nxt = a[end_half & ~over]
        inning[nxt] += half[nxt]
        half[nxt] = 1 - half[nxt]
        outs[nxt] = 0
        b1[nxt] = b3[nxt] = False
        b2[nxt] = ghost & (inning[nxt] >= 10)

    return SimResult(runs, bat_hr, bat_h, bat_rbi, bat_tb, sp_k, chunk, inning)
