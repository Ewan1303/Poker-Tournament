"""Plays only big pairs and big aces, and folds everything else."""

import random

from poker_tournament import Action, PokerBot, estimate_equity

NAME = "Rocky"
STYLE = "Ultra-tight"
DESCRIPTION = (
    "Only plays big pairs and big aces and folds everything else, so he hardly ever plays a hand. "
    "When Rocky bets, believe him and fold; the rest of the time, keep stealing his blinds."
)


def premium(hole: list[int]) -> bool:
    """Tens or better, ace-king, ace-queen, and suited ace-jack or king-queen."""
    high, low = sorted((card >> 2 for card in hole), reverse=True)  # 12 is an ace, 8 a ten
    suited = hole[0] & 3 == hole[1] & 3
    if high == low:
        return high >= 8
    if high == 12 and low >= 10:
        return True
    return suited and (high, low) in ((12, 9), (11, 10))


class Bot(PokerBot):
    def __init__(self):
        self.rng = random.Random()

    def act(self, obs):
        if obs.street == "preflop":
            if premium(obs.hole_card_ints):
                return Action.raise_to(obs.raise_to_pot_fraction(1.0)) if obs.can_raise else Action.call()
            return Action.check() if obs.to_call == 0 else Action.fold()
        equity = estimate_equity(obs.hole_cards, obs.board, max(1, len(obs.active_opponents)), 200, self.rng)
        if obs.can_raise and equity > 0.75:
            return Action.raise_to(obs.raise_to_pot_fraction(0.75))
        if obs.to_call == 0:
            return Action.check()
        return Action.call() if equity > obs.pot_odds + 0.15 else Action.fold()
