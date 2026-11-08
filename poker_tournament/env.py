"""A gym-style environment for reinforcement learning: your agent plays heads-up
against a fixed opponent, one hand per episode.

    from poker_tournament.env import HeadsUpEnv
    from poker_tournament.loader import load_bot

    env = HeadsUpEnv(opponent=load_bot("equity_bot"), seed=0)
    obs = env.reset()
    done = False
    while not done:
        action = my_policy(obs)                    # an Action (or "call", ("raise", 300), ...)
        obs, reward, done, info = env.step(action)
    # reward is the chips you won or lost in the hand; info["result"] is the HandResult

Your seat alternates between the button and the big blind each hand. Hands
where the opponent folds before you act are skipped (they need no decision).
"""

from __future__ import annotations

import random
from typing import Any

from .api import HandResult, MatchInfo, Observation, PokerBot, normalize_action
from .cards import shuffled_deck
from .engine import Hand
from .loader import load_bot


class HeadsUpEnv:
    def __init__(
        self,
        opponent: PokerBot | str,
        *,
        stack: int = 10_000,
        small_blind: int = 50,
        big_blind: int = 100,
        seed: int | None = None,
        agent_name: str = "agent",
    ):
        self.opponent = load_bot(opponent) if isinstance(opponent, str) else opponent
        self.stack = stack
        self.small_blind = small_blind
        self.big_blind = big_blind
        self.agent_name = agent_name
        self._rng = random.Random(seed)
        self.hand: Hand | None = None
        self.hands_played = 0
        self.agent_seat = 0
        self.opponent_name = opponent_name = type(self.opponent).__name__
        self.opponent.on_match_start(
            MatchInfo.from_dict(
                {
                    "format": "heads_up",
                    "your_name": opponent_name,
                    "players": [agent_name, opponent_name],
                    "starting_stack": stack,
                    "small_blind": small_blind,
                    "big_blind": big_blind,
                    "num_hands": None,
                }
            )
        )

    def reset(self) -> Observation:
        """Start a new hand and return your first observation."""
        while True:
            self.hands_played += 1
            self.hand = Hand(
                [self.stack, self.stack],
                button=self.hands_played % 2,
                small_blind=self.small_blind,
                big_blind=self.big_blind,
                deck=shuffled_deck(self._rng.getrandbits(64)),
                names=[self.agent_name, self.opponent_name],
                hand_id=self.hands_played,
            )
            obs = self._play_opponent()
            if obs is not None:
                return obs
            self._finish()

    def step(self, action: Any) -> tuple[Observation | None, float, bool, dict]:
        """Apply your action. Returns (next observation, reward, done, info)."""
        if self.hand is None or self.hand.finished:
            raise RuntimeError("call reset() to start a hand")
        try:
            normalized = normalize_action(action)
        except ValueError:
            normalized = {"type": "check"}  # the engine turns this into a fold if there is a bet to call
        self.hand.apply(normalized)
        obs = self._play_opponent()
        if obs is not None:
            return obs, 0.0, False, {}
        result = self._finish()
        return None, float(self.hand.winnings[self.agent_seat]), True, {"result": result}

    def _play_opponent(self) -> Observation | None:
        hand = self.hand
        while not hand.finished and hand.to_act != self.agent_seat:
            obs = hand.observation(hand.to_act)
            try:
                action = normalize_action(self.opponent.act(Observation.from_dict(obs)))
            except Exception:
                action = {"type": "check" if obs["to_call"] == 0 else "fold"}
            hand.apply(action)
        if hand.finished:
            return None
        return Observation.from_dict(hand.observation(self.agent_seat))

    def _finish(self) -> HandResult:
        try:
            self.opponent.on_hand_end(HandResult.from_dict(self.hand.result_for(1 - self.agent_seat)))
        except Exception:
            pass
        return HandResult.from_dict(self.hand.result_for(self.agent_seat))
