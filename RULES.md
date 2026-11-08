# Rules

## The game

No-limit Texas hold'em, standard rules:

- Heads-up, the button posts the small blind and acts first before the flop and last after it.
- A raise must be at least as big as the last bet or raise on that street. An all-in for less than that doesn't reopen the betting for players who have already acted.
- When a player is all in for less than the others, the extra bets go into a **side pot** that only the players who covered them can win. Main and side pots are each won by the best hand eligible for them. In a split pot, odd chips go to the first winner left of the button. A bet nobody calls goes back to the player who made it.
- **Showing cards works like a real table.** If a hand reaches a showdown, which means betting is finished on the river with two or more players left, everyone still in shows their cards. If everyone else folds, the winner doesn't have to show, and nor does anyone who folded. Any player may still choose to show, for example to advertise a bluff. Bots decide this with `show_cards` (see [docs/BOT_API.md](docs/BOT_API.md)). Every bot is told about every card that was shown.

## Main event: tournaments

- Every bot plays in every tournament, in random seats. Everyone starts with **10,000 chips**; the blinds start at **50/100** and go up every **20 hands**. Lose all your chips and you're out.
- A tournament ends when one bot has every chip, or after **100 hands**. If the hand limit is reached, everyone still in is ranked by chips, and bots knocked out earlier rank below them. A cautious bot that loses slowly therefore finishes above one that busts.
- With more than 10 bots, they start at several tables. The tables are balanced as players go out and merge into a final table.
- A round is many tournaments: **100 or 1,000**, as the organizer announces. Bots are **ranked by tournaments won**, then by average finishing place. When two bots tie for a place, they split it, so a shared 1st place counts as half a win each. The leaderboard also shows win %, with a 95% range showing how much could be luck, plus average finish, top-3 finishes and average chips won per tournament.
- The cards are shuffled from a secret random seed. The seed is published with the results, so anyone can replay the deals.

## Also available: heads-up duplicate round robin

The organizer may also run (`--format heads-up` or `all`) a round robin where every pair of bots plays 1,000 deals twice with the seats swapped, so luck evens out. Stacks reset every hand. The score is big blinds won per 100 hands, averaged over opponents.

## Your bot

- A folder containing `bot.py`, which defines a class named `Bot` (normally a subclass of `PokerBot`) with an `act(obs)` method. See [docs/BOT_API.md](docs/BOT_API.md).
- **Python 3.11+**. You may use the standard library, your own files, and **numpy, scipy, scikit-learn, pandas and PyTorch** (see [requirements-bots.txt](requirements-bots.txt)). Want something else? Ask the organizer before the deadline.
- **Time:** 1 second per decision, a hard limit. Miss it and your bot checks if it can, otherwise folds. You get 60 seconds to load. Aim for well under 50 ms per decision on average, because a round is hundreds of thousands of decisions.
- **Hardware:** one CPU core per bot, no GPU.
- **Size:** at most 50 MB for the whole folder.
- **Errors:** an exception, timeout or invalid reply counts as check-or-fold for that decision. After 25 in one tournament, your bot is cut off and folds for the rest of it.
- Illegal actions are corrected rather than punished: folding when you could check becomes a check, checking when facing a bet becomes a fold, a raise that isn't allowed becomes a call, and raise sizes are clamped between the minimum raise and all-in.
- `poker validate` must pass.

## Fair play

- **One bot per person** (or per team, if teams are allowed for the round).
- Your bot may only use what the engine sends it. During a tournament it must not:
  - write files, or read files outside its own folder
  - use the network, start other processes, or try to inspect the engine or other bots
  - save files to keep memory between games. Learning during a run is fine and encouraged: a bot keeps its memory through a batch of tournaments, and starts fresh in the next batch.
- **No collusion.** Bots must not recognise friends' bots and go easy on them, and must not deliberately lose chips to another bot.
- Studying opponents is encouraged. Bots see opponents' names and every card that's shown, and previous rounds' hand histories are published.
- The organizer reads submissions before running them, can watch any bot play with every card face up, and may disqualify a bot that breaks these rules. The organizer's decisions are final.

## Rounds

| Round | Deadline | Format | Results |
|---|---|---|---|
| 1 | TBA | 1,000 tournaments, 100-hand limit | — |

After each round, the leaderboard goes in [results/](results) and the hand histories are attached to a [GitHub release](https://github.com/Ewan1303/Poker-Tournament/releases).
