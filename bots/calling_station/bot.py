"""Never folds and never raises: checks or calls every time. Punishes bluffers."""

from poker_tournament import Action, PokerBot

NAME = "Callie"
STYLE = "Calls everything"
DESCRIPTION = (
    "Never folds and never raises: she calls every bet just to see what you've got. "
    "Bluffing her is hopeless. Bet your good hands big and she'll pay you off."
)


class Bot(PokerBot):
    def act(self, obs):
        return Action.call()
