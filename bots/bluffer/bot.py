"""Bets whenever nobody else has, holding anything, and backs down when raised."""

import random

from poker_tournament import Action, PokerBot, estimate_equity

NAME = "Bluffing Bella"
STYLE = "Bluffs a lot"
DESCRIPTION = (
    "Bets whenever nobody else has, holding anything at all, but backs down when someone fights back. "
    "Call her with decent hands, and raise her bets to make her fold."
)


class Bot(PokerBot):
    def __init__(self):
        self.rng = random.Random()

    def act(self, obs):
        raised = any(a["action"] in ("bet", "raise") for a in obs.actions_this_street())
        if not raised and obs.can_raise and self.rng.random() < 0.85:
            return Action.raise_to(obs.raise_to_pot_fraction(0.66))
        if obs.to_call == 0:
            return Action.check()
        # Someone bet or raised: carry on only with a hand worth the price.
        equity = estimate_equity(obs.hole_cards, obs.board, max(1, len(obs.active_opponents)), 150, self.rng)
        return Action.call() if equity > obs.pot_odds + 0.1 else Action.fold()
