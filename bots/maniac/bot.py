"""Raises nearly every hand, often all in, whatever the cards."""

import random

from poker_tournament import Action, PokerBot

NAME = "Maniac Max"
STYLE = "Hyper-aggressive"
DESCRIPTION = (
    "Raises nearly every hand and loves to shove all in, whatever he's holding. Scary at first, "
    "but he's usually got nothing: call him down with any decent hand and he'll hand you his chips."
)


class Bot(PokerBot):
    def __init__(self):
        self.rng = random.Random()

    def act(self, obs):
        if obs.can_raise:
            roll = self.rng.random()
            if roll < 0.12:
                return Action.all_in()
            if roll < 0.85:
                return Action.raise_to(obs.raise_to_pot_fraction(self.rng.choice((0.75, 1.0, 1.5))))
        return Action.call()  # Max never folds
