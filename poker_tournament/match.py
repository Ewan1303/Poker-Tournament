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


def deal_deck(seed: object, hand_index: object) -> list[int]:
    return shuffled_deck(f"{seed}:{hand_index}")


def blind_levels(big_blind: int) -> list[tuple[int, int]]:
    """SNG_LEVELS scaled so the first level's big blind is `big_blind`."""
    factor = big_blind / SNG_LEVELS[0][1]
    return [(max(1, round(small * factor)), max(1, round(big * factor))) for small, big in SNG_LEVELS]


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
    offer_shows(hand, players)
    for seat, player in enumerate(players):
        player.hand_end(hand.result_for(seat))
    return hand


def offer_shows(hand: Hand, players: Sequence[BotPlayer | None]) -> None:
    """Like real poker: at a showdown everyone still in shows, and anyone else
    (a winner nobody called, or a player who folded) may show if they want to."""
    for seat, player in enumerate(players):
        if player is not None and hand.shown(seat) is None and player.wants_to_show(hand.result_for(seat)):
            hand.show(seat)


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


class _Table:
    """One table in a tournament: its players in seat order, and where the button is."""

    def __init__(self, seats: list[int], button: int):
        self.seats = seats  # player numbers, in seat order
        self.button_pos = button  # index in seats of the latest button
        self.button_player: int | None = None
        self.big_blind: int | None = None

    def next_button(self, stacks: Sequence[int]) -> int:
        """Index in seats of this hand's button."""
        if self.button_player is None:
            return self.button_pos % len(self.seats)
        if self.button_player in self.seats:
            previous = self.seats.index(self.button_player)
        else:  # the last button left the table, so carry on from about the same spot
            previous = (self.button_pos - 1) % len(self.seats)
        last_big_blind = self.seats.index(self.big_blind) if self.big_blind in self.seats else None
        return next_button([stacks[p] for p in self.seats], previous, last_big_blind)


def _rebalance(tables: list[_Table], stacks: Sequence[int], table_size: int, rng: random.Random) -> list[_Table]:
    """After players go out: break tables that aren't needed, then even the rest up."""
    for table in tables:
        table.seats = [p for p in table.seats if stacks[p] > 0]
    tables = [table for table in tables if table.seats]
    needed = max(1, math.ceil(sum(len(t.seats) for t in tables) / table_size))
    while len(tables) > needed:
        broken = min(tables, key=lambda t: len(t.seats))
        tables.remove(broken)
        for player in broken.seats:
            min(tables, key=lambda t: len(t.seats)).seats.append(player)
    while True:
        biggest = max(tables, key=lambda t: len(t.seats))
        smallest = min(tables, key=lambda t: len(t.seats))
        if len(biggest.seats) - len(smallest.seats) <= 1:
            return tables
        movable = [p for p in biggest.seats if p != biggest.button_player] or biggest.seats
        mover = rng.choice(movable)
        biggest.seats.remove(mover)
        smallest.seats.append(mover)


def play_tournament(
    players: Sequence[BotPlayer],
    seed: object,
    *,
    stack: int = DEFAULT_STACK,
    levels: Sequence[tuple[int, int]] = SNG_LEVELS,
    hands_per_level: int = 20,
    table_size: int = 10,
    max_hands: int | None = 100,
    act_timeout: float | None = None,
    hand_log: HandLog | None = None,
    log_extra: dict | None = None,
) -> dict:
    """A freezeout tournament between started players, seated at random.

    Everyone starts with `stack`, the blinds go up every `hands_per_level`
    hands, and a player who loses all their chips is out. It ends when one
    player has every chip, or after `max_hands` hands at a table, when everyone
    left is ranked by chips (None means no limit). With more players than fit
    at one table they're split across tables, which are balanced as players go
    out and merge into a final table.

    Returns {"names", "places", "stacks", "hands", "rounds", "finished"}:
    places[i] is player i's finishing place (1 = winner; players out on the same
    round are ranked by the chips they had before it, ties sharing), stacks are
    the final chip counts, and finished says whether one player won every chip.
    """
    n = len(players)
    if n < 2:
        raise ValueError("a tournament needs at least two players")
    names = [p.name for p in players]
    rng = random.Random(f"{seed}:seating")
    order = list(range(n))
    rng.shuffle(order)
    count = math.ceil(n / table_size)
    tables = [_Table(order[t::count], rng.randrange(n)) for t in range(count)]
    info = {
        "format": "tournament",
        "players": names,
        "num_hands": max_hands,
        "starting_stack": stack,
        "small_blind": levels[0][0],
        "big_blind": levels[0][1],
        "blind_levels": [list(level) for level in levels],
        "hands_per_level": hands_per_level,
        "table_size": table_size,
        "act_timeout": act_timeout,
    }
    for player in players:
        player.match_start(dict(info, your_name=player.name))
    stacks = [stack] * n
    places = [0.0] * n
    rounds = hands = 0
    while sum(1 for s in stacks if s > 0) > 1 and (max_hands is None or rounds < max_hands):
        small, big = levels[min(rounds // hands_per_level, len(levels) - 1)]
        busted: list[int] = []
        before: dict[int, int] = {}
        for t, table in enumerate(tables):
            if len(table.seats) < 2:
                continue
            seats = table.seats
            button = table.next_button(stacks)
            hand = play_hand(
                [players[p] for p in seats],
                [stacks[p] for p in seats],
                button,
                small,
                big,
                deal_deck(seed, f"{rounds}.{t}"),
                hand_id=hands + 1,
            )
            hands += 1
            for k, p in enumerate(seats):
                before[p] = stacks[p]
                stacks[p] = hand.seats[k].stack
            table.button_pos, table.button_player = button, seats[button]
            table.big_blind = seats[big_blind_of(hand)]
            busted += [p for p in seats if stacks[p] == 0]
            if hand_log:
                hand_log.write(hand, **(log_extra or {}), table=t + 1)
        rounds += 1
        if busted:
            assign_places(places, busted, sum(1 for s in stacks if s > 0) + 1, lambda p: before[p])
            tables = _rebalance(tables, stacks, table_size, rng)
    remaining = [p for p in range(n) if stacks[p] > 0]
    assign_places(places, remaining, 1, lambda p: stacks[p])
    return {"names": names, "places": places, "stacks": stacks, "hands": hands, "rounds": rounds, "finished": len(remaining) == 1}


def play_sng(players: Sequence[BotPlayer], seed: object, *, max_hands: int | None = None, **options) -> dict:
    """A tournament at a single table, played to the end unless max_hands is given."""
    return play_tournament(players, seed, table_size=len(players), max_hands=max_hands, **options)


def big_blind_of(hand: Hand) -> int:
    """The seat that posted the big blind in `hand`."""
    return next(event["seat"] for event in hand.history if event["action"] == "big_blind")


def next_button(stacks: Sequence[int], button: int, last_big_blind: int | None = None) -> int:
    """Where the button goes for the next hand of a tournament: the next player
    still in, except that heads-up the player who just posted the big blind takes
    the button (and so the small blind), so nobody posts the big blind twice."""
    alive = [i for i, s in enumerate(stacks) if s > 0]
    if len(alive) == 2 and last_big_blind in alive:
        return last_big_blind
    button = (button % len(stacks) + 1) % len(stacks)
    while stacks[button] == 0:
        button = (button + 1) % len(stacks)
    return button


def assign_places(places: list[float], seats: list[int], first_place: int, key: Callable[[int], int]) -> None:
    """Give `seats` the places from first_place down, by key (higher is better); ties share places."""
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
