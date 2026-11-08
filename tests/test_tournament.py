import gzip
import json
import random
import time

import pytest

from poker_tournament import Action, PokerBot
from poker_tournament.env import HeadsUpEnv
from poker_tournament.features import (
    HAND_FEATURE_NAMES,
    OBSERVATION_FEATURE_NAMES,
    hand_features,
    observation_features,
)
from poker_tournament.loader import load_bot
from poker_tournament.match import play_match, play_sng
from poker_tournament.players import LocalBot
from poker_tournament.tournament import TournamentConfig, discover_entries, run_tournament


def test_duplicate_match_cancels_out_luck():
    # Two identical bots that always call: every deal is won from one seat and
    # lost from the other, so a duplicate match is exactly even.
    result = play_match(load_bot("calling_station"), load_bot("calling_station"), hands=200, seed=3)
    assert result.hands == 400
    assert all(x == 0 for x in result.pair_nets)


def test_match_is_repeatable_with_deterministic_bots():
    first = play_match(load_bot("calling_station"), load_bot("ml_example"), hands=50, seed=9)
    second = play_match(load_bot("calling_station"), load_bot("ml_example"), hands=50, seed=9)
    assert first.pair_nets == second.pair_nets


def test_sit_and_go_finishes_with_places():
    players = [LocalBot(load_bot(name), name) for name in ("random_bot", "calling_station", "ml_example", "equity_bot")]
    for player in players:
        player.start()
    outcome = play_sng(players, seed=1, hands_per_level=5)
    places = sorted(outcome["places"])
    assert places[0] == 1 and places[1] > 1 and sum(places) == 1 + 2 + 3 + 4  # ties share places
    assert sum(outcome["stacks"]) == 4 * 10_000
    assert sum(1 for s in outcome["stacks"] if s > 0) == 1


def test_tournament_round_and_resume(tmp_path):
    entries = discover_entries(["random_bot", "calling_station", "ml_example"])
    config = TournamentConfig(name="test", formats=("heads_up", "sng"), hands=20, games=3, in_process=True)
    summary = run_tournament(entries, config, tmp_path / "test", echo=lambda line: None)
    board = summary["heads_up"]["leaderboard"]
    assert {row["bot"] for row in board} == {"random_bot", "calling_station", "ml_example"}
    assert abs(sum(row["bb_per_100"] for row in board)) < 1e-6  # zero-sum
    assert len(summary["sng"]["leaderboard"]) == 3
    assert (tmp_path / "test" / "leaderboard.md").exists()
    assert len(list((tmp_path / "test" / "hands").glob("*.jsonl.gz"))) == 3 + 3  # one per pairing, one per game
    with gzip.open(tmp_path / "test" / "hands" / "random_bot__vs__calling_station.jsonl.gz", "rt") as f:
        hands = [json.loads(line) for line in f]
    assert len(hands) == 40 and {h["half"] for h in hands} == {1, 2}
    for hand in hands:  # published logs only show cards that reached a showdown
        for player in hand["players"]:
            assert (player["hole_cards"] is not None) == (hand["showdown"] and not player["folded"])

    lines = []
    again = run_tournament(entries, config, tmp_path / "test", resume=True, echo=lines.append)
    assert "(0 of 6 half-matches to play)" in lines[0]
    assert again["heads_up"]["leaderboard"] == board


def test_nothing_written_during_a_run_reveals_the_seed(tmp_path):
    entries = discover_entries(["random_bot", "calling_station"])
    config = TournamentConfig(name="secret", formats=("heads_up", "sng"), hands=10, games=2, seed=987654321987, in_process=True)
    run_tournament(entries, config, tmp_path / "secret", echo=lambda line: None)
    for path in (tmp_path / "secret").rglob("*.json"):
        if path.name not in ("run.json", "results.json"):  # those two are rewritten with the seed at the end
            assert "987654321987" not in path.read_text(encoding="utf-8"), path


def test_sit_and_go_never_posts_the_big_blind_twice_running():
    from poker_tournament.match import HandLog

    for seed in range(12):
        players = [LocalBot(load_bot(name), name) for name in ("random_bot", "calling_station", "ml_example")]
        log = HandLog()
        play_sng(players, seed=seed, hands_per_level=2, hand_log=log)
        previous = None
        for record in log.records:
            big_blind = next(e["seat"] for e in record["history"] if e["action"] == "big_blind")
            name = record["players"][big_blind]["name"]
            if len(record["players"]) == 2:
                assert name != previous, f"seed {seed}: {name} posted the big blind twice"
            previous = name


def test_in_process_bots_keep_their_own_helper_modules(tmp_path):
    for value in (1, 2):
        folder = tmp_path / f"bot{value}"
        folder.mkdir()
        (folder / "helper.py").write_text(f"VALUE = {value}\n", encoding="utf-8")
        (folder / "bot.py").write_text(
            "from helper import VALUE\nfrom poker_tournament import Action, PokerBot\n\n"
            "class Bot(PokerBot):\n    value = VALUE\n\n    def act(self, obs):\n        return Action.call()\n",
            encoding="utf-8",
        )
    assert load_bot(tmp_path / "bot1").value == 1
    assert load_bot(tmp_path / "bot2").value == 2


def test_names_that_differ_only_in_case_clash(tmp_path):
    from poker_tournament.loader import BotLoadError, BUILTIN_BOTS
    import shutil

    for parent, name in (("a", "Bob"), ("b", "bob")):
        shutil.copytree(BUILTIN_BOTS / "calling_station", tmp_path / parent / name)
    with pytest.raises(BotLoadError, match="share a name"):
        discover_entries([tmp_path / "a" / "Bob", tmp_path / "b" / "bob"])


def test_a_failure_stops_the_round_straight_away():
    from poker_tournament.tournament import _run_tasks

    played = []

    def play(task):
        time.sleep(0.2)
        played.append(task)
        return task

    def on_result(task, result):
        raise RuntimeError("stop")

    started = time.perf_counter()
    with pytest.raises(RuntimeError):
        _run_tasks(list(range(30)), play, 2, on_result)
    assert time.perf_counter() - started < 2
    time.sleep(0.5)  # let tasks that were already running finish
    assert len(played) < 10  # the queued ones were cancelled, not played


def test_tournament_with_separate_processes(tmp_path):
    entries = discover_entries(["random_bot", "calling_station"])
    config = TournamentConfig(name="procs", hands=15, workers=2)
    summary = run_tournament(entries, config, tmp_path / "procs", echo=lambda line: None)
    saved = json.loads((tmp_path / "procs" / "results.json").read_text(encoding="utf-8"))
    assert saved["heads_up"]["leaderboard"][0]["bot"] == summary["heads_up"]["leaderboard"][0]["bot"]
    assert all(row["failures"] == 0 for row in summary["heads_up"]["leaderboard"])


def test_features_have_the_advertised_length():
    rng = random.Random(0)
    for _ in range(200):
        cards = rng.sample(range(52), 7)
        board = cards[2 : 2 + rng.choice((0, 3, 4, 5))]
        assert len(hand_features(cards[:2], board)) == len(HAND_FEATURE_NAMES)


class FeatureChecker(PokerBot):
    def act(self, obs):
        assert len(observation_features(obs)) == len(OBSERVATION_FEATURE_NAMES)
        return Action.call()


def test_env_plays_hands():
    env = HeadsUpEnv(opponent=load_bot("random_bot"), seed=0)
    agent = FeatureChecker()
    total = 0.0
    for _ in range(50):
        obs, done = env.reset(), False
        while not done:
            obs, reward, done, info = env.step(agent.act(obs))
        total += reward
        assert info["result"].net == reward
    assert env.hands_played >= 50
