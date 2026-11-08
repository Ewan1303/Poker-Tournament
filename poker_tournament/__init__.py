"""Poker Tournament: a no-limit Texas hold'em engine and tournament runner for bots.

Write a bot by subclassing PokerBot (see templates/starter_bot and docs/BOT_API.md).
"""

from .api import Action, HandResult, MatchInfo, Observation, PlayerResult, PlayerView, PokerBot
from .cards import card_to_int, card_to_str, cards_to_ints, cards_to_strs, rank_of, suit_of
from .equity import estimate_equity, showdown_odds
from .evaluator import evaluate, hand_category, hand_name

__version__ = "1.0.0"

__all__ = [
    "Action",
    "HandResult",
    "MatchInfo",
    "Observation",
    "PlayerResult",
    "PlayerView",
    "PokerBot",
    "card_to_int",
    "card_to_str",
    "cards_to_ints",
    "cards_to_strs",
    "estimate_equity",
    "evaluate",
    "hand_category",
    "hand_name",
    "rank_of",
    "showdown_odds",
    "suit_of",
]
