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
poker run submissions/round-1 --name round-1 --hands 1000 --workers 4
```

- Every pair of bots plays 1,000 deals from both seats. With 10 bots that's 45 matches and 90,000 hands. Time depends on how fast the bots are, so try `--hands 50` first, then scale up. `--workers` runs that many matches at once. Use about half your CPU cores, so each bot gets a core of its own.
- **The card seed is random and secret.** The shuffle code is public, so a bot that knew the seed could work out the whole deck. Don't pass `--seed` for a real round. The seed is saved in `results/round-1/run.json`, so you can publish it afterwards and anyone can replay the deals.
- If a run stops part way (a crash, a reboot, Ctrl+C), run the same command with `--resume`. Finished matches are kept, and it reuses the same seed.
- To show where everyone stands against the built-in bots, add them: `poker run submissions/round-1 bots/equity_bot bots/ml_example --name round-1`.
- `--format both` adds the sit-and-go side event (`--games 60 --table-size 6`).

Everything goes into `results/round-1/`:

| File | What it is |
|---|---|
| `leaderboard.md` | The rankings, head-to-head table and any bot errors |
| `results.json` | The same data, plus per-bot timing and error details |
| `run.json` | The settings and seed used |
| `hands/` | Hand histories, one gzipped JSON line per hand. Only cards shown at showdown appear; use `--full-hand-logs` to keep every hole card for yourself |
| `logs/` | Anything each bot printed, with tracebacks for its errors |

**How sure is the result?** The leaderboard shows a 95% range for each score. If the top two bots' ranges overlap, the round was close. You can play more hands (`--hands 3000`) or call it a tie.

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
