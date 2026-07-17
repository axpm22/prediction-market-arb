"""Redirect all arb file logging out of the repo BEFORE any arb import:
logutil caches real-path TimedRotatingFileHandlers at first import, so tests
were appending fake supervisor events into production logs/ (observed
2026-07-17 in logs/supervisor.log). Deliberately an env var, not config.yaml:
this is test plumbing, not a tunable."""
import os
import tempfile

os.environ.setdefault("ARB_LOG_DIR", tempfile.mkdtemp(prefix="arb-test-logs-"))
