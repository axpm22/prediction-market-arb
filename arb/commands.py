"""Command handlers. Phase 1: report renders the (empty) daily frame; the rest
arrive in their SPEC §16 phases and are absent until then."""
from __future__ import annotations

from datetime import date, timedelta

from rich.console import Console
from rich.table import Table

console = Console()


def cmd_watch(cfg, store, args):
    from arb.watch import fetch_all_venues
    quotes, status = fetch_all_venues(cfg)
    table = Table(title="watch --once" + (" --dry-run" if args.dry_run else ""))
    for col in ("venue", "ok", "quotes", "latency_ms"):
        table.add_column(col)
    for venue, st in status.items():
        table.add_row(venue, "✓" if st["ok"] else "✗ " + st.get("error", "")[:40],
                      str(st["n"]), f"{st['latency_ms']:.0f}")
    console.print(table)
    if args.dry_run:
        console.print("[dim]dry-run: nothing persisted[/]")
        return 0
    from arb.cycle import run_cycle
    return run_cycle(cfg, store, quotes, status, poll_kind="watch")


def cmd_negrisk_scan(cfg, store, args):
    """PM negRisk events + Kalshi ME groups (SPEC §2). Kalshi non-sports series come
    only from data/nonsports_map.yaml (§6.1) — empty map = PM-only scan, stated."""
    import json as _json
    from datetime import datetime, timezone

    from arb.negrisk import scan_group
    from arb.venues import polymarket

    from arb.tracker import candidate_from_basket, process_poll
    if not cfg["negrisk"]["enabled"]:
        console.print("[yellow]negrisk disabled[/]")
        return 0
    tags = list(cfg["nonsports"]["polymarket_tags"])
    events = polymarket.fetch_negrisk_events(cfg, tags)[:int(cfg["negrisk"]["scan_limit"])]
    console.print(f"[dim]negrisk: {len(events)} flagged events across tags {tags}[/]")
    now = datetime.now(timezone.utc)
    all_opps, all_quotes, skipped = [], [], []
    for e in events:
        markets = [m for m in e.get("markets", []) if m.get("clobTokenIds")]
        if not markets or len(markets) > int(cfg["negrisk"]["max_outcomes"]):
            # M9: skips are audit rows, never silent (bug-E2 doctrine)
            skipped.append((f"nonsports:pm:{e['id']}",
                            "no-tradable-markets" if not markets
                            else "too-many-outcomes"))
            continue
        tokens = []
        for m in markets:
            tokens.extend(_json.loads(m["clobTokenIds"]))
        books = polymarket.fetch_books(cfg, tokens)

        def side_of(book, key):
            if not book or not book.get(key):
                return None
            from decimal import Decimal
            lv = book[key]
            best = min(lv, key=lambda x: Decimal(x["price"])) if key == "asks" \
                else max(lv, key=lambda x: Decimal(x["price"]))
            return Decimal(best["price"])

        outcomes = []
        for m in markets:
            yes_t, no_t = _json.loads(m["clobTokenIds"])
            yb, nb = books.get(yes_t), books.get(no_t)
            from decimal import Decimal
            ya = side_of(yb, "asks")
            outcomes.append({
                "label": m.get("groupItemTitle") or m.get("question", ""),
                "yes_ask": ya, "yes_bid": side_of(yb, "bids"),
                "no_ask": side_of(nb, "asks"), "no_bid": side_of(nb, "bids"),
                "depth_usd": (Decimal(min(yb["asks"], key=lambda x: Decimal(x["price"]))["size"]) * ya)
                             if yb and yb.get("asks") else None,
                "volume_24h": Decimal(str(m.get("volume24hr") or 0))})
        from arb.negrisk import quote_rows
        from arb.venues.fees import PM_TAG_CATEGORY
        category = PM_TAG_CATEGORY.get(str(e.get("_tag")), "politics")
        # rule 7: observed books persist BEFORE any skip — they are backtest
        # data whether or not the group can be scanned
        all_quotes.extend(quote_rows(cfg, "polymarket", category,
                                     f"nonsports:pm:{e['id']}", outcomes, now))
        if any(o["yes_ask"] is None or o["yes_bid"] is None for o in outcomes):
            # a one-sided outcome fails proof condition (c) anyway; skip math
            skipped.append((f"nonsports:pm:{e['id']}", "one-sided-outcome"))
            continue
        opps = scan_group(cfg, "polymarket", category, f"nonsports:pm:{e['id']}",
                          e.get("title", ""), outcomes, venue_flag=bool(e.get("negRisk")))
        for b in opps:
            b["resolution_date"] = e.get("endDate")  # D2 horizon gate input
        all_opps.extend(opps)

    if cfg["negrisk"]["kalshi_events"]:
        from arb.nonsports.kalshi_events import scan_kalshi_events
        k_quotes, k_baskets, n_checked = scan_kalshi_events(cfg, now)
        all_quotes.extend(k_quotes)
        all_opps.extend(k_baskets)
        console.print(f"[dim]kalshi ME: {n_checked} event groups checked, "
                      f"{len(k_baskets)} baskets, {len(k_quotes)} quotes[/]")

    poll_id = store.insert("polls", {
        "started_at": now.isoformat(), "kind": "negrisk",
        "n_quotes": len(all_quotes), "n_opps": len(all_opps)})
    store.insert_many("quotes", [{**q, "poll_id": poll_id} for q in all_quotes])
    store.insert_many("left_on_table", [
        {"poll_id": poll_id, "fingerprint": "", "kind": "NEGRISK",
         "event_key": ek, "venue_pair": "polymarket:self", "legs_json": "[]",
         "reason": reason, "edge_points": None} for ek, reason in skipped])
    # D1 (review 2026-07-10): baskets BOOK — $1 across the basket (§10.1)
    process_poll(cfg, store, poll_id, "negrisk",
                 [candidate_from_basket(b) for b in all_opps],
                 {"polymarket": {"ok": True, "n": len(all_quotes),
                                 "latency_ms": 0}}, now=now)
    store.insert_many("opportunities", [
        {"poll_id": poll_id, "fingerprint": o["fingerprint"],
         "position_key": o["position_key"], "kind": o["kind"], "category": o["category"],
         "event_key": o["event_key"], "edge": o["edge_points"], "status": o["status"],
         "legs_json": o["legs_json"],
         "notes": f"{o['side']}-basket n={o['n_outcomes']} proof={o['proof_status']} "
                  f"{o['notes']}"} for o in all_opps])
    table = Table(title=f"negRisk scan — {len(all_opps)} baskets")
    for col in ("title", "side", "n", "edge", "proof", "status"):
        table.add_column(col)
    for o in all_opps:
        table.add_row(o["title"][:40], o["side"], str(o["n_outcomes"]),
                      str(o["edge_points"]), o["proof_status"], o["status"])
    console.print(table)
    return 0


def cmd_report(cfg, store, args):
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo

    from arb.metrics import day_bounds
    from arb.report import print_daily, write_daily
    # default day = today in the config timezone (§H5), not machine-local
    day = args.daily or _dt.now(ZoneInfo(str(cfg["timezone"]))).date().isoformat()
    start, end = day_bounds(cfg, day)
    polls = store.query(
        "SELECT COUNT(*) c, COALESCE(SUM(n_quotes),0) q FROM polls "
        "WHERE started_at >= ? AND started_at < ?", (start, end))[0]
    console.print(f"[dim]{day}: polls={polls['c']} quotes={polls['q']}[/]")
    write_daily(cfg, store, day)
    print_daily(cfg, store, day)
    return 0


def cmd_scan(cfg, store, args):
    """Odds API consensus refresh (SPEC §13.1). RULE 2: the only credit-spending
    command besides settle scores; no-op without a key or when disabled."""
    from datetime import datetime, timezone

    from arb.consensus import compute_anchor_consensus, compute_consensus
    from arb.venues import oddsapi
    if not cfg["consensus_enabled"]:
        console.print("[dim]consensus disabled; scan is a no-op[/]")
        return 0
    if not oddsapi.api_key():
        console.print("[yellow]no ODDS_API_KEY in .env — scan is a no-op "
                      "(add a free key from the-odds-api.com)[/]")
        return 0
    now = datetime.now(timezone.utc).isoformat()
    total_c, total_b, aborted = 0, 0, False
    # credit math (§18.3 Part A, not asserted live): us h2h+totals (2) + eu h2h
    # anchor (1) = 3 credits x sports x 4 scans/day ~= 360/month for one sport
    try:
        for sport_key in cfg["consensus_sports"]:
            rows = oddsapi.fetch_odds(cfg, str(sport_key))
            cons, books = compute_consensus(cfg, rows, now)
            store.insert_many("consensus", cons)
            store.insert_many("consensus_books", books)
            total_c += len(cons)
            total_b += len(books)
            if cfg["consensus_anchor"]["enabled"]:
                anchor_rows = oddsapi.fetch_anchor_odds(cfg, str(sport_key))
                acons, abooks = compute_anchor_consensus(cfg, anchor_rows, now)
                store.insert_many("consensus", acons)
                store.insert_many("consensus_books", abooks)
                total_c += len(acons)
                total_b += len(abooks)
    except oddsapi.CreditsLow as e:
        # rule 2 abort, gracefully: the credits row must land on exactly the
        # path where credits matter most (M4)
        console.print(f"[red]scan aborted: {e}[/]")
        aborted = True
    if oddsapi.LAST_REMAINING is not None:  # dashboard/status read the latest row
        store.insert("annotations", {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "target_kind": "day", "target_id": now[:10], "kind": "oddsapi-credits",
            "detail": str(oddsapi.LAST_REMAINING)})
    console.print(f"consensus rows: {total_c}; per-book rows: {total_b}")
    return 1 if aborted else 0


def cmd_settle(cfg, store, args):
    from datetime import datetime, timezone

    from arb.invariants import check
    from arb.reconcile import reconcile
    from arb.settlement import settle_auto_mlb, settle_manual
    if args.manual:
        event_key, result = args.manual
        console.print(f"[bold]manual settle[/]: event={event_key!r} result={result!r}")
        if args.dry_run:
            console.print("[dim]--dry-run: nothing appended[/]")
            return 0
        if not args.yes:
            console.print("[red]refusing without -y: appends immutable rows — "
                          "a typo is forever[/]")
            return 1
        n = settle_manual(cfg, store, event_key, result)
        console.print(f"settled {n} bets")
    else:
        n = settle_auto_mlb(cfg, store)
        console.print(f"auto-settled {n} MLB bets (mlbstats)")
    n_rec = reconcile(cfg, store)
    console.print(f"reconciled {n_rec} bets")
    violations = check(cfg, store)
    if violations:
        console.print(f"[red]INVARIANT VIOLATIONS: {len(violations)}[/]")
        return 1
    return 0


def cmd_check_invariants(cfg, store, args):
    from arb.invariants import check
    violations = check(cfg, store)
    if violations:
        for v in violations:
            console.print(f"[red]{v}[/]")
        return 1
    return 0  # silent when clean


_REGRADE_BASES = (("as_stored", "mean_diff"), ("closing", "mean_diff"), ("ev_subset", "win_rate"))


def cmd_regrade(cfg, store, args):
    """§18.3 `arb regrade`: read-only paired-Brier re-scoring vs venue mids.
    Reads only the local DB + the keyless MLB Stats API — NEVER the live Odds
    API (rule 2) and NEVER writes any table (rule 9 ethos)."""
    from datetime import datetime, timezone

    from arb.regrade import run, write_report
    db_path = getattr(args, "regrade_db", None) or args.db
    regrade_store = store if db_path == args.db else Store(db_path)
    result = run(cfg, regrade_store)
    for bucket in ("median", "anchor"):
        table = Table(title=f"regrade — {bucket}")
        for col in ("basis", "n_events", "n_pairs", "value", "95% CI"):
            table.add_column(col)
        for basis, label in _REGRADE_BASES:
            c = result["buckets"][bucket][basis]
            if c["insufficient"]:
                table.add_row(basis, str(c["n_events"]), str(c["n_pairs"]),
                             f"INSUFFICIENT n={c['n_events']}", "—")
            else:
                table.add_row(basis, str(c["n_events"]), str(c["n_pairs"]),
                             f"{label}={c['mean']}", f"[{c['ci_low']}, {c['ci_high']}]")
        console.print(table)
        excl = result["exclusions"][bucket]
        console.print(f"[dim]excluded: no-final={excl['no_final']}, "
                      f"out-of-tolerance={excl['out_of_tolerance']}[/]")
    today = datetime.now(timezone.utc).date().isoformat()
    path = write_report(result, today)
    console.print(f"wrote {path}")
    return 0


def cmd_political(cfg, store, args):
    """POLITICAL lane: Polymarket-only intra-market YES+NO<1 (§12)."""
    import json as _json
    from datetime import datetime, timezone

    from arb.lanes.political import book_quote_rows, scan_market
    from arb.tracker import process_poll
    from arb.venues import polymarket
    events, seen = [], set()  # dedupe by event id across overlapping tags (H10)
    for tag in cfg["nonsports"]["polymarket_tags"]:
        for e in polymarket.fetch_gamma_events(
                cfg, str(tag), max_pages=int(cfg["nonsports"]["gamma_scan_pages"])):
            if e.get("id") not in seen:
                seen.add(e.get("id"))
                events.append(e)
    markets = [m for e in events for m in e.get("markets", [])
               if m.get("clobTokenIds") and not m.get("negRisk")]
    tokens = [t for m in markets for t in _json.loads(m["clobTokenIds"])]
    books = polymarket.fetch_books(cfg, tokens)
    opps = [o for m in markets if (o := scan_market(cfg, m, books))]
    now = datetime.now(timezone.utc)
    quotes = [q for m in markets for q in book_quote_rows(cfg, m, books, now)]
    poll_id = store.insert("polls", {"started_at": now.isoformat(), "kind": "political",
                                     "n_quotes": len(quotes), "n_opps": len(opps)})
    store.insert_many("quotes", [{**q, "poll_id": poll_id} for q in quotes])
    process_poll(cfg, store, poll_id, "political", opps,
                 {"polymarket": {"ok": True, "n": len(quotes), "latency_ms": 0}}, now=now)
    console.print(f"political: {len(markets)} binary markets scanned, "
                  f"{len(opps)} YES+NO<1 flags")
    return 0


def cmd_live(cfg, store, args):
    """In-play lane (PROMPT_LIVE_LEADLAG Part A). Never calls The Odds API."""
    from arb.lanes.live import run_live_cycle
    if not cfg["live_lane"]["enabled"]:
        console.print("[yellow]live_lane disabled (kill gate?) — no-op[/]")
        return 0
    s = run_live_cycle(cfg, store, dry_run=args.dry_run)
    console.print(f"live cycle: shortlist={len(s['shortlist'])} events, "
                  f"identities={s['identities']}, requoted={s['requoted']} "
                  f"(coverage {s['coverage']:.0%}), flags={s['flags']}, "
                  f"rejects={s['rejects']}, cycle={s['cycle_secs']}s "
                  f"(target {cfg['live_lane']['cadence_secs']}s)")
    for ek in s["shortlist"]:
        console.print(f"  [dim]{ek}[/]")
    if args.dry_run:
        console.print("[dim]dry-run: nothing persisted[/]")
    return 0


def cmd_leadlag(cfg, store, args):
    """Lead-lag study over stored quotes (read-time; PROMPT_LIVE_LEADLAG B)."""
    from arb.leadlag import run_study
    res = run_study(cfg, store)
    table = Table(title=f"lead-lag — excluded (no bid): "
                        f"{res['excluded_no_bid_fraction']:.1%}")
    for col in ("pair", "matched", "p_lead(first)", "95% CI", "leader",
                "median lead", "events"):
        table.add_column(col)
    gate_moves = int(cfg["leadlag"]["min_matched_moves"])
    gate_events = int(cfg["leadlag"]["min_events"])
    for pair, p in sorted(res["pairs"].items()):
        below = p["n_matched"] < gate_moves or p["n_events"] < gate_events
        leader = p["leader"] or "no lead"
        if below:
            leader += " (below gate n)"
        table.add_row(pair, str(p["n_matched"]), f"{p['p_lead_first']:.3f}",
                      f"[{p['ci_low']:.3f}, {p['ci_high']:.3f}]", leader,
                      f"{p['median_lead_secs']:.0f}s" if p["median_lead_secs"]
                      else "—", str(p["n_events"]))
    console.print(table)
    if res["velocities"]:
        vt = Table(title="observed live line velocity (pts/sec) — B4: replaces "
                         "the config PLACEHOLDERS via dated change")
        for col in ("sport", "venue", "median |Δmid|/Δt"):
            vt.add_column(col)
        for sport, venues in sorted(res["velocities"].items()):
            for venue, v in sorted(venues.items()):
                vt.add_row(sport, venue, v)
        console.print(vt)
    return 0


def cmd_nonsports(cfg, store, args):
    """Phase 1.2 blocking-key matcher: writes data/nonsports_candidates.yaml
    for human curation (data/nonsports_curation_protocol.md). --eval runs the
    Haiku stage on curated pairs and scores it (§18.5); --ai is the live stage
    (requires ai_matcher.enabled, flipped only by dated amendment)."""
    if getattr(args, "ai", False) or getattr(args, "ai_eval", False) \
            or getattr(args, "ai_sweep", False):
        from arb.nonsports.ai import evaluate, run_ai
        mode = ("eval" if getattr(args, "ai_eval", False) else
                "sweep" if getattr(args, "ai_sweep", False) else "live")
        s = run_ai(cfg, store, mode=mode)
        console.print(f"ai matcher [{mode}]: {s}")
        if mode == "eval":
            m = evaluate(cfg, store)
            console.print(f"§18.5 score vs ground truth: {m}")
        return 0
    from arb.nonsports.scan import run_match
    s = run_match(cfg)
    console.print(f"nonsports match: {s['candidates']} candidates "
                  f"({s['new']} new) from {s['kalshi_rows']} kalshi x "
                  f"{s['pm_rows']} pm rows; {s['unverdicted']} of "
                  f"{s['total_on_file']} on file await curation")
    return 0


def cmd_tickets(cfg, store, args):
    """Open locked flags as explicit MANUAL order tickets, persistence-ranked.
    Read-only; rule 1 forbids this codebase from placing anything."""
    from arb.tickets import open_locked_positions
    console.print("[bold yellow]PAPER INSTRUMENT — tickets are instructions "
                  "for a HUMAN; nothing here places orders (rule 1)[/]")
    ts = open_locked_positions(cfg, store)
    if not ts:
        console.print("no open locked positions")
        return 0
    for t in ts:
        console.print(f"\n[bold]{t['kind']}[/] {t['event_key']} — edge "
                      f"{t['edge_points']}pts, seen [bold]{t['sightings']}x[/] "
                      f"(first {t['first_seen'][:16]}, last {t['last_seen'][:16]}) "
                      f"lane={t['lane']} trusted={t['trusted']}")
        for lg in t["legs"]:
            console.print(f"   BUY {lg.get('venue_side', lg['side'])} @ "
                          f"{lg['cost']} on {lg['venue']} "
                          f"[dim]{lg.get('venue_market_id', '?')}[/] "
                          f"(fee {lg.get('fee_cost')}, depth "
                          f"{lg.get('depth_usd', '?')})")
    return 0


def cmd_mapped(cfg, store, args):
    """Mapped-pair cross-venue lane over data/nonsports_map.yaml."""
    from arb.nonsports.mapped import run_mapped
    s = run_mapped(cfg, store)
    console.print(f"mapped: {s['pairs']} pairs, {s['quotes']} quotes, "
                  f"{s['opps']} flags, {s['errors']} errors")
    return 0


def cmd_verify(cfg, store, args):
    from arb.lanes.verify import run_once
    n = run_once(cfg, store)
    console.print(f"verify: re-quoted {n} fresh locked flags")
    return 0


def cmd_supervisor(cfg, store, args):
    from arb.heartbeat import run_supervisor
    # children must inherit --db/--config or data silently splits across DBs
    return run_supervisor(cfg, db=args.db, config=args.config)


def _sntp_offset(out: str) -> float | None:
    """Offset seconds from `sntp` output: the token before '+/-'
    (`+0.014657 +/- 0.009361 time.apple.com ...`). None when unparseable —
    the old split()[-2] read the server hostname and the alarm never fired."""
    toks = (out or "").split()
    if "+/-" not in toks:
        return None
    try:
        return float(toks[toks.index("+/-") - 1])
    except (ValueError, IndexError):
        return None


def cmd_status(cfg, store, args):
    import subprocess
    from datetime import datetime, timezone
    from pathlib import Path

    from arb.heartbeat import LOCK_DIR
    now = datetime.now(timezone.utc)

    # supervisor semantics (§13.3): fresh beat OR live child pid -> ALIVE;
    # stale beat + live supervisor lock -> STALE; DEAD only with no supervisor
    lock_alive = False
    if (LOCK_DIR / "pid").exists():
        try:
            import os
            os.kill(int((LOCK_DIR / "pid").read_text().strip()), 0)
            lock_alive = True
        except (ProcessLookupError, ValueError, PermissionError):
            pass
    beats = store.query(
        "SELECT process, MAX(beat_at) beat_at, head_sha FROM heartbeats GROUP BY process")
    fresh = any((now - datetime.fromisoformat(b["beat_at"])).total_seconds()
                < int(cfg["heartbeat_stale_polls"]) * float(cfg["watch_interval_secs"])
                for b in beats)
    state = "ALIVE" if fresh or lock_alive else ("STALE" if beats else "DEAD")
    console.print(f"supervisor: [bold]{state}[/] (lock={'live' if lock_alive else 'none'})")
    for b in beats:
        console.print(f"  {b['process']}: last beat {b['beat_at']} sha={b['head_sha']}")

    newest = store.query("SELECT MAX(fetched_at) t FROM quotes")[0]["t"]
    if newest:
        age = (now - datetime.fromisoformat(newest)).total_seconds()
        alarm = age > float(cfg["data_stale_alarm_secs"])
        console.print(f"newest quote: {round(age)}s ago"
                      + (" [red]STALE-DATA ALARM[/]" if alarm else ""))
    else:
        console.print("[yellow]no quotes persisted yet[/]")

    backups = sorted(Path(str(cfg["db_backup"]["dest"])).glob("*.sqlite"))
    if backups:
        bage = now.timestamp() - backups[-1].stat().st_mtime
        console.print(f"latest backup: {backups[-1].name} ({round(bage / 3600, 1)}h old)")
    else:
        console.print("[yellow]no backups yet[/]")

    try:  # NTP offset alarm (§13.3); sntp may need privileges — degrade honestly
        out = subprocess.run(["sntp", "time.apple.com"], capture_output=True,
                             text=True, timeout=5).stdout
        offset = _sntp_offset(out)
        if offset is not None and abs(offset) > float(cfg["clock_skew_alarm_secs"]):
            console.print(f"[red]CLOCK SKEW ALARM: {offset}s[/]")
        elif offset is not None:
            console.print(f"clock offset: {offset}s")
    except Exception:  # noqa: BLE001
        console.print("[dim]ntp check unavailable[/]")

    from zoneinfo import ZoneInfo

    from arb.metrics import day_bounds
    today = now.astimezone(ZoneInfo(str(cfg["timezone"]))).date().isoformat()  # ET
    start, end = day_bounds(cfg, today)
    lanes = store.query(
        "SELECT lane, COUNT(*) n FROM flat_bets WHERE created_at >= ? "
        "AND created_at < ? GROUP BY lane", (start, end))
    console.print("today's bets by lane: "
                  + (", ".join(f"{r['lane']}={r['n']}" for r in lanes) or "none"))
    demoted = store.query(
        "SELECT DISTINCT pair FROM pair_demotions ORDER BY pair")
    if demoted:
        console.print("[red]demoted pairs (settlement mismatch, §11.2): "
                      + ", ".join(r["pair"] for r in demoted) + "[/]")
    open_n = store.query(
        """SELECT COUNT(*) n FROM flat_bets fb
           WHERE fb.kind IN ('EV','NEGRISK','BTC')
             AND NOT EXISTS (SELECT 1 FROM bet_events be WHERE be.bet_id=fb.id
                             AND be.kind IN ('settled','void'))""")[0]["n"]
    console.print(f"open resolution-settled bets: {open_n}")
    return 0


def cmd_dashboard(cfg, store, args):
    from arb.dashboard import serve
    serve(cfg, args.db)
    return 0


def _prune_backups(dest, keep_last: int) -> list:
    """Delete all but the keep_last newest arb-*.sqlite backups (H8). Returns the
    pruned filenames. keep_last <= 0 disables pruning."""
    from pathlib import Path
    if keep_last is None or int(keep_last) <= 0:
        return []
    backups = sorted(Path(dest).glob("arb-*.sqlite"),
                     key=lambda p: p.stat().st_mtime)
    pruned = backups[:-int(keep_last)] if len(backups) > int(keep_last) else []
    for p in pruned:
        p.unlink()
    return [p.name for p in pruned]


def cmd_backup(cfg, store, args):
    from datetime import datetime, timezone
    from pathlib import Path
    dest = Path(str(cfg["db_backup"]["dest"]))
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / f"arb-{datetime.now(timezone.utc).strftime('%Y%m%d')}.sqlite"
    tmp = out.with_suffix(".sqlite.tmp")
    tmp.unlink(missing_ok=True)
    store.conn.execute("VACUUM INTO ?", (str(tmp),))
    tmp.replace(out)  # VACUUM INTO refuses an existing target; §14.1 idempotency
    console.print(f"backup written: {out} ({out.stat().st_size} bytes)")
    # 04:10 is the quiet window: force the WAL down (15.2 GB on 2026-07-17 —
    # long-lived readers starve auto-checkpoint). Busy/partial is a WARNING,
    # never a failure; tonight's slot or the next will win.
    busy, log_frames, ckpt = store.conn.execute(
        "PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    msg = f"wal_checkpoint(TRUNCATE): busy={busy} log={log_frames} checkpointed={ckpt}"
    console.print(("WARNING partial " if busy else "") + msg)
    pruned = _prune_backups(dest, cfg["db_backup"].get("keep_last"))
    if pruned:
        console.print(f"pruned {len(pruned)} old backup(s) beyond keep_last")
    return 0


def cmd_archive(cfg, store, args):
    from arb.archive import run_archive
    res = run_archive(cfg, store)
    console.print(f"archive: moved {res['moved']} quotes into "
                  f"{len(res['months'])} monthly archive(s) under data/archive/")
    return 0


def cmd_canary(cfg, store, args):
    """Monthly synthetic fixture through the full pipeline (§13.3): known
    standards through the assay. A mismatch is a sev-1."""
    from datetime import datetime, timedelta, timezone
    from decimal import Decimal

    from arb import metrics
    from arb.settlement import settle_manual
    from arb.tracker import process_poll
    from tests.canary_fixture import canary_opps  # synthetic, deterministic

    now = datetime.now(timezone.utc)
    tag = now.strftime("%Y%m")
    poll_id = store.insert("polls", {"started_at": now.isoformat(), "kind": "canary",
                                     "n_quotes": 0, "n_opps": 2})
    cfg = dict(cfg, ev_enabled=True)  # canary exercises the frozen path too
    opps, event_key = canary_opps(tag, now)
    process_poll(cfg, store, poll_id, "canary", opps, {}, now=now)
    settle_manual(cfg, store, event_key, "canary-yes", now=now + timedelta(hours=10))
    bets = store.query("SELECT * FROM flat_bets WHERE poll_kind='canary' AND event_key=?",
                       (event_key,))
    expect = {"ARB": Decimal("0.041667"), "EV": Decimal("0.932367")}
    failures = []
    for b in bets:
        got = metrics.bet_pnl_optimistic(store, dict(b))
        if got != expect[b["kind"]]:
            failures.append(f"{b['kind']}: expected {expect[b['kind']]}, got {got}")
    store.insert("annotations", {
        "created_at": now.isoformat(), "target_kind": "day",
        "target_id": now.date().isoformat(), "kind": "canary",
        "detail": "PASS" if not failures else f"SEV-1 FAIL: {failures}"})
    if failures:
        console.print(f"[red]CANARY SEV-1: {failures}[/]")
        return 1
    console.print("[green]canary PASS (ARB 0.041667, EV 0.932367 to the cent)[/]")
    return 0


def cmd_shape_check(cfg, store, args):
    """Weekly live shape-check (§13.3): response SCHEMA (not values) vs fixtures."""
    from arb.venues import base
    problems = []
    checks = [
        ("kalshi", "https://api.elections.kalshi.com/trade-api/v2/markets"
                   "?series_ticker=KXMLBGAME&status=open&limit=1",
         lambda d: {"ticker", "event_ticker", "yes_ask_dollars", "no_ask_dollars",
                    "volume_24h_fp", "close_time"} <=
                   set((d.get("markets") or [{}])[0].keys()) if d.get("markets")
                   else True),
        ("polymarket", "https://gamma-api.polymarket.com/events?tag_slug=mlb"
                       "&closed=false&limit=1",
         lambda d: {"markets", "title"} <= set(d[0].keys()) if d else True),
        ("sx", "https://api.sx.bet/markets/active?leagueId=171&onlyMainLine=true",
         lambda d: {"marketHash", "percentageOdds"}.isdisjoint(set()) and
                   {"marketHash", "teamOneName", "type", "gameTime"} <=
                   set((d.get("data", {}).get("markets") or [{}])[0].keys())),
    ]
    for venue, url, ok in checks:
        try:
            if not ok(base.get_json(url, cfg)):
                problems.append(f"{venue}: schema drift")
        except Exception as e:  # noqa: BLE001
            problems.append(f"{venue}: {e}")
    if problems:
        for p in problems:
            console.print(f"[red]shape-check: {p}[/]")
        return 1
    console.print("[green]shape-check: all venue schemas match fixtures[/]")
    return 0


def cmd_replay(cfg, store, args):
    """Offline policy replay over stored quotes (PROMPT_REPLAY). Opens its own
    read-only handle: nothing here touches the DB the supervisor is writing."""
    from arb.replay import open_ro, replay
    s = replay(cfg, open_ro(store.path), args.from_date, args.to_date, args.kind)
    console.print(f"[dim]replay {args.from_date}..{args.to_date or args.from_date} "
                  f"kind={args.kind} — read-only, nothing written. Sports "
                  f"ARB/XSPREAD/DIVERGENCE detect path only: negRisk and political "
                  f"lanes read raw books, not quote rows, and are NOT replayed.[/]")
    horizon = date.today() - timedelta(days=int(cfg["archive_after_days"]))
    if date.fromisoformat(args.from_date) < horizon:
        console.print(f"[yellow]WARNING[/]: quotes before {horizon} have moved to "
                      f"data/archive/ (archive_after_days) — this replay under-counts "
                      f"that window; a quiet day there is not a replayed day")
    table = Table(title=f"replay — {s.polls} polls, {s.quotes} quotes, "
                        f"edge sum {s.edge_total}")
    for col in ("bucket", "key", "n"):
        table.add_column(col)
    for day, n in sorted(s.days.items()):
        table.add_row("day", day, str(n))
    for (kind, status), n in sorted(s.opps.items()):
        table.add_row("opp", f"{kind}/{status}", str(n))
    for reason, n in sorted(s.rejects.items(), key=lambda kv: -kv[1]):
        table.add_row("reject", reason, str(n))
    for label, n in (("both", s.both), ("replay-only", s.replay_only),
                     ("stored-only", s.stored_only)):
        table.add_row("diff", label, str(n))
    console.print(table)
    if s.examples:
        console.print("[yellow]diff examples — a difference means THE CODE OR CONFIG "
                      "CHANGED, not that history changed:[/]")
        ex = Table()
        for col in ("set", "fingerprint", "event_key", "edge"):
            ex.add_column(col)
        for row in s.examples:
            ex.add_row(*row)
        console.print(ex)
        if s.replay_only + s.stored_only > len(s.examples):
            console.print(f"[dim]... {s.replay_only + s.stored_only - len(s.examples)} "
                          f"further differences not shown (counts above are complete)[/]")
    return 0
