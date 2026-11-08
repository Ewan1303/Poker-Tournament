# Machine learning guide

Any approach is allowed, as long as the finished bot runs within the rules: 1 second per decision, one CPU core, 50 MB, and the allowed libraries. Training can use whatever you like, including GPUs and other libraries, because only the bot folder is submitted.

## The pipeline

1. **Get data:** simulate hands with the engine, or use published hand histories.
2. **Train** a model.
3. **Save the weights** into your bot folder.
4. **Load them** in `Bot.__init__` and use the model in `act`.
5. Check with `poker validate`.

[bots/ml_example](../bots/ml_example) does all five in about 100 lines. Its [train.py](../bots/ml_example/train.py) deals random hands, labels each with whether it went on to win, fits a logistic regression with numpy, and saves `model.npz`. Its [bot.py](../bots/ml_example/bot.py) loads that file and turns the predicted chance of winning into bets.

```bash
python bots/ml_example/train.py
poker match ml_example equity_bot
```

## Getting data

### Hand histories from your own matches

```bash
poker match equity_bot ml_example --hands 20000 --in-process --log data/equity_vs_ml.jsonl.gz
```

Each line is one hand. Logs from `poker match` include every player's hole cards:

```json
{
  "half": 1, "hand_id": 17, "button": 0, "small_blind": 50, "big_blind": 100,
  "board": ["Qs", "7d", "2c", "Jh", "9h"], "showdown": true,
  "players": [
    {"seat": 0, "name": "equity_bot", "start_stack": 10000, "end_stack": 10900, "net": 900, "folded": false, "hole_cards": ["Qh", "Qc"]},
    {"seat": 1, "name": "ml_example", "start_stack": 10000, "end_stack": 9100, "net": -900, "folded": false, "hole_cards": ["Ad", "7s"]}
  ],
  "pots": [{"amount": 1800, "eligible": [1, 0], "winners": [0]}],
  "history": [
    {"street": "preflop", "seat": 0, "action": "small_blind", "amount": 50, "to": 50, "all_in": false},
    {"street": "preflop", "seat": 1, "action": "big_blind", "amount": 100, "to": 100, "all_in": false},
    {"street": "preflop", "seat": 0, "action": "raise", "amount": 250, "to": 300, "all_in": false}
  ]
}
```

(The `history` list is shortened here.)

Reading it:

```python
import gzip, json

with gzip.open("data/equity_vs_ml.jsonl.gz", "rt") as f:
    hands = [json.loads(line) for line in f]
```

To turn a hand into training examples, step through `history` and rebuild what the acting player could see at each decision: their hole cards, the board for that street, and the actions so far.

### Tournament hand histories

After each round, the organizer publishes every hand from the round as a GitHub release, in the same format. Those logs only show hole cards that reached a showdown, which is what the players could actually see. Use them to study how each opponent plays, or to train against the field.

### Simulating directly

For self-play or search, drive the engine yourself:

```python
from poker_tournament.cards import shuffled_deck
from poker_tournament.engine import Hand

hand = Hand(stacks=[10000, 10000], button=0, small_blind=50, big_blind=100, deck=shuffled_deck(seed=1))
while not hand.finished:
    obs = hand.observation(hand.to_act)          # a dict; Observation.from_dict(obs) for the bot view
    hand.apply(my_policy(obs))                   # "call", ("raise", 300), Action.fold(), ...
print(hand.winnings)                             # net chips per seat
```

It plays thousands of hands per second.

### Reinforcement learning

`HeadsUpEnv` wraps the engine in a gym-style interface, with a fixed opponent:

```python
from poker_tournament.env import HeadsUpEnv
from poker_tournament.loader import load_bot

env = HeadsUpEnv(opponent=load_bot("equity_bot"), seed=0)
obs = env.reset()
done = False
while not done:
    obs, reward, done, info = env.step(agent(obs))   # reward = chips won in the hand
```

Rotate opponents, or train against copies of your own agent, so it doesn't overfit to one style.

## Features

`poker_tournament.features` gives you a starting point:

- `hand_features(hole, board)`: 31 numbers about hand strength, such as made hand, draws, board texture and overcards.
- `observation_features(obs)`: those 31 plus the betting situation, such as pot odds, stack-to-pot ratio and position.
- `one_hot_cards(cards)`: 52 numbers, one per card.
- `HAND_FEATURE_NAMES` and `OBSERVATION_FEATURE_NAMES` say what each number is.

## Approaches worth trying

- **Hand strength:** predict your chance of winning quickly, instead of simulating it at every decision.
- **Imitation:** learn the actions of a strong bot from its hand histories, then improve on it.
- **Reinforcement learning:** self-play with PPO, DQN or Neural Fictitious Self-Play, using `HeadsUpEnv` or the engine directly.
- **Game theory:** counterfactual regret minimisation (CFR), with your cards and bet sizes grouped into buckets. It's the idea behind Libratus and Pluribus, the programs that beat top professionals. Train the strategy offline and store it as a table in your bot folder.
- **Opponent modelling:** track each opponent's habits by name, such as how often they voluntarily put chips in, how often they raise and when they fold, and exploit them. Use previous rounds' logs to start with good guesses.
- **Mix them.** A balanced core strategy plus opponent-specific adjustments is hard to beat.

## Practical tips

- **Speed matters.** Aim for under 50 ms per decision on average. `estimate_equity` with 1,000 iterations takes about 15 ms. A small neural network takes under 1 ms.
- **One core.** Bots run with `OMP_NUM_THREADS=1`, so PyTorch and numpy use a single thread. Keep networks small.
- **Save weights, not pickles.** Use numpy `.npz`, a PyTorch `state_dict`, or JSON. A pickled scikit-learn model can break if the organizer's library version differs from yours.
- **Use the same feature code** when training and when playing. If you use `poker_tournament.features`, train with the same version of this repo that the round uses.
- **Think in big blinds.** Divide chip amounts by `obs.big_blind`, so a model works at any stake.
- **Mix up your play.** Opponents can study the published hand histories, and a fully predictable bot is easy to exploit.
