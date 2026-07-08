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
