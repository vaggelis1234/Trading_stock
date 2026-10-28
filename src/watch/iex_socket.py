"""Alpaca IEX trade stream.

The socket only reads trades. It does not submit orders. Health stays on
``IexFeed``: a quiet symbol is not a disconnect, and an unconfirmed or stale
socket cannot be used for a price.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Callable, Optional

import pandas as pd

from src.watch.feed import IEX_STREAM_URL, IexFeed

logger = logging.getLogger(__name__)

_MAX_BACKOFF_SECONDS = 30.0


class IexAuthenticationFailed(RuntimeError):
    """The stream rejected the credentials. Reconnect would repeat the same failure."""


def run_iex_connection(feed: IexFeed, socket, credentials: tuple[str, str], clock: Callable[[], pd.Timestamp], stop: Callable[[], bool]) -> None:
    """Drive one open socket until it stops or the server drops it."""
    feed.connect()
    _ingest_raw(feed, socket.recv(), clock())
    key, secret = credentials
    socket.send(json.dumps({"action": "auth", "key": key, "secret": secret}))
    _ingest_raw(feed, socket.recv(), clock())
    if not feed.authenticated_ok():
        feed.disconnect()
        raise IexAuthenticationFailed("IEX authentication was not confirmed")
    _send_subscribe(socket, feed.required)
    feed.take_resubscribe()
    _ingest_raw(feed, socket.recv(), clock())
    while not stop():
        pending = feed.take_resubscribe()
        if pending is not None:
            _send_subscribe(socket, pending)
        try:
            raw = socket.recv(timeout=5)
        except TimeoutError:
            continue
        _ingest_raw(feed, raw, clock())


def run_iex_until_stopped(
    feed: IexFeed,
    connect: Callable,
    credentials: tuple[str, str],
    clock: Callable[[], pd.Timestamp],
    stop: Callable[[], bool],
    sleep: Optional[Callable[[float], None]] = None,
) -> None:
    """Reconnect with backoff. Authentication failure stops the loop."""
    delay = 1.0
    while not stop():
        try:
            with connect() as socket:
                run_iex_connection(feed, socket, credentials, clock, stop)
            delay = 1.0
        except IexAuthenticationFailed:
            logger.error("IEX stream rejected authentication; buys stay blocked")
            feed.disconnect()
            return
        except Exception:
            logger.exception("IEX stream disconnected; buys stay blocked until it resubscribes")
            feed.disconnect()
            if stop():
                return
            if sleep is not None:
                sleep(delay)
            delay = min(delay * 2, _MAX_BACKOFF_SECONDS)


def start_iex_stream(feed: IexFeed, key: str, secret: str):
    """Start a daemon reader. Returns a stopper. Missing credentials do not connect."""
    if not key or not secret:
        logger.warning("IEX stream not started; Alpaca credentials are missing. Buys stay blocked.")
        return None
    stop_event = threading.Event()

    def connect():
        from websockets.sync.client import connect as open_socket

        return open_socket(IEX_STREAM_URL, open_timeout=10, close_timeout=5)

    def clock():
        return pd.Timestamp.now(tz="UTC")

    def sleep(seconds: float) -> None:
        stop_event.wait(seconds)

    thread = threading.Thread(
        target=run_iex_until_stopped,
        args=(feed, connect, (key, secret), clock, stop_event.is_set, sleep),
        name="iex-stream",
        daemon=True,
    )
    thread.start()

    def stop() -> None:
        stop_event.set()
        feed.disconnect()

    return stop


def _send_subscribe(socket, symbols) -> None:
    socket.send(json.dumps({"action": "subscribe", "trades": list(symbols)}))


def _ingest_raw(feed: IexFeed, raw, now) -> None:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if not isinstance(raw, str):
        feed.ingest(raw, now)
        return
    feed.ingest(json.loads(raw), now)
