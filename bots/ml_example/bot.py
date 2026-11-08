"""Machine-learning example: a logistic-regression model trained by train.py
predicts the chance of winning, and simple rules turn that into bets.

The model is tiny and the rules are crude; the point is the pipeline. Train
your own model however you like (numpy, scikit-learn, PyTorch), save its
weights in your bot's folder and load them in __init__ like this.
"""

from pathlib import Path

import numpy as np

from poker_tournament import Action, PokerBot
from poker_tournament.features import HAND_FEATURE_NAMES, hand_features

MODEL = Path(__file__).parent / "model.npz"  # build paths from __file__, not the working directory


class Bot(PokerBot):
    def __init__(self):
        data = np.load(MODEL)
        if list(data["feature_names"]) != HAND_FEATURE_NAMES:
            raise RuntimeError("model.npz was trained with different features; run train.py again")
        self.weights = data["weights"]
        self.bias = float(data["bias"])
        self.mean = data["mean"]
        self.std = data["std"]

    def win_probability(self, obs) -> float:
        x = (np.array(hand_features(obs.hole_cards, obs.board)) - self.mean) / self.std
        heads_up = 1 / (1 + np.exp(-(x @ self.weights + self.bias)))
        # The model knows one opponent; against several, assume each must be beaten.
        return float(heads_up) ** max(1, len(obs.active_opponents))

    def act(self, obs):
        equity = self.win_probability(obs)
        opponents = max(1, len(obs.active_opponents))
        average = 1 / (opponents + 1)
        if obs.can_raise and equity >= average + (1 - average) * 0.45:
            return Action.raise_to(obs.raise_to_pot_fraction(0.75))
        if obs.to_call == 0:
            return Action.check()
        if equity > obs.pot_odds + 0.05:
            return Action.call()
        return Action.fold()
