import itertools
import random
from collections import Counter

import pytest

from poker_tournament import estimate_equity, evaluate, hand_name
from poker_tournament.cards import card_to_int, cards_to_ints


def reference_rank(cards):
    """A slow, obvious five-card ranking to check the fast evaluator against."""
    ranks = sorted((c >> 2 for c in cards), reverse=True)
    flush = len({c & 3 for c in cards}) == 1
    by_count = sorted(Counter(ranks).items(), key=lambda rc: (rc[1], rc[0]), reverse=True)
    grouped = [rank for rank, _ in by_count]
    unique = sorted(set(ranks), reverse=True)
    straight = None
    if len(unique) == 5 and unique[0] - unique[4] == 4:
        straight = unique[0]
    elif unique == [12, 3, 2, 1, 0]:
        straight = 3
    if straight is not None and flush:
        return (8, [straight])
    if by_count[0][1] == 4:
        return (7, grouped)
    if by_count[0][1] == 3 and by_count[1][1] == 2:
        return (6, grouped)
    if flush:
        return (5, ranks)
    if straight is not None:
        return (4, [straight])
    if by_count[0][1] == 3:
        return (3, grouped)
    if by_count[0][1] == 2 and by_count[1][1] == 2:
        return (2, grouped)
    if by_count[0][1] == 2:
        return (1, grouped)
    return (0, ranks)


def sign(x):
    return (x > 0) - (x < 0)


def test_five_card_ordering_matches_reference():
    rng = random.Random(1)
    deck = list(range(52))
    for _ in range(20000):
        a, b = rng.sample(deck, 5), rng.sample(deck, 5)
        ra, rb = reference_rank(a), reference_rank(b)
        assert sign(evaluate(a) - evaluate(b)) == sign((ra > rb) - (ra < rb)), (a, b)


def test_seven_cards_score_the_best_five():
    rng = random.Random(2)
    deck = list(range(52))
    for _ in range(3000):
        seven = rng.sample(deck, 7)
        assert evaluate(seven) == max(evaluate(five) for five in itertools.combinations(seven, 5))


def test_seven_card_ordering_matches_reference():
    rng = random.Random(3)
    deck = list(range(52))
    for _ in range(1500):
        a, b = rng.sample(deck, 7), rng.sample(deck, 7)
        ra = max(reference_rank(five) for five in itertools.combinations(a, 5))
        rb = max(reference_rank(five) for five in itertools.combinations(b, 5))
        assert sign(evaluate(a) - evaluate(b)) == sign((ra > rb) - (ra < rb)), (a, b)


@pytest.mark.parametrize(
    "better, worse",
    [
        ("As Ks Qs Js Ts 2d 3c", "9h 9d 9c 9s Ah 2d 3c"),  # royal flush beats quads
        ("5d 4d 3d 2d Ad Kc Kh", "Ac Ad Ah Kd Ks 2c 3c"),  # steel wheel beats a full house
        ("Ac 2d 3h 4s 5c Kd Qd", "Ac Ad Ah 7d 2s 9c Jc"),  # wheel straight beats trips
        ("6c 2d 3h 4s 5c Kd Qd", "Ac 2d 3h 4s 5c Kd Qd"),  # six-high straight beats the wheel
        ("Ah Kh 9h 4h 2h 3c 3d", "Ah Qh Jh Th 8h 3c 3d"),  # flush kickers count
        ("Kc Kd 7h 7s 2c 3d Ac", "Kc Kd 7h 7s 2c 3d Qc"),  # two-pair kicker
        ("Ac Ad Kh Ks Qc Qd 2h", "Ac Ad Kh Ks Jc Jd Th"),  # third pair is the kicker: Q beats J/T
        ("9c 9d 9h 8c 8d 8h 2c", "9c 9d 9h 7c 7d 2h 3c"),  # two sets make the best full house
        ("Jc Jd 9h 8s 7c 3d 2h", "Jc Jd 9h 8s 6c 3d 2h"),  # pair with third kicker
    ],
)
def test_specific_hands(better, worse):
    assert evaluate(better.split()) > evaluate(worse.split())


def test_split_when_the_board_plays():
    board = "As Ks Qs Js Ts".split()
    assert evaluate(board + ["2c", "3d"]) == evaluate(board + ["4h", "5h"])


def test_names_and_parsing():
    assert hand_name(evaluate("Ah Kh Qh Jh Th".split())) == "Straight flush"
    assert hand_name(evaluate("2c 2d 5h 5s 9c".split())) == "Two pair"
    assert card_to_int("10h") == card_to_int("Th") == card_to_int("th")
    assert cards_to_ints(["2c", "As"]) == [0, 51]
    with pytest.raises(ValueError):
        card_to_int("Xx")


def test_showdown_odds_with_known_hands():
    from poker_tournament import showdown_odds

    # Aces against kings before the flop: about 82% to 18%.
    aces, kings = showdown_odds([["As", "Ah"], ["Kd", "Kc"]], samples=4000, rng=random.Random(5))
    assert aces == pytest.approx(0.82, abs=0.03) and aces + kings == pytest.approx(1)
    # On the turn it's exact: kings need one of the two kings left in 44 cards.
    aces, kings = showdown_odds([["As", "Ah"], ["Kd", "Kc"]], ["2c", "7d", "9h", "3s"])
    assert kings == pytest.approx(2 / 44)
    # The river decides it; a shared hand splits.
    assert showdown_odds([["As", "Kd"], ["Ah", "Kc"]], ["2c", "7d", "9h", "3s", "4s"]) == [0.5, 0.5]


def test_equity_estimates_are_sensible():
    rng = random.Random(4)
    assert estimate_equity(["As", "Ah"], iterations=3000, rng=rng) == pytest.approx(0.85, abs=0.03)
    assert estimate_equity(["7c", "2d"], iterations=3000, rng=rng) == pytest.approx(0.35, abs=0.03)
    # The nuts on the river always wins.
    assert estimate_equity(["As", "Ks"], ["Qs", "Js", "Ts", "2c", "3d"], iterations=200, rng=rng) == 1.0
    # More opponents means less equity.
    assert estimate_equity(["As", "Ah"], opponents=4, iterations=2000, rng=rng) < 0.65
