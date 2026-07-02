"""Compute every SPEC_V2 frozen test vector to the cent. Money math in Decimal.
Output is pasted verbatim into SPEC_V2.md's appendix, so this script IS the check."""
from decimal import Decimal as D, getcontext, ROUND_CEILING, ROUND_HALF_UP
import math
getcontext().prec = 40

def q6(x): return D(x).quantize(D("0.000001"), ROUND_HALF_UP)
def cents(x): return D(x).quantize(D("0.01"), ROUND_CEILING)

print("=== DE-VIG (proportional) ===")
def devig_prop(odds):
    q = [D(1)/D(str(o)) for o in odds]
    s = sum(q)
    return [q6(x/s) for x in q], q6(s)
p, s = devig_prop([1.91,1.91]); print("  (1.91,1.91):", p, "sum_impl=", s)
p, s = devig_prop([1.50,2.86,6.00]); print("  (1.50,2.86,6.00):", p, "sum_impl(k)=", s)

print("=== DE-VIG (power) ===  solve k: sum q_i^k = 1")
def devig_power(odds):
    q = [1.0/o for o in odds]
    lo, hi = 0.2, 5.0
    for _ in range(200):
        k = (lo+hi)/2
        if sum(v**k for v in q) > 1: lo = k
        else: hi = k
    k = (lo+hi)/2
    return [q6(v**k) for v in q], round(k,6)
p,k = devig_power([1.50,2.86,6.00]); print("  (1.50,2.86,6.00): k=",k," p=",p)
p,k = devig_power([1.91,1.91]); print("  (1.91,1.91): k=",k," p=",p)

print("=== DE-VIG (shin) ===  solve z: sum p_i(z)=1")
def devig_shin(odds):
    q = [1.0/o for o in odds]; B=sum(q)
    def probs(z): return [ (math.sqrt(z*z+4*(1-z)*qi*qi/B)-z)/(2*(1-z)) for qi in q ]
    lo,hi=1e-9,0.5
    for _ in range(200):
        z=(lo+hi)/2
        if sum(probs(z))>1: lo=z
        else: hi=z
    z=(lo+hi)/2
    return [q6(x) for x in probs(z)], round(z,6)
p,z = devig_shin([1.50,2.86,6.00]); print("  (1.50,2.86,6.00): z=",z," p=",p)

print("=== CONSENSUS median + SE (3-book fixture, proportional de-vig) ===")
# 3 books, 2-outcome moneyline, decimal odds (home, away)
books = {"bookA":(1.80,2.10),"bookB":(1.91,1.91),"bookC":(2.00,1.83)}
home_ps=[]; away_ps=[]
for b,(oh,oa) in books.items():
    (ph,pa),_ = devig_prop([oh,oa]); home_ps.append(float(ph)); away_ps.append(float(pa))
import statistics as st
med_h=st.median(home_ps); med_a=st.median(away_ps)
tot=med_h+med_a
print("  per-book home p:", [round(x,6) for x in home_ps])
print("  median home=",round(med_h,6)," median away=",round(med_a,6)," renorm home=",q6(med_h/tot)," away=",q6(med_a/tot))
se_h=st.pstdev(home_ps)/math.sqrt(len(home_ps))    # dispersion/sqrt(n), population sd
print("  home dispersion(pstdev)=",round(st.pstdev(home_ps),6)," SE=stdev/sqrt(n)=",round(se_h,6))

print("=== EDGE_POINTS & EV ratio (display only) ===")
p_hat=D("0.55"); cost=D("0.50"); fee=D("0.07")*D("0.5")*D("0.5")   # kalshi cost-unit fee at P=.5
c=cost+fee
print("  p_hat=0.55 cost=0.50 kalshi_fee=",q6(fee)," c=",q6(c))
print("  edge_points=p_hat-c=",q6(p_hat-c)," EV_ratio=(p_hat-c)/c=",q6((p_hat-c)/c))

print("=== KALSHI fee ===")
# cost-per-$1 unit: rate*P*(1-P);  money: ceil_to_cent(rate*contracts*P*(1-P))
print("  unit @P=0.50:", q6(D("0.07")*D("0.5")*D("0.5")))
money=D("0.07")*D("10")*D("0.40")*D("0.60"); print("  money 10ct@P=0.40: raw=",money," ceil=",cents(money))

print("=== POLYMARKET linear fee (fee_cost = rate*cost) ===")
print("  sports_fee 0.03 * cost 0.50 =", q6(D("0.03")*D("0.50")))

print("=== SX taker inversion + winning fee ===")
pct = D("55000000000000000000")   # 0.55 * 1e20
taker = D(1) - pct/D("1e20")
print("  percentageOdds=5.5e19 -> taker cost =", q6(taker))
# winning-fee charged conservatively as win_fee_rate*(1-cost) on the SX leg
win_fee = D("0.05")*(D(1)-taker)
print("  win_fee_rate 0.05 * (1-cost) =", q6(win_fee), " effective SX cost =", q6(taker+win_fee))

print("=== TRUE ARB (odds 2.10 & 2.05, B=$100) ===")
ca=D(1)/D("2.10"); cb=D(1)/D("2.05"); S=ca+cb
print("  cost_a=",q6(ca)," cost_b=",q6(cb)," S=",q6(S)," ROI=1/S-1=",q6(D(1)/S-1))
B=D("100"); sa=B*ca/S; sb=B*cb/S
print("  stake_a=",q6(sa)," stake_b=",q6(sb)," payout_a=",q6(sa*D("2.10"))," payout_b=",q6(sb*D("2.05")))
S2=D(1)/D("1.95")+D(1)/D("1.95"); print("  NO-ARB 1.95/1.95 S=",q6(S2))

print("=== DIVERGENCE spread ===")
midA=D("0.58"); midB=D("0.53"); print("  spread=midA-midB=",q6(midA-midB))

print("=== DEPTH-AWARE SLIPPAGE  slip = base + impact*(stake/depth)^exp + vol*sigma ===")
base=D("0.005"); impact=D("0.02"); stake=D("50"); depth=D("500"); exp=D("1"); vol=D("0"); sigma=D("0")
slip = base + impact*(stake/depth)**1 + vol*sigma
print("  base .005 impact .02 stake50 depth500 -> slip =", q6(slip))
print("  legacy (impact=0) -> slip =", q6(base))

print("=== STALENESS DECAY (exponential, half-life 6h) ===")
edge=D("0.03"); hl=D("6"); age=D("3")
lam = D(str(math.log(2)))/hl
dec = edge*D(str(math.exp(-float(lam*age))))
print("  edge .03 half_life 6h age 3h -> decayed edge =", q6(dec))

print("=== EDGE/SE GATE ===")
for se in ("0.015","0.025"):
    r=D("0.02")/D(se); print(f"  edge .02 / SE {se} = {q6(r)}  (gate>=1.0 -> {'PASS' if r>=1 else 'FAIL'})")

print("=== ISOTONIC calibration (PAV) worked example ===")
# (raw p_hat, y) sorted by p_hat; PAV pools adjacent violators of monotonicity
data=[(0.20,0),(0.30,0),(0.40,1),(0.50,0),(0.60,1),(0.80,1)]
# PAV
vals=[float(y) for _,y in data]; w=[1.0]*len(vals)
blocks=[[v,1.0] for v in vals]
i=0
while i < len(blocks)-1:
    if blocks[i][0] > blocks[i+1][0]:
        tw=blocks[i][1]+blocks[i+1][1]
        tv=(blocks[i][0]*blocks[i][1]+blocks[i+1][0]*blocks[i+1][1])/tw
        blocks[i:i+2]=[[tv,tw]]
        if i>0: i-=1
    else: i+=1
# expand
out=[];
for v,wt in blocks: out += [round(v,4)]*int(wt)
print("  raw p_hat:", [d[0] for d in data])
print("  y:        ", [d[1] for d in data])
print("  isotonic-> ", out)

print("=== NEG-RISK basket (linear fee both sides, C0) ===")
# 3 mutually-exclusive-exhaustive outcomes, ask costs; linear poly fee rate 0.03
asks=[D("0.30"),D("0.34"),D("0.33")]; rate=D("0.03")
eff=[a+rate*a for a in asks]; Sb=sum(eff)
print("  asks",[str(a) for a in asks]," eff(+3% linear)=",[str(q6(e)) for e in eff]," S=",q6(Sb)," edge=",q6(D(1)-Sb))
