"""The built-in bots: their profiles, and that each one plays the way its description says."""

import pytest

from poker_tournament import PokerBot
from poker_tournament.api import normalize_action
from poker_tournament.loader import BUILTIN_BOTS, bot_info, load_bot
from poker_tournament.match import play_match

BUILTIN = sorted(p.name for p in BUILTIN_BOTS.iterdir() if (p / "bot.py").is_file())


@pytest.mark.parametrize("name", BUILTIN)
def test_every_built_in_bot_has_a_profile(name):
    info = bot_info(BUILTIN_BOTS / name)
    assert info["id"] == name
    assert info["name"] != name, "give it a NAME"
    assert info["style"] and len(info["description"]) > 40


def test_profile_falls_back_to_folder_name_and_docstring(tmp_path):
    folder = tmp_path / "alice"
    folder.mkdir()
    (folder / "bot.py").write_text('"""Plays tight.\n\nMore detail."""\nNAME = None\nSTYLE = f"{1}"\n', encoding="utf-8")
    assert bot_info(folder) == {"id": "alice", "name": "alice", "style": "", "description": "Plays tight."}
    (folder / "bot.py").write_text("this is not python", encoding="utf-8")
    assert bot_info(folder)["name"] == "alice"


class Recorder(PokerBot):
    """Passes decisions through to a bot and keeps (observation, action) pairs."""

    def __init__(self, inner):
        self.inner = inner
        self.decisions = []

    def on_match_start(self, info):
        self.inner.on_match_start(info)

    def act(self, obs):
        action = self.inner.act(obs)
        self.decisions.append((obs, normalize_action(action)["type"]))
        return action

    def on_hand_end(self, result):
        self.inner.on_hand_end(result)


def habits(name, opponent, hands=250):
    bot = Recorder(load_bot(name))
    play_match(bot, load_bot(opponent), hands=hands, seed=7)
    d = bot.decisions
    aggressive = ("raise", "all_in")

    def share(chosen, among):
        among = list(among)
        return sum(1 for obs, act in among if act in chosen) / max(1, len(among))

    preflop_facing = [(o, a) for o, a in d if o.street == "preflop" and o.to_call > 0]
    postflop_facing = [(o, a) for o, a in d if o.street != "preflop" and o.to_call > 0]
    checked_to = [(o, a) for o, a in d if o.street != "preflop" and o.to_call == 0 and o.can_raise]
    return {
        "plays_preflop": 1 - share({"fold"}, preflop_facing),
        "raises": share(aggressive, d),
        "folds_to_bets": share({"fold"}, postflop_facing),
        "bets_when_checked_to": share(aggressive, checked_to),
        "folds": share({"fold"}, d),
    }


def test_rocky_hardly_plays_a_hand():
    assert habits("rock", "calling_station")["plays_preflop"] < 0.2


def test_max_raises_nearly_everything():
    stats = habits("maniac", "calling_station")
    assert stats["raises"] > 0.6 and stats["folds"] == 0


def test_bella_bets_whenever_nobody_else_has():
    assert habits("bluffer", "calling_station")["bets_when_checked_to"] > 0.7


def test_nick_folds_when_you_bet_at_him():
    assert habits("fit_or_fold", "maniac")["folds_to_bets"] > 0.5


def test_callie_never_folds_or_raises():
    stats = habits("calling_station", "maniac")
    assert stats["folds"] == 0 and stats["raises"] == 0
