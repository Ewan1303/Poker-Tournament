"""Sees lots of flops, then folds unless the flop hits him."""

from poker_tournament import Action, PokerBot, evaluate, hand_category
from poker_tournament.evaluator import PAIR, TWO_PAIR

NAME = "Nervous Nick"
STYLE = "Fit or fold"
DESCRIPTION = (
    "Limps into lots of pots but only carries on if the flop hits him hard. "
    "Bet at him and he'll usually fold; if he raises you, he really has it."
)


def decent_start(hole: list[int]) -> bool:
    """Any pair, or two cards jack or better."""
    high, low = sorted((card >> 2 for card in hole), reverse=True)
    return high == low or low >= 9


def made_hand(obs) -> int:
    """2 for two pair or better (using his own cards), 1 for top pair or an overpair, 0 for a miss."""
    score = evaluate(obs.hole_card_ints + obs.board_ints)
    category = hand_category(score)
    if category >= TWO_PAIR:
        return 2 if category > hand_category(evaluate(obs.board_ints)) else 0  # the board alone doesn't count
    if category == PAIR:
        pair = ((score >> 16) & 15) - 1  # the rank of the pair
        mine = [card >> 2 for card in obs.hole_card_ints]
        top = max(card >> 2 for card in obs.board_ints)
        return 1 if pair in mine and pair >= top else 0
    return 0


class Bot(PokerBot):
    def act(self, obs):
        raised = any(a["action"] in ("bet", "raise") for a in obs.actions_this_street())
        if obs.street == "preflop":
            if raised and not decent_start(obs.hole_card_ints):
                return Action.fold()
            return Action.call()  # limps, and checks when he can
        strength = made_hand(obs)
        if strength == 2 and obs.can_raise:
            return Action.raise_to(obs.raise_to_pot_fraction(1.0))
        if strength >= 1:
            return Action.call()
        return Action.check() if obs.to_call == 0 else Action.fold()
