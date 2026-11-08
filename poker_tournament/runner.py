"""Runs one bot in its own process; the tournament talks to it over stdin/stdout.

The tournament starts this as `python -P -m poker_tournament.runner <bot folder>`,
so a bot only ever sees what the engine sends it (its own cards, the board and
the betting), never the deck or another bot's memory. You don't need to run it
yourself.

Protocol (one JSON object per line):
    runner -> engine   {"type": "ready"}  or  {"type": "error", "error": "..."}
    engine -> runner   {"id": 7, "type": "act", "obs": {...}}
    runner -> engine   {"id": 7, "action": {"type": "raise", "amount": 300}}
    engine -> runner   {"id": 8, "type": "hand_end", "result": {...}}   (also "match_start" with "info")
    runner -> engine   {"id": 8, "ok": true}    or    {"id": 8, "error": "traceback"}
    engine -> runner   {"type": "shutdown"}
"""

import json
import os
import sys
import traceback


def main() -> int:
    bot_dir = os.path.abspath(sys.argv[1])

    # Keep private copies of stdin/stdout for the protocol, then point the real
    # ones elsewhere so a print() in the bot (or in a library) can't corrupt it.
    protocol_out = os.fdopen(os.dup(1), "w", encoding="utf-8", newline="\n")
    protocol_in = os.fdopen(os.dup(0), "r", encoding="utf-8")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    os.dup2(os.open(os.devnull, os.O_RDONLY), 0)
    sys.stdin = open(os.devnull)

    def send(message: dict) -> None:
        protocol_out.write(json.dumps(message, separators=(",", ":")) + "\n")
        protocol_out.flush()

    from poker_tournament.api import HandResult, MatchInfo, Observation, normalize_action
    from poker_tournament.loader import load_bot_class

    try:
        bot = load_bot_class(bot_dir, module_name="bot")()
    except BaseException:
        send({"type": "error", "error": traceback.format_exc()})
        return 1
    send({"type": "ready"})

    for line in protocol_in:
        if not line.strip():
            continue
        message = json.loads(line)
        kind = message.get("type")
        if kind == "shutdown":
            break
        reply = {"id": message.get("id")}
        try:
            if kind == "act":
                reply["action"] = normalize_action(bot.act(Observation.from_dict(message["obs"])))
            elif kind == "hand_end":
                bot.on_hand_end(HandResult.from_dict(message["result"]))
                reply["ok"] = True
            elif kind == "match_start":
                bot.on_match_start(MatchInfo.from_dict(message["info"]))
                reply["ok"] = True
            else:
                reply["error"] = f"unknown message type {kind!r}"
        except Exception:
            text = traceback.format_exc()
            print(text, file=sys.stderr, flush=True)
            reply = {"id": message.get("id"), "error": text}
        send(reply)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
