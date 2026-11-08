# Built-in bots

Opponents to test against, and examples to read. Use them by name in any command, for example `poker match my_bots/alice equity_bot`.

| Bot | How it plays |
|---|---|
| [random_bot](random_bot/bot.py) | Random actions. Anything sensible beats it. |
| [calling_station](calling_station/bot.py) | Always checks or calls. Bluffing it doesn't work. |
| [equity_bot](equity_bot/bot.py) | Simulates the rest of the hand to estimate its chance of winning, and bets when that beats the price. A fair first target. |
| [ml_example](ml_example/bot.py) | A logistic-regression model, trained by [train.py](ml_example/train.py), predicts its chance of winning. Shows the whole machine-learning pipeline. |

`poker run bots my_bots/alice` plays your bot against all of them.
