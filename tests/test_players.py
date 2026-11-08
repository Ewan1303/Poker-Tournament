"""Bots in their own processes: misbehaving bots must not break a match."""

import textwrap
import threading

import pytest

from poker_tournament.loader import resolve_bot
from poker_tournament.match import play_heads_up
from poker_tournament.players import ProcessBot


def make_bot(tmp_path, name, body, imports=""):
    folder = tmp_path / name
    folder.mkdir()
    code = f"""
import os, sys, time, gc
{imports}
from poker_tournament import Action, PokerBot

class Bot(PokerBot):
    def __init__(self):
        self.calls = 0

    def act(self, obs):
        self.calls += 1
{textwrap.indent(textwrap.dedent(body), "        ")}
"""
    (folder / "bot.py").write_text(code, encoding="utf-8")
    return folder


def play(folder, hands=20, act_timeout=1.0, max_failures=25):
    bot = ProcessBot(folder, "test_bot", act_timeout=act_timeout, max_failures=max_failures)
    other = ProcessBot(resolve_bot("calling_station"), "calling_station")
    try:
        bot.start()
        other.start()
        nets = play_heads_up([bot, other], hands, seed=1)
    finally:
        bot.close()
        other.close()
    assert len(nets) == hands
    return bot


def test_printing_does_not_break_the_protocol(tmp_path):
    folder = make_bot(tmp_path, "printer", """
        print("hello from the bot", obs.hole_cards)
        sys.stdout.write("{not json\\n")
        os.write(1, b"raw bytes on fd 1\\n")
        return Action.call()
    """)
    bot = play(folder)
    assert bot.stats.failures == 0 and bot.stats.decisions > 0
    assert "hello from the bot" in bot.output and "raw bytes on fd 1" in bot.output


def test_exceptions_count_as_failures_and_play_goes_on(tmp_path):
    folder = make_bot(tmp_path, "raiser", """
        if self.calls % 3 == 0:
            raise ValueError("boom")
        return Action.call()
    """)
    stats = play(folder).stats
    assert stats.failures > 0 and not stats.cut_off
    assert "ValueError: boom" in stats.errors[0]


def test_invalid_actions_are_failures(tmp_path):
    folder = make_bot(tmp_path, "nonsense", "return 'banana'")
    stats = play(folder, hands=5).stats
    assert stats.failures > 0 and "banana" in stats.errors[0]


def test_slow_bots_time_out_and_get_cut_off(tmp_path):
    folder = make_bot(tmp_path, "slow", """
        time.sleep(0.6)
        return Action.call()
    """)
    stats = play(folder, hands=10, act_timeout=0.2, max_failures=3).stats
    assert stats.timeouts == 3 and stats.cut_off


def test_stuck_bot_cannot_hang_the_match(tmp_path):
    # A bot that never answers stops reading its input. The engine must not keep
    # writing to it (a full pipe would block forever); it should just cut it off.
    folder = make_bot(tmp_path, "stuck", """
        while True:
            pass
    """)
    outcome = {}
    worker = threading.Thread(target=lambda: outcome.update(bot=play(folder, hands=30, act_timeout=0.1, max_failures=20)))
    worker.start()
    worker.join(timeout=60)
    assert not worker.is_alive(), "the match hung on a stuck bot"
    assert outcome["bot"].stats.cut_off


def test_junk_on_the_protocol_stream_is_ignored(tmp_path):
    folder = make_bot(tmp_path, "junk", """
        frame = sys._getframe(1)
        while frame and "protocol_out" not in frame.f_locals:
            frame = frame.f_back
        if frame:
            frame.f_locals["protocol_out"].write('[]\\n"text"\\n{"id": -5}\\n')
            frame.f_locals["protocol_out"].flush()
        return Action.call()
    """)
    stats = play(folder, hands=5).stats
    assert stats.failures == 0


def test_crashing_bot_is_cut_off(tmp_path):
    folder = make_bot(tmp_path, "crasher", """
        if self.calls == 4:
            os._exit(3)
        return Action.call()
    """)
    stats = play(folder).stats
    assert stats.cut_off and "stopped" in stats.errors[-1]


def test_bot_that_fails_to_load(tmp_path):
    folder = tmp_path / "broken"
    folder.mkdir()
    (folder / "bot.py").write_text("import this_module_does_not_exist\n", encoding="utf-8")
    bot = ProcessBot(folder, "broken")
    bot.start()
    bot.close()
    assert bot.dead and "this_module_does_not_exist" in bot.stats.errors[0]


def test_bot_cannot_see_the_engine(tmp_path):
    # In its own process a bot can't find the Hand object (and the deck) in memory.
    folder = make_bot(tmp_path, "snoop", """
        if any(type(o).__name__ == "Hand" for o in gc.get_objects()):
            raise RuntimeError("found the engine")
        return Action.call()
    """)
    stats = play(folder, hands=3).stats
    assert stats.failures == 0


def test_bot_can_load_files_and_helpers_from_its_folder(tmp_path):
    folder = make_bot(tmp_path, "helpers", """
        return Action.call() if helper.ALWAYS_CALL and (HERE / "weights.txt").read_text() == "42" else Action.fold()
    """, imports="from pathlib import Path\nimport helper\nHERE = Path(__file__).parent")
    (folder / "helper.py").write_text("ALWAYS_CALL = True\n", encoding="utf-8")
    (folder / "weights.txt").write_text("42", encoding="utf-8")
    stats = play(folder, hands=3).stats
    assert stats.failures == 0
