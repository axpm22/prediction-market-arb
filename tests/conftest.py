"""Redirect all arb file logging out of the repo BEFORE any arb import:
logutil caches real-path TimedRotatingFileHandlers at first import, so tests
were appending fake supervisor events into production logs/ (observed
2026-07-17 in logs/supervisor.log). Deliberately an env var, not config.yaml:
this is test plumbing, not a tunable."""
import os
import tempfile

os.environ.setdefault("ARB_LOG_DIR", tempfile.mkdtemp(prefix="arb-test-logs-"))

# Global venue request pacing sleeps real seconds against a shared flock file.
# Rule 2 means no test makes a live venue call, so pacing buys nothing here and
# costs flakiness: its sleep is indistinguishable from the retry backoff ladder
# that tests/test_venues.py asserts on.
os.environ.setdefault("ARB_PACE_DISABLED", "1")
