"""Cards.

Bots see cards as two-character strings: rank then suit, e.g. "As", "Td", "7h", "2c".
Ranks are 2 3 4 5 6 7 8 9 T J Q K A and suits are c d h s.

Inside the engine a card is an int from 0 to 51: rank_index * 4 + suit_index,
so card >> 2 is the rank (0 = deuce ... 12 = ace) and card & 3 is the suit.
"""

from __future__ import annotations

import random
from typing import Iterable

RANKS = "23456789TJQKA"
SUITS = "cdhs"
CARD_STRINGS = [rank + suit for rank in RANKS for suit in SUITS]
_CARD_INDEX = {card: index for index, card in enumerate(CARD_STRINGS)}


def card_to_int(card: str | int) -> int:
    """Convert "As" (or an int 0-51) to the engine's int form."""
    if isinstance(card, int):
        if 0 <= card < 52:
            return card
        raise ValueError(f"card int out of range: {card}")
    text = str(card).strip()
    if text[:2] == "10":
        text = "T" + text[2:]
    try:
        return _CARD_INDEX[text[0].upper() + text[1:].lower()]
    except (KeyError, IndexError):
        raise ValueError(f"not a card: {card!r} (expected something like 'As', 'Td' or '7h')") from None


def card_to_str(card: int) -> str:
    return CARD_STRINGS[card]


def cards_to_ints(cards: Iterable[str | int]) -> list[int]:
    return [card_to_int(card) for card in cards]


def cards_to_strs(cards: Iterable[int]) -> list[str]:
    return [CARD_STRINGS[card] for card in cards]


def rank_of(card: str | int) -> int:
    """0 for a deuce up to 12 for an ace."""
    return card_to_int(card) >> 2


def suit_of(card: str | int) -> int:
    """0-3 for clubs, diamonds, hearts, spades."""
    return card_to_int(card) & 3


def shuffled_deck(seed: object) -> list[int]:
    """A deck shuffled from `seed`. The same seed gives the same deck on every machine."""
    deck = list(range(52))
    random.Random(str(seed)).shuffle(deck)
    return deck
