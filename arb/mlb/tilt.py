"""Exponential tilting of sim weights to market anchors (prereg PREREG_MLB_CORR_v1 §5).

w_i ∝ exp(θ_a·R_away,i + θ_h·R_home,i); θ minimizes Σ_j (Σ_i w_i·ind_ij − mid_j)²,
|θ| ≤ 1. Solved with damped Gauss-Newton (Levenberg-Marquardt) in numpy; the
Jacobian is the weighted covariance of each anchor indicator with the runs. With
fewer anchors than parameters LM's damping picks a small-step solution (not unique).
With `chunk`, everything is done separately inside each chunk (per-chunk tilt) and
weights are normalized to sum to 1 within each chunk.

interval(): fair = mean of the chunk estimates; lo/hi = 5th/95th percentile of the
chunk estimates ∓ 1/√ESS, ESS = Σ over chunks of (Σw)²/Σw² (Kish); clipped to [0, 1].
"""
from __future__ import annotations

import numpy as np

BOUND = 1.0


def _fit_one(R, A, mids, iters=100, tol=1e-10):
    """R (n,2) centred runs, A (n,J) float indicators -> (w (n,), θ (2,))."""
    th, lam = np.zeros(2), 1e-3
    w = np.full(len(R), 1 / len(R))
    if A.shape[1] == 0:
        return w, th

    def state(t):
        z = R @ t
        e = np.exp(z - z.max())
        w = e / e.sum()
        p = w @ A
        return w, p, np.sum((p - mids) ** 2)

    w, p, loss = state(th)
    for _ in range(iters):
        if loss < tol:
            break
        Rbar = w @ R
        Jac = (A * w[:, None]).T @ R - np.outer(p, Rbar)        # (J, 2)
        H, g = Jac.T @ Jac, Jac.T @ (p - mids)
        while True:
            step = np.linalg.solve(H + lam * np.diag(np.diag(H) + 1e-12), -g)
            t_new = np.clip(th + step, -BOUND, BOUND)
            w2, p2, l2 = state(t_new)
            if l2 < loss:
                th, w, p, loss, lam = t_new, w2, p2, l2, max(lam / 10, 1e-9)
                break
            lam *= 10
            if lam > 1e10:
                return w, th
    return w, th


def fit_tilt(runs, anchors, chunk=None):
    """runs (n,2) [away, home]; anchors [(bool ind (n,), mid)]; chunk (n,) or None.
    Returns (w (n,), theta, ess): theta (2,) [θ_a, θ_h] and scalar ess without
    chunks; (C,2) thetas and (C,) per-chunk ess with chunks."""
    runs = np.asarray(runs, float)
    A = np.column_stack([np.asarray(i, float) for i, _ in anchors]) if anchors \
        else np.zeros((len(runs), 0))
    mids = np.array([m for _, m in anchors], float)
    groups = [np.arange(len(runs))] if chunk is None else \
        [np.flatnonzero(chunk == c) for c in np.unique(chunk)]
    w, thetas, ess = np.empty(len(runs)), [], []
    for g in groups:
        R = runs[g] - runs[g].mean(0)                           # centring: θ unchanged
        w[g], th = _fit_one(R, A[g], mids)
        thetas.append(th)
        ess.append(1 / np.sum(w[g] ** 2))
    if chunk is None:
        return w, thetas[0], ess[0]
    return w, np.array(thetas), np.array(ess)


def prob(ind, w, chunk):
    """-> (mean over chunks, p5, p95 of chunk estimates, mc_se = 1/√ESS)."""
    ind, w = np.asarray(ind, float), np.asarray(w, float)
    chunk = np.zeros(len(w), int) if chunk is None else np.asarray(chunk)
    est, ess = [], 0.0
    for c in np.unique(chunk):
        m = chunk == c
        wc = w[m] / w[m].sum()
        est.append(wc @ ind[m])
        ess += 1 / np.sum(wc ** 2)
    est = np.array(est)
    return est.mean(), np.percentile(est, 5), np.percentile(est, 95), 1 / np.sqrt(ess)


def interval(ind, w, chunk):
    """(fair, lo, hi) per prereg §5: [p5 − 1/√ESS, p95 + 1/√ESS] of chunk estimates."""
    fair, p5, p95, se = prob(ind, w, chunk)
    return fair, max(0.0, p5 - se), min(1.0, p95 + se)
