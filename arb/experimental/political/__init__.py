"""Political / econ / weather market hunting.

Split into focused modules:
  intramarket.py — YES+NO < $1 on ONE contract (no matching, no cross-venue risk)
  series.py      — load the discovered Kalshi series cache, horizon-filtered
  sweep.py       — the fetch + detect + persist loop
"""
