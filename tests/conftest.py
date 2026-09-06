"""Redirect all arb file logging out of the repo BEFORE any arb import:
logutil caches real-path TimedRotatingFileHandlers at first import, so tests
were appending fake supervisor events into production logs/ (observed
2026-07-17 in logs/supervisor.log). Deliberately an env var, not config.yaml:
this is test plumbing, not a tunable."""
import os
import sys
import tempfile

import pytest

os.environ.setdefault("ARB_LOG_DIR", tempfile.mkdtemp(prefix="arb-test-logs-"))

# Global venue request pacing sleeps real seconds against a shared flock file.
# Rule 2 means no test makes a live venue call, so pacing buys nothing here and
# costs flakiness: its sleep is indistinguishable from the retry backoff ladder
# that tests/test_venues.py asserts on.
os.environ.setdefault("ARB_PACE_DISABLED", "1")


# tests/test_config.py asserts the SHIPPED string values of config.yaml
# itself (spec-fidelity: "does the file on disk still say data/whale_tape.
# sqlite") — it never opens the path, so it is the one legitimate case that
# needs the real, un-redirected config.
_NO_REDIRECT_MODULES = {"test_config", "tests.test_config"}


@pytest.fixture(autouse=True)
def _no_production_tape(tmp_path, monkeypatch, request):
    """Regrade 2026-08-29: every per-module `cfg` fixture loads the shipped
    config.yaml unchanged, so `cfg["whale"]["tape_db"]` (and maker's, and
    kalshi_history's L11 trade tape) point at the LIVE production tapes under
    data/ — files the running collectors rewrite every cycle. A test that
    opens one reads production data, and reading the SAME file twice across
    two calls (e.g. the hash-chain test) can see two different snapshots of
    it, which is the flake. Redirect all three to this test's own tmp_path.

    Every `cfg` fixture does `from arb.config import load`, which binds a
    private reference in that test module's namespace at collection time —
    patching `arb.config.load` alone would never reach it, since that name
    lookup happens in the test module's own globals, not arb.config's. So
    also patch the `load` attribute on every already-imported module that
    holds that same reference (which covers both import styles: modules
    that later do `arb.config.load(...)` see the patched module attribute
    directly; modules that already did `from arb.config import load` get
    their own copy of the reference patched here by identity)."""
    if request.module.__name__ in _NO_REDIRECT_MODULES:
        yield
        return
    import arb.config as config_mod
    orig_load = config_mod.load

    def _redirected(*args, **kwargs):
        cfg = orig_load(*args, **kwargs)
        cfg["whale"]["tape_db"] = str(tmp_path / "whale_tape.sqlite")
        cfg["maker"]["tape_db"] = str(tmp_path / "pm_tape.sqlite")
        cfg["bench"]["tape_db"] = str(tmp_path / "kalshi_tape.sqlite")
        return cfg

    monkeypatch.setattr(config_mod, "load", _redirected)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "load", None) is orig_load:
            monkeypatch.setattr(mod, "load", _redirected, raising=False)
    yield


@pytest.fixture(autouse=True)
def _no_production_ledger(tmp_path, monkeypatch):
    """F1 (portability regrade, 2026-09-06): arb.cli.DEFAULT_DB is the bare
    relative path "data/arb.sqlite" — the live production ledger when tests
    run from the repo root. Any Store(db or DEFAULT_DB) call that forgets to
    pass db= (arb.heartbeat.run_supervisor did, before its stop-flag fix)
    silently opens it: on a dev box the real 6.9GB ledger, on a fresh box
    whatever stale file an older branch left behind — schema mismatches
    either way. arb.cli.DEFAULT_DB is read fresh at call time everywhere it's
    used (heartbeat's `from arb.cli import DEFAULT_DB` is a local import
    inside the function body, and cli.main() looks it up as a module global
    when it runs) so patching the one module attribute is enough — no
    per-module copy to chase, unlike config.load above."""
    import arb.cli as cli_mod
    monkeypatch.setattr(cli_mod, "DEFAULT_DB", str(tmp_path / "unused-default.sqlite"))
