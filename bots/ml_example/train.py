"""Train the ml_example bot's model.

    python bots/ml_example/train.py              # about 20 seconds
    python bots/ml_example/train.py --samples 1000000

1. Deal random hands. For each, record hand_features() at a random street and
   whether the hand went on to beat one random opponent hand (1, 0.5 or 0).
2. Fit a logistic regression with numpy, so the model predicts the chance of
   winning from the features.
3. Save the weights to model.npz next to this file; bot.py loads them.

This is the whole pipeline in miniature: make data with the engine, train,
save weights in the bot's folder, load them in __init__. Swap in your own
data (hand histories from `poker match --log`, or a previous round's logs),
features, labels and model.
"""

import argparse
import random
import time
from pathlib import Path

import numpy as np

from poker_tournament import estimate_equity, evaluate
from poker_tournament.features import HAND_FEATURE_NAMES, hand_features

MODEL = Path(__file__).parent / "model.npz"


def make_dataset(samples: int, seed: int) -> tuple[np.ndarray, np.ndarray]:
    rng = random.Random(seed)
    X = np.empty((samples, len(HAND_FEATURE_NAMES)), dtype=np.float64)
    y = np.empty(samples, dtype=np.float64)
    deck = list(range(52))
    for i in range(samples):
        cards = rng.sample(deck, 9)
        hole, opponent, board = cards[:2], cards[2:4], cards[4:]
        X[i] = hand_features(hole, board[: rng.choice((0, 3, 4, 5))])
        mine, theirs = evaluate(hole + board), evaluate(opponent + board)
        y[i] = 1.0 if mine > theirs else 0.5 if mine == theirs else 0.0
    return X, y


def fit_logistic(X: np.ndarray, y: np.ndarray, l2: float = 1e-4, steps: int = 12) -> np.ndarray:
    """Logistic regression by Newton's method. Returns weights with the bias last."""
    Xb = np.hstack([X, np.ones((len(X), 1))])
    w = np.zeros(Xb.shape[1])
    for _ in range(steps):
        p = 1 / (1 + np.exp(-(Xb @ w)))
        gradient = Xb.T @ (p - y) / len(y) + l2 * w
        hessian = (Xb.T * (p * (1 - p))) @ Xb / len(y) + l2 * np.eye(len(w))
        w -= np.linalg.solve(hessian, gradient)
    return w


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--samples", type=int, default=200_000)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    started = time.perf_counter()
    X, y = make_dataset(args.samples, args.seed)
    print(f"Made {len(y):,} examples in {time.perf_counter() - started:.0f}s")

    split = int(0.9 * len(y))
    mean, std = X[:split].mean(axis=0), X[:split].std(axis=0) + 1e-6
    w = fit_logistic((X[:split] - mean) / std, y[:split])

    test = np.hstack([(X[split:] - mean) / std, np.ones((len(y) - split, 1))])
    p = np.clip(1 / (1 + np.exp(-(test @ w))), 1e-6, 1 - 1e-6)
    loss = -np.mean(y[split:] * np.log(p) + (1 - y[split:]) * np.log(1 - p))
    print(f"Held-out log loss {loss:.4f} (always guessing 50% scores {np.log(2):.4f})")

    for hole, board in [(["As", "Ah"], []), (["7c", "2d"], []), (["Kh", "Qh"], ["Jh", "Th", "2c"]), (["9s", "9d"], ["As", "Kd", "4c", "2h"])]:
        x = (np.array(hand_features(hole, board)) - mean) / std
        predicted = 1 / (1 + np.exp(-(x @ w[:-1] + w[-1])))
        simulated = estimate_equity(hole, board, iterations=4000)
        print(f"  {' '.join(hole):6} on {' '.join(board) or '(preflop)':12}  model {predicted:.2f}   simulation {simulated:.2f}")

    np.savez(MODEL, weights=w[:-1], bias=w[-1], mean=mean, std=std, feature_names=np.array(HAND_FEATURE_NAMES))
    print(f"Saved {MODEL}")


if __name__ == "__main__":
    main()
