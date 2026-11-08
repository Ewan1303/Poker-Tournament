"""Real-poker details: showing cards, side pots, and tournaments with several tables and a hand limit."""

import random

import pytest

from poker_tournament import Action, PokerBot
from poker_tournament.cards import shuffled_deck
from poker_tournament.engine import Hand
from poker_tournament.loader import load_bot, resolve_bot
from poker_tournament.match import HandLog, play_hand, play_tournament
from poker_tournament.players import LocalBot, ProcessBot
from poker_tournament.tournament import Entry, TournamentConfig, _finish_shares, _summarize_tournaments


def stacked(*holes, board="2c 7d 9h 4s 3c"):
    return [card for hole in holes for card in hole.split()] + board.split()


def test_cards_stay_hidden_without_a_showdown_unless_shown():
    hand = Hand([1000, 1000, 1000], button=0, small_blind=5, big_blind=10, deck=shuffled_deck(1))
    hand.apply(("raise", 50))
    hand.apply("fold")
    hand.apply("fold")
    assert hand.finished and not hand.showdown and hand.shown(0) is None
    assert hand.result_for(1)["players"][0]["hole_cards"] is None  # the winner needn't show
    hand.show(0)
    seen = hand.result_for(1)["players"][0]
    assert seen["hole_cards"] is not None and seen["shown"] == "chose"
    assert hand.result_for(0)["players"][1]["hole_cards"] is None  # nobody else's cards


def test_everyone_still_in_shows_at_a_showdown():
    hand = Hand([1000, 1000], button=0, small_blind=5, big_blind=10, deck=shuffled_deck(2))
    with pytest.raises(RuntimeError):
        hand.show(0)  # not before the hand is over
    while not hand.finished:
        hand.apply("call")
    assert hand.showdown and hand.shown(0) == hand.shown(1) == "showdown"
    assert all(p["hole_cards"] for p in hand.result_for(0)["players"])


def test_side_pots_while_betting_and_at_the_end():
    hand = Hand([100, 300, 1000, 1000], button=3, small_blind=5, big_blind=10, deck=stacked("Ac Ad", "Kc Kd", "Qc Qd", "Jc Jd"))
    hand.apply(("raise", 300))  # seat 2
    hand.apply("call")  # seat 3
    hand.apply("all_in")  # seat 0, the small blind: all in for 100
    hand.apply("all_in")  # seat 1, the big blind: all in for 300
    assert [(p["amount"], p["eligible"]) for p in hand.pot_breakdown()] == [(400, [0, 1, 2, 3]), (600, [1, 2, 3])]
    hand.apply(("raise", 200))  # seat 2 bets on the flop; seat 3 hasn't called yet
    assert [(p["amount"], p["eligible"]) for p in hand.pot_breakdown()] == [
        (400, [0, 1, 2, 3]),
        (600, [1, 2, 3]),
        (200, [2, 3]),
    ]
    hand.apply("call")
    while not hand.finished:
        hand.apply("check")
    # Aces win the main pot, kings the first side pot, queens the second.
    assert [(p["amount"], p["winners"]) for p in hand.pots] == [(400, [0]), (600, [1]), (400, [2])]
    assert hand.winnings == [300, 300, -100, -500]


def test_an_uncalled_bet_goes_back():
    hand = Hand([1000, 1000], button=0, small_blind=5, big_blind=10, deck=shuffled_deck(3))
    hand.apply(("raise", 300))
    hand.apply("fold")
    assert [(p["amount"], p["uncalled"]) for p in hand.pots] == [(20, False), (290, True)]
    assert hand.winnings == [10, -10]


class Folder(PokerBot):
    def __init__(self):
        self.seen = []

    def act(self, obs):
        return Action.fold()

    def on_hand_end(self, result):
        self.seen.append(result.players[1 - result.seat])


def test_bots_can_choose_to_show_through_the_sandbox(tmp_path):
    folder = tmp_path / "shower"
    folder.mkdir()
    (folder / "bot.py").write_text(
        "from poker_tournament import Action, PokerBot\n\n"
        "class Bot(PokerBot):\n"
        "    def act(self, obs):\n        return Action.raise_to(obs.min_raise_to) if obs.can_raise else Action.call()\n\n"
        "    def show_cards(self, result):\n        return result.net > 0  # show off every win\n",
        encoding="utf-8",
    )
    shower, watcher = ProcessBot(folder, "shower"), Folder()
    players = [shower, LocalBot(watcher, "folder")]
    try:
        for player in players:
            player.start()
        assert shower.shows
        for k in range(6):
            play_hand(players, [1000, 1000], k % 2, 5, 10, shuffled_deck(k), hand_id=k + 1)
    finally:
        shower.close()
    assert all(seen.hole_cards and seen.shown == "chose" for seen in watcher.seen)


def test_bots_that_dont_choose_are_never_asked():
    player = ProcessBot(resolve_bot("calling_station"), "callie")
    player.start()
    try:
        assert not player.shows
    finally:
        player.close()


@pytest.mark.parametrize("seed", range(3))
def test_tournaments_across_several_tables(seed):
    rng = random.Random(seed)
    kinds = ["random_bot", "calling_station", "maniac", "fit_or_fold"]
    for _ in range(3):
        n = rng.randint(11, 23)
        table_size = rng.randint(3, 10)
        limit = rng.choice([None, 40])
        players = [LocalBot(load_bot(kinds[i % 4]), f"bot{i}") for i in range(n)]
        for player in players:
            player.start()
        log = HandLog()
        out = play_tournament(players, f"{seed}:{n}", table_size=table_size, hands_per_level=3, max_hands=limit, hand_log=log)
        assert sum(out["stacks"]) == n * 10_000  # no chips appear or vanish
        assert sorted(out["places"])[0] == 1 and sum(out["places"]) == n * (n + 1) / 2
        assert all(2 <= len(record["players"]) <= table_size for record in log.records)
        if limit:
            assert out["rounds"] <= limit
        if out["finished"]:
            assert sum(1 for s in out["stacks"] if s > 0) == 1
        # Tables in each round are never more than one player apart.
        rounds, current = [], []
        for record in log.records:
            if current and record["table"] <= current[-1]["table"]:
                rounds.append(current)
                current = []
            current.append(record)
        rounds.append(current)
        for tables in rounds:
            sizes = [len(t["players"]) for t in tables]
            assert max(sizes) - min(sizes) <= 1, sizes


def test_hand_limit_ranks_survivors_by_chips():
    players = [LocalBot(load_bot("calling_station"), f"callie{i}") for i in range(4)]
    for player in players:
        player.start()
    out = play_tournament(players, "limit", max_hands=5)
    assert out["rounds"] == 5 and not out["finished"]
    by_place = sorted(range(4), key=lambda i: out["places"][i])
    assert [out["stacks"][i] for i in by_place] == sorted(out["stacks"], reverse=True)


def test_shared_places_split_wins():
    shares = _finish_shares({"names": ["a", "b", "c", "d"], "places": [1, 2.5, 2.5, 4]})
    assert shares["a"] == [1, 0, 0, 0] and shares["b"] == [0, 0.5, 0.5, 0]
    tied = _finish_shares({"names": ["a", "b"], "places": [1.5, 1.5]})
    assert tied["a"] == [0.5, 0.5]

    from poker_tournament.players import BotStats

    outcomes = {
        1: {"names": ["a", "b"], "places": [1, 2], "stacks": [20000, 0], "rounds": 30, "finished": True},
        2: {"names": ["a", "b"], "places": [1.5, 1.5], "stacks": [10000, 10000], "rounds": 100, "finished": False},
    }
    summary = _summarize_tournaments(
        [Entry("a", None), Entry("b", None)], outcomes, {"a": BotStats(), "b": BotStats()}, TournamentConfig(name="x")
    )
    a, b = summary["leaderboard"]
    assert (a["bot"], a["wins"], b["wins"]) == ("a", 1.5, 0.5)
    assert a["avg_chips"] == 5000 and b["avg_chips"] == -5000
    assert summary["finished_share"] == 0.5 and summary["avg_hands"] == 65
