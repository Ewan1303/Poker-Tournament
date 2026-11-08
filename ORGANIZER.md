# Running the tournament

This is for the organizer. Everything happens on one machine with this repo cloned and installed (`pip install -e .` and `pip install -r requirements-bots.txt`).

## Before a round

1. Choose a deadline and settings, and fill in the Rounds table in [RULES.md](RULES.md).
2. Tag the framework, so everyone builds against the same engine:

   ```bash
   git tag v1.0
   git push --tags
   ```

   Don't change the engine or the bot interface while a round is open.
3. Tell everyone how to submit: a private repo (Option A in [SUBMITTING.md](SUBMITTING.md)) or a zip (Option B).

## Collecting the bots

Use one folder per round, such as `submissions/round-1/`. Git ignores `submissions/`, so nothing in it is published.

**Zips:** save them straight into `submissions/round-1/`. Each zip's file name becomes that bot's name.

**Private repos:**

1. Accept the collaborator invitations. Each one arrives by email, or you can accept them all at once. In PowerShell:

   ```powershell
   gh api user/repository_invitations --jq ".[].id" | ForEach-Object { gh api -X PATCH user/repository_invitations/$_ }
   ```

   In bash:

   ```bash
   for id in $(gh api user/repository_invitations --jq '.[].id'); do gh api -X PATCH user/repository_invitations/$id; done
   ```

2. List the repos in `submissions/round-1/entries.csv`:

   ```
   name,source,ref
   alice,https://github.com/alice/alice-poker-bot,
   bob,https://github.com/bob/poker,
   ```

3. Fetch them all at the round's tag:

   ```bash
   poker collect submissions/round-1/entries.csv --ref round-1
   ```

   This clones each repo into `submissions/round-1/<name>/`, or updates it if it's already there. It checks out the `round-1` tag and records each commit and its date in `manifest.json`. A `ref` in the CSV overrides `--ref` for that row, and a `subdir` column says where the bot sits inside a repo. Don't also drop a zip into the folder for someone who's in the CSV.

## Checking the bots

Bots are other people's code running on your computer. Each bot runs in its own process and only sees what the engine sends it: its own cards, the board and the betting. That stops cheating at the table, but Python code can still read your files or use the internet. So:

- Read each `bot.py`; they're usually short. Look for file writes, `open(...)` outside the bot's folder, `subprocess`, `os.system`, `socket`, `requests` or `urllib`.
- Run `poker validate submissions/round-1`. Every bot should pass, so send any failures back to their owners before you start.
- For extra safety, run the round inside Docker (see below).

## Running the round

```bash
poker run submissions/round-1 --name round-1 --tournaments 1000 --workers 4
```

- Every bot plays in all 1,000 tournaments. Each one ends when a bot has every chip, or after 100 hands, when the rest are ranked by chips. `--max-hands 0` plays every tournament to the end, `--hands-per-level` sets how fast the blinds rise, and `--table-size` sets how many fit at one table (more bots than that start at several tables and merge into a final table).
- Time depends on the bots. Try `--tournaments 20` first and scale up from the time it reports. `--workers` plays that many tournaments at once; use about half your CPU cores so each bot gets a core of its own. Each worker keeps one process per bot for a batch of tournaments, so slow-loading bots (big PyTorch models) only start once per batch.
- **The card seed is random and secret.** The shuffle code is public, so a bot that knew the seed could work out the whole deck. Don't pass `--seed` when starting a real round. The seed is printed when the run starts, so note it down, but nothing a bot could read contains it until the round is over. Then it's saved in `results/round-1/run.json` and `results.json`, so anyone can replay the deals.
- If a run stops part way (a crash, a reboot, Ctrl+C), run the same command with `--resume --seed <the seed it printed>`. Finished batches are kept. Ctrl+C stops the bots straight away.
- To show where everyone stands against the built-in bots, add them: `poker run submissions/round-1 bots/equity_bot bots/ml_example --name round-1`.
- `--format all` also runs the heads-up duplicate round robin (`--hands 1000` deals per pairing), or `--format heads-up` runs only that.

Everything goes into `results/round-1/`:

| File | What it is |
|---|---|
| `leaderboard.md` | The rankings (wins, win %, average finish, top 3, average chips won), finishing positions, and any bot errors |
| `results.json` | The same data, plus per-bot timing and error details |
| `run.json` | The settings used, plus the seed once the round has finished |
| `hands/` | Hand histories, one gzipped JSON line per hand. Only cards that were shown appear (at showdown, or by choice); use `--full-hand-logs` to keep every hole card for yourself |
| `logs/` | Anything each bot printed, with tracebacks for its errors |

**How sure is the result?** Each win % comes with a 95% range. If the top two bots' ranges overlap, the round was close: play more tournaments (`--tournaments 3000`) or call it a tie.

## Showing the results

`poker results` opens the leaderboards in your browser: a bar for each bot's win % with its 95% range, a chart of how often each bot finished 1st, 2nd, 3rd and so on, and the heads-up grid if you ran one. It reads `results/`. If a round is still running, it shows the tournaments finished so far and refreshes every few seconds, so you can leave it up on a screen during the run.

## Checking for cheating by watching

Put the entries at a table with `poker watch submissions/round-1` (up to 10 at once). Every card is face up, with each player's chance of winning, so you can see what a bot held for every decision. A bot that somehow knew other players' cards would stand out: folding strong hands exactly when beaten, or calling every bluff. Pause and step through hands one action at a time if something looks odd. For the real round, `--full-hand-logs` saves every hole card from every hand for the same kind of check.

## Publishing

```bash
git add results/round-1/leaderboard.md results/round-1/results.json results/round-1/run.json
git commit -m "Round 1 results"
git push
```

Then share the hand histories, so people can learn from the round. In PowerShell:

```powershell
Compress-Archive results/round-1/hands round-1-hands.zip
gh release create round-1 round-1-hands.zip --title "Round 1" --notes "Leaderboard: results/round-1/leaderboard.md"
```

Add the result to the Rounds table in RULES.md.

## The next round

Repeat with `submissions/round-2/`, the tag `round-2` and `--name round-2`. Participants with private repos just push a new tag, and `poker collect` updates their clones.

## Running inside Docker (optional)

Docker cuts the bots off from the network and from your files:

```bash
docker build -t poker-tournament .
docker run --rm --network none --cpus 4 --memory 8g -v "${PWD}/submissions:/app/submissions:ro" -v "${PWD}/results:/app/results" poker-tournament run submissions/round-1 --name round-1 --workers 4
```

The image installs the allowed packages from requirements-bots.txt, with the CPU-only build of PyTorch.
