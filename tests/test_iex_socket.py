"""IEX socket lifecycle without a network connection."""

import json

import pandas as pd

from src.watch.feed import IexFeed
from src.watch.iex_socket import run_iex_connection, run_iex_until_stopped


def _et():
    return pd.Timestamp("2026-10-19 11:00", tz="America/New_York")


def _message(payload) -> str:
    return json.dumps(payload)


class _Socket:
    def __init__(self, incoming):
        self.incoming = list(incoming)
        self.sent = []

    def recv(self, timeout=None):
        del timeout
        if not self.incoming:
            raise TimeoutError()
        item = self.incoming.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def send(self, payload):
        self.sent.append(json.loads(payload))


class _Connection:
    def __init__(self, socket):
        self.socket = socket

    def __enter__(self):
        return self.socket

    def __exit__(self, exc_type, exc, traceback):
        del exc_type, exc, traceback
        return False


def _arm_messages(symbols):
    return [
        _message([{"T": "success", "msg": "connected"}]),
        _message([{"T": "success", "msg": "authenticated"}]),
        _message([{"T": "subscription", "trades": list(symbols)}]),
        _message([{"T": "t", "S": symbols[0], "p": 10.5}]),
    ]


def test_handshake_confirms_health_and_a_quiet_symbol_is_not_a_disconnect():
    feed = IexFeed()
    feed.set_required([], ["MSFT", "AAPL"])
    socket = _Socket(_arm_messages(["MSFT", "AAPL"]))
    timeouts = {"n": 0}

    def stop():
        return timeouts["n"] >= 1

    def clock():
        return _et()

    original = socket.recv

    def recv(timeout=None):
        try:
            return original(timeout)
        except TimeoutError:
            timeouts["n"] += 1
            raise

    socket.recv = recv
    run_iex_connection(feed, socket, ("key", "secret"), clock, stop)
    assert socket.sent[0]["action"] == "auth"
    assert socket.sent[0]["secret"] == "secret"
    assert socket.sent[1] == {"action": "subscribe", "trades": ["MSFT", "AAPL"]}
    assert feed.healthy(_et()) is True
    assert feed.price("MSFT") == 10.5
    assert feed.price("AAPL") is None
    assert feed.connected is True


def test_auth_failure_stops_and_keeps_the_feed_unhealthy():
    feed = IexFeed()
    socket = _Socket(
        [
            _message([{"T": "success", "msg": "connected"}]),
            _message([{"T": "error", "code": 401, "msg": "auth failed"}]),
        ]
    )
    calls = {"n": 0}

    def connect():
        calls["n"] += 1
        return _Connection(socket)

    sleeps = []
    run_iex_until_stopped(feed, connect, ("key", "secret"), _et, lambda: False, sleeps.append)
    assert calls["n"] == 1
    assert sleeps == []
    assert feed.healthy(_et()) is False
    assert feed.auth_failed is True


def test_disconnect_reconnects_once_and_a_stale_socket_is_unhealthy():
    feed = IexFeed(stale_after=pd.Timedelta(seconds=30))
    feed.set_required([], ["AAPL"])
    first = _Socket([ConnectionResetError("dropped")])
    second = _Socket(_arm_messages(["AAPL"]))
    opened = {"n": 0}

    def connect():
        opened["n"] += 1
        if opened["n"] == 1:
            raise ConnectionResetError("reset")
        return _Connection(second)

    timeouts = {"n": 0}

    def stop():
        return timeouts["n"] >= 1

    original = second.recv

    def recv(timeout=None):
        try:
            return original(timeout)
        except TimeoutError:
            timeouts["n"] += 1
            raise

    second.recv = recv
    sleeps = []
    run_iex_until_stopped(feed, connect, ("key", "secret"), _et, stop, sleeps.append)
    assert opened["n"] == 2
    assert sleeps == [1.0]
    assert feed.healthy(_et()) is True
    assert feed.healthy(_et() + pd.Timedelta(seconds=31)) is False
    assert feed.price("AAPL") == 10.5


def test_resubscribe_uses_the_new_symbol_list():
    feed = IexFeed()
    feed.set_required([], ["AAPL"])
    socket = _Socket(
        [
            *_arm_messages(["AAPL"]),
            _message([{"T": "subscription", "trades": ["AAPL", "MSFT"]}]),
        ]
    )
    timeouts = {"n": 0}

    def stop():
        return timeouts["n"] >= 1

    original = socket.recv

    def recv(timeout=None):
        try:
            return original(timeout)
        except TimeoutError:
            timeouts["n"] += 1
            raise

    socket.recv = recv
    feed.set_required = feed.set_required

    def clock():
        if len(socket.sent) >= 2 and "MSFT" not in feed.required:
            feed.set_required([], ["AAPL", "MSFT"])
        return _et()

    run_iex_connection(feed, socket, ("key", "secret"), clock, stop)
    assert socket.sent[-1] == {"action": "subscribe", "trades": ["AAPL", "MSFT"]}
    assert feed.healthy(_et()) is True
