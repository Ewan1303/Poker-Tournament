"""My poker bot.

The tournament imports this file and creates one Bot() per match. Every time
it's your turn, act(obs) is called and must return an Action within the time
limit (1 second by default). The full interface is in docs/BOT_API.md.

Try it:     poker match path/to/this/folder equity_bot
Check it:   poker validate path/to/this/folder
"""

import random
from pathlib import Path

from poker_tournament import Action, PokerBot, estimate_equity

HERE = Path(__file__).parent  # put model files next to this one and load them via HERE


class Bot(PokerBot):
    def __init__(self):
        # Load models here, e.g. self.model = torch.load(HERE / "model.pt") or np.load(HERE / "weights.npz").
        self.rng = random.Random()
        self.opponent_raises = {}  # simple opponent modelling: how often each opponent raises

    def on_match_start(self, info):
        # info.players, info.num_hands, info.starting_stack, info.big_blind, info.format ...
        self.big_blind = info.big_blind

    def act(self, obs):
        # obs.hole_cards e.g. ["As", "Kd"]; obs.board; obs.pot; obs.to_call;
        # obs.min_raise_to / obs.max_raise_to; obs.players; obs.history ...
        opponents = max(1, len(obs.active_opponents))
        equity = estimate_equity(obs.hole_cards, obs.board, opponents=opponents, iterations=200, rng=self.rng)

        # Replace these rules with your own logic or model.
        if obs.can_raise and equity > 0.7:
            return Action.raise_to(obs.raise_to_pot_fraction(0.75))  # bet three-quarters of the pot
        if obs.to_call == 0:
            return Action.check()
        if equity > obs.pot_odds:
            return Action.call()
        return Action.fold()

    def on_hand_end(self, result):
        # result.net is what you won or lost; result.players[i].hole_cards is shown
        # for anyone who reached showdown. Learn from it here.
        for event in result.history:
            if event["action"] in ("bet", "raise") and event["seat"] != result.seat:
                name = result.players[event["seat"]].name
                self.opponent_raises[name] = self.opponent_raises.get(name, 0) + 1
