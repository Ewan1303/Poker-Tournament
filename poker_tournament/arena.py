"""Games to watch or play in the browser: bots, and you, around one table.

A TableSession plays hands in a background thread at a pace people can
follow. In play mode seat 0 is yours and the session waits for your
decisions. In a cash game it also replays every deal with each comparison
bot in your seat, against its own copies of your opponents, so you can see
how you compare with exactly the same cards.

webapp.py shows sessions in the browser. You can also drive one directly:

    session = TableSession(SessionConfig(mode="play", bots=["equity_bot"]), available)
    session.start()
    snapshot = session.wait(since=-1)
    session.submit_action({"type": "call"})
"""

from __future__ import annotations

import itertools
import queue
import secrets
import threading
import time
import traceback
from collections import deque
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from .api import normalize_action
from .cards import cards_to_strs
from .engine import STREETS, Hand
from .evaluator import evaluate, hand_name
from .loader import bot_info
from .match import SNG_LEVELS, HandLog, assign_places, big_blind_of, deal_deck, next_button, play_hand
from .players import BotPlayer, LocalBot

SPEEDS = {"slow": 1.6, "normal": 0.8, "fast": 0.3, "turbo": 0.05, "instant": 0.0}  # seconds per bot decision
MAX_SEATS = 10
MAX_COMPARE = 8


@dataclass
class SessionConfig:
    mode: str = "watch"  # "watch": bots only; "play": you take seat 0
    format: str = "cash"  # "cash": everyone back to `stack` every hand; "sng": last one standing
    bots: list[str] = field(default_factory=list)  # bots at the table, by name
    compare: list[str] = field(default_factory=list)  # bots to replay your seat with (play mode, cash game)
    player_name: str = "You"
    stack: int = 10_000
    small_blind: int = 50
    big_blind: int = 100
    hands_per_level: int = 10  # sit-and-go: how often the blinds go up
    speed: str = "normal"
    pause_after_hand: float | None = None  # seconds to show each result (None: depends on mode and speed)

    @classmethod
    def from_dict(cls, data: dict) -> SessionConfig:
        """Build from untrusted JSON, converting types; unknown keys are ignored."""
        known = {f.name for f in fields(cls)}
        values = {k: v for k, v in data.items() if k in known}
        try:
            for key in ("stack", "small_blind", "big_blind", "hands_per_level"):
                if key in values:
                    values[key] = int(values[key])
            for key in ("bots", "compare"):
                if key in values:
                    values[key] = [str(v) for v in values[key]]
            if values.get("pause_after_hand") is not None:
                values["pause_after_hand"] = float(values["pause_after_hand"])
        except (TypeError, ValueError):
            raise ValueError("those settings aren't valid numbers") from None
        return cls(**values)

    def validate(self, available: dict[str, Path]) -> None:
        if self.mode not in ("watch", "play"):
            raise ValueError("mode must be watch or play")
        if self.format not in ("cash", "sng"):
            raise ValueError("format must be cash or sng")
        seats = len(self.bots) + (self.mode == "play")
        if self.mode == "play" and not self.bots:
            raise ValueError("pick at least one bot to play against")
        if seats < 2:
            raise ValueError("pick at least two bots")
        if seats > MAX_SEATS:
            raise ValueError(f"a table has at most {MAX_SEATS} seats")
        unknown = sorted({b for b in self.bots + self.compare if b not in available})
        if unknown:
            raise ValueError(f"unknown bot: {', '.join(unknown)}")
        if len(self.compare) > MAX_COMPARE:
            raise ValueError(f"compare with at most {MAX_COMPARE} bots")
        if not 0 < self.small_blind <= self.big_blind or self.stack < self.big_blind:
            raise ValueError("need 0 < small blind <= big blind <= starting chips")
        if self.hands_per_level < 1:
            raise ValueError("blinds must go up after at least one hand")
        if self.speed not in SPEEDS:
            raise ValueError(f"speed must be one of {', '.join(SPEEDS)}")
        if not self.player_name.strip() or len(self.player_name) > 24:
            raise ValueError("your name needs 1 to 24 characters")


def describe(event: dict) -> str:
    """'Raise to 300', 'Call 100 (all in)' and so on."""
    kind, amount, to = event["action"], event["amount"], event["to"]
    text = {
        "small_blind": f"Small blind {amount:,}",
        "big_blind": f"Big blind {amount:,}",
        "fold": "Fold",
        "check": "Check",
        "call": f"Call {amount:,}",
        "bet": f"Bet {to:,}",
        "raise": f"Raise to {to:,}",
    }[kind]
    if event["all_in"] and kind not in ("fold", "check"):
        text += " (all in)"
    return text


class _Shadow:
    """Replays your deals with a bot in your seat, against its own copies of your opponents."""

    def __init__(self, name: str, path: Path, seat_paths: list[Path | None], names: list[str], your_seat: int):
        self.name = name
        self.your_seat = your_seat
        self.players = [LocalBot(path if seat == your_seat else p, names[seat]) for seat, p in enumerate(seat_paths)]
        self.total = 0
        self.hands = 0
        self.last = 0

    def start(self, info: dict) -> bool:
        for seat, player in enumerate(self.players):
            player.start()
            if player.dead:
                return False
            player.match_start(dict(info, your_name=player.name, your_seat=seat))
        return True

    def play(self, stacks, button, small, big, deck, hand_id) -> int:
        hand = play_hand(self.players, stacks, button, small, big, deck, hand_id)
        self.last = hand.winnings[self.your_seat]
        self.total += self.last
        self.hands += 1
        return self.last

    @property
    def errors(self) -> int:
        return self.players[self.your_seat].stats.failures


class TableSession:
    """One table in the browser: bots (and maybe you) playing hand after hand."""

    def __init__(self, config: SessionConfig, available: dict[str, Path], hand_log: str | Path | None = None):
        config.player_name = config.player_name.strip()
        config.validate(available)
        self.config = config
        self.available = available
        self.id = secrets.token_hex(4)
        self._cond = threading.Condition(threading.RLock())
        self._stop = threading.Event()
        self._human_actions: queue.Queue = queue.Queue()
        self.version = 0
        self.status = "loading"
        self.error: str | None = None
        self.paused = False
        self.speed = config.speed
        self.delay = SPEEDS[config.speed]
        self.show_cards = config.mode == "watch"
        self._steps = 0
        self._skip_wait = False
        self._pending: dict | None = None  # what you can see while it's your turn
        self._decision = 0
        self.log: deque[dict] = deque(maxlen=400)
        self._log_ids = itertools.count(1)

        play = config.mode == "play"
        self.your_seat = 0 if play else None
        self.bot_names: list[str | None] = ([None] if play else []) + list(config.bots)
        self.profiles = {spec: bot_info(available[spec]) for spec in set(config.bots) | set(config.compare)}
        self.names = self._seat_names()
        n = len(self.names)
        self.players: list[BotPlayer | None] = [None] * n
        self.stacks = [config.stack] * n
        self.totals = [0] * n
        self.places: list[float | None] = [None] * n
        self.hands_played = 0
        self.button = secrets.randbelow(n)
        self._big_blind_seat: int | None = None
        self.hand_no = 0
        self.hand: Hand | None = None
        self.active: list[int] = []
        self.blinds = (config.small_blind, config.big_blind)
        self.last_actions: dict[int, str] = {}
        self.result: dict | None = None
        self.seed = secrets.randbelow(2**63)
        self.shadows: list[_Shadow] = []
        self._hand_log = HandLog(hand_log, full_info=True) if hand_log else None
        self._thread = threading.Thread(target=self._run, name="poker-table", daemon=True)

    # -- controls (called from web requests) --------------------------------

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=10)

    def submit_action(self, action) -> None:
        """Your decision. Raises ValueError if it isn't your turn or the action makes no sense."""
        normalized = normalize_action(action)
        with self._cond:
            if self._pending is None:
                raise ValueError("it isn't your turn")
            self._pending = None
            self.status = "running"
            self._human_actions.put(normalized)
            self._publish()

    def control(self, paused=None, speed=None, step=False, next_hand=False, show_cards=None) -> None:
        with self._cond:
            if paused is not None:
                self.paused = bool(paused)
            if speed is not None:
                if speed not in SPEEDS:
                    raise ValueError(f"speed must be one of {', '.join(SPEEDS)}")
                self.speed, self.delay = speed, SPEEDS[speed]
            if step:
                self._steps += 1
            if next_hand:
                self._skip_wait = True
            if show_cards is not None and self.config.mode == "watch":
                self.show_cards = bool(show_cards)
            self._publish()

    def wait(self, since: int, timeout: float = 20.0) -> dict:
        """The next snapshot newer than `since` (or the current one after `timeout` seconds)."""
        with self._cond:
            self._cond.wait_for(lambda: self.version > since or self._stop.is_set(), timeout)
            return self.snapshot()

    # -- what the browser sees ---------------------------------------------

    def snapshot(self) -> dict:
        with self._cond:
            cfg = self.config
            hand = self.hand
            reveal_all = cfg.mode == "watch" and self.show_cards
            shown = (self.result or {}).get("shown", {})
            collected = {w["seat"]: w["amount"] for w in (self.result or {}).get("winners", [])}
            seats = []
            for seat, name in enumerate(self.names):
                player = self.players[seat]
                profile = self.profiles.get(self.bot_names[seat]) or {}
                row = {
                    "seat": seat,
                    "name": name,
                    "bot": self.bot_names[seat],
                    "style": profile.get("style", ""),
                    "description": profile.get("description", ""),
                    "you": seat == self.your_seat,
                    "stack": self.stacks[seat],
                    "bet": 0,
                    "total": self.totals[seat],
                    "in_hand": False,
                    "folded": False,
                    "all_in": False,
                    "cards": None,
                    "turn": False,
                    "button": False,
                    "action": self.last_actions.get(seat),
                    "place": self.places[seat],
                    "errors": player.stats.failures if player else 0,
                    "collected": collected.get(seat, 0),
                    "hand_name": shown.get(seat, {}).get("hand_name"),
                }
                if hand is not None and seat in self.active:
                    k = self.active.index(seat)
                    s = hand.seats[k]
                    row.update(
                        stack=s.stack,
                        bet=s.bet,
                        in_hand=True,
                        folded=s.folded,
                        all_in=s.all_in,
                        turn=not hand.finished and hand.to_act == k,
                        button=hand.button == k,
                    )
                    visible = reveal_all or seat == self.your_seat or seat in shown
                    if visible:
                        row["cards"] = cards_to_strs(s.hole)
                    elif not s.folded:
                        row["cards"] = ["back", "back"]
                seats.append(row)
            you = None
            if self._pending is not None:
                obs = self._pending
                you = {key: obs[key] for key in ("to_call", "min_raise_to", "max_raise_to", "can_raise", "legal_actions", "pot", "current_bet", "bet", "stack", "big_blind")}
                you["decision"] = self._decision
            return {
                "session": self.id,
                "version": self.version,
                "status": self.status,
                "error": self.error,
                "mode": cfg.mode,
                "format": cfg.format,
                "config": asdict(cfg),
                "hand_no": hand.hand_id if hand else 0,
                "street": STREETS[hand.street] if hand else None,
                "board": cards_to_strs(hand.board) if hand else [],
                "pot": hand.pot if hand else 0,
                "blinds": list(self.blinds),
                "seats": seats,
                "your_seat": self.your_seat,
                "you": you,
                "result": self.result,
                "scoreboard": self._scoreboard(),
                "comparison": self._comparison(),
                "log": list(self.log)[-150:],
                "paused": self.paused,
                "speed": self.speed,
                "show_cards": self.show_cards,
            }

    def _scoreboard(self) -> dict:
        big = self.config.big_blind
        rows = []
        for seat, name in enumerate(self.names):
            row = {"seat": seat, "name": name, "you": seat == self.your_seat}
            if self.config.format == "cash":
                row["total"] = self.totals[seat]
                row["bb_per_100"] = 100 * self.totals[seat] / big / self.hands_played if self.hands_played else 0.0
            else:
                row["stack"] = self.stacks[seat]
                row["place"] = self.places[seat]
            rows.append(row)
        if self.config.format == "cash":
            rows.sort(key=lambda r: r["total"], reverse=True)
        else:
            rows.sort(key=lambda r: (r["place"] is not None, r["place"] or 0, -r["stack"]))
        return {"hands": self.hands_played, "rows": rows}

    def _comparison(self) -> dict | None:
        if not self.shadows:
            return None
        big = self.config.big_blind
        hands = self.hands_played
        you = self.your_seat
        last = self.result.get("comparison") if self.result else None
        rows = [
            {
                "name": self.names[you],
                "you": True,
                "total": self.totals[you],
                "last": last["you"] if last else None,
            }
        ]
        for shadow in self.shadows:
            rows.append(
                {
                    "name": shadow.name,
                    "you": False,
                    "total": shadow.total,
                    "last": last["bots"].get(shadow.name) if last else None,
                    "errors": shadow.errors,
                }
            )
        for row in rows:
            row["bb_per_100"] = 100 * row["total"] / big / hands if hands else 0.0
        rows.sort(key=lambda r: r["total"], reverse=True)
        return {"hands": hands, "rows": rows}

    # -- the game -------------------------------------------------------------

    def _seat_names(self) -> list[str]:
        """Display names: yours, then each bot's NAME (Randy, Callie...), numbered if repeated."""
        names: list[str] = []
        for spec in self.bot_names:
            base = self.config.player_name if spec is None else self.profiles[spec]["name"]
            name, copy = base, 2
            while name.lower() in (n.lower() for n in names):
                name = f"{base} {copy}"
                copy += 1
            names.append(name)
        return names

    def _levels(self) -> list[tuple[int, int]]:
        if self.config.format == "cash":
            return [(self.config.small_blind, self.config.big_blind)]
        factor = self.config.big_blind / 100
        return [(max(1, round(s * factor)), max(1, round(b * factor))) for s, b in SNG_LEVELS]

    def _match_info(self) -> dict:
        levels = self._levels()
        heads_up = len(self.names) == 2
        return {
            "format": "sng" if self.config.format == "sng" else ("heads_up" if heads_up else "cash"),
            "players": list(self.names),
            "num_hands": None,
            "starting_stack": self.config.stack,
            "small_blind": levels[0][0],
            "big_blind": levels[0][1],
            "blind_levels": [list(level) for level in levels],
            "hands_per_level": self.config.hands_per_level if self.config.format == "sng" else None,
            "act_timeout": None,
        }

    def _publish(self) -> None:
        """Call with the lock held after changing anything the browser shows."""
        self.version += 1
        self._cond.notify_all()

    def _note(self, text: str, kind: str = "action", cards: list[str] | None = None) -> None:
        entry = {"id": next(self._log_ids), "hand": self.hand.hand_id if self.hand else 0, "kind": kind, "text": text}
        if cards:
            entry["cards"] = cards
        self.log.append(entry)

    def _run(self) -> None:
        try:
            self._load()
            while not self._stop.is_set():
                alive = [i for i, s in enumerate(self.stacks) if s > 0]
                if self.config.format == "sng" and len(alive) < 2:
                    self._finish_sng(alive)
                    return
                self._play_hand()
                if not self._stop.is_set():
                    self._between_hands()
        except Exception:
            with self._cond:
                self.status = "error"
                self.error = traceback.format_exc()
                self._publish()
        finally:
            for player in self.players:
                if player:
                    player.close()
            for shadow in self.shadows:
                for player in shadow.players:
                    player.close()
            if self._hand_log:
                self._hand_log.close()

    def _load(self) -> None:
        info = self._match_info()
        seat_paths = [self.available[spec] if spec else None for spec in self.bot_names]
        for seat, path in enumerate(seat_paths):
            if path is None:
                continue
            player = LocalBot(path, self.names[seat])
            player.start()
            if player.dead:
                raise RuntimeError(f"{self.names[seat]} failed to load:\n" + "\n".join(player.stats.errors))
            player.match_start(dict(info, your_name=player.name, your_seat=seat))
            self.players[seat] = player
        if self.config.mode == "play" and self.config.format == "cash":
            for spec in self.config.compare:
                name = self.profiles[spec]["name"]
                shadow = _Shadow(name, self.available[spec], seat_paths, self.names, self.your_seat)
                if shadow.start(info):
                    self.shadows.append(shadow)
                else:
                    with self._cond:
                        self._note(f"{name} couldn't be loaded for the comparison", "error")
        with self._cond:
            self.status = "running"
            self._publish()

    def _play_hand(self) -> None:
        cfg = self.config
        n = len(self.names)
        if cfg.format == "cash":
            self.stacks = [cfg.stack] * n
        if self.hand_no:
            self.button = next_button(self.stacks, self.button, self._big_blind_seat)
        while self.stacks[self.button] == 0:
            self.button = (self.button + 1) % n
        levels = self._levels()
        small, big = levels[min(self.hand_no // cfg.hands_per_level, len(levels) - 1)] if cfg.format == "sng" else levels[0]
        active = [i for i in range(n) if self.stacks[i] > 0]
        deck = deal_deck(self.seed, self.hand_no)
        starting = [self.stacks[i] for i in active]
        hand = Hand(starting, active.index(self.button), small, big, deck, names=[self.names[i] for i in active], hand_id=self.hand_no + 1)
        with self._cond:
            self.hand, self.active, self.blinds, self.result = hand, active, (small, big), None
            self.last_actions = {active[e["seat"]]: describe(e) for e in hand.history}
            self.status = "running"
            self._note(f"Hand {hand.hand_id}: {self.names[self.button]} has the button, blinds {small:,}/{big:,}", "hand")
            self._note_board(0)
            self._publish()

        while not hand.finished:
            k = hand.to_act
            seat = active[k]
            obs = hand.observation(k)
            if seat == self.your_seat:
                action = self._wait_for_you(obs)
            else:
                action = self.players[seat].act(obs) if self._pace() else None
            if self._stop.is_set():
                return
            if action is None:
                action = {"type": "check" if obs["to_call"] == 0 else "fold"}
            with self._cond:
                dealt = len(hand.board)
                applied = hand.apply(action)
                self.last_actions[seat] = describe(applied)
                self._note(f"{self.names[seat]}: {describe(applied)}")
                if len(hand.board) > dealt:
                    self.last_actions = {s: a for s, a in self.last_actions.items() if a == "Fold" or "all in" in a}
                    self._note_board(dealt)
                self._publish()

        for k, seat in enumerate(active):
            if self.players[seat]:
                self.players[seat].hand_end(hand.result_for(k))
        with self._cond:
            for k, seat in enumerate(active):
                self.stacks[seat] = hand.seats[k].stack
                if cfg.format == "cash":
                    self.totals[seat] += hand.winnings[k]
            self._big_blind_seat = active[big_blind_of(hand)]
            self.hands_played += 1
            self.result = self._summarize(hand, active)
            for winner in self.result["winners"]:
                with_hand = f" with {_PHRASES[winner['hand_name']]}" if winner["hand_name"] else ""
                verb = "win" if winner["name"].lower() == "you" else "wins"
                self._note(f"{winner['name']} {verb} {winner['amount']:,}{with_hand}", "win")
            if cfg.format == "sng":
                self._place_busted(active, starting)
            self.status = "hand_over"
            self._publish()
        if self._hand_log:
            self._hand_log.write(hand)
        if self.shadows:
            self._compare(hand, active, small, big, deck)
        self.hand_no += 1

    def _compare(self, hand: Hand, active: list[int], small: int, big: int, deck: list[int]) -> None:
        stacks = [self.config.stack] * len(active)
        bots = {}
        for shadow in self.shadows:
            bots[shadow.name] = shadow.play(stacks, hand.button, small, big, deck, hand.hand_id)
            if self._stop.is_set():
                return
        with self._cond:
            you = hand.winnings[active.index(self.your_seat)]
            self.result["comparison"] = {"you": you, "bots": bots}
            self._publish()

    def _note_board(self, dealt_before: int) -> None:
        board = cards_to_strs(self.hand.board)
        for size, street in ((3, "Flop"), (4, "Turn"), (5, "River")):
            if dealt_before < size <= len(board):
                self._note(street, "board", board[:size] if size == 3 else board[size - 1 : size])

    def _summarize(self, hand: Hand, active: list[int]) -> dict:
        contributions = [s.contributed for s in hand.seats]
        winners = []
        shown = {}
        for k, s in enumerate(hand.seats):
            seat = active[k]
            if hand.showdown and not s.folded:
                shown[seat] = {"cards": cards_to_strs(s.hole), "hand_name": hand_name(evaluate(s.hole + hand.board))}
            others = max((c for j, c in enumerate(contributions) if j != k), default=0)
            collected = hand.winnings[k] + s.contributed - max(0, s.contributed - others)  # not counting an uncalled bet
            if collected > 0:
                winners.append(
                    {"seat": seat, "name": self.names[seat], "amount": collected, "hand_name": shown.get(seat, {}).get("hand_name")}
                )
        return {
            "hand": hand.hand_id,
            "showdown": hand.showdown,
            "winners": winners,
            "shown": shown,
            "nets": {active[k]: hand.winnings[k] for k in range(len(active))},
            "comparison": None,
        }

    def _place_busted(self, active: list[int], starting: list[int]) -> None:
        busted = [seat for seat in active if self.stacks[seat] == 0]
        if not busted:
            return
        alive = sum(1 for s in self.stacks if s > 0)
        before = dict(zip(active, starting))
        assign_places(self.places, busted, alive + 1, lambda seat: before[seat])
        for seat in sorted(busted, key=lambda s: self.places[s]):
            self._note(f"{self.names[seat]} is out in {_ordinal(self.places[seat])} place", "out")

    def _finish_sng(self, alive: list[int]) -> None:
        with self._cond:
            assign_places(self.places, alive, 1, lambda seat: self.stacks[seat])
            if alive:
                winner = "You won" if alive[0] == self.your_seat else f"{self.names[alive[0]]} wins"
                self._note(f"{winner} the sit-and-go!", "win")
            self.status = "finished"
            self._publish()
        self._stop.wait()

    def _pace(self) -> bool:
        """Wait before a bot acts: the speed delay, or while paused until you step."""
        deadline = time.monotonic() + self.delay
        with self._cond:
            while not self._stop.is_set():
                if self.paused:
                    if self._steps:
                        self._steps -= 1
                        return True
                    self._cond.wait(0.25)
                    deadline = time.monotonic() + self.delay
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return True
                self._cond.wait(remaining)
        return False

    def _wait_for_you(self, obs: dict) -> dict | None:
        while not self._human_actions.empty():
            self._human_actions.get_nowait()
        with self._cond:
            self._decision += 1
            self._pending = obs
            self.status = "your_turn"
            self._publish()
        while not self._stop.is_set():
            try:
                return self._human_actions.get(timeout=0.25)
            except queue.Empty:
                continue
        return None

    def _between_hands(self) -> None:
        """Leave the result on screen for a moment (or until 'next hand')."""
        if self.config.pause_after_hand is not None:
            pause = self.config.pause_after_hand
        elif self.config.mode == "play":
            pause = 2.5
        else:
            pause = min(2.5, self.delay * 3)
        deadline = time.monotonic() + pause
        with self._cond:
            while not self._stop.is_set() and not self._skip_wait:
                if self.paused:
                    if self._steps:
                        self._steps -= 1
                        break
                    self._cond.wait(0.25)
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            self._skip_wait = False


_PHRASES = {
    "High card": "high card",
    "Pair": "a pair",
    "Two pair": "two pair",
    "Three of a kind": "three of a kind",
    "Straight": "a straight",
    "Flush": "a flush",
    "Full house": "a full house",
    "Four of a kind": "four of a kind",
    "Straight flush": "a straight flush",
}


def _ordinal(place: float) -> str:
    if place != int(place):
        return f"joint {place:g}"
    place = int(place)
    suffix = "th" if 10 <= place % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(place % 10, "th")
    return f"{place}{suffix}"
