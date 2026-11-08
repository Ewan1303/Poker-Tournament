"""Poker hand evaluator for 5, 6 or 7 cards (fewer works too, for partial hands).

    score = evaluate(["As", "Ks", "Qs", "Js", "Ts", "2d", "3c"])

A higher score is a better hand, so compare hands by comparing scores. The
category is score >> 20 (0 = high card ... 8 = straight flush); use
hand_category() and hand_name() rather than relying on that layout.
"""

from __future__ import annotations

from typing import Iterable, Sequence

from .cards import card_to_int

HIGH_CARD, PAIR, TWO_PAIR, TRIPS, STRAIGHT, FLUSH, FULL_HOUSE, QUADS, STRAIGHT_FLUSH = range(9)
CATEGORY_NAMES = (
    "High card",
    "Pair",
    "Two pair",
    "Three of a kind",
    "Straight",
    "Flush",
    "Full house",
    "Four of a kind",
    "Straight flush",
)

_WHEEL = (1 << 12) | 0b1111  # A-2-3-4-5


def _straight_high(mask: int) -> int:
    """Rank of the top card of the best straight in a 13-bit rank mask, or -1."""
    for high in range(12, 3, -1):
        window = 0b11111 << (high - 4)
        if mask & window == window:
            return high
    if mask & _WHEEL == _WHEEL:
        return 3  # five-high straight
    return -1


# STRAIGHT_HIGH[rank_mask] -> top rank of the best straight, or -1 if there is none.
STRAIGHT_HIGH = [_straight_high(mask) for mask in range(1 << 13)]


def _pack(category: int, ranks: Sequence[int]) -> int:
    # Category in the top bits, then up to five ranks (stored as rank + 1 so a
    # missing kicker sorts below a deuce).
    score = category
    for i in range(5):
        score = (score << 4) | (ranks[i] + 1 if i < len(ranks) else 0)
    return score


def evaluate(cards: Iterable[str | int]) -> int:
    """Score the best five-card hand that can be made from `cards`."""
    counts = [0] * 13
    suit_masks = [0, 0, 0, 0]
    for card in cards:
        if card.__class__ is not int:
            card = card_to_int(card)
        rank = card >> 2
        counts[rank] += 1
        suit_masks[card & 3] |= 1 << rank

    flush = 0
    for mask in suit_masks:
        if mask.bit_count() >= 5:
            flush = mask
            break
    if flush:
        high = STRAIGHT_HIGH[flush]
        if high >= 0:
            return _pack(STRAIGHT_FLUSH, (high,))

    quad = -1
    trips: list[int] = []
    pairs: list[int] = []
    singles: list[int] = []
    for rank in range(12, -1, -1):
        n = counts[rank]
        if n:
            if n == 1:
                singles.append(rank)
            elif n == 2:
                pairs.append(rank)
            elif n == 3:
                trips.append(rank)
            else:
                quad = rank

    if quad >= 0:
        others = trips[:1] + pairs[:1] + singles[:1]
        return _pack(QUADS, (quad, max(others)) if others else (quad,))
    if trips and (len(trips) > 1 or pairs):
        second = max(trips[1] if len(trips) > 1 else -1, pairs[0] if pairs else -1)
        return _pack(FULL_HOUSE, (trips[0], second))
    if flush:
        return _pack(FLUSH, [rank for rank in range(12, -1, -1) if flush >> rank & 1][:5])
    high = STRAIGHT_HIGH[suit_masks[0] | suit_masks[1] | suit_masks[2] | suit_masks[3]]
    if high >= 0:
        return _pack(STRAIGHT, (high,))
    if trips:
        return _pack(TRIPS, [trips[0]] + singles[:2])
    if len(pairs) >= 2:
        others = pairs[2:3] + singles[:1]
        return _pack(TWO_PAIR, [pairs[0], pairs[1]] + ([max(others)] if others else []))
    if pairs:
        return _pack(PAIR, [pairs[0]] + singles[:3])
    return _pack(HIGH_CARD, singles[:5])


def hand_category(score: int) -> int:
    """0 (high card) to 8 (straight flush)."""
    return score >> 20


def hand_name(score: int) -> str:
    """e.g. "Full house"."""
    return CATEGORY_NAMES[score >> 20]
