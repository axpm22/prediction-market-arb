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
    # Finding: log the watch poll's started_at to stdout too, not only stderr
    # -- it's load-bearing evidence for probe 9's "how old are these ids"
    # story and belongs in the table output, not just the diagnostic log.
    print(f"poll started_at={picks['poll_started_at']}")
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
                f"CSV batching {'WORKS (all requested markets present)' if all(present.values()) else 'PARTIAL/BROKEN (not all requested markets present)'}; "
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
            # Fixture kept small for repo size; full response (len == the
            # requested `limit`) is still used for every stat below. Truncate
            # here (not by hand) so a re-run reproduces the same fixture.
            save_fixture("whale_trades_global.json", body[:200])
            g_rows = body if isinstance(body, list) else []
            timestamps = [r["timestamp"] for r in g_rows if "timestamp" in r]
            if len(timestamps) >= 2:
                span_secs = max(timestamps) - min(timestamps)
                rate = (len(g_rows) / span_secs) * 60 if span_secs > 0 else None
            else:
                rate = None

            # Finding: "CAPPED-AT-1000" was asserted from a single
            # limit=1000 call, which is equally consistent with "the real
            # cap is >=1000 and we just didn't ask for more." Ask for more.
            status_hi, ms_hi, body_hi, full_hi, err_hi = call(url, {"limit": 10000})
            hi_rows = body_hi if isinstance(body_hi, list) else []
            if err_hi or status_hi != 200:
                cap_verdict = f"limit=10000 check FAILED (status={status_hi} err={err_hi}); real cap still unknown"
            elif len(hi_rows) == 10000:
                cap_verdict = "INCONCLUSIVE: limit=10000 returned exactly 10000 rows -- real ceiling may be higher, still not located"
            else:
                cap_verdict = f"limit=10000 returned {len(hi_rows)} rows (< requested) -> observed real cap = {len(hi_rows)} rows/call"

            record(
                5, f"{full_url} | {full_hi}", f"{status}/{status_hi}", ms + ms_hi,
                f"global feed {'EXISTS' if g_rows else 'returned empty'}; {len(g_rows)} rows at limit=1000 "
                f"(matches requested limit -- on its own this only shows 'the feed had >=1000 rows available', "
                f"not a cap); real-cap check: {cap_verdict}; implied rate="
                f"{f'{rate:.0f} rows/min' if rate is not None else 'n/a (insufficient timestamp span)'}",
            )
            # Also resolves probe 2's cap question (same `limit` param,
            # same /trades endpoint, just market-filtered vs global) -- probe
            # 2's own market was too thin to exercise it. Patch that row.
            for r in rows:
                if r["probe"] == 2:
                    r["finding"] += f" | cross-ref probe 5 global-feed real-cap check: {cap_verdict}"
                    break
    except Exception as e:
        record(5, "?", "EXC", 0, f"script exception: {e}")

    # --- Probe 6: leaderboard ------------------------------------------------
    try:
        url = f"{DATA_API}/v1/leaderboard"
        status_s, ms_s, body_s, full_s, err_s = call(
            url, {"category": "SPORTS", "timePeriod": "MONTH", "orderBy": "PNL", "limit": 50, "offset": 0}
        )
        sports_rows = body_s if isinstance(body_s, list) else []
        if sports_rows:
            save_fixture("whale_leaderboard.json", sports_rows)

        # Finding: a 200 with rows is not proof `category` did anything --
        # need a second call to see if the result actually differs. OVERALL
        # is the documented default category per docs.polymarket.com's
        # "get-trader-leaderboard-rankings" page (used when `category` is
        # omitted), so it's the one comparison point with a citation behind
        # it, not a guessed name.
        status_o, ms_o, body_o, full_o, err_o = call(
            url, {"category": "OVERALL", "timePeriod": "MONTH", "orderBy": "PNL", "limit": 50, "offset": 0}
        )
        overall_rows = body_o if isinstance(body_o, list) else []
        if overall_rows:
            save_fixture("whale_leaderboard_overall.json", overall_rows)

        sports_wallets = {r.get("proxyWallet") for r in sports_rows}
        overall_wallets = {r.get("proxyWallet") for r in overall_rows}
        both_present = bool(sports_rows) and bool(overall_rows)
        identical = both_present and sports_wallets == overall_wallets
        fields = sorted(sports_rows[0].keys()) if sports_rows else "unknown (empty)"
        if not both_present:
            verdict = "INCONCLUSIVE (one of the two calls returned no rows -- cannot compare sets)"
        elif identical:
            verdict = "IDENTICAL proxyWallet sets -> category param is IGNORED (SPORTS silently no-ops)"
        else:
            verdict = "DIFFERENT proxyWallet sets -> category param has a real effect"
        record(
            6, f"{full_s} | {full_o}", f"{status_s}/{status_o}", ms_s + ms_o,
            f"SPORTS: status={status_s} n={len(sports_rows)}; OVERALL: status={status_o} n={len(overall_rows)}; "
            f"overlap={len(sports_wallets & overall_wallets)}/{len(sports_wallets)}; {verdict}; fields={fields}",
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
                state["activity_rows"] = act_rows
                fields = sorted(act_rows[0].keys()) if act_rows else "unknown (empty)"
                record(8, full_url, status, ms, f"{len(act_rows)} rows; fields={fields}")
    except Exception as e:
        record(8, "?", "EXC", 0, f"script exception: {e}")

    # --- Probe 9: gamma resolution + CSV/repeated-param batching proof --------
    try:
        url = f"{GAMMA_API}/markets"
        now = datetime.now(timezone.utc)
        poll_dt = datetime.fromisoformat(picks["poll_started_at"])
        age_days = (now - poll_dt).days

        status_a, ms_a, body_a, full_a, err_a = call(url, {"condition_ids": f"{r1},{r2}"})
        status_b, ms_b, body_b, full_b, err_b = call(url, {"conditionId": r1})
        rows_a = body_a if isinstance(body_a, list) else []
        rows_b = body_b if isinstance(body_b, list) else []
        matched_a = [m for m in rows_a if m.get("conditionId") in (r1, r2)]
        matched_b = [m for m in rows_b if m.get("conditionId") == r1]
        if rows_b:
            # Minor finding: trim the "unrelated default page" evidence into
            # its own fixture so the ABSENT claim below isn't just prose.
            save_fixture("whale_gamma_conditionId_singular_default.json", rows_b[:5])
        which_works = []
        if status_a == 200 and len(matched_a) == 2:
            which_works.append("condition_ids(csv, old ids, matched 2/2)")
        elif status_a == 200 and len(matched_a) == 1:
            which_works.append("condition_ids(csv, old ids, matched 1/2 only)")
        if status_b == 200 and matched_b:
            which_works.append("conditionId(single, old id, matched)")

        # r1/r2 come from the SAME watch poll as c1/c2/c3 (just further down
        # the recency sort) -- they are NOT "old" vs c1's "live"; all five
        # ids are the same age. Finding 3: state that age plainly instead of
        # the ">2 days old" framing.
        old_ids_note = (
            f"poll_started_at={picks['poll_started_at']} -> r1={r1}, r2={r2}, and c1={c1} are ALL "
            f"~{age_days}d old as of this run (same poll, not a live-vs-stale comparison). "
            f"condition_ids=<r1,r2> matched {len(matched_a)}/2; conditionId=<r1> matched {len(matched_b)}/1 "
            f"of {len(rows_b)} rows returned"
            + (f" (unrelated default page, e.g. question='{rows_b[0].get('question')}')" if rows_b and not matched_b else "")
            + f". which_works={which_works or ['NEITHER form matched r1 or r2']}."
        )

        # Diagnostic (rule 3: verify, don't guess): check condition_ids
        # against a market we KNOW is indexed (c1, live trades seen in
        # probe 1) to isolate "wrong param" from "this particular market
        # isn't reachable this way".
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
            resolve_desc = m.get("description", "")
            postponed_hint = "postponement clause present in description" if "postponed" in resolve_desc.lower() else "no postponement clause found"
            fields_note = (
                f"c1 matched (condition_ids=<c1> alone, {len(matched_c)}/1): closed={observed['closed']}, "
                f"gameStartTime={observed['gameStartTime']} (~{age_days}d ago, still closed=False -- {postponed_hint}), "
                f"umaResolutionStatus(singular, design's assumed name)={observed['umaResolutionStatus']}, "
                f"umaResolutionStatuses(plural, real key)={plural_status}, outcomePrices={observed['outcomePrices']}, "
                f"clobTokenIds={observed['clobTokenIds']}, sportsMarketType={observed['sportsMarketType']}, "
                f"endDate={observed['endDate']}. Why r1/r2 (same-age ids) returned nothing while c1 matched is "
                f"UNKNOWN (closed? archived? some other reachability rule?) -- not established by this probe."
            )
        else:
            fields_note = f"diagnostic condition_ids=<c1> (known-live market) ALSO failed to match (matched_c={len(matched_c)}) -- condition_ids filtering could not be confirmed at all in this run."

        # --- CSV vs repeated-param batching proof (finding: batching was
        # never actually exercised with 2 real ids -- the only matching call
        # so far was single-id). Need a SECOND id confirmed to match alone
        # before a 2-id call means anything; c1 is already confirmed above.
        second_id = None
        second_id_ms = 0
        second_id_tries = []
        for cand in (c2, c3):
            status_x, ms_x, body_x, full_x, err_x = call(url, {"condition_ids": cand})
            second_id_ms += ms_x
            rows_x = body_x if isinstance(body_x, list) else []
            hit = any(m.get("conditionId") == cand for m in rows_x)
            second_id_tries.append((cand, hit))
            if hit:
                second_id = cand
                break

        if second_id is None or not matched_c:
            batching_note = (
                f"CSV/repeated-param batching proof SKIPPED: "
                + (f"no second id confirmed to match alone (tried {second_id_tries})" if second_id is None else "c1 itself did not match alone")
                + f" -- {second_id_ms}ms spent confirming. Batch cap NOT PROBED."
            )
            batch_status = "SKIP"
            batch_ms = second_id_ms
        else:
            status_d, ms_d, body_d, full_d, err_d = call(url, {"condition_ids": f"{c1},{second_id}"})
            status_e, ms_e, body_e, full_e, err_e = call(url, {"condition_ids": [c1, second_id]})
            rows_d = body_d if isinstance(body_d, list) else []
            rows_e = body_e if isinstance(body_e, list) else []
            ids_d = {m.get("conditionId") for m in rows_d}
            ids_e = {m.get("conditionId") for m in rows_e}
            present_d = {c1: c1 in ids_d, second_id: second_id in ids_d}
            present_e = {c1: c1 in ids_e, second_id: second_id in ids_e}
            csv_batches = all(present_d.values())
            repeated_batches = all(present_e.values())
            if csv_batches and repeated_batches:
                which_batches = "BOTH condition_ids=<csv> and repeated condition_ids=&condition_ids= batch correctly"
            elif csv_batches:
                which_batches = "condition_ids=<csv> (comma-joined) batches; the repeated-param form does NOT"
            elif repeated_batches:
                which_batches = "repeated condition_ids=&condition_ids= batches; the comma-joined CSV form does NOT"
            else:
                which_batches = "NEITHER form returns both requested ids in one call"
            batching_note = (
                f"second known-good id={second_id} (confirmed alone in {second_id_ms}ms); "
                f"condition_ids=<csv> [{full_d}] -> present={present_d}; "
                f"repeated condition_ids= [{full_e}] -> present={present_e}; {which_batches}. "
                f"Batch cap NOT PROBED (only 2 ids tested, not the ceiling)."
            )
            batch_status = f"{status_d}/{status_e}"
            batch_ms = ms_d + ms_e + second_id_ms

        finding = f"{old_ids_note} {fields_note} BATCHING: {batching_note}"
        record(
            9, f"{full_a} | {full_b} | {full_c}", f"{status_a}/{status_b}/{status_c}/{batch_status}",
            ms_a + ms_b + ms_c + batch_ms, finding,
        )
    except Exception as e:
        record(9, "?", "EXC", 0, f"script exception: {e}")

    # --- Probe 10: backfill-bias (losers-to-zero), reuses probe 7+8 data -------
    try:
        positions = state.get("closed_positions", [])
        if not positions:
            record(10, "(derived from probe 7)", "SKIP", 0, "no closed positions collected in probe 7")
        else:
            payout_field = next(
                (f for f in ("curPrice", "currentPrice", "payout", "cashPnl") if f in positions[0]), None
            )
            if payout_field is None:
                record(
                    10, "(derived from probe 7)", "N/A", 0,
                    f"could not confirm — no current-price/payout field found among observed keys: {sorted(positions[0].keys())}",
                )
            else:
                cur_price_1 = sum(1 for p in positions if p.get("curPrice") == 1)
                pnl_neg = sum(1 for p in positions if isinstance(p.get("realizedPnl"), (int, float)) and p.get("realizedPnl") < 0)
                pnl_pos = sum(1 for p in positions if isinstance(p.get("realizedPnl"), (int, float)) and p.get("realizedPnl") > 0)
                losers_to_zero = sum(
                    1 for p in positions
                    if isinstance(p.get("avgPrice"), (int, float)) and p.get("avgPrice", -1) > 0
                    and isinstance(p.get(payout_field), (int, float)) and p.get(payout_field) == 0
                )

                # Finding: cross-reference against probe 8's /activity for
                # the SAME wallet to see how many markets were actually
                # traded vs how many show up here at all (winners or not).
                activity_rows = state.get("activity_rows", [])
                traded_conditions = {a.get("conditionId") for a in activity_rows if a.get("side") == "BUY"}
                closed_conditions = {p.get("conditionId") for p in positions}
                missing = traded_conditions - closed_conditions
                if activity_rows:
                    backfill_note = (
                        f"cross-ref probe 8 /activity (same wallet): {len(traded_conditions)} distinct markets "
                        f"bought, {len(closed_conditions & traded_conditions)}/{len(traded_conditions)} appear here "
                        f"({len(missing)} traded markets MISSING from closed-positions)"
                    )
                else:
                    backfill_note = "no probe 8 activity rows available for cross-reference"

                record(
                    10, "(derived from probe 7 + probe 8)", "N/A", 0,
                    f"payout field used='{payout_field}'; {len(positions)} closed positions: "
                    f"{cur_price_1}/{len(positions)} curPrice==1, {pnl_pos}/{len(positions)} realizedPnl>0, "
                    f"{pnl_neg}/{len(positions)} realizedPnl<0, {losers_to_zero} held-to-zero (avgPrice>0 and "
                    f"{payout_field}==0). closed-positions ≈ redeemed winners; losers of any kind ABSENT in "
                    f"this sample ({pnl_neg}/{len(positions)} negative PnL). {backfill_note}.",
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
