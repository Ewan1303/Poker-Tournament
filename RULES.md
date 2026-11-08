# Rules

## The game

No-limit Texas hold'em, standard rules:

- Heads-up, the button posts the small blind and acts first before the flop and last after it.
- A raise must be at least as big as the last bet or raise on that street. An all-in for less than that doesn't reopen the betting for players who have already acted.
- Side pots are split as usual. In a split pot, odd chips go to the first winner left of the button.
- At a showdown everyone still in shows their cards, and every bot is told what they were.

## Main event: heads-up duplicate round robin

- Every bot plays every other bot. A match is **1,000 deals** (the organizer may change this for a round), and each deal is played twice with the seats swapped.
- The cards are shuffled from a secret random seed. The seed is published with the results, so anyone can replay the deals.
- Every hand starts with **10,000 chips each (100 big blinds)**, blinds 50/100. Stacks reset after each hand, and the button alternates.
- Your **score is the big blinds you win per 100 hands (bb/100), averaged over all your opponents**. The highest score wins the round.
- The leaderboard shows a 95% range for each score. If the top bots are within each other's range, the organizer may play more hands to separate them.
- The two halves of a match run in fresh processes, so a bot can't remember the first half's cards in the second half.

## Side event: sit-and-go (if the organizer runs one)

- Tables of up to 6 bots. Everyone starts with 10,000 chips; blinds start at 50/100 and go up every 20 hands. Play continues until one bot has all the chips.
- Points: 1 for first place, down to 0 for last (linear). Bots are ranked by average points over many games with random seating.

## Your bot

- A folder containing `bot.py`, which defines a class named `Bot` (normally a subclass of `PokerBot`) with an `act(obs)` method. See [docs/BOT_API.md](docs/BOT_API.md).
- **Python 3.11+**. You may use the standard library, your own files, and **numpy, scipy, scikit-learn, pandas and PyTorch** (see [requirements-bots.txt](requirements-bots.txt)). Want something else? Ask the organizer before the deadline.
- **Time:** 1 second per decision, a hard limit. Miss it and your bot checks if it can, otherwise folds. You get 60 seconds to load. Aim for well under 50 ms per decision on average, because a round is tens of thousands of hands.
- **Hardware:** one CPU core per bot, no GPU.
- **Size:** at most 50 MB for the whole folder.
- **Errors:** an exception, timeout or invalid reply counts as check-or-fold for that decision. After 25 in one half of a match, your bot is cut off and folds every hand for the rest of it.
- Illegal actions are corrected rather than punished: folding when you could check becomes a check, checking when facing a bet becomes a fold, a raise that isn't allowed becomes a call, and raise sizes are clamped between the minimum raise and all-in.
- `poker validate` must pass.

## Fair play

- **One bot per person** (or per team, if teams are allowed for the round).
- Your bot may only use what the engine sends it. During a tournament it must not:
  - write files, or read files outside its own folder
  - use the network, start other processes, or try to inspect the engine or other bots
  - keep memory between matches, for example by saving files. Learning during a match is fine and encouraged.
- **No collusion.** Bots must not recognise friends' bots and go easy on them, and must not deliberately lose chips to another bot.
- Studying opponents is encouraged. Bots see opponents' names and showdown cards, and previous rounds' hand histories are published.
- The organizer reads submissions before running them and may disqualify a bot that breaks these rules. The organizer's decisions are final.

## Rounds

| Round | Deadline | Format | Results |
|---|---|---|---|
| 1 | TBA | Heads-up, 1,000 deals per match | — |

After each round, the leaderboard goes in [results/](results) and the hand histories are attached to a [GitHub release](https://github.com/Ewan1303/Poker-Tournament/releases).
