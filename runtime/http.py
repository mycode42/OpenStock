"""Shared HTTP sessions for broker and market-data calls.

Each thread gets its own session so keep-alive connections are reused without
sharing a pool across threads. Retries apply to GET only: order submission is
a POST and must never be replayed by the transport layer, since the execution
layer resolves ambiguous submissions by client order ID.
"""

from __future__ import annotations

import threading

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


_LOCAL = threading.local()


def _build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=2,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=8))
    return session


def get_session() -> requests.Session:
    """Return this thread's pooled session."""
    session = getattr(_LOCAL, "session", None)
    if session is None:
        session = _build_session()
        _LOCAL.session = session
    return session
