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
import random
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
from .equity import showdown_odds
from .evaluator import evaluate, hand_name
from .loader import bot_info
from .match import HandLog, assign_places, big_blind_of, blind_levels, deal_deck, next_button, play_hand
from .players import BotPlayer, LocalBot

SPEEDS = {"slow": 1.6, "normal": 0.8, "fast": 0.3, "turbo": 0.05, "instant": 0.0}  # seconds per bot decision
MAX_SEATS = 10
MAX_COMPARE = 8


@dataclass
class SessionConfig:
    mode: str = "watch"  # "watch": bots only; "play": you take seat 0
    # "tournament": chips carry over, last one standing (or most chips at the hand limit),
    # then the next tournament; "cash": everyone back to `stack` every hand
    format: str = "tournament"
    bots: list[str] = field(default_factory=list)  # bots at the table, by name
    compare: list[str] = field(default_factory=list)  # bots to replay your seat with (play mode)
    player_name: str = "You"
    stack: int = 10_000
    small_blind: int = 50
    big_blind: int = 100
    hands_per_level: int = 10  # tournament: how often the blinds go up
    max_hands: int = 100  # tournament: after this many hands it's decided on chips (0 plays to the end)
    speed: str = "normal"
    pause_after_hand: float | None = None  # seconds to show each result (None: depends on mode and speed)

    @classmethod
    def from_dict(cls, data: dict) -> SessionConfig:
        """Build from untrusted JSON, converting types; unknown keys are ignored."""
        known = {f.name for f in fields(cls)}
        values = {k: v for k, v in data.items() if k in known}
        try:
            for key in ("stack", "small_blind", "big_blind", "hands_per_level", "max_hands"):
                if key in values:
                    values[key] = int(values[key] or 0)
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
        if self.format == "sng":  # the old name for a tournament
            self.format = "tournament"
        if self.format not in ("tournament", "cash"):
            raise ValueError("format must be tournament or cash")
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
        if self.max_hands < 0:
            raise ValueError("the hand limit can't be negative (0 means no limit)")
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
    """Replays each of your hands with a bot in your seat, against its own copies of
    your opponents: same cards, same stacks, same button and blinds."""

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

    def play(self, active, stacks, button, small, big, deck, hand_id) -> int:
        """Replay one hand between the seats in `active` (table seat numbers)."""
        hand = play_hand([self.players[seat] for seat in active], stacks, button, small, big, deck, hand_id)
        self.last = hand.winnings[active.index(self.your_seat)]
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
        self.hand_no = 0  # hands played in the current tournament (or in total, in a cash game)
        self.tournament_no = 1
        self.tournament_wins = [0.0] * n  # tournaments each seat has won this session (ties split)
        self.tournaments_finished = 0
        self.hand: Hand | None = None
        self.active: list[int] = []
        self.blinds = (config.small_blind, config.big_blind)
        self.last_actions: dict[int, str] = {}
        self.result: dict | None = None
        self.seed = secrets.randbelow(2**63)
        self.shadows: list[_Shadow] = []
        self.last_comparison: dict | None = None  # the latest hand's replay results
        self._hand_closed = True  # the bots have been told how the current hand ended
        self._odds_cache: tuple | None = None
        self._odds_rng = random.Random()
        self._odds_lock = threading.Lock()
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

    def show_yours(self) -> None:
        """Show your cards when nobody made you (after winning uncalled, or folding).
        Raises ValueError if there's nothing to show right now."""
        with self._cond:
            if not self._can_show():
                raise ValueError("there are no cards of yours to show right now")
            k = self.active.index(self.your_seat)
            self.hand.show(k)
            self._note(f"{self.names[self.your_seat]} showed", "show", cards_to_strs(self.hand.seats[k].hole))
            self._publish()

    def _can_show(self) -> bool:
        hand = self.hand
        return bool(
            hand is not None
            and hand.finished
            and not self._hand_closed
            and self.your_seat in self.active
            and hand.shown(self.active.index(self.your_seat)) is None
        )

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

    def _odds(self) -> dict[int, float]:
        """Watching with every card face up: each live player's chance of winning.
        Worked out outside the lock (it can take a moment) and cached per street."""
        with self._cond:
            hand = self.hand
            if self.config.mode != "watch" or not self.show_cards or hand is None or hand.finished:
                return {}
            if self.delay < 0.25 and not self.paused:
                return {}  # too fast to read, and working them out would slow the game down
            live = [(self.active[k], list(s.hole)) for k, s in enumerate(hand.seats) if not s.folded]
            key = (self.id, hand.hand_id, tuple(hand.board), tuple(seat for seat, _ in live))
            board = list(hand.board)
        if len(live) < 2:
            return {}
        with self._odds_lock:  # several requests at once share one calculation
            cached = self._odds_cache
            if cached and cached[0] == key:
                return cached[1]
            chances = showdown_odds([hole for _, hole in live], board, samples=1000, rng=self._odds_rng)
            odds = {seat: chance for (seat, _), chance in zip(live, chances)}
            self._odds_cache = (key, odds)
            return odds

    def snapshot(self) -> dict:
        odds = self._odds()
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
                    "showed": None,  # "showdown" or "chose" once their cards were shown to the table
                    "odds": odds.get(seat),  # chance of winning, when watching with cards face up
                }
                if hand is not None and seat in self.active:
                    k = self.active.index(seat)
                    s = hand.seats[k]
                    showed = hand.shown(k) if hand.finished else None
                    row.update(
                        stack=s.stack,
                        bet=s.bet,
                        in_hand=True,
                        folded=s.folded,
                        all_in=s.all_in,
                        turn=not hand.finished and hand.to_act == k,
                        button=hand.button == k,
                        showed=showed,
                    )
                    # In play mode you see only what a player at the table would: your own
                    # cards, and others' when they're shown. Watching, you can see them all.
                    if reveal_all or seat == self.your_seat or showed:
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
                "pots": self._pots(),
                "blinds": list(self.blinds),
                "hand_limit": cfg.max_hands if cfg.format == "tournament" else 0,
                "tournament_no": self.tournament_no,
                "tournaments_finished": self.tournaments_finished,
                "seats": seats,
                "your_seat": self.your_seat,
                "you": you,
                "can_show": self._can_show(),
                "result": self.result,
                "scoreboard": self._scoreboard(),
                "comparison": self._comparison(),
                "log": list(self.log)[-150:],
                "paused": self.paused,
                "speed": self.speed,
                "show_cards": self.show_cards,
            }

    def _pots(self) -> list[dict]:
        """The main pot and any side pots, for the table: [{"label", "amount"}]."""
        hand = self.hand
        if hand is None:
            return []
        if hand.finished and self.result:
            return [{"label": p["label"], "amount": p["amount"]} for p in self.result["pots"] if p["label"] != "returned"]
        pots = hand.pot_breakdown()
        if len(pots) <= 1:
            return [{"label": "Pot", "amount": hand.pot}]
        return [{"label": "Main pot" if i == 0 else f"Side pot {i}", "amount": p["amount"]} for i, p in enumerate(pots)]

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
                row["wins"] = self.tournament_wins[seat]
            rows.append(row)
        if self.config.format == "cash":
            rows.sort(key=lambda r: r["total"], reverse=True)
        else:
            rows.sort(key=lambda r: (r["place"] is not None, r["place"] or 0, -r["stack"]))
        return {"hands": self.hands_played, "tournaments": self.tournaments_finished, "rows": rows}

    def _comparison(self) -> dict | None:
        if not self.shadows:
            return None
        big = self.config.big_blind
        you = self.your_seat
        last = self.last_comparison  # stays up until the next hand's replays are done
        rows = [
            {
                "name": self.names[you],
                "you": True,
                "total": self.totals[you],
                "hands": self.hands_played,
                "last": last["you"] if last else None,
            }
        ]
        for shadow in self.shadows:
            rows.append(
                {
                    "name": shadow.name,
                    "you": False,
                    "total": shadow.total,
                    "hands": shadow.hands,
                    "last": last["bots"].get(shadow.name) if last else None,
                    "errors": shadow.errors,
                }
            )
        for row in rows:
            row["bb_per_100"] = 100 * row["total"] / big / row["hands"] if row["hands"] else 0.0
        rows.sort(key=lambda r: r["total"], reverse=True)
        return {"hands": self.hands_played, "last_hand": last["hand"] if last else None, "rows": rows}

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
        return blind_levels(self.config.big_blind)

    def _match_info(self) -> dict:
        levels = self._levels()
        heads_up = len(self.names) == 2
        tournament = self.config.format == "tournament"
        return {
            "format": "tournament" if tournament else ("heads_up" if heads_up else "cash"),
            "players": list(self.names),
            "num_hands": (self.config.max_hands or None) if tournament else None,
            "starting_stack": self.config.stack,
            "small_blind": levels[0][0],
            "big_blind": levels[0][1],
            "blind_levels": [list(level) for level in levels],
            "hands_per_level": self.config.hands_per_level if tournament else None,
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
            limit = self.config.max_hands
            while not self._stop.is_set():
                alive = [i for i, s in enumerate(self.stacks) if s > 0]
                if self.config.format == "tournament" and (len(alive) < 2 or (limit and self.hand_no >= limit)):
                    self._finish_tournament(alive)
                    if not self._between_tournaments():
                        return
                    self._new_tournament()
                    continue
                self._play_hand()
                if not self._stop.is_set():
                    self._between_hands()
                    self._close_hand()
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
        if self.config.mode == "play":
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
        tournament = cfg.format == "tournament"
        small, big = levels[min(self.hand_no // cfg.hands_per_level, len(levels) - 1)] if tournament else levels[0]
        active = [i for i in range(n) if self.stacks[i] > 0]
        deck = deal_deck(self.seed, f"{self.tournament_no}.{self.hand_no}")
        starting = [self.stacks[i] for i in active]
        hand = Hand(starting, active.index(self.button), small, big, deck, names=[self.names[i] for i in active], hand_id=self.hand_no + 1)
        with self._cond:
            self.hand, self.active, self.blinds, self.result = hand, active, (small, big), None
            self.last_actions = {active[e["seat"]]: describe(e) for e in hand.history}
            self.status = "running"
            title = f"Tournament {self.tournament_no}, hand {hand.hand_id}" if tournament else f"Hand {hand.hand_id}"
            self._note(f"{title}: {self.names[self.button]} has the button, blinds {small:,}/{big:,}", "hand")
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

        # Like real poker: at a showdown everyone still in shows; anyone else may choose to.
        for k, seat in enumerate(active):
            player = self.players[seat]
            if player is not None and hand.shown(k) is None and player.wants_to_show(hand.result_for(k)):
                with self._cond:
                    hand.show(k)
        with self._cond:
            for k, seat in enumerate(active):
                self.stacks[seat] = hand.seats[k].stack
                self.totals[seat] += hand.winnings[k]
            self._big_blind_seat = active[big_blind_of(hand)]
            self.hands_played += 1
            self.result = self._summarize(hand, active)
            self._note_shows(hand, active)
            self._note_pots()
            if tournament:
                self._place_busted(active, starting)
            self._hand_closed = False  # you can still choose to show until the next hand
            self.status = "hand_over"
            self._publish()
        if self.shadows:
            self._compare(hand, active, small, big, deck)
        self.hand_no += 1

    def _close_hand(self) -> None:
        """Tell the bots how the hand ended (including whether you showed)."""
        hand = self.hand
        if hand is None or not hand.finished or self._hand_closed:
            return
        with self._cond:
            self._hand_closed = True
            self._publish()
        for k, seat in enumerate(self.active):
            if self.players[seat]:
                self.players[seat].hand_end(hand.result_for(k))
        if self._hand_log:
            extra = {"tournament": self.tournament_no} if self.config.format == "tournament" else {}
            self._hand_log.write(hand, **extra)

    def _note_shows(self, hand: Hand, active: list[int]) -> None:
        order = [(hand.button + j) % hand.n for j in range(1, hand.n + 1)]
        for k in order:
            how = hand.shown(k)
            if how:
                cards = cards_to_strs(hand.seats[k].hole)
                detail = f" ({_PHRASES[hand_name(evaluate(hand.seats[k].hole + hand.board))]})" if how == "showdown" else " (didn't have to)"
                self._note(f"{self.names[active[k]]} showed{detail}", "show", cards)

    def _note_pots(self) -> None:
        """Who won what: the pot, or the main pot and each side pot."""
        result = self.result
        pots = [p for p in result["pots"] if p["label"] != "returned"]
        for pot in result["pots"]:
            names = [self.names[seat] for seat in pot["winners"]]
            if not names:
                continue
            who = " and ".join(names)
            if pot["label"] == "returned":
                self._note(f"{pot['amount']:,} nobody called goes back to {who}", "returned")
                continue
            hand_names = {result["shown"].get(seat, {}).get("hand_name") for seat in pot["winners"]} - {None}
            with_hand = f" with {_PHRASES[hand_names.pop()]}" if len(hand_names) == 1 else ""
            if len(pots) == 1:
                what = f"{pot['amount']:,}"
            elif pot["label"] == "Main pot":
                what = f"the main pot ({pot['amount']:,})"
            else:
                what = f"{pot['label'].lower()} ({pot['amount']:,})"
            if len(names) > 1:
                self._note(f"{who} split {what}{with_hand}", "win")
            else:
                verb = "win" if names[0].lower() == "you" else "wins"
                self._note(f"{who} {verb} {what}{with_hand}", "win")

    def _compare(self, hand: Hand, active: list[int], small: int, big: int, deck: list[int]) -> None:
        """Replay the hand you just played with each comparison bot in your seat:
        the same cards, stacks, button and blinds (so it works in tournaments too)."""
        if self.your_seat not in active:
            return  # you're out of this tournament
        stacks = [s.start_stack for s in hand.seats]
        bots = {}
        for shadow in self.shadows:
            bots[shadow.name] = shadow.play(active, stacks, hand.button, small, big, deck, hand.hand_id)
            if self._stop.is_set():
                return
        with self._cond:
            you = hand.winnings[active.index(self.your_seat)]
            self.last_comparison = {"hand": hand.hand_id, "you": you, "bots": bots}
            self.result["comparison"] = self.last_comparison
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
        pots, side = [], 0
        for pot in hand.pots:
            if pot["uncalled"]:
                label = "returned"
            elif not any(p["label"] == "Main pot" for p in pots):
                label = "Main pot"
            else:
                side += 1
                label = f"Side pot {side}"
            pots.append({"label": label, "amount": pot["amount"], "winners": [active[w] for w in pot["winners"]]})
        return {
            "hand": hand.hand_id,
            "showdown": hand.showdown,
            "winners": winners,
            "shown": shown,
            "pots": pots,
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

    def _finish_tournament(self, alive: list[int]) -> None:
        with self._cond:
            assign_places(self.places, alive, 1, lambda seat: self.stacks[seat])
            if len(alive) > 1:
                self._note(f"That's {self.hand_no} hands: the rest are ranked by chips", "hand")
            winners = [seat for seat in alive if self.places[seat] == 1]
            for seat in winners:  # a tie for first splits the win
                self.tournament_wins[seat] += 1 / len(winners)
            if len(winners) == 1:
                winner = "You won" if winners[0] == self.your_seat else f"{self.names[winners[0]]} wins"
                self._note(f"{winner} tournament {self.tournament_no}!", "win")
            else:
                self._note(f"{' and '.join(self.names[s] for s in winners)} share tournament {self.tournament_no}", "win")
            self.tournaments_finished += 1
            self.status = "finished"
            self._publish()

    def _between_tournaments(self) -> bool:
        """Show the result. Watching, the next tournament starts after a few seconds;
        playing, it waits for you to press Next tournament. False if the table closed."""
        watching = self.config.mode == "watch"
        pause = 5.0 if self.config.pause_after_hand is None else max(1.0, self.config.pause_after_hand)
        deadline = time.monotonic() + pause
        with self._cond:
            self._skip_wait = False
            while not self._stop.is_set():
                if self._skip_wait:
                    break
                if watching and not self.paused:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._cond.wait(remaining)
                else:
                    self._cond.wait(0.25)
            self._skip_wait = False
        return not self._stop.is_set()

    def _new_tournament(self) -> None:
        """Everyone back to the starting chips for the next tournament."""
        info = self._match_info()
        for seat, player in enumerate(self.players):
            if player:
                player.match_start(dict(info, your_name=player.name, your_seat=seat))
        with self._cond:
            n = len(self.names)
            self.tournament_no += 1
            self.stacks = [self.config.stack] * n
            self.places = [None] * n
            self.hand_no = 0
            self.button = secrets.randbelow(n)
            self._big_blind_seat = None
            self.hand, self.result, self.last_actions = None, None, {}
            self._note(f"Tournament {self.tournament_no}: everyone starts again with {self.config.stack:,} chips", "hand")
            self.status = "running"
            self._publish()

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
