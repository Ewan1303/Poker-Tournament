import random
from fractions import Fraction

import pytest

from poker_tournament.cards import shuffled_deck
from poker_tournament.engine import Hand
from poker_tournament.evaluator import evaluate


def stacked(*holes, board="2c 7d 9h Js 3c"):
    """A deck where seat i is dealt holes[i] and then the board comes."""
    return [card for hole in holes for card in hole.split()] + board.split()


def test_heads_up_blinds_and_order():
    hand = Hand([1000, 1000], button=0, small_blind=5, big_blind=10, deck=shuffled_deck(1))
    assert [s.bet for s in hand.seats] == [5, 10]
    assert hand.to_act == 0  # the button posts the small blind and acts first preflop
    hand.apply("call")
    assert hand.to_act == 1  # the big blind gets its option
    obs = hand.observation(1)
    assert obs["to_call"] == 0 and obs["can_raise"] and obs["legal_actions"] == ["check", "raise"]
    hand.apply("check")
    assert hand.street == 1 and len(hand.board) == 3
    assert hand.to_act == 1  # the big blind acts first after the flop


def test_three_handed_order():
    hand = Hand([1000] * 3, button=0, small_blind=5, big_blind=10, deck=shuffled_deck(2))
    assert hand.to_act == 0  # the button is first to act preflop three-handed
    hand.apply("call")
    assert hand.to_act == 1
    hand.apply("call")
    assert hand.to_act == 2
    hand.apply("check")
    assert hand.street == 1 and hand.to_act == 1  # first player left of the button


def test_fold_to_the_big_blind():
    hand = Hand([1000, 1000], button=0, small_blind=5, big_blind=10, deck=shuffled_deck(3))
    hand.apply("fold")
    assert hand.finished and not hand.showdown
    assert hand.winnings == [-5, 5]
    result = hand.result_for(0)
    assert result["players"][1]["hole_cards"] is None  # cards aren't shown without a showdown
    assert result["players"][0]["hole_cards"] is not None  # you always see your own


def test_minimum_raise():
    hand = Hand([1000, 1000], button=0, small_blind=5, big_blind=10, deck=shuffled_deck(4))
    hand.apply(("raise", 30))  # a raise of 20
    obs = hand.observation(1)
    assert obs["min_raise_to"] == 50 and obs["max_raise_to"] == 1000
    applied = hand.apply(("raise", 40))  # too small: becomes the minimum
    assert applied["to"] == 50 and not applied["corrected"]


def test_short_all_in_does_not_reopen_betting():
    hand = Hand([1000, 40, 1000], button=0, small_blind=5, big_blind=10, deck=shuffled_deck(5))
    hand.apply(("raise", 30))  # seat 0 raises 20
    hand.apply("all_in")  # seat 1 goes all-in to 40: only 10 more, not a full raise
    obs = hand.observation(2)
    assert obs["can_raise"] and obs["min_raise_to"] == 60  # seat 2 hasn't acted yet
    hand.apply("call")
    obs = hand.observation(0)
    assert obs["to_call"] == 10 and not obs["can_raise"] and obs["legal_actions"] == ["fold", "call"]
    applied = hand.apply(("raise", 500))
    assert applied["action"] == "call" and applied["corrected"]


def test_short_all_ins_that_add_up_to_a_raise_reopen_betting():
    hand = Hand([40, 55, 1000, 1000], button=0, small_blind=5, big_blind=10, deck=shuffled_deck(6))
    assert hand.to_act == 3
    hand.apply(("raise", 30))  # raise of 20
    hand.apply("all_in")  # seat 0 to 40
    hand.apply("all_in")  # seat 1 to 55: 25 more than seat 3's raise, at least a full raise
    hand.apply("call")  # seat 2
    obs = hand.observation(3)
    assert obs["to_call"] == 25 and obs["can_raise"] and obs["min_raise_to"] == 75


def test_side_pots():
    deck = stacked("Ac Ad", "Kc Kd", "Qc Qd")
    hand = Hand([100, 300, 600], button=0, small_blind=5, big_blind=10, deck=deck)
    hand.apply("all_in")  # seat 0: 100
    hand.apply("all_in")  # seat 1: 300
    obs = hand.observation(2)
    assert obs["to_call"] == 290 and not obs["can_raise"]  # nobody left to raise against
    hand.apply("call")
    assert hand.finished and hand.showdown
    # Main pot 300 to the aces, side pot 400 to the kings.
    assert hand.winnings == [200, 100, -300]
    assert [p["amount"] for p in hand.pots] == [300, 400]


def test_split_pot_odd_chip_goes_left_of_the_button():
    deck = stacked("2c 3d", "4c 5d", "6c 7d", board="As Ks Qs Js Ts")
    hand = Hand([1000] * 3, button=0, small_blind=5, big_blind=10, deck=deck)
    hand.apply("call")  # button
    hand.apply("fold")  # small blind leaves 5 behind
    hand.apply("check")  # big blind
    while not hand.finished:
        hand.apply("check")
    # 25 chips split between the button and the big blind; seat 2 is first left of the button.
    assert hand.winnings == [2, -5, 3]


def test_short_big_blind_all_in():
    deck = stacked("Ac Ad", "8c 4d")
    hand = Hand([1000, 3], button=0, small_blind=5, big_blind=10, deck=deck)
    assert hand.finished  # nobody has a decision to make
    assert hand.winnings == [3, -3]


def test_short_big_blind_others_must_call_the_full_blind():
    hand = Hand([1000, 1000, 30], button=0, small_blind=50, big_blind=100, deck=shuffled_deck(7))
    obs = hand.observation(0)
    assert obs["to_call"] == 100  # the full big blind, though the big blind only had 30


def test_illegal_actions_are_corrected():
    hand = Hand([1000, 1000], button=0, small_blind=5, big_blind=10, deck=shuffled_deck(8))
    applied = hand.apply("check")  # facing the big blind: a check is a fold
    assert applied["action"] == "fold" and applied["corrected"]
    hand = Hand([1000, 1000], button=0, small_blind=5, big_blind=10, deck=shuffled_deck(8))
    hand.apply("call")
    applied = hand.apply("fold")  # folding when you could check is a check
    assert applied["action"] == "check" and applied["corrected"]
    applied = hand.apply("banana")
    assert applied["action"] == "check" and applied["corrected"]
    applied = hand.apply(("raise", 10**12))
    assert applied["to"] == 990 and applied["all_in"]


def reference_payouts(hand):
    """Pays out chip by chip: chip number x from each player goes to the best
    hand still in that put in at least x chips (or back if nobody did)."""
    contributions = [s.contributed for s in hand.seats]
    live = [i for i, s in enumerate(hand.seats) if not s.folded]
    if len(live) == 1:
        scores = {live[0]: 0}
    else:
        scores = {i: evaluate(hand.seats[i].hole + hand.board) for i in live}
    payouts = [Fraction(0)] * hand.n
    for x in range(1, max(contributions) + 1):
        contributors = [i for i, c in enumerate(contributions) if c >= x]
        eligible = [i for i in live if contributions[i] >= x]
        if not eligible:
            for i in contributors:
                payouts[i] += 1
            continue
        best = max(scores[i] for i in eligible)
        winners = [i for i in eligible if scores[i] == best]
        for w in winners:
            payouts[w] += Fraction(len(contributors), len(winners))
    return payouts


def random_action(rng, obs):
    roll = rng.random()
    if roll < 0.05:
        return rng.choice(["banana", None, ("raise", "x"), {"type": "raise"}, ("raise", -5)])
    if roll < 0.15:
        return "all_in"
    if roll < 0.3:
        return "fold"
    if roll < 0.4:
        return "check"
    if roll < 0.7:
        return "call"
    low, high = obs["min_raise_to"], obs["max_raise_to"]
    return ("raise", rng.randint(max(0, low - 20), high + 20) if high else 50)


@pytest.mark.parametrize("seed", range(4))
def test_random_play_keeps_the_rules(seed):
    rng = random.Random(seed)
    for _ in range(400):
        n = rng.randint(2, 9)
        stacks = [rng.choice([1, 3, 9, 10, 25, 100, 400, rng.randint(1, 1500)]) for _ in range(n)]
        small, big = rng.choice([(5, 10), (1, 2), (10, 10), (25, 50)])
        hand = Hand(stacks, rng.randrange(n), small, big, shuffled_deck(rng.random()))
        steps = 0
        while not hand.finished:
            seat = hand.to_act
            s = hand.seats[seat]
            assert not s.folded and not s.all_in
            obs = hand.observation(seat)
            assert 0 <= obs["to_call"] <= obs["stack"]
            assert obs["legal_actions"] == hand.legal_actions(seat)
            if obs["can_raise"]:
                assert obs["current_bet"] < obs["min_raise_to"] <= obs["max_raise_to"] == s.bet + s.stack
            action = random_action(rng, obs)
            applied = hand.apply(action)
            kind = action if isinstance(action, str) else None
            if kind in obs["legal_actions"] and kind != "raise":
                assert not applied["corrected"], (action, obs["legal_actions"])
            steps += 1
            assert steps < 200
        assert sum(hand.winnings) == 0
        assert all(s.stack >= 0 for s in hand.seats)
        assert [s.stack for s in hand.seats] == [stacks[i] + hand.winnings[i] for i in range(n)]
        assert sum(p["amount"] for p in hand.pots) == sum(s.contributed for s in hand.seats)
        reference = reference_payouts(hand)
        for i, s in enumerate(hand.seats):
            paid = hand.winnings[i] + s.contributed
            assert abs(paid - reference[i]) < len(hand.pots), (i, paid, reference[i])
