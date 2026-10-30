"""Cached wrapper around the single paced Client; call counter. Raw responses in data/corr/raw/."""
import json, hashlib, os
from arb.mlb.kalshi_pull import Client
os.makedirs("data/corr/raw", exist_ok=True)
_cl = None
def get(path, params=None, cap=450):
    global _cl
    _cl = _cl or Client()
    k = hashlib.md5((path + json.dumps(params, sort_keys=True)).encode()).hexdigest()
    f = f"data/corr/raw/{k}.json"
    if os.path.exists(f):
        return json.load(open(f))
    n = sum(1 for _ in open("data/corr/calls.log")) if os.path.exists("data/corr/calls.log") else 0
    assert n < cap, "call cap"
    st, j = _cl.get(path, params)
    open("data/corr/calls.log", "a").write(f"{path}\t{params}\t{st}\n")
    json.dump([st, j], open(f, "w"))
    return [st, j]
