"""karlon_client.py — the one place PC-side scripts build their HTTP session.

wa_bridge.py and invoice_worker.py each used bare `requests.get/post` with
their own ad-hoc timeouts and hand-rolled retry loops: a fresh TCP+TLS
handshake per call (expensive against a HuggingFace Space) and retry logic
that lived in only one of the two callers. This module gives every caller a
pooled keep-alive session with the same timeouts, retry policy and (optional)
auth header.

Retry policy is deliberately asymmetric:
  * connect errors are retried for every method — the request never reached
    the server, so replaying it cannot double-apply anything;
  * read timeouts and 429/5xx responses are retried for GET/HEAD only. A POST
    or PATCH whose response was lost may already have taken effect (e.g.
    invoices `claim`), so those callers decide for themselves. This is the
    end-to-end argument (Saltzer, Reed & Clark, MIT, 1984): the transport
    cannot know whether a replay is safe — the endpoint that owns the
    operation's idempotence must.
"""

from __future__ import annotations

import threading

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from local_config import KARLON_API_TOKEN

# (connect, read). A HuggingFace Space woken from idle can take 20-30s to
# answer its first request, so read is generous; connect stays short so a
# dead host fails fast.
HTTP_TIMEOUT = (15, 60)
UPLOAD_TIMEOUT = (15, 120)
DOWNLOAD_TIMEOUT = (15, 60)

_RETRY = Retry(
    total=4,
    connect=4,
    read=2,
    status=3,
    backoff_factor=1.0,            # 0s, 2s, 4s, 8s ... between attempts
    status_forcelist=(429, 502, 503, 504),
    allowed_methods=frozenset({"GET", "HEAD"}),
    respect_retry_after_header=True,
    raise_on_status=False,
)


def make_session() -> requests.Session:
    """A new pooled session. `requests.Session` is not guaranteed thread-safe
    for cookie mutation, but we use no cookies; wa_bridge's two poller threads
    and main thread each call `get_session()` and get their own instance."""
    s = requests.Session()
    adapter = HTTPAdapter(max_retries=_RETRY, pool_connections=4, pool_maxsize=8)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers["User-Agent"] = "Mozilla/5.0 (KarlonBridge)"
    if KARLON_API_TOKEN:
        s.headers["Authorization"] = f"Bearer {KARLON_API_TOKEN}"
    return s


_local = threading.local()


def get_session() -> requests.Session:
    """Per-thread session (one per poller thread, one for the main thread)."""
    s = getattr(_local, "session", None)
    if s is None:
        s = _local.session = make_session()
    return s
