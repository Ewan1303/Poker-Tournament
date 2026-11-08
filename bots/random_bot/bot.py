"""Plays random actions. Any real bot should beat it easily."""

import random

from poker_tournament import Action, PokerBot

NAME = "Randy"
STYLE = "Random"
DESCRIPTION = (
    "Does whatever pops into his head: folds, calls and raises completely at random, "
    "now and then shoving all in. A sanity check: any real bot should beat him easily."
)


class Bot(PokerBot):
    def __init__(self):
        self.rng = random.Random()

    def act(self, obs):
        roll = self.rng.random()
        if obs.to_call > 0 and roll < 0.25:
            return Action.fold()
        if obs.can_raise and roll > 0.97:
            return Action.all_in()
        if obs.can_raise and roll > 0.8:
            biggest = max(obs.min_raise_to, obs.raise_to_pot_fraction(1.5))
            return Action.raise_to(self.rng.randint(obs.min_raise_to, biggest))
        return Action.call()  # checks when there is nothing to call
