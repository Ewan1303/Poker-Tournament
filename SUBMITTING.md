# Submitting your bot

Your bot is a folder: `bot.py` plus anything it needs, such as model weights and helper modules. Before you submit, run:

```bash
poker validate my_bots/alice
```

It must say `PASSED`.

**Keep your bot private until the deadline.** Don't fork this repo to work on your bot: forks of a public repo are public. And don't open a pull request with your bot in it.

There are two ways to submit. Use whichever the organizer asks for.

## Option A: a private GitHub repo (recommended)

You set this up once, and each round you just push a tag.

**Once:**

1. Turn your bot folder into a private repo:

   ```bash
   cd my_bots/alice
   git init
   git add .
   git commit -m "My poker bot"
   gh repo create alice-poker-bot --private --source . --push
   ```

   No GitHub CLI? Create an empty private repo on github.com, then run `git remote add origin <its URL>` and `git push -u origin HEAD`.

2. Invite the organizer, **Ewan1303**, as a collaborator. On GitHub that's the repo's Settings → Collaborators → Add people. Read access is enough. Or from the command line:

   ```bash
   gh api -X PUT repos/<your-username>/alice-poker-bot/collaborators/Ewan1303 -f permission=pull
   ```

3. Send the organizer the repo's URL.

**Each round:** commit your final version and tag it with the round's name before the deadline.

```bash
git tag round-1
git push origin round-1
```

The organizer collects exactly the commit that tag points to. To change your entry before the deadline, move the tag:

```bash
git tag -f round-1
git push -f origin round-1
```

`bot.py` should be at the top level of the repo. If it isn't, tell the organizer which folder it's in.

## Option B: send a zip

```bash
poker package my_bots/alice
```

This validates the bot, then writes `alice.zip`. Send it to the organizer before the deadline, by email or wherever they ask. The zip's file name becomes your bot's name on the leaderboard, so name it after yourself.

## Big models

The limit is 50 MB, which also keeps you under GitHub's file-size limits, so you won't need Git LFS. To shrink a model, save only the weights (a PyTorch `state_dict`, or numpy `.npz`), use float16, or train a smaller network. Keep training data out of the bot folder.
