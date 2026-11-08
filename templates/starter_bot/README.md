# My poker bot

Made with `poker new`. Everything the bot needs (code, model files) lives in this folder.

```bash
poker match . equity_bot          # play a match against the equity baseline
poker validate .                  # the checks the organizer runs
poker package .                   # make a zip to submit
```

Rules for this folder:

- `bot.py` must define a class called `Bot` with an `act(self, obs)` method.
- Load files relative to `Path(__file__).parent`, not the current directory.
- Only import the standard library, your own files and the allowed packages (see RULES.md).
- Keep it under 50 MB. Keep training data and scripts' output somewhere else.
