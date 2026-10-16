import http.server
import threading
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from src.http_session import new_session, retry_after_seconds

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


def test_retry_after_reads_whole_seconds():
    assert retry_after_seconds("120", NOW) == 120.0


def test_retry_after_reads_an_http_date():
    assert retry_after_seconds("Wed, 14 Oct 2026 12:05:00 GMT", NOW) == 300.0


def test_retry_after_in_the_past_is_zero():
    assert retry_after_seconds("Wed, 14 Oct 2026 11:00:00 GMT", NOW) == 0.0


@pytest.mark.parametrize("value", [None, "", "soon", "-5", "1.5e9x", MagicMock(), 42])
def test_a_missing_or_garbled_retry_after_is_none(value):
    assert retry_after_seconds(value, NOW) is None


def test_the_session_is_reused_and_never_sends_cookies_back():
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(dict(self.headers))
            self.send_response(200)
            self.send_header("Set-Cookie", "bcookie=abc; Path=/")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        session = new_session()
        url = f"http://127.0.0.1:{server.server_port}/x"
        session.get(url, headers={"User-Agent": "ua"})
        session.get(url, headers={"User-Agent": "ua"})
    finally:
        server.shutdown()
    assert "Cookie" not in seen[1]
    assert seen[0] == seen[1]
