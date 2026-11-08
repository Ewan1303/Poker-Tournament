"""The browser table: sessions you watch or play, and the web server around them."""

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from poker_tournament.arena import SessionConfig, TableSession
from poker_tournament.loader import BUILTIN_BOTS
from poker_tournament.tournament import TournamentConfig, discover_entries, run_tournament
from poker_tournament.webapp import App, create_server

AVAILABLE = {p.name: p for p in sorted(BUILTIN_BOTS.iterdir()) if (p / "bot.py").exists()}


def until(session, done, answer=None, timeout=60):
    """Follow snapshots until done(snapshot); answer(you) is called on your turns."""
    snap = session.wait(-1)
    deadline = time.monotonic() + timeout
    while not done(snap):
        assert time.monotonic() < deadline, "the table got stuck"
        assert snap["status"] != "error", snap["error"]
        if snap["you"] and answer:
            try:
                session.submit_action(answer(snap["you"]))
            except ValueError:
                pass  # the turn moved on while we were deciding
        snap = session.wait(snap["version"], timeout=2)
    return snap


def test_playing_hides_bot_cards_until_showdown_and_compares():
    config = SessionConfig(
        mode="play",
        bots=["random_bot", "calling_station"],
        compare=["equity_bot", "calling_station"],
        speed="instant",
        pause_after_hand=0,
    )
    session = TableSession(config, AVAILABLE)
    session.start()
    try:
        def check(snap):
            for seat in snap["seats"]:
                if seat["you"] or not seat["cards"] or seat["cards"] == ["back", "back"]:
                    continue
                # A bot's cards are only ever sent when they were shown at showdown.
                assert snap["result"] and seat["seat"] in snap["result"]["shown"]
            return snap["scoreboard"]["hands"] >= 12 and (snap["comparison"]["rows"][0]["last"] is not None)

        snap = until(session, check, answer=lambda you: {"type": "call"})
    finally:
        session.stop()
    names = {row["name"] for row in snap["comparison"]["rows"]}
    assert names == {"You", "equity_bot", "calling_station"}
    you = next(row for row in snap["scoreboard"]["rows"] if row["you"])
    assert you["total"] == next(row for row in snap["comparison"]["rows"] if row["you"])["total"]
    assert sum(row["total"] for row in snap["scoreboard"]["rows"]) == 0  # chips only move between players


def test_your_raise_is_applied():
    config = SessionConfig(mode="play", bots=["calling_station"], speed="instant", pause_after_hand=0)
    session = TableSession(config, AVAILABLE)
    session.start()
    try:
        snap = until(session, lambda s: s["you"] is not None)
        you = snap["you"]
        assert "raise" in you["legal_actions"]
        session.submit_action({"type": "raise", "amount": you["min_raise_to"]})
        with pytest.raises(ValueError):
            session.submit_action({"type": "call"})  # not your turn any more
        snap = until(session, lambda s: any(e["text"].startswith("You: Raise to") or e["text"].startswith("You: Bet") for e in s["log"]))
    finally:
        session.stop()


def test_watching_a_sit_and_go_to_the_end():
    config = SessionConfig(mode="watch", format="sng", bots=["random_bot", "calling_station", "equity_bot"], hands_per_level=2, speed="instant", pause_after_hand=0)
    session = TableSession(config, AVAILABLE)
    session.start()
    try:
        snap = until(session, lambda s: s["status"] == "finished", timeout=120)
    finally:
        session.stop()
    assert sorted(row["place"] for row in snap["scoreboard"]["rows"]) == [1, 2, 3]
    assert sum(seat["stack"] for seat in snap["seats"]) == 3 * 10_000
    assert any(e["kind"] == "out" for e in snap["log"])


def test_pause_and_step():
    config = SessionConfig(mode="watch", bots=["calling_station", "calling_station"], speed="instant", pause_after_hand=0)
    session = TableSession(config, AVAILABLE)
    session.control(paused=True)
    session.start()
    try:
        snap = until(session, lambda s: s["status"] == "running" and s["hand_no"] == 1)
        time.sleep(0.3)
        frozen = session.snapshot()["log"]
        time.sleep(0.3)
        assert session.snapshot()["log"] == frozen  # nothing happens while paused
        session.control(step=True)
        snap = until(session, lambda s: len(s["log"]) > len(frozen))
        assert len(snap["log"]) == len(frozen) + 1  # exactly one action
    finally:
        session.stop()
    assert snap["seats"][0]["name"] == "calling_station" and snap["seats"][1]["name"] == "calling_station 2"


def test_bad_settings_are_rejected():
    with pytest.raises(ValueError):
        TableSession(SessionConfig(mode="play", bots=[]), AVAILABLE)
    with pytest.raises(ValueError):
        TableSession(SessionConfig(mode="watch", bots=["random_bot", "nope"]), AVAILABLE)
    with pytest.raises(ValueError):
        TableSession(SessionConfig(mode="watch", bots=["random_bot"] * 11), AVAILABLE)
    with pytest.raises(ValueError):
        SessionConfig.from_dict({"stack": "lots"})


def request(base, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read()
            return response.status, json.loads(raw) if response.headers["Content-Type"] == "application/json" else raw
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


def test_web_server(tmp_path):
    entries = discover_entries(["random_bot", "calling_station"])
    run_tournament(entries, TournamentConfig(name="round-x", hands=5, in_process=True), tmp_path / "round-x", echo=lambda line: None)

    config = SessionConfig(mode="watch", bots=["random_bot", "calling_station"], speed="instant", pause_after_hand=0)
    app = App(AVAILABLE, set(AVAILABLE), config, results_dir=tmp_path)
    server = create_server(app, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        status, page = request(base, "/")
        assert status == 200 and b"<title>Poker Tournament</title>" in page
        for asset in ("/app.js", "/style.css"):
            assert request(base, asset)[0] == 200

        status, snap = request(base, "/api/state?since=-1&session=")
        assert status == 200 and snap["mode"] == "watch" and len(snap["seats"]) == 2

        status, bots = request(base, "/api/bots")
        assert {b["name"] for b in bots["bots"]} == set(AVAILABLE)

        status, snap = request(base, "/api/start", {"mode": "play", "bots": ["calling_station"], "speed": "instant"})
        assert status == 200 and snap["mode"] == "play" and snap["seats"][0]["you"]
        status, error = request(base, "/api/start", {"mode": "play", "bots": []})
        assert status == 400 and "at least one bot" in error["error"]

        status, runs = request(base, "/api/results")
        assert [run["name"] for run in runs["runs"]] == ["round-x"]
        status, run = request(base, "/api/results/round-x")
        assert run["done"] and len(run["heads_up"]["leaderboard"]) == 2
        assert request(base, "/api/results/..%2Fsecret")[0] == 404
    finally:
        server.shutdown()
        server.server_close()
        app.stop()


def test_play_and_watch_commands(monkeypatch, tmp_path):
    import poker_tournament.webapp as webapp
    from poker_tournament.cli import main

    started = []
    monkeypatch.setattr(webapp, "serve", lambda app, **options: started.append((app, options)))
    monkeypatch.chdir(tmp_path)  # no my_bots/ here
    assert main(["play", "--no-browser", "--speed", "instant"]) == 0
    assert main(["watch", "random_bot", "calling_station", "--format", "sng", "--no-browser"]) == 0
    assert main(["results", "--no-browser"]) == 0
    try:
        play, watch, results = (app.session for app, _ in started)
        assert play.config.mode == "play" and sorted(play.config.bots) == sorted(AVAILABLE)
        assert sorted(play.config.compare) == sorted(AVAILABLE)  # you against every bot, compared with each
        assert watch.config.mode == "watch" and watch.config.format == "sng" and watch.config.bots == ["random_bot", "calling_station"]
        assert results.paused and started[2][1]["page"] == "#results"
    finally:
        for app, _ in started:
            app.stop()


def test_results_of_a_run_in_progress(tmp_path):
    from poker_tournament.tournament import summarize_saved

    entries = discover_entries(["random_bot", "calling_station", "ml_example"])
    run_tournament(entries, TournamentConfig(name="live", hands=5, in_process=True), tmp_path / "live", echo=lambda line: None)
    (tmp_path / "live" / "results.json").unlink()  # as if it were still running
    next(iter((tmp_path / "live" / "matches").glob("*.json"))).unlink()
    summary = summarize_saved(tmp_path / "live")
    assert summary["heads_up"]["progress"] == [2, 3]
    assert len(summary["heads_up"]["leaderboard"]) == 3
