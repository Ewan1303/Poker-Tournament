"""Turning cards and game state into numbers for machine learning.

Everything returns plain lists of floats (no numpy needed); wrap them with
np.array(...) or torch.tensor(...) as you like. These are a starting point:
the best bots will probably engineer their own.

    from poker_tournament.features import observation_features, OBSERVATION_FEATURE_NAMES
    x = observation_features(obs)       # len(x) == len(OBSERVATION_FEATURE_NAMES)
"""

from __future__ import annotations

import math
from typing import Iterable

from .api import Observation
from .cards import cards_to_ints
from .evaluator import PAIR, STRAIGHT_HIGH, evaluate


def one_hot_cards(cards: Iterable[str | int]) -> list[float]:
    """52 numbers, 1.0 for each card present (index = rank * 4 + suit)."""
    vector = [0.0] * 52
    for card in cards_to_ints(cards):
        vector[card] = 1.0
    return vector


HAND_FEATURE_NAMES = (
    ["street_preflop", "street_flop", "street_turn", "street_river"]
    + ["high_rank", "low_rank", "pocket_pair", "suited", "gap", "connected"]
    + [f"made_{name}" for name in ("high_card", "pair", "two_pair", "trips", "straight", "flush", "full_house", "quads", "straight_flush")]
    + ["made_top_rank", "made_second_rank", "beats_board", "board_paired", "board_flush_possible"]
    + ["board_straight_possible", "overcards", "top_pair", "overpair", "flush_draw", "gutshot", "open_ended"]
)


def hand_features(hole_cards: Iterable[str | int], board: Iterable[str | int] = ()) -> list[float]:
    """Hand-strength features for two hole cards and 0, 3, 4 or 5 board cards."""
    hole = cards_to_ints(hole_cards)
    board = cards_to_ints(board)
    street = {0: 0, 3: 1, 4: 2, 5: 3}[len(board)]
    features = [0.0] * 4
    features[street] = 1.0

    high, low = sorted((hole[0] >> 2, hole[1] >> 2), reverse=True)
    gap = high - low
    features += [high / 12, low / 12, float(gap == 0), float(hole[0] & 3 == hole[1] & 3), gap / 12, float(gap == 1 or (high == 12 and low == 0))]

    score = evaluate(hole + board)
    category = score >> 20
    made = [0.0] * 9
    made[category] = 1.0
    features += made
    features += [((score >> 16) & 15) / 13, ((score >> 12) & 15) / 13]

    if not board:
        return features + [0.0] * 10

    board_score = evaluate(board)
    board_ranks = [card >> 2 for card in board]
    top_board = max(board_ranks)
    suit_counts = [0] * 4
    for card in board:
        suit_counts[card & 3] += 1
    board_mask = 0
    for rank in board_ranks:
        board_mask |= 1 << rank
    windows = [0b11111 << (h - 4) for h in range(4, 13)] + [(1 << 12) | 0b1111]
    straight_possible = any((board_mask & w).bit_count() >= 3 for w in windows)

    pair_rank = (score >> 16 & 15) - 1 if category == PAIR else -1
    all_cards = hole + board
    flush_draw = 0.0
    straight_outs = 0
    if street < 3:
        for suit in range(4):
            in_suit = [c for c in all_cards if c & 3 == suit]
            if len(in_suit) == 4 and any(c & 3 == suit for c in hole):
                flush_draw = 1.0
        mask = 0
        for card in all_cards:
            mask |= 1 << (card >> 2)
        if STRAIGHT_HIGH[mask] < 0:
            straight_outs = sum(1 for r in range(13) if not mask >> r & 1 and STRAIGHT_HIGH[mask | 1 << r] >= 0)

    features += [
        float(score >> 20 > board_score >> 20),
        float(board_score >> 20 >= PAIR),
        float(max(suit_counts) >= 3),
        float(straight_possible),
        sum(1 for r in (high, low) if r > top_board) / 2,
        float(pair_rank == top_board and pair_rank in (high, low)),
        float(gap == 0 and high > top_board),
        flush_draw,
        float(straight_outs == 1),
        float(straight_outs >= 2),
    ]
    return features


OBSERVATION_FEATURE_NAMES = list(HAND_FEATURE_NAMES) + [
    "pot_odds",
    "to_call_over_pot",
    "pot_over_starting_chips",
    "stack_to_pot",
    "big_blinds_behind",
    "is_button",
    "acts_last",
    "active_opponents",
    "raises_this_street",
    "raises_this_hand",
    "can_raise",
]


def observation_features(obs: Observation) -> list[float]:
    """hand_features plus the betting situation. Everything is roughly scaled to 0-1."""
    bb = obs.big_blind
    total_start = sum(p.start_stack for p in obs.players) or 1
    raises = [a for a in obs.history if a["action"] in ("bet", "raise")]
    live = [p.seat for p in obs.players if not p.folded]
    # Postflop the last live player before the button (clockwise) acts last.
    after_me = [(s - obs.button - 1) % obs.num_players for s in live]
    acts_last = (obs.seat - obs.button - 1) % obs.num_players == max(after_me)
    return hand_features(obs.hole_cards, obs.board) + [
        obs.pot_odds,
        min(obs.to_call / max(obs.pot, 1), 5.0) / 5,
        obs.pot / total_start,
        min(obs.stack / max(obs.pot, 1), 20.0) / 20,
        math.log1p(obs.stack / bb) / math.log1p(1000),
        float(obs.is_button),
        float(acts_last),
        len(obs.active_opponents) / 9,
        min(sum(1 for a in raises if a["street"] == obs.street), 4) / 4,
        min(len(raises), 8) / 8,
        float(obs.can_raise),
    ]
