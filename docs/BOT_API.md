# Bot API

## The bot folder

```
alice/
├── bot.py        required: defines a class called Bot
├── helpers.py    your own modules, imported as normal (import helpers)
└── model.npz     model weights and any other files your bot needs
```

For each half of each match, and for each sit-and-go game, the tournament starts a **fresh Python process** for your bot. That process imports `bot.py` and creates `Bot()`. Its working directory is the bot folder, but it's safer to build paths from `Path(__file__).parent`. `print()` output goes to a log file the organizer can read, not to the game.

## Naming your bot

Optionally, set three strings at the top of `bot.py` to give your bot a personality:

```python
NAME = "The Shark"
STYLE = "Tight-aggressive"
DESCRIPTION = "Folds most hands, then bets hard when it plays. Hard to bluff."
```

`poker play` and `poker watch` show the name at the table, with the style and description in the bot picker and when you hover over the seat. `poker bots` lists them too. They're read straight from the file without running it, so they must be plain strings. Without them, the folder name is used, and the first paragraph of the file's docstring becomes the description. Tournament leaderboards always use the folder (or zip) name.

## Lifecycle

```python
from pathlib import Path
from poker_tournament import Action, PokerBot

class Bot(PokerBot):
    def __init__(self):                 # once per process; load models here (60 s limit)
        self.weights = Path(__file__).parent / "model.npz"

    def on_match_start(self, info):     # once per match half or sit-and-go game (optional)
        ...

    def act(self, obs):                 # every time it's your turn: return an Action
        return Action.call()

    def on_hand_end(self, result):      # after every hand, even ones you folded (optional)
        ...
```

`act` and `on_hand_end` must each return within the decision time limit (1 second by default). `__init__` and `on_match_start` get 60 seconds. If a call raises an exception or runs out of time, your bot checks if it can and otherwise folds, and the failure goes on its record.

## Actions

| Return | Meaning |
|---|---|
| `Action.fold()` | Give up the hand. If you could check instead, the engine checks for you. |
| `Action.check()` | Pass without betting. Facing a bet, this counts as a fold. |
| `Action.call()` | Match the current bet, or check when there's nothing to call. |
| `Action.raise_to(amount)` | Bet or raise so that **your total bet on this street** is `amount`. |
| `Action.all_in()` | Put in every chip you have. |

`raise_to` takes the total, not the extra amount. Facing a bet of 300 when you've put in 100, `Action.raise_to(900)` adds 800 more. Amounts below `obs.min_raise_to` are raised to it, and amounts above `obs.max_raise_to` become all-in. When you can't raise, a raise becomes a call.

You can also return plain values: `"call"`, `("raise", 900)`, or `{"type": "raise", "amount": 900}`.

## Observation (`obs`, passed to `act`)

| Field | Type | Meaning |
|---|---|---|
| `hole_cards` | `list[str]` | Your two cards, like `["As", "Td"]` (ranks `23456789TJQKA`, suits `cdhs`) |
| `board` | `list[str]` | Community cards: 0, 3, 4 or 5 of them |
| `street` | `str` | `"preflop"`, `"flop"`, `"turn"` or `"river"` |
| `pot` | `int` | All chips in the pot, including bets on this street |
| `to_call` | `int` | Chips you must add to call (0 means you can check) |
| `current_bet` | `int` | The biggest total bet on this street |
| `can_raise` | `bool` | Whether raising is allowed |
| `min_raise_to`, `max_raise_to` | `int` | Legal range for `raise_to` (`max_raise_to` is all-in); both 0 if you can't raise |
| `legal_actions` | `list[str]` | Some of `fold`, `check`, `call`, `raise` |
| `stack` | `int` | Your chips behind (not in the pot) |
| `bet` | `int` | What you've put in on this street |
| `seat`, `button`, `num_players` | `int` | Seats are numbered 0 to `num_players - 1` |
| `small_blind`, `big_blind` | `int` | Current blinds |
| `players` | `list[PlayerView]` | Everyone at the table: `seat`, `name`, `stack`, `bet`, `contributed`, `folded`, `all_in`, `start_stack` |
| `history` | `list[dict]` | Every action this hand, oldest first (see below) |
| `hand_id` | `int` | Hand number within the match or game |

Helpers:

- `obs.pot_odds`: `to_call / (pot + to_call)`, the share of the final pot you'd be paying to call.
- `obs.raise_to_pot_fraction(f)`: a legal raise-to amount for a bet of `f` times the pot. `1.0` is pot-sized and `0.5` is half pot.
- `obs.me`, `obs.opponents`, `obs.active_opponents` (opponents who haven't folded), `obs.is_button`.
- `obs.street_index` (0 to 3), `obs.hole_card_ints`, `obs.board_ints`, `obs.actions_this_street()`.

### History events

Each entry in `obs.history`, and in `result.history`, looks like this:

```python
{"street": "flop", "seat": 1, "action": "raise", "amount": 600, "to": 900, "all_in": False}
```

- `action` is one of `small_blind`, `big_blind`, `fold`, `check`, `call`, `bet` (the first bet on a street) or `raise`.
- `amount` is the number of chips that action added.
- `to` is the player's total bet on that street afterwards.

## MatchInfo (`info`, passed to `on_match_start`)

| Field | Meaning |
|---|---|
| `format` | `"heads_up"` or `"sng"` in the tournament. `poker play` and `poker watch` can also send `"cash"`: three or more players, each starting every hand with the same chips |
| `your_name`, `players` | Your name, and the names of everyone in the match or game |
| `starting_stack`, `small_blind`, `big_blind` | Chips and opening blinds |
| `num_hands` | Hands in this half of a heads-up match (`None` in a sit-and-go) |
| `blind_levels`, `hands_per_level` | Sit-and-go blind schedule: `[[small, big], ...]`, rising every `hands_per_level` hands |
| `act_timeout` | Seconds allowed per decision |

## HandResult (`result`, passed to `on_hand_end`)

| Field | Meaning |
|---|---|
| `net` | Chips you won (positive) or lost (negative) |
| `seat`, `hole_cards`, `board` | Your seat and cards, and the board as far as it was dealt |
| `showdown` | Whether the hand reached a showdown |
| `players` | For each seat: `name`, `net`, `start_stack`, `end_stack`, `folded`, and `hole_cards`, which is `None` unless the cards are yours or were shown at showdown |
| `history` | Every action in the hand |

## Seats, the button and names

- **Heads-up:** you keep the same seat for a whole half of a match, and the button alternates each hand. Stacks reset to the starting stack every hand.
- **Sit-and-go:** seats are renumbered each hand as players are knocked out, so track opponents by `name`, not seat. Stacks carry over and blinds rise.

## Useful tools

```python
from poker_tournament import estimate_equity, evaluate, hand_name

estimate_equity(["As", "Kd"], ["Qh", "Jc", "2s"], opponents=1, iterations=500)  # chance of winning, 0 to 1
hand_name(evaluate(["As", "Kd", "Qh", "Jc", "Ts"]))                               # "Straight"; higher score = better hand

from poker_tournament.features import observation_features   # feature vectors for ML
from poker_tournament.loader import load_bot                  # load_bot("equity_bot") gives you a bot object
from poker_tournament.match import play_match                 # play_match(bot_a, bot_b, hands=1000) in-process
```

## Testing and debugging

- `poker match my_bots/alice equity_bot` plays a duplicate match in separate processes, exactly as the tournament does.
- `poker watch my_bots/alice equity_bot` shows them playing at a table in your browser, every card face up and one action at a time if you like. `poker play my_bots/alice` lets you play against your own bot.
- Add `--in-process` to run both bots in your own process. It's faster, and breakpoints and `pdb` work.
- `poker validate my_bots/alice` lists errors with tracebacks, illegal actions and how long decisions take.

## Under the hood

The engine talks to your bot's process over stdin and stdout, one JSON message per line. [poker_tournament/runner.py](../poker_tournament/runner.py) describes the protocol. Your bot never sees the deck or the other bots, only the messages above.
