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
    day = args.daily or date.today().isoformat()
    bets = store.query(
        "SELECT * FROM flat_bets WHERE date(created_at) = ? ORDER BY id", (day,))
    polls = store.query(
        "SELECT COUNT(*) c, COALESCE(SUM(n_quotes),0) q FROM polls WHERE date(started_at) = ?",
        (day,))[0]
    table = Table(title=f"Daily report {day} — polls={polls['c']} quotes={polls['q']}")
    for col in ("kind", "lane", "bets", "staked", "pnl_optimistic"):
        table.add_column(col)
    if not bets:
        table.add_row("—", "—", "0", "$0.00", "$0.00")
    console.print(table)
    return 0
