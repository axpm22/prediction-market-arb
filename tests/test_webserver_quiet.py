"""F5: the control-center server must swallow client-disconnect errors (no traceback in
logs/web.log), but stay loud on every other exception."""
from __future__ import annotations

from arb.experimental.webserver import Handler, _QuietServer


def test_disconnect_swallowed_other_errors_loud(capsys):
    srv = _QuietServer(("127.0.0.1", 0), Handler)  # port 0 = ephemeral, no real serving
    try:
        try:
            raise BrokenPipeError("client gone")
        except BrokenPipeError:
            srv.handle_error(None, ("1.2.3.4", 5))
        out = capsys.readouterr()
        assert "disconnected mid-response" in out.out
        assert "Traceback" not in (out.out + out.err)

        try:
            raise ValueError("boom")
        except ValueError:
            srv.handle_error(None, ("1.2.3.4", 5))
        loud = capsys.readouterr()
        assert "Traceback" in (loud.out + loud.err)  # default handler still dumps it
    finally:
        srv.server_close()
