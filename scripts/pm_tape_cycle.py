"""One bounded PM-tape recording session. Prints a JSON line; exit 2 = dead feed.

Exists because pm_tape.record() has NO reconnect: arb/pm_tape.py breaks out of
its receive loop on any socket drop and returns NORMALLY. A single 3-day call
would therefore end at the first disconnect and report success, which is the
worst kind of failure — you would believe you had three days and have forty
minutes. Resilience lives in the caller, and a cycle is the unit of restart.

Re-resolving contested_tokens() every cycle matters just as much: markets
resolve, and a book pinned at 0.002/0.998 cannot produce a maker fill, so a
subscription fixed at t=0 decays into recording dead weight.
"""
import json
import sys
import time

from arb import pm_tape
from arb.config import load


def main() -> int:
    secs = int(sys.argv[1]) if len(sys.argv) > 1 else 3600
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "want_secs": secs}
    t0 = time.time()
    cfg = None
    try:
        cfg = load("config.yaml")
        tokens = pm_tape.contested_tokens(cfg)
        rec["tokens"] = len(tokens)
        if not tokens:
            rec["error"] = "no contested tokens"
        else:
            rec.update(pm_tape.record(cfg, tokens, secs))
    except Exception as e:  # noqa: BLE001 — a bad cycle must not end the run
        rec["error"] = f"{type(e).__name__}: {e}"
    rec["actual_secs"] = round(time.time() - t0, 1)
    # A cycle that returns far early is a DROP, not a clean session. Naming it
    # here is what makes truncation visible in the log instead of invisible.
    rec["short"] = rec["actual_secs"] < secs * 0.9

    # A cycle that runs its FULL duration can still have recorded nothing: the
    # socket opens, delivers one book snapshot (~1 frame per token), then goes
    # silent — which is what a resumed-from-sleep zombie socket looks like.
    # `short` cannot see it, so on 2026-09-02 three consecutive 1800s cycles
    # reported healthy while capturing 64 frames and 0 trades. Rate is the
    # signal that separates them; the caller must not credit a dead cycle.
    rate = rec.get("frames", 0) / max(rec["actual_secs"], 1e-9)
    rec["frames_per_sec"] = round(rate, 2)
    floor = float(cfg["maker"]["min_frames_per_sec"]) if cfg else 1.0
    rec["dead"] = "error" not in rec and rate < floor
    print(json.dumps(rec), flush=True)
    return 2 if rec["dead"] else 0


if __name__ == "__main__":
    sys.exit(main())
