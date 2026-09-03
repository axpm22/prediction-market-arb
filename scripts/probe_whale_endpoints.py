"""L14 Task 1 — Rule-3 verification probes for the whale-consensus data
sources (design doc §4). One-shot script: hits each endpoint listed in the
design's data-sources table exactly as specified, saves real recorded
responses as fixtures under tests/fixtures/, and prints a markdown findings
table. Never imported by anything under arb/ — read-only exploration tool.

Usage: .venv/bin/python scripts/probe_whale_endpoints.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
DB = ROOT / "data" / "arb.sqlite"

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
TIMEOUT = 20
PACE_SECS = 1.0

rows = []  # markdown table rows: dict(probe, url, status, ms, finding)
_last_call = 0.0


def pace():
    global _last_call
    wait = PACE_SECS - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()


def call(url, params=None):
    """Single GET, paced >=1s apart, TIMEOUT-bounded. Returns
    (status_or_None, elapsed_ms, json_body_or_None, full_url, error_or_None)."""
    pace()
    t0 = time.monotonic()
    try:
        r = requests.get(url, params=params, timeout=TIMEOUT)
        ms = int((time.monotonic() - t0) * 1000)
        try:
            body = r.json()
        except ValueError:
            body = None
        print(f"  -> {r.status_code} {ms}ms {r.url}", file=sys.stderr)
        return r.status_code, ms, body, r.url, None
    except requests.RequestException as e:
        ms = int((time.monotonic() - t0) * 1000)
        print(f"  -> ERROR {ms}ms {e}", file=sys.stderr)
        return None, ms, None, url, str(e)


def record(probe, url, status, ms, finding):
    rows.append({"probe": probe, "url": url, "status": status, "ms": ms, "finding": finding})


def save_fixture(name, obj):
    path = FIXTURES / name
    path.write_text(json.dumps(obj, indent=2))
    return path


def pick_condition_ids():
    """Condition ids from the ledger, per task instructions: query the latest
    watch poll's polymarket quotes by poll id only (37GB DB — never scan)."""
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cur = con.cursor()
    cur.execute(
        "SELECT venue_market_id, sport, start_utc FROM quotes "
        "WHERE poll_id = (SELECT MAX(id) FROM polls WHERE kind='watch') AND venue='polymarket'"
    )
    all_rows = cur.fetchall()
    cur.execute("SELECT started_at FROM polls WHERE kind='watch' ORDER BY id DESC LIMIT 1")
    poll_started_at = cur.fetchone()[0]
    con.close()

    poll_date = poll_started_at[:10]
    now = datetime.now(timezone.utc)

    recent, old = [], []
    seen = set()
    for vmid, sport, start_utc in all_rows:
        cid = vmid.split(":")[0]
        if cid in seen or not start_utc:
            continue
        seen.add(cid)
        if start_utc[:10] == poll_date:
            recent.append((cid, start_utc))
        try:
            start_dt = datetime.fromisoformat(start_utc)
        except ValueError:
            continue
        if now - start_dt > timedelta(days=2):
            old.append((cid, start_utc))

    recent.sort(key=lambda x: x[1])
    old.sort(key=lambda x: x[1])
    return {
        "poll_started_at": poll_started_at,
        "recent": recent[:5],
        "old": old[:5],
    }


def main():
    picks = pick_condition_ids()
    recent = picks["recent"]
    old = picks["old"]
    if len(recent) < 3:
        print("FATAL: fewer than 3 recent condition ids found in the latest watch poll", file=sys.stderr)
        sys.exit(1)
    if not old:
        print("FATAL: no condition id more than 2 days old found for the resolution probe", file=sys.stderr)
        sys.exit(1)

    c1, c2, c3 = recent[0][0], recent[1][0], recent[2][0]
    r1, r2 = old[0][0], (old[1][0] if len(old) > 1 else old[0][0])
    print(f"poll started_at={picks['poll_started_at']}", file=sys.stderr)
    print(f"recent (probes 1-5): c1={c1} c2={c2} c3={c3}", file=sys.stderr)
    print(f"old (probe 9): r1={r1} r2={r2}", file=sys.stderr)

    state = {}

    # --- Probe 1: CSV batching -------------------------------------------
    try:
        url = f"{DATA_API}/trades"
        params = {"market": f"{c1},{c2},{c3}", "limit": 100}
        status, ms, body, full_url, err = call(url, params)
        if err:
            record(1, full_url, "ERR", ms, f"request error: {err}")
        elif status != 200:
            record(1, full_url, status, ms, f"non-200; body={str(body)[:200]}")
        else:
            save_fixture("whale_trades_batch.json", body)
            trade_rows = body if isinstance(body, list) else []
            conds = set(t.get("conditionId") for t in trade_rows)
            present = {c: (c in conds) for c in (c1, c2, c3)}
            state["probe1_rows"] = trade_rows
            record(
                1, full_url, status, ms,
                f"CSV batching {'WORKS' if len(conds & {c1, c2, c3}) >= 1 else 'no rows matched'}; "
                f"{len(trade_rows)} rows; markets present: {present}; distinct conditionIds returned: {len(conds)}",
            )
    except Exception as e:
        record(1, "?", "EXC", 0, f"script exception: {e}")

    # --- Probe 2: real limit cap ------------------------------------------
    try:
        url = f"{DATA_API}/trades"
        status_a, ms_a, body_a, full_a, err_a = call(url, {"market": c1, "limit": 10000})
        n_a = len(body_a) if isinstance(body_a, list) else None
        status_b, ms_b, body_b, full_b, err_b = call(url, {"market": c1, "limit": 1001})
        n_b = len(body_b) if isinstance(body_b, list) else None
        state["c1_total_trades"] = n_a
        caveat = ""
        if n_a is not None and n_a < 1001:
            caveat = (
                f" -- INCONCLUSIVE: c1 only has {n_a} total trades, well under both limits, so neither "
                f"call can show where the real cap bites; would need a market with >1001 trades to test."
            )
        finding = (
            f"limit=10000 -> status={status_a} err={err_a} rows={n_a}; "
            f"limit=1001 -> status={status_b} err={err_b} rows={n_b}{caveat}"
        )
        record(2, f"{full_a} | {full_b}", f"{status_a}/{status_b}", ms_a + ms_b, finding)
    except Exception as e:
        record(2, "?", "EXC", 0, f"script exception: {e}")

    # --- Probe 3: takerOnly ------------------------------------------------
    try:
        url = f"{DATA_API}/trades"
        status_f, ms_f, body_f, full_f, err_f = call(url, {"market": c1, "takerOnly": "false", "limit": 200})
        status_t, ms_t, body_t, full_t, err_t = call(url, {"market": c1, "takerOnly": "true", "limit": 200})
        rows_f = body_f if isinstance(body_f, list) else []
        rows_t = body_t if isinstance(body_t, list) else []
        hashes_f = [r.get("transactionHash") for r in rows_f]
        hashes_t = [r.get("transactionHash") for r in rows_t]
        dup_within_f = len(hashes_f) - len(set(hashes_f))
        dup_within_t = len(hashes_t) - len(set(hashes_t))
        overlap = len(set(hashes_f) & set(hashes_t))
        state["taker_only_diff"] = len(rows_f) - len(rows_t)
        finding = (
            f"takerOnly=false rows={len(rows_f)} (dup txHash within call: {dup_within_f}); "
            f"takerOnly=true rows={len(rows_t)} (dup txHash within call: {dup_within_t}); "
            f"row-count diff={len(rows_f) - len(rows_t)}; transactionHash overlap across calls={overlap}"
        )
        record(3, f"{full_f} | {full_t}", f"{status_f}/{status_t}", ms_f + ms_t, finding)
    except Exception as e:
        record(3, "?", "EXC", 0, f"script exception: {e}")

    # --- Probe 4: start/end filter -----------------------------------------
    try:
        p1_rows = state.get("probe1_rows", [])
        c1_ts = [r["timestamp"] for r in p1_rows if r.get("conditionId") == c1 and "timestamp" in r]
        if c1_ts:
            t = max(int(x) for x in c1_ts)
        else:
            t = int(time.time())
        url = f"{DATA_API}/trades"
        params = {"market": c1, "start": t - 3600, "end": t}
        status, ms, body, full_url, err = call(url, params)
        if err:
            record(4, full_url, "ERR", ms, f"request error: {err}")
        elif status != 200:
            record(4, full_url, status, ms, f"non-200; body={str(body)[:200]}")
        else:
            filt_rows = body if isinstance(body, list) else []
            in_window = [r for r in filt_rows if t - 3600 <= r.get("timestamp", -1) <= t]
            total_c1 = state.get("c1_total_trades")
            if not filt_rows:
                verdict = "EMPTY-inconclusive"
            elif len(in_window) != len(filt_rows):
                verdict = "NOT HONORED (rows outside window returned)"
            elif total_c1 is not None and len(filt_rows) == total_c1:
                verdict = (
                    f"INCONCLUSIVE - all {total_c1} of c1's total trades fall in-window (t derived from "
                    f"their own max ts), so this cannot distinguish a real filter from a no-op that "
                    f"returns everything"
                )
            else:
                verdict = "HONORED (returned a strict subset, all in-window)"
            record(
                4, full_url, status, ms,
                f"t={t} (from probe1 max ts for c1, or now if none seen); {len(filt_rows)} rows returned "
                f"(c1 has {total_c1} trades total per probe 2); {len(in_window)}/{len(filt_rows)} within "
                f"[t-3600,t] -> {verdict}",
            )
    except Exception as e:
        record(4, "?", "EXC", 0, f"script exception: {e}")

    # --- Probe 5: global feed -----------------------------------------------
    try:
        url = f"{DATA_API}/trades"
        params = {"limit": 1000}
        status, ms, body, full_url, err = call(url, params)
        if err:
            record(5, full_url, "ERR", ms, f"request error: {err}")
        elif status != 200:
            record(5, full_url, status, ms, f"non-200; body={str(body)[:200]}")
        else:
            save_fixture("whale_trades_global.json", body)
            g_rows = body if isinstance(body, list) else []
            timestamps = [r["timestamp"] for r in g_rows if "timestamp" in r]
            if len(timestamps) >= 2:
                span_secs = max(timestamps) - min(timestamps)
                rate = (len(g_rows) / span_secs) * 60 if span_secs > 0 else None
            else:
                rate = None
            record(
                5, full_url, status, ms,
                f"global feed {'EXISTS' if g_rows else 'returned empty'}; {len(g_rows)} rows; "
                f"cap-at-limit={len(g_rows) == 1000}; implied rate="
                f"{f'{rate:.0f} rows/min' if rate is not None else 'n/a (insufficient timestamp span)'}",
            )
    except Exception as e:
        record(5, "?", "EXC", 0, f"script exception: {e}")

    # --- Probe 6: leaderboard ------------------------------------------------
    try:
        url = f"{DATA_API}/v1/leaderboard"
        status, ms, body, full_url, err = call(
            url, {"category": "SPORTS", "timePeriod": "MONTH", "orderBy": "PNL", "limit": 50, "offset": 0}
        )
        tried = [("SPORTS", status, ms, body, full_url, err)]
        if err or (status is not None and status >= 400):
            # SPORTS 400'd (or errored) - try OVERALL as the only other category
            # name this task has independent grounds to believe might exist
            # (default/no-filter leaderboard). No other category list is
            # documented anywhere in this repo's verified facts, so this is
            # the single fallback attempt, not a guessed list (rule 3).
            status2, ms2, body2, full_url2, err2 = call(
                url, {"category": "OVERALL", "timePeriod": "MONTH", "orderBy": "PNL", "limit": 50, "offset": 0}
            )
            tried.append(("OVERALL", status2, ms2, body2, full_url2, err2))
        ok = next((t for t in tried if t[1] == 200), None)
        total_ms = sum(t[2] for t in tried)
        urls = " | ".join(f"{t[0]}:{t[4]}" for t in tried)
        if ok:
            save_fixture("whale_leaderboard.json", ok[3])
            lb_rows = ok[3] if isinstance(ok[3], list) else (ok[3].get("data") if isinstance(ok[3], dict) else None)
            fields = sorted(lb_rows[0].keys()) if lb_rows else "unknown (empty list)"
            statuses = "/".join(str(t[1]) for t in tried)
            record(
                6, urls, statuses, total_ms,
                f"category '{ok[0]}' returned 200; fields={fields}; n={len(lb_rows) if lb_rows else 0}; "
                f"attempted categories: {[t[0] for t in tried]}",
            )
        else:
            statuses = "/".join(str(t[1]) for t in tried)
            record(
                6, urls, statuses, total_ms,
                f"no category returned 200; attempted {[t[0] for t in tried]} -> statuses {[t[1] for t in tried]}; "
                f"errors {[t[5] for t in tried]}",
            )
    except Exception as e:
        record(6, "?", "EXC", 0, f"script exception: {e}")

    # --- Probe 7: closed-positions (+ paging) --------------------------------
    try:
        p1_rows = state.get("probe1_rows", [])
        notional = {}
        for r in p1_rows:
            w = r.get("proxyWallet")
            if not w:
                continue
            try:
                notional[w] = notional.get(w, 0.0) + float(r.get("price", 0)) * float(r.get("size", 0))
            except (TypeError, ValueError):
                continue
        if not notional:
            record(7, f"{DATA_API}/closed-positions", "SKIP", 0, "no proxyWallet seen in probe 1 trades; cannot pick a wallet")
        else:
            wallet = max(notional, key=notional.get)
            state["wallet"] = wallet
            url = f"{DATA_API}/closed-positions"
            status_a, ms_a, body_a, full_a, err_a = call(url, {"user": wallet, "limit": 50, "offset": 0})
            status_b, ms_b, body_b, full_b, err_b = call(url, {"user": wallet, "limit": 50, "offset": 50})
            rows_a = body_a if isinstance(body_a, list) else []
            rows_b = body_b if isinstance(body_b, list) else []
            if rows_a:
                save_fixture("whale_closed_positions.json", rows_a)
            state["closed_positions"] = rows_a + rows_b
            fields = sorted(rows_a[0].keys()) if rows_a else "unknown (empty at offset=0)"
            key_a = {(r.get("conditionId"), r.get("outcomeIndex")) for r in rows_a}
            key_b = {(r.get("conditionId"), r.get("outcomeIndex")) for r in rows_b}
            overlap = key_a & key_b
            finding = (
                f"wallet={wallet} (largest notional=${notional[wallet]:.2f} in probe1 batch); "
                f"offset=0 -> {len(rows_a)} rows, offset=50 -> {len(rows_b)} rows, "
                f"paging {'CONFIRMED (no overlap)' if rows_b and not overlap else ('SAME-PAGE (overlap)' if overlap else 'inconclusive (offset=50 empty)')}; "
                f"fields={fields}"
            )
            record(7, f"{full_a} | {full_b}", f"{status_a}/{status_b}", ms_a + ms_b, finding)
    except Exception as e:
        record(7, "?", "EXC", 0, f"script exception: {e}")

    # --- Probe 8: activity ----------------------------------------------------
    try:
        wallet = state.get("wallet")
        if not wallet:
            record(8, f"{DATA_API}/activity", "SKIP", 0, "no wallet from probe 7")
        else:
            url = f"{DATA_API}/activity"
            status, ms, body, full_url, err = call(url, {"user": wallet, "type": "TRADE", "limit": 100})
            if err:
                record(8, full_url, "ERR", ms, f"request error: {err}")
            elif status != 200:
                record(8, full_url, status, ms, f"non-200; body={str(body)[:200]}")
            else:
                save_fixture("whale_activity.json", body)
                act_rows = body if isinstance(body, list) else []
                fields = sorted(act_rows[0].keys()) if act_rows else "unknown (empty)"
                record(8, full_url, status, ms, f"{len(act_rows)} rows; fields={fields}")
    except Exception as e:
        record(8, "?", "EXC", 0, f"script exception: {e}")

    # --- Probe 9: gamma resolution ---------------------------------------------
    try:
        url = f"{GAMMA_API}/markets"
        status_a, ms_a, body_a, full_a, err_a = call(url, {"condition_ids": f"{r1},{r2}"})
        status_b, ms_b, body_b, full_b, err_b = call(url, {"conditionId": r1})
        rows_a = body_a if isinstance(body_a, list) else []
        rows_b = body_b if isinstance(body_b, list) else []
        matched_a = [m for m in rows_a if m.get("conditionId") in (r1, r2)]
        matched_b = [m for m in rows_b if m.get("conditionId") == r1]
        which_works = []
        if status_a == 200 and matched_a:
            which_works.append("condition_ids(csv)")
        if status_b == 200 and matched_b:
            which_works.append("conditionId(single)")

        # Diagnostic (rule 3: verify, don't guess): r1/r2 are >2 days old per
        # the brief, but neither form matched them above. Before concluding
        # the endpoint/params are broken, check condition_ids against a market
        # we KNOW is real and indexed (c1, live trades seen in probe 1) to
        # isolate "wrong param" from "old/resolved markets fall out of the
        # default index".
        status_c, ms_c, body_c, full_c, err_c = call(url, {"condition_ids": c1})
        rows_c = body_c if isinstance(body_c, list) else []
        matched_c = [m for m in rows_c if m.get("conditionId") == c1]

        watch_fields = ["closed", "umaResolutionStatus", "outcomePrices", "gameStartTime",
                         "clobTokenIds", "sportsMarketType", "endDate"]

        if matched_c:
            save_fixture("whale_gamma_resolution.json", rows_c)
            m = matched_c[0]
            observed = {f: m.get(f, "<absent - not a key on this object>") for f in watch_fields}
            plural_status = m.get("umaResolutionStatuses", "<absent>")
            finding = (
                f"OLD r1/r2 (>2d, per brief): condition_ids csv matched 0/2, conditionId single matched "
                f"0/1 of {len(rows_b)} rows returned (param silently ignored, returns an unrelated default "
                f"page e.g. '{rows_b[0].get('question') if rows_b else '?'}'). DIAGNOSTIC on live market c1 "
                f"(condition_ids=<c1> alone): matched 1/1 -> condition_ids CSV genuinely filters correctly "
                f"when the market is still in gamma's default index; old/resolved condition ids just are not "
                f"in it (closed:true resolved markets appear to drop out, needs a separate closed=true query "
                f"to reach them - NOT VERIFIED here, out of this probe's specified scope). conditionId "
                f"(singular) is NOT a working filter param at all. Fields on the matched live market: "
                f"{observed}; NOTE design doc assumes 'umaResolutionStatus' (singular) but the real key is "
                f"'umaResolutionStatuses' (plural, list) = {plural_status}. Resolution-time values (closed:true "
                f"market) remain UNVERIFIED — no matched row in this probe was actually closed."
            )
        else:
            finding = (
                f"NEITHER form matched r1/r2, AND diagnostic condition_ids=<c1> (known-live market) also "
                f"failed to match (matched_c={len(matched_c)}) - condition_ids filtering could not be "
                f"confirmed at all in this run."
            )
        record(
            9, f"{full_a} | {full_b} | {full_c}", f"{status_a}/{status_b}/{status_c}",
            ms_a + ms_b + ms_c, finding,
        )
    except Exception as e:
        record(9, "?", "EXC", 0, f"script exception: {e}")

    # --- Probe 10: backfill-bias (losers-to-zero), reuses probe 7 data ---------
    try:
        positions = state.get("closed_positions", [])
        if not positions:
            record(10, "(derived from probe 7)", "SKIP", 0, "no closed positions collected in probe 7")
        else:
            sample = positions[0]
            payout_field = next(
                (f for f in ("curPrice", "currentPrice", "payout", "cashPnl") if f in sample), None
            )
            if payout_field is None:
                record(
                    10, "(derived from probe 7)", "N/A", 0,
                    f"could not confirm — no current-price/payout field found among observed keys: {sorted(sample.keys())}",
                )
            else:
                losers_to_zero = 0
                entries_gt0 = 0
                for p in positions:
                    try:
                        avg = float(p.get("avgPrice", -1))
                    except (TypeError, ValueError):
                        continue
                    if avg > 0:
                        entries_gt0 += 1
                        try:
                            payout = float(p.get(payout_field, -1))
                        except (TypeError, ValueError):
                            continue
                        if payout == 0:
                            losers_to_zero += 1
                record(
                    10, "(derived from probe 7)", "N/A", 0,
                    f"payout field used='{payout_field}'; {entries_gt0} positions with avgPrice>0 out of "
                    f"{len(positions)} total; {losers_to_zero} of those show payout==0 (held to zero) -> "
                    f"losers-to-zero {'APPEAR' if losers_to_zero > 0 else 'DO NOT APPEAR'} in this endpoint",
                )
    except Exception as e:
        record(10, "?", "EXC", 0, f"script exception: {e}")

    # --- Print markdown table ---------------------------------------------------
    print("\n| probe | url | status | ms | finding |")
    print("|---|---|---|---|---|")
    for row in rows:
        url_short = row["url"] if len(str(row["url"])) < 140 else str(row["url"])[:137] + "..."
        finding = str(row["finding"]).replace("|", "\\|")
        print(f"| {row['probe']} | {url_short} | {row['status']} | {row['ms']} | {finding} |")


if __name__ == "__main__":
    main()
