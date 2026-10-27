"""Kite websocket price cache (market_data/tick_feed.py) and the Proposer runner's TickMarket."""
from live import proposer_runner as pr
from market_data import tick_feed as tf


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeTicker:
    MODE_FULL = "full"

    def __init__(self, api_key, access_token):
        self.session = (api_key, access_token)
        self.subscribed, self.unsubscribed, self.modes = [], [], []
        self.connected = self.closed = False

    def connect(self, threaded=False):
        self.connected = True

    def close(self):
        self.closed = True

    def subscribe(self, tokens):
        self.subscribed += list(tokens)

    def unsubscribe(self, tokens):
        self.unsubscribed += list(tokens)

    def set_mode(self, mode, tokens):
        self.modes.append((mode, list(tokens)))


def _feed(creds=("k", "t1")):
    made, clock, box = [], Clock(), {"creds": creds}

    def factory(api_key, access_token):
        made.append(FakeTicker(api_key, access_token))
        return made[-1]
    return tf.TickFeed(lambda: box["creds"], ticker_factory=factory, clock=clock), made, clock, box


def _tick(token, ltp, bid=None, ask=None):
    t = {"instrument_token": token, "last_price": ltp}
    if bid is not None:
        t["depth"] = {"buy": [{"price": bid}, {"price": bid - 0.5}], "sell": [{"price": ask}, {"price": ask + 0.5}]}
    return t


def test_watch_connects_and_subscribes_full_mode_on_connect():
    feed, made, _, _ = _feed()
    feed.watch(265, always=True)
    assert len(made) == 1 and made[0].connected
    feed._on_connect(made[0], None)
    assert made[0].subscribed == [265] and made[0].modes == [("full", [265])]
    feed.watch(123)                      # a new token after connect is subscribed at once
    assert made[0].subscribed == [265, 123]


def test_price_is_fresh_then_stale():
    feed, made, clock, _ = _feed()
    feed.watch(9, always=False)
    feed._on_connect(made[0], None)
    feed._on_ticks(made[0], [_tick(9, 280.0, 279.5, 280.5)])
    assert feed.price(9) == {"ltp": 280.0, "bid": 279.5, "ask": 280.5}
    assert feed.ticked.is_set()
    clock.t += tf.STALE_S + 0.1
    assert feed.price(9) is None         # the caller falls back to REST


def test_idle_option_is_unsubscribed_index_kept():
    feed, made, clock, _ = _feed()
    feed.watch(265, always=True)
    feed.watch(9)
    feed._on_connect(made[0], None)
    clock.t += tf.IDLE_S + 1
    feed.watch(265, always=True)
    assert made[0].unsubscribed == [9]


def test_rejected_session_backs_off_until_a_new_token():
    feed, made, clock, box = _feed()
    feed.watch(265, always=True)
    feed._on_error(made[0], 403, "403 Forbidden")
    assert made[0].closed and not feed.live()
    feed.watch(265, always=True)
    assert len(made) == 1                # same token inside the backoff: no reconnect storm
    box["creds"] = ("k", "t2")
    feed.watch(265, always=True)
    assert len(made) == 2 and made[1].session == ("k", "t2")


def test_new_daily_token_reopens_the_socket():
    feed, made, _, box = _feed()
    feed.watch(265, always=True)
    box["creds"] = ("k", "t2")
    feed.watch(265, always=True)
    assert made[0].closed and made[1].session == ("k", "t2")


class FakeKite:
    api_key, access_token = "k", "t1"

    def __init__(self):
        self.quotes = self.ltps = 0

    def quote(self, key):
        self.quotes += 1
        return {key: {"instrument_token": 777, "last_price": 300.0,
                      "depth": {"buy": [{"price": 299.0}], "sell": [{"price": 301.0}]}}}

    def ltp(self, keys):
        self.ltps += 1
        keys = [keys] if isinstance(keys, str) else keys
        return {k: {"last_price": 25000.0 if "NIFTY" in k else 82000.0} for k in keys}


def _market():
    kite, feed, made, clock = FakeKite(), *_feed()[:3]
    m = pr.TickMarket(feed=feed)
    m._kite = lambda: kite
    return m, kite, feed, made, clock


def test_option_quote_rest_first_then_ticks_then_rest_when_stale():
    m, kite, feed, made, clock = _market()
    assert m.quote("SENSEX2610382000CE") == {"ltp": 300.0, "bid": 299.0, "ask": 301.0}
    assert kite.quotes == 1
    feed._on_connect(made[0], None)
    assert 777 in made[0].subscribed
    feed._on_ticks(made[0], [_tick(777, 305.0, 304.5, 305.5)])
    assert m.quote("SENSEX2610382000CE") == {"ltp": 305.0, "bid": 304.5, "ask": 305.5}
    assert kite.quotes == 1              # served from the tick
    clock.t += tf.STALE_S + 0.1
    assert m.quote("SENSEX2610382000CE")["ltp"] == 300.0
    assert kite.quotes == 2


def test_tick_without_depth_is_not_used_for_an_option():
    m, kite, feed, made, _ = _market()
    m.quote("X")
    feed._on_ticks(made[0], [_tick(777, 305.0)])
    assert m.quote("X")["ltp"] == 300.0 and kite.quotes == 2


def test_spots_from_ticks_else_rest():
    m, kite, feed, made, _ = _market()
    assert m.spots() == {"SENSEX": 82000.0, "NIFTY": 25000.0} and kite.ltps == 1
    feed._on_ticks(made[0], [_tick(265, 82010.0), _tick(256265, 25005.0)])
    assert m.spots() == {"SENSEX": 82010.0, "NIFTY": 25005.0} and kite.ltps == 1
    assert m.spot() == 82010.0 and kite.ltps == 1


def test_heartbeat_written_at_most_every_two_seconds(monkeypatch):
    calls = []
    monkeypatch.setattr(pr, "publish_heartbeat", lambda *a: calls.append(a) or False)
    ctx = {"task_id": "t"}
    assert pr.process_connection("u", "c", ctx=ctx, feed=None, market=None, now=None) is False
    assert pr.process_connection("u", "c", ctx=ctx, feed=None, market=None, now=None) is False
    assert len(calls) == 2               # a failed claim is retried, never remembered
