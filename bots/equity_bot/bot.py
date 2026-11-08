"""Rule-based baseline: estimates its chance of winning by simulating the rest of
the hand, then bets when that chance beats the price. A fair first target."""

import random

from poker_tournament import Action, PokerBot, estimate_equity


class Bot(PokerBot):
    ITERATIONS = 300  # more is more accurate but slower

    def __init__(self):
        self.rng = random.Random()

    def act(self, obs):
        opponents = max(1, len(obs.active_opponents))
        equity = estimate_equity(obs.hole_cards, obs.board, opponents, self.ITERATIONS, self.rng)
        average = 1 / (opponents + 1)  # what an average hand would have
        strong = average + (1 - average) * 0.5  # 0.75 heads-up
        good = average + (1 - average) * 0.25  # 0.625 heads-up

        if obs.can_raise and equity >= strong:
            return Action.raise_to(obs.raise_to_pot_fraction(1.0 if obs.street == "preflop" else 0.75))
        if obs.to_call == 0:
            if obs.can_raise and (equity >= good or self.rng.random() < 0.1):
                return Action.raise_to(obs.raise_to_pot_fraction(0.5))
            return Action.check()
        if equity > obs.pot_odds + 0.05:
            return Action.call()
        return Action.fold()
