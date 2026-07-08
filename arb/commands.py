"""Command handlers. Phase 1: report renders the (empty) daily frame; the rest
arrive in their SPEC §16 phases and are absent until then."""
from __future__ import annotations

from datetime import date

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

    if not cfg["negrisk"]["enabled"]:
        console.print("[yellow]negrisk disabled[/]")
        return 0
    tags = list(cfg["nonsports"]["polymarket_tags"])
    events = polymarket.fetch_negrisk_events(cfg, tags)[:int(cfg["negrisk"]["scan_limit"])]
    console.print(f"[dim]negrisk: {len(events)} flagged events across tags {tags}[/]")
    all_opps = []
    for e in events:
        markets = [m for m in e.get("markets", []) if m.get("clobTokenIds")]
        if not markets or len(markets) > int(cfg["negrisk"]["max_outcomes"]):
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
        if any(o["yes_ask"] is None or o["yes_bid"] is None for o in outcomes):
            continue  # a one-sided outcome fails proof condition (c) anyway; skip math
        opps = scan_group(cfg, "polymarket", "politics", f"nonsports:pm:{e['id']}",
                          e.get("title", ""), outcomes, venue_flag=bool(e.get("negRisk")))
        all_opps.extend(opps)

    poll_id = store.insert("polls", {
        "started_at": datetime.now(timezone.utc).isoformat(), "kind": "negrisk",
        "n_quotes": 0, "n_opps": len(all_opps)})
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
    from arb.report import print_daily, write_daily
    day = args.daily or date.today().isoformat()
    polls = store.query(
        "SELECT COUNT(*) c, COALESCE(SUM(n_quotes),0) q FROM polls WHERE date(started_at) = ?",
        (day,))[0]
    console.print(f"[dim]{day}: polls={polls['c']} quotes={polls['q']}[/]")
    write_daily(cfg, store, day)
    print_daily(cfg, store, day)
    return 0


def cmd_scan(cfg, store, args):
    """Odds API consensus refresh (SPEC §13.1). RULE 2: the only credit-spending
    command besides settle scores; no-op without a key or when disabled."""
    from datetime import datetime, timezone

    from arb.consensus import compute_consensus
    from arb.venues import oddsapi
    if not cfg["consensus_enabled"]:
        console.print("[dim]consensus disabled; scan is a no-op[/]")
        return 0
    if not oddsapi.api_key():
        console.print("[yellow]no ODDS_API_KEY in .env — scan is a no-op "
                      "(add a free key from the-odds-api.com)[/]")
        return 0
    now = datetime.now(timezone.utc).isoformat()
    total_c, total_b = 0, 0
    for sport_key in cfg["consensus_sports"]:
        rows = oddsapi.fetch_odds(cfg, str(sport_key))
        cons, books = compute_consensus(cfg, rows, now)
        store.insert_many("consensus", cons)
        store.insert_many("consensus_books", books)
        total_c += len(cons)
        total_b += len(books)
    if oddsapi.LAST_REMAINING is not None:  # dashboard/status read the latest row
        store.insert("annotations", {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "target_kind": "day", "target_id": now[:10], "kind": "oddsapi-credits",
            "detail": str(oddsapi.LAST_REMAINING)})
    console.print(f"consensus rows: {total_c}; per-book rows: {total_b}")
    return 0


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


def cmd_political(cfg, store, args):
    """POLITICAL lane: Polymarket-only intra-market YES+NO<1 (§12)."""
    import json as _json
    from datetime import datetime, timezone

    from arb.lanes.political import scan_market
    from arb.tracker import process_poll
    from arb.venues import polymarket
    events = []
    for tag in cfg["nonsports"]["polymarket_tags"]:
        events.extend(polymarket.fetch_gamma_events(cfg, str(tag), max_pages=2))
    markets = [m for e in events for m in e.get("markets", [])
               if m.get("clobTokenIds") and not m.get("negRisk")]
    tokens = [t for m in markets for t in _json.loads(m["clobTokenIds"])]
    books = polymarket.fetch_books(cfg, tokens)
    opps = [o for m in markets if (o := scan_market(cfg, m, books))]
    now = datetime.now(timezone.utc)
    poll_id = store.insert("polls", {"started_at": now.isoformat(), "kind": "political",
                                     "n_quotes": len(tokens), "n_opps": len(opps)})
    process_poll(cfg, store, poll_id, "political", opps,
                 {"polymarket": {"ok": True, "n": len(tokens), "latency_ms": 0}}, now=now)
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


def cmd_verify(cfg, store, args):
    from arb.lanes.verify import run_once
    n = run_once(cfg, store)
    console.print(f"verify: re-quoted {n} fresh locked flags")
    return 0


def cmd_supervisor(cfg, store, args):
    from arb.heartbeat import run_supervisor
    return run_supervisor(cfg)


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
                < 3 * float(cfg["watch_interval_secs"]) for b in beats)
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
        offset = float(out.split()[-2].lstrip("+")) if out else None
        if offset is not None and abs(offset) > float(cfg["clock_skew_alarm_secs"]):
            console.print(f"[red]CLOCK SKEW ALARM: {offset}s[/]")
        elif offset is not None:
            console.print(f"clock offset: {offset}s")
    except Exception:  # noqa: BLE001
        console.print("[dim]ntp check unavailable[/]")

    today = now.date().isoformat()
    lanes = store.query(
        "SELECT lane, COUNT(*) n FROM flat_bets WHERE date(created_at)=? GROUP BY lane",
        (today,))
    console.print("today's bets by lane: "
                  + (", ".join(f"{r['lane']}={r['n']}" for r in lanes) or "none"))
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


def cmd_backup(cfg, store, args):
    from datetime import datetime, timezone
    from pathlib import Path
    dest = Path(str(cfg["db_backup"]["dest"]))
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / f"arb-{datetime.now(timezone.utc).strftime('%Y%m%d')}.sqlite"
    store.conn.execute("VACUUM INTO ?", (str(out),))
    console.print(f"backup written: {out} ({out.stat().st_size} bytes)")
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
