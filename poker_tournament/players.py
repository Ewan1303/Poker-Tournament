"""Seats at the table for bots.

ProcessBot runs a bot in its own process with time limits (what the tournament
uses). LocalBot runs it inside this process: faster and easier to debug, but
with no isolation, so use it only for your own testing.

Either way a bot that crashes, raises or runs out of time gets the default
action (check if free, otherwise fold) and a failure on its record; after
max_failures it is cut off and auto-folds for the rest of the match.
"""

from __future__ import annotations

import atexit
import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .api import HandResult, MatchInfo, Observation, PokerBot, normalize_action
from .loader import FRAMEWORK_ROOT, load_bot_class

_EOF = object()
_live_processes: set[subprocess.Popen] = set()


@atexit.register
def _kill_leftovers() -> None:
    for proc in list(_live_processes):
        if proc.poll() is None:
            proc.kill()


@dataclass
class BotStats:
    decisions: int = 0
    think_time: float = 0.0  # seconds, summed over decisions
    max_think: float = 0.0
    failures: int = 0  # errors + timeouts + invalid replies
    timeouts: int = 0
    corrections: int = 0  # illegal actions the engine had to change
    startup_time: float = 0.0
    cut_off: bool = False
    errors: list[str] = field(default_factory=list)  # the first few error messages
    correction_examples: list[dict] = field(default_factory=list)

    def record_decision(self, seconds: float) -> None:
        self.decisions += 1
        self.think_time += seconds
        self.max_think = max(self.max_think, seconds)

    def add(self, other: BotStats) -> None:
        self.decisions += other.decisions
        self.think_time += other.think_time
        self.max_think = max(self.max_think, other.max_think)
        self.failures += other.failures
        self.timeouts += other.timeouts
        self.corrections += other.corrections
        self.startup_time = max(self.startup_time, other.startup_time)
        self.cut_off = self.cut_off or other.cut_off
        for error in other.errors:
            if len(self.errors) < 5 and error not in self.errors:
                self.errors.append(error)
        for example in other.correction_examples:
            if len(self.correction_examples) < 3:
                self.correction_examples.append(example)

    @property
    def avg_think_ms(self) -> float:
        return 1000 * self.think_time / self.decisions if self.decisions else 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> BotStats:
        return cls(**d)


class BotPlayer:
    """Common bookkeeping for both kinds of player."""

    def __init__(self, name: str, max_failures: int = 25):
        self.name = name
        self.max_failures = max_failures
        self.stats = BotStats()
        self.dead = False

    def start(self) -> None: ...

    def match_start(self, info: dict) -> None: ...

    def act(self, obs: dict) -> dict | None:
        """The bot's action, or None if it failed (the caller then checks or folds)."""
        raise NotImplementedError

    def hand_end(self, result: dict) -> None: ...

    def close(self) -> None: ...

    def __enter__(self) -> BotPlayer:
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _fail(self, message: str) -> None:
        self.stats.failures += 1
        self._note(message)
        if self.stats.failures >= self.max_failures and not self.dead:
            self._cut_off(f"cut off after {self.stats.failures} errors/timeouts")

    def _note(self, message: str) -> None:
        message = message.strip()[-3000:]
        if len(self.stats.errors) < 5 and message not in self.stats.errors:
            self.stats.errors.append(message)

    def _cut_off(self, reason: str) -> None:
        self.dead = True
        self.stats.cut_off = True
        self._note(reason)


class ProcessBot(BotPlayer):
    """A bot running in its own Python process, with time limits.

    Whatever the bot prints is kept in memory (the most recent ~500k characters)
    and available as .output once it has finished.
    """

    def __init__(
        self,
        path: str | Path,
        name: str | None = None,
        *,
        act_timeout: float = 1.0,
        startup_timeout: float = 60.0,
        max_failures: int = 25,
        max_output_chars: int = 500_000,
    ):
        super().__init__(name or Path(path).name, max_failures)
        self.path = Path(path).resolve()
        self.act_timeout = act_timeout
        self.startup_timeout = startup_timeout
        self.max_output_chars = max_output_chars
        self._proc: subprocess.Popen | None = None
        self._inbox: queue.Queue = queue.Queue()
        self._next_id = 0
        self._output: deque[str] = deque()
        self._output_chars = 0
        self._output_dropped = False
        self._drain: threading.Thread | None = None

    @property
    def output(self) -> str:
        """Everything the bot printed (only the latest part if there was a lot)."""
        text = "".join(self._output)
        return "[earlier output dropped]\n" + text if self._output_dropped else text

    def start(self) -> None:
        started = time.perf_counter()
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(FRAMEWORK_ROOT), env.get("PYTHONPATH")]))
        env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        for var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
            env.setdefault(var, "1")  # one core per bot, so parallel matches stay fair
        try:
            self._proc = subprocess.Popen(
                [sys.executable, "-P", "-m", "poker_tournament.runner", str(self.path)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self.path,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as exc:
            self._cut_off(f"could not start a process for the bot: {exc}")
            return
        _live_processes.add(self._proc)
        threading.Thread(target=self._pump, daemon=True).start()
        self._drain = threading.Thread(target=self._collect_output, daemon=True)
        self._drain.start()
        status, message = self._receive(None, self.startup_timeout)
        self.stats.startup_time = time.perf_counter() - started
        if status == "timeout":
            self._cut_off(f"did not finish starting within {self.startup_timeout:g}s")
        elif status == "eof":
            self._cut_off("the bot process exited while starting (see its log)")
        elif message.get("type") == "error":
            self._cut_off("bot.py failed to load:\n" + message.get("error", ""))
        if self.dead:
            self._kill()

    def match_start(self, info: dict) -> None:
        self._request("match_start", "info", info, self.startup_timeout)

    def act(self, obs: dict) -> dict | None:
        if self.dead:
            return None
        started = time.perf_counter()
        reply = self._request("act", "obs", obs, self.act_timeout)
        self.stats.record_decision(time.perf_counter() - started)
        if reply is None:
            return None
        try:
            return normalize_action(reply.get("action"))
        except ValueError as exc:
            self._fail(f"invalid action: {exc}")
            return None

    def hand_end(self, result: dict) -> None:
        self._request("hand_end", "result", result, self.act_timeout)

    def close(self) -> None:
        proc = self._proc
        if proc is not None:
            if proc.poll() is None:
                try:
                    proc.stdin.write('{"type":"shutdown"}\n')
                    proc.stdin.flush()
                    proc.wait(timeout=3)
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    pass
            self._kill()
            if self._drain is not None:
                self._drain.join(timeout=5)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
            self._proc = None

    # -- internals -----------------------------------------------------------

    def _kill(self) -> None:
        proc = self._proc
        if proc is not None:
            if proc.poll() is None:
                proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            _live_processes.discard(proc)

    def _cut_off(self, reason: str) -> None:
        super()._cut_off(reason)
        self._kill()

    def _collect_output(self) -> None:
        try:
            for line in self._proc.stderr:
                self._output.append(line)
                self._output_chars += len(line)
                while self._output_chars > self.max_output_chars and len(self._output) > 1:
                    self._output_chars -= len(self._output.popleft())
                    self._output_dropped = True
        except (OSError, ValueError):
            pass

    def _pump(self) -> None:
        proc = self._proc
        try:
            for line in proc.stdout:
                line = line.strip()
                if line:
                    try:
                        self._inbox.put(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        except (OSError, ValueError):
            pass
        self._inbox.put(_EOF)

    def _receive(self, message_id: int | None, timeout: float) -> tuple[str, dict | None]:
        deadline = time.perf_counter() + timeout
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return "timeout", None
            try:
                message = self._inbox.get(timeout=remaining)
            except queue.Empty:
                return "timeout", None
            if message is _EOF:
                return "eof", None
            if message_id is None or message.get("id") == message_id:
                return "ok", message
            # Otherwise it is a late reply to a request that already timed out.

    def _request(self, kind: str, key: str, payload: dict, timeout: float) -> dict | None:
        if self.dead or self._proc is None:
            return None
        self._next_id += 1
        message_id = self._next_id
        try:
            self._proc.stdin.write(json.dumps({"id": message_id, "type": kind, key: payload}, separators=(",", ":")) + "\n")
            self._proc.stdin.flush()
        except (OSError, ValueError):
            self._cut_off("the bot process stopped (crashed or exited)")
            return None
        status, reply = self._receive(message_id, timeout)
        if status == "eof":
            self._cut_off("the bot process stopped (crashed or exited)")
            return None
        if status == "timeout":
            self.stats.timeouts += 1
            self._fail(f"no reply to '{kind}' within {timeout:g}s")
            return None
        if "error" in reply:
            self._fail(reply["error"])
            return None
        return reply


class LocalBot(BotPlayer):
    """A bot running inside this process: no isolation and no time limits."""

    def __init__(self, bot: str | Path | PokerBot, name: str | None = None, *, max_failures: int = 25):
        if isinstance(bot, (str, Path)):
            self.path: Path | None = Path(bot).resolve()
            self.bot = None
            default_name = self.path.name
        else:
            self.path = None
            self.bot = bot
            default_name = type(bot).__name__
        super().__init__(name or default_name, max_failures)

    def start(self) -> None:
        if self.bot is not None:
            return
        started = time.perf_counter()
        try:
            self.bot = load_bot_class(self.path)()
        except Exception:
            self._cut_off("bot.py failed to load:\n" + traceback.format_exc())
        self.stats.startup_time = time.perf_counter() - started

    def match_start(self, info: dict) -> None:
        if not self.dead:
            try:
                self.bot.on_match_start(MatchInfo.from_dict(info))
            except Exception:
                self._fail(traceback.format_exc())

    def act(self, obs: dict) -> dict | None:
        if self.dead:
            return None
        started = time.perf_counter()
        try:
            action = normalize_action(self.bot.act(Observation.from_dict(obs)))
        except Exception:
            self._fail(traceback.format_exc())
            action = None
        self.stats.record_decision(time.perf_counter() - started)
        return action

    def hand_end(self, result: dict) -> None:
        if not self.dead:
            try:
                self.bot.on_hand_end(HandResult.from_dict(result))
            except Exception:
                self._fail(traceback.format_exc())
