# Built-in bots

Eight simple opponents to test your bot against, each with a personality. None of them is meant to win the tournament. Each one pokes at a different weakness, so a good bot should beat all of them.

Use them by folder name in any command (`poker match my_bots/alice maniac`), or pick them by name in `poker play` and `poker watch`. `poker bots` lists them in the terminal.

| Bot | Folder | How it plays | What it tests |
|---|---|---|---|
| **Randy** | [random_bot](random_bot/bot.py) | Folds, calls and raises completely at random | The basics: anything sensible should crush him |
| **Callie** | [calling_station](calling_station/bot.py) | Calls every bet, never folds, never raises | Value betting: bluffs fail, so bet your good hands big |
| **Maniac Max** | [maniac](maniac/bot.py) | Raises nearly every hand, often all in | Nerve: do you call him down with decent hands, or get bullied? |
| **Rocky** | [rock](rock/bot.py) | Plays only big pairs and big aces | Respect and stealing: fold to his bets, take his blinds |
| **Bluffing Bella** | [bluffer](bluffer/bot.py) | Bets whenever nobody else has, gives up when raised, and often shows you what you folded to (her code shows how to use `show_cards`) | Catching bluffs: call her lighter, or raise her off her hand |
| **Nervous Nick** | [fit_or_fold](fit_or_fold/bot.py) | Limps a lot, then folds unless the flop hits him | Aggression: does your bot bet enough to win the pots he gives up? |
| **Professor Odds** | [equity_bot](equity_bot/bot.py) | Simulates the hand and bets when the odds are right | A solid all-round baseline, and the one to beat |
| **Lenny the Learner** | [ml_example](ml_example/bot.py) | A small model trained by [train.py](ml_example/train.py) predicts his chance of winning | The machine-learning pipeline, start to finish |

In a test run of 100 tournaments with all eight, Professor Odds won 27, Lenny 24, Nick 19 and Rocky 17. Rocky had the best average finish, 2.90, because playing tight survives to the hand limit. Bella won 7, Maniac Max 4, and Randy and Callie 1 each.

Your own bot can have a name and blurb like these: set `NAME`, `STYLE` and `DESCRIPTION` at the top of its `bot.py` (see [docs/BOT_API.md](../docs/BOT_API.md#naming-your-bot)).
