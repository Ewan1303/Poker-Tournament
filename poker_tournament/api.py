"""The interface your bot implements. Full reference: docs/BOT_API.md.

    from poker_tournament import Action, PokerBot

    class Bot(PokerBot):
        def act(self, obs):
            if obs.to_call == 0:
                return Action.check()
            return Action.call() if obs.pot_odds < 0.3 else Action.fold()
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .cards import cards_to_ints

ACTION_TYPES = ("fold", "check", "call", "raise", "all_in")
_ACTION_ALIASES = {"allin": "all_in", "bet": "raise", "raise_to": "raise", "check_call": "call"}


@dataclass(frozen=True)
class Action:
    """What your bot does on its turn.

    type is "fold", "check", "call", "raise" or "all_in". For a raise, amount is
    the total your bet will be on this street ("raise to"), not the extra chips.
    """

    type: str
    amount: int = 0

    @classmethod
    def fold(cls) -> Action:
        return cls("fold")

    @classmethod
    def check(cls) -> Action:
        return cls("check")

    @classmethod
    def call(cls) -> Action:
        """Call the bet you face, or check if there is nothing to call."""
        return cls("call")

    @classmethod
    def raise_to(cls, amount: int) -> Action:
        """Bet or raise so your total bet on this street is `amount`.

        Amounts below the minimum raise are raised to the minimum; amounts above
        your stack become all-in.
        """
        return cls("raise", int(amount))

    @classmethod
    def all_in(cls) -> Action:
        return cls("all_in")

    def to_dict(self) -> dict:
        return {"type": self.type, "amount": self.amount}


def normalize_action(raw: Any) -> dict:
    """Turn what a bot returned into {"type": ..., "amount": ...}.

    Accepts an Action, a dict like {"type": "raise", "amount": 300}, a string like
    "call", or a tuple like ("raise", 300). Raises ValueError for anything else.
    """
    if isinstance(raw, Action):
        kind, amount = raw.type, raw.amount
    elif isinstance(raw, dict):
        kind, amount = raw.get("type", raw.get("action")), raw.get("amount", 0)
    elif isinstance(raw, str):
        kind, amount = raw, 0
    elif isinstance(raw, (tuple, list)) and 1 <= len(raw) <= 2:
        kind, amount = raw[0], (raw[1] if len(raw) == 2 else 0)
    else:
        raise ValueError(f"expected an Action, got {raw!r}")
    kind = str(kind).strip().lower().replace("-", "_").replace(" ", "_")
    kind = _ACTION_ALIASES.get(kind, kind)
    if kind not in ACTION_TYPES:
        raise ValueError(f"unknown action type {kind!r}; use one of {', '.join(ACTION_TYPES)}")
    try:
        amount = int(round(float(amount or 0)))
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"raise amount must be a number, got {amount!r}") from None
    return {"type": kind, "amount": amount}


@dataclass
class PlayerView:
    """One player at the table, as everyone can see them."""

    seat: int
    name: str
    stack: int  # chips behind (not yet in the pot)
    bet: int  # chips put in on the current street
    contributed: int  # chips put in during this hand
    folded: bool
    all_in: bool
    start_stack: int  # stack at the start of this hand

    @classmethod
    def from_dict(cls, d: dict) -> PlayerView:
        return cls(
            seat=d["seat"],
            name=d["name"],
            stack=d["stack"],
            bet=d["bet"],
            contributed=d["contributed"],
            folded=d["folded"],
            all_in=d["all_in"],
            start_stack=d["start_stack"],
        )


@dataclass
class Observation:
    """Everything your bot can see when it is its turn to act.

    Seats are numbered 0..num_players-1 for this hand. history lists every action
    so far this hand as dicts: {"street", "seat", "action", "amount", "to", "all_in"}
    where action is small_blind, big_blind, fold, check, call, bet or raise,
    amount is the chips that action added and to is the player's total bet on
    that street afterwards.
    """

    hand_id: int
    seat: int
    num_players: int
    button: int
    street: str  # "preflop", "flop", "turn" or "river"
    hole_cards: list[str]
    board: list[str]
    pot: int  # all chips in the pot, including bets on this street
    to_call: int  # chips you must add to call (0 means you can check)
    current_bet: int  # the biggest total bet on this street
    min_raise_to: int  # smallest legal raise-to amount (0 if you can't raise)
    max_raise_to: int  # raise-to amount that puts you all-in (0 if you can't raise)
    can_raise: bool
    legal_actions: list[str]
    stack: int  # your chips behind
    bet: int  # your bet on this street so far
    small_blind: int
    big_blind: int
    players: list[PlayerView]
    history: list[dict]
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: dict) -> Observation:
        return cls(
            hand_id=d["hand_id"],
            seat=d["seat"],
            num_players=d["num_players"],
            button=d["button"],
            street=d["street"],
            hole_cards=list(d["hole_cards"]),
            board=list(d["board"]),
            pot=d["pot"],
            to_call=d["to_call"],
            current_bet=d["current_bet"],
            min_raise_to=d["min_raise_to"],
            max_raise_to=d["max_raise_to"],
            can_raise=d["can_raise"],
            legal_actions=list(d["legal_actions"]),
            stack=d["stack"],
            bet=d["bet"],
            small_blind=d["small_blind"],
            big_blind=d["big_blind"],
            players=[PlayerView.from_dict(p) for p in d["players"]],
            history=list(d["history"]),
            raw=d,
        )

    @property
    def me(self) -> PlayerView:
        return self.players[self.seat]

    @property
    def opponents(self) -> list[PlayerView]:
        """Every other player at the table, including ones who have folded."""
        return [p for p in self.players if p.seat != self.seat]

    @property
    def active_opponents(self) -> list[PlayerView]:
        """Other players still in the hand."""
        return [p for p in self.players if p.seat != self.seat and not p.folded]

    @property
    def is_button(self) -> bool:
        return self.seat == self.button

    @property
    def pot_odds(self) -> float:
        """Share of the final pot you would be paying to call: to_call / (pot + to_call)."""
        return self.to_call / (self.pot + self.to_call) if self.to_call else 0.0

    @property
    def street_index(self) -> int:
        return ("preflop", "flop", "turn", "river").index(self.street)

    @property
    def hole_card_ints(self) -> list[int]:
        return cards_to_ints(self.hole_cards)

    @property
    def board_ints(self) -> list[int]:
        return cards_to_ints(self.board)

    def actions_this_street(self) -> list[dict]:
        return [a for a in self.history if a["street"] == self.street]

    def raise_to_pot_fraction(self, fraction: float) -> int:
        """Raise-to amount for a bet of `fraction` x the pot (after calling), kept legal.

        raise_to_pot_fraction(1.0) is a pot-sized raise, 0.5 is half pot.
        Only meaningful when can_raise is True.
        """
        target = self.current_bet + int(fraction * (self.pot + self.to_call))
        return max(self.min_raise_to, min(self.max_raise_to, target))


@dataclass
class MatchInfo:
    """Sent once before a match (heads-up) or a sit-and-go game starts."""

    format: str  # "heads_up" or "sng"
    your_name: str
    players: list[str]  # names of everyone in this match or game
    starting_stack: int
    small_blind: int  # opening blinds (in a sit-and-go they go up, see blind_levels)
    big_blind: int
    num_hands: int | None  # hands in this heads-up match; None for a sit-and-go
    blind_levels: list[list[int]]  # [[small, big], ...] for a sit-and-go
    hands_per_level: int | None
    act_timeout: float | None  # seconds you get per decision
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: dict) -> MatchInfo:
        return cls(
            format=d.get("format", "heads_up"),
            your_name=d.get("your_name", ""),
            players=list(d.get("players", [])),
            starting_stack=d.get("starting_stack", 0),
            small_blind=d.get("small_blind", 0),
            big_blind=d.get("big_blind", 0),
            num_hands=d.get("num_hands"),
            blind_levels=[list(level) for level in d.get("blind_levels", [])],
            hands_per_level=d.get("hands_per_level"),
            act_timeout=d.get("act_timeout"),
            raw=d,
        )


@dataclass
class PlayerResult:
    seat: int
    name: str
    start_stack: int
    end_stack: int
    net: int  # chips won (positive) or lost (negative) this hand
    folded: bool
    hole_cards: list[str] | None  # None unless they are yours or were shown at showdown


@dataclass
class HandResult:
    """Sent to every player when a hand ends."""

    hand_id: int
    seat: int  # your seat in this hand
    net: int  # chips you won or lost
    hole_cards: list[str]
    board: list[str]
    showdown: bool
    players: list[PlayerResult]
    history: list[dict]
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: dict) -> HandResult:
        players = [
            PlayerResult(
                seat=p["seat"],
                name=p["name"],
                start_stack=p["start_stack"],
                end_stack=p["end_stack"],
                net=p["net"],
                folded=p["folded"],
                hole_cards=p["hole_cards"],
            )
            for p in d["players"]
        ]
        me = players[d["seat"]]
        return cls(
            hand_id=d["hand_id"],
            seat=d["seat"],
            net=me.net,
            hole_cards=list(me.hole_cards or []),
            board=list(d["board"]),
            showdown=d["showdown"],
            players=players,
            history=list(d["history"]),
            raw=d,
        )


class PokerBot:
    """Base class for bots. Subclass it in bot.py, name your class Bot, and implement act().

    Load models in __init__ (it has 60 seconds by default). Build file paths from
    Path(__file__).parent so they work wherever your folder ends up.
    """

    def on_match_start(self, info: MatchInfo) -> None:
        """Called once before the first hand of a match or sit-and-go game."""

    def act(self, obs: Observation) -> Action:
        """Called when it is your turn. Return an Action."""
        raise NotImplementedError("your Bot class needs an act(self, obs) method")

    def on_hand_end(self, result: HandResult) -> None:
        """Called after every hand, with showdown cards if there was a showdown."""
