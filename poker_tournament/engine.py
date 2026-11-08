"""No-limit Texas hold'em rules for one hand with 2 to 10 players.

The engine is pure Python with no dependencies, so you can drive it directly
when training (poker_tournament.env wraps it in a gym-style interface):

    hand = Hand(stacks=[10000, 10000], button=0, small_blind=50, big_blind=100,
                deck=shuffled_deck(seed=1))
    while not hand.finished:
        obs = hand.observation(hand.to_act)   # the dict a bot would see
        hand.apply({"type": "call"})
    print(hand.winnings)                       # net chips for each seat

Rules: heads-up the button posts the small blind and acts first before the
flop and last after it. A raise must be at least the size of the previous bet
or raise on the street; an all-in for less does not reopen the betting to
players who have already acted. Side pots, split pots and odd chips (to the
first winner left of the button) are handled. Hole cards for seat i are
deck[2i], deck[2i+1] and the board is the next five cards, so the same deck
always gives the same seat the same cards.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .api import normalize_action
from .cards import card_to_int, cards_to_strs
from .evaluator import evaluate

STREETS = ("preflop", "flop", "turn", "river")
_BOARD_SIZE = (0, 3, 4, 5)


@dataclass
class Seat:
    name: str
    stack: int  # chips behind
    start_stack: int
    hole: list[int]
    bet: int = 0  # chips put in on the current street
    contributed: int = 0  # chips put in this hand
    folded: bool = False
    all_in: bool = False
    acted: bool = False  # has made a decision on this street
    level_after_action: int = 0  # the bet to match just after this player's last decision this street


class Hand:
    """One hand of no-limit hold'em. Call apply() for the player in to_act until finished."""

    def __init__(
        self,
        stacks: Sequence[int],
        button: int,
        small_blind: int,
        big_blind: int,
        deck: Sequence[int | str],
        names: Sequence[str] | None = None,
        hand_id: int = 0,
    ):
        n = len(stacks)
        if not 2 <= n <= 10:
            raise ValueError("a hand needs 2 to 10 players")
        if any(stack <= 0 for stack in stacks):
            raise ValueError("every player in a hand needs chips")
        if not 0 < small_blind <= big_blind:
            raise ValueError("blinds must satisfy 0 < small_blind <= big_blind")
        if len(deck) < 2 * n + 5:
            raise ValueError(f"the deck needs at least {2 * n + 5} cards for {n} players")
        cards = [card_to_int(card) for card in deck[: 2 * n + 5]]
        names = list(names) if names else [f"player{i}" for i in range(n)]

        self.n = n
        self.button = button % n
        self.small_blind = small_blind
        self.big_blind = big_blind
        self.hand_id = hand_id
        self.seats = [
            Seat(name=names[i], stack=stacks[i], start_stack=stacks[i], hole=cards[2 * i : 2 * i + 2])
            for i in range(n)
        ]
        self._runout = cards[2 * n : 2 * n + 5]
        self.board: list[int] = []
        self.street = 0
        self.history: list[dict] = []
        self.current_bet = 0
        self.min_raise = big_blind  # size of the last full bet or raise on this street
        self.to_act: int | None = None
        self.finished = False
        self.showdown = False
        self.winnings: list[int] | None = None  # net chips per seat once the hand is over
        self.pots: list[dict] = []
        self.chose_to_show: list[int] = []  # seats that showed cards they didn't have to
        self._post_blinds()

    # -- state queries -------------------------------------------------------

    @property
    def pot(self) -> int:
        return sum(seat.contributed for seat in self.seats)

    def to_call(self, seat: int) -> int:
        """Chips `seat` must add to call (capped at their stack)."""
        s = self.seats[seat]
        target = self.current_bet if self._others_can_act(seat) else self._max_other_bet(seat)
        return max(0, min(target - s.bet, s.stack))

    def can_raise(self, seat: int) -> bool:
        s = self.seats[seat]
        if s.folded or s.all_in or not self._others_can_act(seat):
            return False
        if s.bet + s.stack <= self.current_bet:
            return False
        # Betting is reopened only by a full raise (or several all-ins adding up to one).
        return not s.acted or self.current_bet - s.level_after_action >= self.min_raise

    def legal_actions(self, seat: int) -> list[str]:
        legal = ["fold", "call"] if self.to_call(seat) else ["check"]
        if self.can_raise(seat):
            legal.append("raise")
        return legal

    def observation(self, seat: int) -> dict:
        """What `seat` is allowed to see, as the dict sent to bots."""
        s = self.seats[seat]
        to_call = self.to_call(seat)
        can_raise = self.can_raise(seat)
        max_to = s.bet + s.stack if can_raise else 0
        min_to = min(self.current_bet + self.min_raise, max_to) if can_raise else 0
        legal = ["fold", "call"] if to_call else ["check"]
        if can_raise:
            legal.append("raise")
        return {
            "hand_id": self.hand_id,
            "seat": seat,
            "num_players": self.n,
            "button": self.button,
            "street": STREETS[self.street],
            "hole_cards": cards_to_strs(s.hole),
            "board": cards_to_strs(self.board),
            "pot": self.pot,
            "to_call": to_call,
            "current_bet": self.current_bet,
            "min_raise_to": min_to,
            "max_raise_to": max_to,
            "can_raise": can_raise,
            "legal_actions": legal,
            "stack": s.stack,
            "bet": s.bet,
            "small_blind": self.small_blind,
            "big_blind": self.big_blind,
            "players": [self._player_view(i) for i in range(self.n)],
            "history": [dict(event) for event in self.history],
        }

    def pot_breakdown(self) -> list[dict]:
        """The pot as it stands, split into the main pot and side pots.

        A side pot starts above each player who is all in for less than the
        others; the last pot is the one still being bet into. Each entry is
        {"amount", "eligible"} where eligible lists the seats that can win it.
        """
        contributions = [s.contributed for s in self.seats]
        caps = sorted({s.contributed for s in self.seats if s.all_in and not s.folded})
        top = max(contributions)
        pots = []
        previous = 0
        for cap in caps + [top]:
            if cap <= previous:
                continue
            amount = sum(min(c, cap) - min(c, previous) for c in contributions)
            # Players still betting can win the pot being bet into even before they've matched it.
            eligible = [
                i
                for i, s in enumerate(self.seats)
                if not s.folded and (s.contributed > previous or (cap == top and not s.all_in))
            ]
            if amount:
                pots.append({"amount": amount, "eligible": eligible})
            previous = cap
        return pots

    def shown(self, seat: int) -> str | None:
        """'showdown' if the seat had to show its cards, 'chose' if it showed them anyway, else None."""
        if self.showdown and not self.seats[seat].folded:
            return "showdown"
        return "chose" if seat in self.chose_to_show else None

    def show(self, seat: int) -> None:
        """Once the hand is over, a player may show cards nobody made them show
        (say, to show off a bluff). At a showdown everyone still in shows anyway."""
        if not self.finished:
            raise RuntimeError("cards can only be shown once the hand is over")
        if self.shown(seat) is None:
            self.chose_to_show.append(seat)

    def result(self, reveal_all: bool = True, seat: int | None = None) -> dict:
        """The finished hand. With reveal_all=False, hole cards are hidden unless
        they belong to `seat`, were shown at showdown, or their owner chose to show them."""
        if not self.finished:
            raise RuntimeError("the hand is not over yet")
        players = []
        for i, s in enumerate(self.seats):
            shown = self.shown(i)
            players.append(
                {
                    "seat": i,
                    "name": s.name,
                    "start_stack": s.start_stack,
                    "end_stack": s.stack,
                    "net": self.winnings[i],
                    "folded": s.folded,
                    "shown": shown,
                    "hole_cards": cards_to_strs(s.hole) if reveal_all or i == seat or shown else None,
                }
            )
        result = {
            "hand_id": self.hand_id,
            "button": self.button,
            "small_blind": self.small_blind,
            "big_blind": self.big_blind,
            "board": cards_to_strs(self.board),
            "showdown": self.showdown,
            "players": players,
            "pots": [dict(pot) for pot in self.pots],
            "history": [dict(event) for event in self.history],
        }
        if seat is not None:
            result["seat"] = seat
        return result

    def result_for(self, seat: int) -> dict:
        """The finished hand as `seat` is allowed to see it."""
        return self.result(reveal_all=False, seat=seat)

    # -- actions -------------------------------------------------------------

    def apply(self, action) -> dict:
        """Apply an action for the player in to_act and move the hand on.

        Illegal actions are turned into the nearest legal one: folding when you
        could check becomes a check, checking when facing a bet becomes a fold,
        raising when you may not raise becomes a call, and raise amounts are
        clamped between the minimum raise and all-in. Returns the action as
        applied, with "corrected": True if it had to be changed.
        """
        if self.finished:
            raise RuntimeError("the hand is over")
        seat = self.to_act
        s = self.seats[seat]
        corrected = False
        try:
            normalized = normalize_action(action)
            kind, amount = normalized["type"], normalized["amount"]
        except ValueError:
            kind, amount, corrected = "check", 0, True
        to_call = self.to_call(seat)
        can_raise = self.can_raise(seat)

        if kind == "all_in":
            kind, amount = ("raise", s.bet + s.stack) if can_raise else ("call", 0)
        if kind == "raise" and not can_raise:
            kind, corrected = "call", True
        if kind == "call" and to_call == 0:
            kind = "check"
        if kind == "fold" and to_call == 0:
            kind, corrected = "check", True
        if kind == "check" and to_call > 0:
            kind, corrected = "fold", True

        put = 0
        label = kind
        if kind == "raise":
            max_to = s.bet + s.stack
            target = min(max(amount, min(self.current_bet + self.min_raise, max_to)), max_to)
            put = target - s.bet
            if target - self.current_bet >= self.min_raise:
                self.min_raise = target - self.current_bet
            label = "bet" if self.current_bet == 0 else "raise"
            self.current_bet = target
        elif kind == "call":
            put = to_call
        elif kind == "fold":
            s.folded = True
        if put:
            s.stack -= put
            s.bet += put
            s.contributed += put
            s.all_in = s.stack == 0
        s.acted = True
        s.level_after_action = self.current_bet

        event = {
            "street": STREETS[self.street],
            "seat": seat,
            "action": label,
            "amount": put,
            "to": s.bet,
            "all_in": s.all_in,
        }
        self.history.append(event)
        self._continue_from(seat)
        return dict(event, corrected=corrected)

    # -- internals -----------------------------------------------------------

    def _next(self, seat: int) -> int:
        return (seat + 1) % self.n

    def _player_view(self, i: int) -> dict:
        s = self.seats[i]
        return {
            "seat": i,
            "name": s.name,
            "stack": s.stack,
            "bet": s.bet,
            "contributed": s.contributed,
            "folded": s.folded,
            "all_in": s.all_in,
            "start_stack": s.start_stack,
        }

    def _others_can_act(self, seat: int) -> bool:
        return any(not s.folded and not s.all_in for i, s in enumerate(self.seats) if i != seat)

    def _max_other_bet(self, seat: int) -> int:
        return max((s.bet for i, s in enumerate(self.seats) if i != seat and not s.folded), default=0)

    def _needs_action(self, seat: int) -> bool:
        s = self.seats[seat]
        if s.folded or s.all_in:
            return False
        if not self._others_can_act(seat):
            # Nobody left to bet against: only act to call an all-in bigger than our bet.
            return s.bet < self._max_other_bet(seat)
        return not s.acted or s.bet < self.current_bet

    def _post(self, seat: int, amount: int, kind: str) -> None:
        s = self.seats[seat]
        amount = min(amount, s.stack)
        s.stack -= amount
        s.bet += amount
        s.contributed += amount
        s.all_in = s.stack == 0
        self.history.append(
            {"street": "preflop", "seat": seat, "action": kind, "amount": amount, "to": s.bet, "all_in": s.all_in}
        )

    def _post_blinds(self) -> None:
        if self.n == 2:
            small, big = self.button, self._next(self.button)
        else:
            small = self._next(self.button)
            big = self._next(small)
        self._post(small, self.small_blind, "small_blind")
        self._post(big, self.big_blind, "big_blind")
        # Players must call the full big blind even if the big blind is all-in for less.
        self.current_bet = self.big_blind
        self.min_raise = self.big_blind
        self._continue_from(big)

    def _continue_from(self, seat: int) -> None:
        live = [i for i, s in enumerate(self.seats) if not s.folded]
        if len(live) == 1:
            self._award({live[0]: 0}, showdown=False)
            return
        i = seat
        for _ in range(self.n):
            i = self._next(i)
            if self._needs_action(i):
                self.to_act = i
                return
        self._next_street()

    def _next_street(self) -> None:
        while self.street < 3:
            self.street += 1
            for s in self.seats:
                s.bet = 0
                s.acted = False
                s.level_after_action = 0
            self.current_bet = 0
            self.min_raise = self.big_blind
            self.board = self._runout[: _BOARD_SIZE[self.street]]
            i = self.button
            for _ in range(self.n):
                i = self._next(i)
                if self._needs_action(i):
                    self.to_act = i
                    return
        self.board = list(self._runout)
        scores = {i: evaluate(s.hole + self._runout) for i, s in enumerate(self.seats) if not s.folded}
        self._award(scores, showdown=True)

    def _award(self, scores: dict[int, int], showdown: bool) -> None:
        contributions = [s.contributed for s in self.seats]
        payouts = [0] * self.n
        # Odd chips go to the first winner to the left of the button.
        order = [(self.button + k) % self.n for k in range(1, self.n + 1)]
        # Slice the chips by contribution level: the main pot, then each side pot.
        pots: list[dict] = []
        previous = 0
        for level in sorted({c for c in contributions if c > 0}):
            amount = sum(min(c, level) - min(c, previous) for c in contributions)
            eligible = [i for i in order if i in scores and contributions[i] >= level]
            # A slice only one player put chips into is a bet nobody called: it goes back.
            uncalled = sum(1 for c in contributions if c >= level) == 1
            if not eligible:  # nobody still in the hand put this much in, so it goes back
                for i, c in enumerate(contributions):
                    payouts[i] += min(c, level) - min(c, previous)
            if pots and pots[-1]["eligible"] == eligible and pots[-1]["uncalled"] == uncalled:
                pots[-1]["amount"] += amount
            else:
                pots.append({"amount": amount, "eligible": eligible, "winners": [], "uncalled": uncalled})
            previous = level
        for pot in pots:
            if pot["eligible"]:
                best = max(scores[i] for i in pot["eligible"])
                winners = [i for i in pot["eligible"] if scores[i] == best]
                share, odd = divmod(pot["amount"], len(winners))
                for k, winner in enumerate(winners):
                    payouts[winner] += share + (1 if k < odd else 0)
                pot["winners"] = winners
        for i, s in enumerate(self.seats):
            s.stack += payouts[i]
        self.winnings = [payouts[i] - contributions[i] for i in range(self.n)]
        self.pots = pots
        self.showdown = showdown
        self.finished = True
        self.to_act = None
