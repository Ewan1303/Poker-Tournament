"""Never folds and never raises: checks or calls every time. Punishes bluffers."""

from poker_tournament import Action, PokerBot


class Bot(PokerBot):
    def act(self, obs):
        return Action.call()
