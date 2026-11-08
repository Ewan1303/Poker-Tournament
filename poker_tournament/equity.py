"""Equity: how often a hand wins, against random hands or against known ones."""

from __future__ import annotations

import itertools
import random
from typing import Iterable, Sequence

from .cards import cards_to_ints
from .evaluator import evaluate


def estimate_equity(
    hole_cards: Iterable[str | int],
    board: Iterable[str | int] = (),
    opponents: int = 1,
    iterations: int = 1000,
    rng: random.Random | None = None,
) -> float:
    """Chance (0-1) that hole_cards win at showdown against `opponents` random hands.

    Ties count as a share of the pot. The remaining board is dealt at random.
    About 1000 iterations takes 10-20 ms heads-up; fewer is noisier but faster.
    """
    hole = cards_to_ints(hole_cards)
    known = cards_to_ints(board)
    if len(hole) != 2 or len(known) > 5:
        raise ValueError("need 2 hole cards and at most 5 board cards")
    opponents = max(1, int(opponents))
    used = set(hole) | set(known)
    deck = [card for card in range(52) if card not in used]
    missing = 5 - len(known)
    draw_size = missing + 2 * opponents
    sample = (rng or random).sample
    total = 0.0
    for _ in range(iterations):
        draw = sample(deck, draw_size)
        full_board = known + draw[:missing]
        mine = evaluate(hole + full_board)
        best = -1
        ties = 0
        for k in range(missing, draw_size, 2):
            score = evaluate(draw[k : k + 2] + full_board)
            if score > best:
                best, ties = score, 1
            elif score == best:
                ties += 1
        if mine > best:
            total += 1.0
        elif mine == best:
            total += 1.0 / (ties + 1)
    return total / iterations


def showdown_odds(
    hands: Sequence[Iterable[str | int]],
    board: Iterable[str | int] = (),
    samples: int = 2000,
    rng: random.Random | None = None,
) -> list[float]:
    """Each hand's chance (0-1) of winning from here when everyone's cards are
    known, as on a TV broadcast. Ties count as a share. Exact once the flop is
    out (every remaining board is tried); before it, `samples` random boards."""
    holes = [cards_to_ints(hand) for hand in hands]
    known = cards_to_ints(board)
    used = {card for hole in holes for card in hole} | set(known)
    deck = [card for card in range(52) if card not in used]
    missing = 5 - len(known)
    if missing <= 2:
        runouts = itertools.combinations(deck, missing)
    else:
        draw = (rng or random).sample
        runouts = (draw(deck, missing) for _ in range(samples))
    shares = [0.0] * len(holes)
    count = 0
    for extra in runouts:
        full = known + list(extra)
        scores = [evaluate(hole + full) for hole in holes]
        best = max(scores)
        winners = [i for i, score in enumerate(scores) if score == best]
        for i in winners:
            shares[i] += 1 / len(winners)
        count += 1
    return [share / count for share in shares]
