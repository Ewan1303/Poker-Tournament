"""Playing hands, heads-up matches and sit-and-go games between bot players.

Heads-up matches are played in duplicate: the same cards are dealt twice with
the players' seats swapped, so a bot can't win just by being dealt better
cards. Hand k of a match always uses deal_deck(seed, k), whoever is playing.
"""

from __future__ import annotations

import gzip
import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from .cards import shuffled_deck
from .engine import Hand
from .players import BotPlayer, BotStats, LocalBot

DEFAULT_STACK = 10_000
DEFAULT_BLINDS = (50, 100)
# Sit-and-go blind levels (small, big); the last level repeats until there is a winner.
SNG_LEVELS = [
    (50, 100), (75, 150), (100, 200), (150, 300), (200, 400), (300, 600), (400, 800),
    (600, 1200), (800, 1600), (1000, 2000), (1500, 3000), (2000, 4000), (3000, 6000),
    (4000, 8000), (6000, 12000), (8000, 16000), (10000, 20000), (15000, 30000),
    (20000, 40000), (30000, 60000), (50000, 100000),
]  # fmt: skip


def deal_deck(seed: object, hand_index: int) -> list[int]:
    return shuffled_deck(f"{seed}:{hand_index}")


class HandLog:
    """Records every hand as a JSON object: to a file, one per line (gzipped if
    the path ends in .gz), or, with no path, to the .records list.

    With full_info=False only cards that were shown at showdown are kept,
    which is what the other players could see.
    """

    def __init__(self, path: str | Path | None = None, full_info: bool = False):
        self.full_info = full_info
        self.records: list[dict] = []
        self._file = _open_log(path) if path else None

    def write(self, hand: Hand, **extra) -> None:
        record = dict(extra)
        record.update(hand.result(reveal_all=self.full_info))
        if self._file:
            self._file.write(json.dumps(record, separators=(",", ":")) + "\n")
        else:
            self.records.append(record)

    def close(self) -> None:
        if self._file:
            self._file.close()

    def __enter__(self) -> HandLog:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _open_log(path: str | Path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return (gzip.open if path.suffix == ".gz" else open)(path, "wt", encoding="utf-8")


def write_hand_records(path: str | Path, records: Sequence[dict]) -> None:
    with _open_log(path) as f:
        for record in records:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")


def play_hand(
    players: Sequence[BotPlayer],
    stacks: Sequence[int],
    button: int,
    small_blind: int,
    big_blind: int,
    deck: Sequence[int],
    hand_id: int = 0,
) -> Hand:
    """Play one hand to the end, asking each player for actions. Returns the finished Hand."""
    hand = Hand(stacks, button, small_blind, big_blind, deck, names=[p.name for p in players], hand_id=hand_id)
    while not hand.finished:
        seat = hand.to_act
        obs = hand.observation(seat)
        action = players[seat].act(obs)
        if action is None:  # error, timeout or crash
            action = {"type": "check" if obs["to_call"] == 0 else "fold"}
        applied = hand.apply(action)
        if applied["corrected"]:
            stats = players[seat].stats
            stats.corrections += 1
            if len(stats.correction_examples) < 3:
                stats.correction_examples.append(
                    {
                        "street": obs["street"],
                        "to_call": obs["to_call"],
                        "legal_actions": obs["legal_actions"],
                        "requested": action,
                        "applied": applied["action"],
                    }
                )
    for seat, player in enumerate(players):
        player.hand_end(hand.result_for(seat))
    return hand


def play_heads_up(
    players: Sequence[BotPlayer],
    hands: int,
    seed: object,
    *,
    stack: int = DEFAULT_STACK,
    blinds: tuple[int, int] = DEFAULT_BLINDS,
    act_timeout: float | None = None,
    hand_log: HandLog | None = None,
    log_extra: dict | None = None,
) -> list[int]:
    """Play `hands` hands between two started players; returns seat 0's net chips per hand.

    Stacks reset every hand and the button alternates. Replaying the same seed
    with the players swapped gives the other half of a duplicate match.
    """
    small, big = blinds
    names = [p.name for p in players]
    for seat, player in enumerate(players):
        player.match_start(
            {
                "format": "heads_up",
                "your_name": player.name,
                "your_seat": seat,
                "players": names,
                "num_hands": hands,
                "starting_stack": stack,
                "small_blind": small,
                "big_blind": big,
                "blind_levels": [[small, big]],
                "hands_per_level": None,
                "act_timeout": act_timeout,
            }
        )
    nets = []
    for k in range(hands):
        hand = play_hand(players, [stack, stack], k % 2, small, big, deal_deck(seed, k), hand_id=k + 1)
        nets.append(hand.winnings[0])
        if hand_log:
            hand_log.write(hand, **(log_extra or {}))
    return nets


@dataclass
class DuplicateResult:
    """A duplicate heads-up match from player A's point of view."""

    a: str
    b: str
    big_blind: int
    pair_nets: list[int]  # A's net chips over hand k played from both seats
    stats: dict[str, BotStats] = field(default_factory=dict)

    @property
    def hands(self) -> int:
        return 2 * len(self.pair_nets)

    @property
    def chips(self) -> int:
        return sum(self.pair_nets)

    @property
    def bb_per_100(self) -> float:
        """Big blinds A won per 100 hands."""
        return 100 * self.chips / self.big_blind / self.hands if self.pair_nets else 0.0

    @property
    def stderr_bb_per_100(self) -> float:
        n = len(self.pair_nets)
        if n < 2:
            return 0.0
        mean = self.chips / n
        variance = sum((x - mean) ** 2 for x in self.pair_nets) / (n - 1)
        return 100 * math.sqrt(variance / n) / 2 / self.big_blind


def duplicate_result(a: str, b: str, a_nets_first: Sequence[int], a_nets_second: Sequence[int], big_blind: int) -> DuplicateResult:
    return DuplicateResult(a, b, big_blind, [x + y for x, y in zip(a_nets_first, a_nets_second)])


def play_match(
    bot_a,
    bot_b,
    hands: int = 1000,
    seed: object = 0,
    *,
    stack: int = DEFAULT_STACK,
    blinds: tuple[int, int] = DEFAULT_BLINDS,
    make_player: Callable[[object], BotPlayer] | None = None,
    hand_log: HandLog | None = None,
) -> DuplicateResult:
    """A duplicate heads-up match: `hands` deals, each played from both seats.

    bot_a and bot_b can be PokerBot instances, bot folders or names of bots in
    bots/. By default they run in this process (fast, handy in training
    scripts); pass make_player to run them some other way. Each half of the
    match uses a fresh player from make_player, so with folders nothing is
    remembered between halves. Instances you pass in are reused as they are.
    """
    from .loader import resolve_bot

    def default_player(bot) -> BotPlayer:
        if isinstance(bot, (str, Path)):
            return LocalBot(resolve_bot(bot))
        return LocalBot(bot)

    make = make_player or default_player
    halves = []
    stats: dict[str, BotStats] = {}
    for swapped in (False, True):
        a, b = make(bot_a), make(bot_b)
        if a.name == b.name:
            a.name, b.name = a.name + "_A", b.name + "_B"
        seats = [b, a] if swapped else [a, b]
        try:
            for player in seats:
                player.start()
            nets = play_heads_up(
                seats, hands, seed, stack=stack, blinds=blinds, hand_log=hand_log, log_extra={"half": int(swapped) + 1}
            )
        finally:
            for player in seats:
                player.close()
        halves.append([-x for x in nets] if swapped else nets)
        for player in (a, b):
            stats.setdefault(player.name, BotStats()).add(player.stats)
    result = duplicate_result(a.name, b.name, halves[0], halves[1], blinds[1])
    result.stats = stats
    return result


def play_sng(
    players: Sequence[BotPlayer],
    seed: object,
    *,
    stack: int = DEFAULT_STACK,
    levels: Sequence[tuple[int, int]] = SNG_LEVELS,
    hands_per_level: int = 20,
    max_hands: int = 3000,
    act_timeout: float | None = None,
    hand_log: HandLog | None = None,
    log_extra: dict | None = None,
) -> dict:
    """Play a sit-and-go to the end with started players in seat order.

    Everyone starts with `stack`; the blinds go up every `hands_per_level`
    hands. Returns {"names", "places", "hands", "stacks"} where places[i] is
    the finishing place of seat i (1 = winner; players who bust on the same
    hand are placed by the chips they started that hand with, ties sharing).
    """
    n = len(players)
    names = [p.name for p in players]
    for seat, player in enumerate(players):
        player.match_start(
            {
                "format": "sng",
                "your_name": player.name,
                "your_seat": seat,
                "players": names,
                "num_hands": None,
                "starting_stack": stack,
                "small_blind": levels[0][0],
                "big_blind": levels[0][1],
                "blind_levels": [list(level) for level in levels],
                "hands_per_level": hands_per_level,
                "act_timeout": act_timeout,
            }
        )
    stacks = [stack] * n
    places = [0.0] * n
    button = random.Random(f"{seed}:button").randrange(n)
    hand_no = 0
    while sum(1 for s in stacks if s > 0) > 1 and hand_no < max_hands:
        small, big = levels[min(hand_no // hands_per_level, len(levels) - 1)]
        if hand_no:
            button = (button + 1) % n
            while stacks[button] == 0:
                button = (button + 1) % n
        active = [i for i in range(n) if stacks[i] > 0]
        before = {i: stacks[i] for i in active}
        hand = play_hand(
            [players[i] for i in active],
            [stacks[i] for i in active],
            active.index(button),
            small,
            big,
            deal_deck(seed, hand_no),
            hand_id=hand_no + 1,
        )
        for k, i in enumerate(active):
            stacks[i] = hand.seats[k].stack
        hand_no += 1
        if hand_log:
            hand_log.write(hand, **(log_extra or {}))
        busted = [i for i in active if stacks[i] == 0]
        if busted:
            alive = sum(1 for s in stacks if s > 0)
            _assign_places(places, busted, alive + 1, lambda i: before[i])
    remaining = [i for i in range(n) if stacks[i] > 0]
    _assign_places(places, remaining, 1, lambda i: stacks[i])
    return {"names": names, "places": places, "hands": hand_no, "stacks": stacks}


def _assign_places(places: list[float], seats: list[int], first_place: int, key: Callable[[int], int]) -> None:
    ordered = sorted(seats, key=key, reverse=True)
    place = first_place
    k = 0
    while k < len(ordered):
        j = k
        while j < len(ordered) and key(ordered[j]) == key(ordered[k]):
            j += 1
        shared = place + (j - k - 1) / 2  # players tied on chips share the places they span
        for seat in ordered[k:j]:
            places[seat] = shared
        place += j - k
        k = j
