# FAQ — the short version

One page: the three findings, then the methodology questions the project is most
often challenged on.

## The three findings

1. **Cross-venue sports arb is a mirage.** ~945k quotes, 44 settled arbs net +$0.13
   (~$0.003/bet). Flash re-quote: 33 verified, 10 vanished. The edge dies to fees,
   thin depth, and settlement-rule mismatch. The system was built to find that out
   honestly — and a $0 result, rigorously established, is a real result.
2. **The EV signal is contaminated and unproven.** 856 flags; the raw ">5%" bucket showed
   four-digit "edges" — tiny asks on empty books. Quality layer quarantines them (label,
   never delete); on the clean signal, CLV hasn't beaten the venue close, so EV betting
   stays disabled.
3. **Built the lower-risk alternatives** (negative-risk basket arb, favorite-longshot,
   lead-lag), each pre-registered, each measuring — no overclaiming ahead of n.

## The three hardest methodology questions

**Q: Why flat-$1 for the headline instead of Kelly?**
Flat-$1 is a *measurement* unit — it makes every flag comparable and isolates "did the
flag win?" from "how would sizing have compounded?". Kelly is a parallel projection column;
letting it drive the headline would conflate signal quality with a bankroll-growth model
and hide whether the underlying edge is even real. Measure first, size second.

**Q: Why is the optimistic−confirmed gap a valid slippage proxy?**
Optimistic fills at first-sighting prices (what a perfect, instant taker sees); confirmed
fills at the second consecutive observation (what someone slightly slower than the market
gets). The gap is the empirical cost of not being first. It's a *proxy* — it doesn't model
partial fills or real latency — but it's an honest lower bound on slippage built from
observed prices, not assumed parameters. The flash verifier tightens it to ~10s, and adds
a sequential-fill edge (hold one leg at detection price, re-quote the rest) to measure leg
risk directly.

**Q: You de-vig by proportional normalization, which biases favorites/longshots. Why trust
the consensus?**
I don't, fully — that's why per-book probabilities are persisted, so any flag can be
re-graded under Shin/power/additive de-vig without re-running the experiment (instrumentation
is mutable; the frozen analysis rules are not). And it's why the EV gate is CLV, not the
de-vig itself: CLV asks "did the price beat the venue's own close," which is agnostic to my
probability model. If CLV is positive, the edge is real regardless of de-vig bias; if it's
not, no de-vig refinement saves it.

## The meta-point

This isn't a profitable bot — it's a measurement instrument with a pre-registered
methodology and an append-only ledger that refuses to lie. It killed its own headline
thesis (riskless arb) for the cost of a laptop's electricity, and it won't enable a signal
it can't prove. That discipline — not a P&L number — is the deliverable.
