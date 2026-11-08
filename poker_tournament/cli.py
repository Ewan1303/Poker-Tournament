"""The `poker` command. Run `poker --help` or `poker <command> --help`."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import sys
from datetime import datetime
from pathlib import Path

from . import __version__
from .loader import FRAMEWORK_ROOT, BotLoadError, resolve_bot


def _cmd_new(args) -> int:
    template = FRAMEWORK_ROOT / "templates" / "starter_bot"
    dest = Path(args.path)
    if dest.exists() and any(dest.iterdir()):
        print(f"{dest} already exists and isn't empty")
        return 1
    shutil.copytree(template, dest, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
    print(f"Created your bot in {dest}")
    print(f"  1. Edit {dest / 'bot.py'}")
    print(f"  2. Try it:    poker match {dest.as_posix()} equity_bot")
    print(f"  3. Check it:  poker validate {dest.as_posix()}")
    return 0


def _cmd_validate(args) -> int:
    results = []
    for spec in args.bots:
        path = Path(spec)
        # A folder of submissions: check every bot in it.
        specs = [spec]
        if path.is_dir() and not (path / "bot.py").exists():
            from .tournament import discover_entries

            specs = [str(e.path) for e in discover_entries([path])]
        for item in specs:
            results.append(_validate_one(item, args))
            print()
    if len(results) > 1:
        print(f"{sum(results)} of {len(results)} bots passed.")
    return 0 if all(results) else 1


def _validate_one(spec, args) -> bool:
    from .validate import validate_bot

    return validate_bot(spec, hands=args.hands, act_timeout=args.timeout, startup_timeout=args.startup_timeout)


def _cmd_package(args) -> int:
    from .validate import format_size, package_bot, validate_bot

    if not args.skip_validate and not validate_bot(args.bot):
        print("Not packaged: fix the problems above first (or use --skip-validate).")
        return 1
    out = package_bot(args.bot, args.output)
    print(f"\nPackaged {out} ({format_size(out.stat().st_size)}). Send this zip to the organizer (see SUBMITTING.md).")
    return 0


def _cmd_match(args) -> int:
    from .match import HandLog, play_match
    from .players import LocalBot, ProcessBot

    sides = [(resolve_bot(spec), _display_name(spec)) for spec in (args.bot_a, args.bot_b)]
    if sides[0][1] == sides[1][1]:
        sides = [(sides[0][0], sides[0][1] + "_A"), (sides[1][0], sides[1][1] + "_B")]

    def make_player(side):
        path, name = side
        if args.in_process:
            return LocalBot(path, name)
        return ProcessBot(path, name, act_timeout=args.timeout, startup_timeout=args.startup_timeout)

    log = HandLog(args.log, full_info=True) if args.log else None
    seed = args.seed if args.seed is not None else secrets.randbelow(2**63)
    mode = "in this process" if args.in_process else "in separate processes"
    print(f"{sides[0][1]} vs {sides[1][1]}: {args.hands:,} deals from both seats ({2 * args.hands:,} hands), {mode}, seed {seed}...")
    try:
        result = play_match(sides[0], sides[1], args.hands, seed, stack=args.stack, blinds=args.blinds, make_player=make_player, hand_log=log)
    finally:
        if log:
            log.close()
    print()
    print(f"  {result.a}: {result.bb_per_100:+.1f} bb/100 (95% range +/-{1.96 * result.stderr_bb_per_100:.1f}), {result.chips:+,} chips")
    for name, stats in result.stats.items():
        line = f"  {name}: {stats.avg_think_ms:.1f} ms per decision (slowest {1000 * stats.max_think:.0f} ms), {stats.failures} errors"
        if stats.corrections:
            line += f", {stats.corrections} illegal actions corrected"
        print(line)
        for error in stats.errors[:1]:
            print("    first error: " + error.strip().splitlines()[-1])
    winner = result.a if result.chips > 0 else result.b if result.chips < 0 else None
    print()
    print(f"  {'Winner: ' + winner if winner else 'A draw'}." + (f" Hand histories written to {args.log}." if args.log else ""))
    if abs(result.bb_per_100) < 1.96 * result.stderr_bb_per_100:
        print("  (Within the margin of luck; play more hands with --hands for a clearer answer.)")
    return 0


def _display_name(spec: str) -> str:
    path = Path(spec)
    if path.suffix.lower() == ".zip":
        return path.stem
    return path.resolve().name if path.exists() else str(spec)


def _cmd_run(args) -> int:
    from .tournament import TournamentConfig, discover_entries, run_tournament

    entries = discover_entries(args.paths)
    formats = {"heads-up": ("heads_up",), "sng": ("sng",), "both": ("heads_up", "sng")}[args.format]
    name = args.name or datetime.now().strftime("run-%Y%m%d-%H%M%S")
    out_dir = Path(args.out) / name
    seed = args.seed
    saved_run = out_dir / "run.json"
    if args.resume and saved_run.exists():
        saved = json.loads(saved_run.read_text(encoding="utf-8"))
        if seed is None:
            seed = saved.get("seed")  # only written once a run has finished
        if seed is None:
            print("error: to resume, pass the run's seed with --seed (it was printed when the run started)", file=sys.stderr)
            return 2
        if saved.get("seed_hash") and TournamentConfig(name=name, seed=seed).seed_hash() != saved["seed_hash"]:
            print(f"error: seed {seed} isn't the one '{name}' was started with", file=sys.stderr)
            return 2
    if seed is None:
        # The shuffle code is public, so a known seed would let a bot work out the deck.
        seed = secrets.randbelow(2**63)
    config = TournamentConfig(
        name=name,
        formats=formats,
        hands=args.hands,
        seed=seed,
        stack=args.stack,
        small_blind=args.blinds[0],
        big_blind=args.blinds[1],
        games=args.games,
        table_size=args.table_size,
        hands_per_level=args.hands_per_level,
        act_timeout=args.timeout,
        startup_timeout=args.startup_timeout,
        workers=args.workers,
        in_process=args.in_process,
        hand_logs=not args.no_hand_logs,
        full_info_logs=args.full_hand_logs,
    )
    print(f"Tournament '{name}' with {len(entries)} bots: {', '.join(e.name for e in entries)}")
    print(f"Card seed: {seed}")
    print("  Keep it to yourself until the round is over, but note it down: to pick up an interrupted run,")
    print(f"  use the same command with --resume --seed {seed}. It's saved with the results at the end.")
    print(f"Results go to {out_dir}")
    summary = run_tournament(entries, config, out_dir, resume=args.resume)
    print()
    if "heads_up" in summary:
        print("Heads-up round robin")
        print(f"  {'#':>2}  {'bot':<24} {'bb/100':>8} {'95% range':>10} {'won':>7} {'avg ms':>7} {'errors':>6}")
        for rank, row in enumerate(summary["heads_up"]["leaderboard"], 1):
            print(
                f"  {rank:>2}  {row['bot']:<24} {row['bb_per_100']:>+8.1f} {'+/-' + format(1.96 * row['stderr'], '.1f'):>10} "
                f"{str(row['won']) + '/' + str(row['opponents']):>7} {row['avg_ms']:>7.1f} {row['failures']:>6}"
            )
        print()
    if "sng" in summary:
        print("Sit-and-go")
        print(f"  {'#':>2}  {'bot':<24} {'points':>7} {'wins':>5} {'games':>6} {'avg place':>9}")
        for rank, row in enumerate(summary["sng"]["leaderboard"], 1):
            print(f"  {rank:>2}  {row['bot']:<24} {row['avg_points']:>7.3f} {row['wins']:>5} {row['games']:>6} {row['avg_place']:>9.2f}")
        print()
    print(f"Leaderboard: {out_dir / 'leaderboard.md'}")
    return 0


def _cmd_table(args) -> int:
    """poker watch / play / results: the browser app."""
    from .arena import MAX_COMPARE, MAX_SEATS, SessionConfig
    from .loader import BUILTIN_BOTS, find_bot_root
    from .tournament import discover_entries
    from .webapp import App, serve

    builtin = {p.name: p.resolve() for p in sorted(BUILTIN_BOTS.iterdir()) if (p / "bot.py").is_file()}
    available = dict(builtin)
    mine = Path("my_bots")  # your own bots show up in the app automatically
    if mine.is_dir():
        for folder in sorted(mine.iterdir()):
            root = find_bot_root(folder) if folder.is_dir() else None
            if root:
                available.setdefault(folder.name, root.resolve())
    chosen = []
    for entry in discover_entries(args.bots) if args.bots else []:
        available[entry.name] = entry.path
        chosen.append(entry.name)
    chosen = chosen or list(available)
    mode = "play" if args.command == "play" else "watch"
    room = MAX_SEATS - (mode == "play")
    if len(chosen) > room:
        print(f"Only {room} bots fit at the table, so seating: {', '.join(chosen[:room])}")
        chosen = chosen[:room]
    compare = list(dict.fromkeys(chosen + list(builtin)))[:MAX_COMPARE]
    config = SessionConfig(
        mode=mode,
        format=args.format,
        bots=chosen,
        compare=compare if mode == "play" and args.format == "cash" else [],
        player_name=getattr(args, "name", "You"),
        stack=args.stack,
        small_blind=args.blinds[0],
        big_blind=args.blinds[1],
        hands_per_level=args.hands_per_level,
        speed=args.speed,
    )
    app = App(available, set(builtin), config, results_dir=Path(args.results), hand_log=getattr(args, "log", None))
    if args.command == "results":
        app.session.control(paused=True)  # just looking at results: don't spend CPU on a table
    serve(app, port=args.port, open_browser=not args.no_browser, page="#results" if args.command == "results" else "")
    return 0


def _cmd_collect(args) -> int:
    from .collect import collect

    entries_csv = Path(args.entries)
    out = Path(args.out) if args.out else entries_csv.parent
    manifest = collect(entries_csv, out, ref=args.ref)
    return 1 if any("error" in e for e in manifest["entries"].values()) else 0


def _blinds(text: str) -> tuple[int, int]:
    try:
        small, big = (int(x) for x in text.split("/"))
    except ValueError:
        raise argparse.ArgumentTypeError("write blinds as SMALL/BIG, e.g. 50/100") from None
    if not 0 < small <= big:
        raise argparse.ArgumentTypeError("need 0 < small blind <= big blind")
    return small, big


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="poker", description="Build, test and run poker bots for the tournament.")
    parser.add_argument("--version", action="version", version=f"poker-tournament {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    def timing(p):
        p.add_argument("--timeout", type=float, default=1.0, help="seconds allowed per decision (default 1)")
        p.add_argument("--startup-timeout", type=float, default=60.0, help="seconds allowed to load (default 60)")

    p = sub.add_parser("new", help="create a new bot from the starter template")
    p.add_argument("path", help="folder to create, e.g. my_bots/alice_bot")
    p.set_defaults(func=_cmd_new)

    p = sub.add_parser("validate", help="check a bot works the way the tournament needs")
    p.add_argument("bots", nargs="+", help="bot folders or zips (or a folder of submissions)")
    p.add_argument("--hands", type=int, default=100, help="deals to play against random_bot (default 100)")
    timing(p)
    p.set_defaults(func=_cmd_validate)

    p = sub.add_parser("package", help="validate a bot and zip it for submission")
    p.add_argument("bot", help="your bot's folder")
    p.add_argument("-o", "--output", help="zip file to write (default: <folder name>.zip here)")
    p.add_argument("--skip-validate", action="store_true", help="zip without checking first")
    p.set_defaults(func=_cmd_package)

    p = sub.add_parser("match", help="play a duplicate heads-up match between two bots")
    p.add_argument("bot_a", help="bot folder, zip, or name of a bot in bots/")
    p.add_argument("bot_b", help="bot folder, zip, or name of a bot in bots/")
    p.add_argument("--hands", type=int, default=500, help="deals, each played from both seats (default 500)")
    p.add_argument("--seed", type=int, help="card seed, to replay the same deals (default: random)")
    p.add_argument("--stack", type=int, default=10_000, help="chips each player starts every hand with (default 10000)")
    p.add_argument("--blinds", type=_blinds, default=(50, 100), help="SMALL/BIG (default 50/100)")
    p.add_argument("--in-process", action="store_true", help="run both bots in this process: faster, no isolation")
    p.add_argument("--log", help="write every hand, with all hole cards, to this .jsonl (or .jsonl.gz) file")
    timing(p)
    p.set_defaults(func=_cmd_match)

    p = sub.add_parser("run", help="run a tournament between many bots")
    p.add_argument("paths", nargs="+", help="a folder of submissions and/or bot folders, zips or built-in bot names")
    p.add_argument("--name", help="name for this run, e.g. round-1 (default: date and time)")
    p.add_argument("--format", choices=["heads-up", "sng", "both"], default="heads-up", help="default heads-up")
    p.add_argument("--hands", type=int, default=1000, help="deals per heads-up pairing, each played from both seats (default 1000)")
    p.add_argument("--games", type=int, default=50, help="sit-and-go games (default 50)")
    p.add_argument("--table-size", type=int, default=6, help="players per sit-and-go table (default 6)")
    p.add_argument("--hands-per-level", type=int, default=20, help="sit-and-go hands between blind increases (default 20)")
    p.add_argument("--seed", type=int, help="card seed (default: a new random one, which keeps the deals secret)")
    p.add_argument("--stack", type=int, default=10_000, help="starting chips (default 10000)")
    p.add_argument("--blinds", type=_blinds, default=(50, 100), help="heads-up blinds SMALL/BIG (default 50/100)")
    p.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) // 2), help="matches to play at once")
    p.add_argument("--out", default="results", help="folder for results (default results/)")
    p.add_argument("--resume", action="store_true", help="reuse matches already saved for this run name")
    p.add_argument("--full-hand-logs", action="store_true", help="include every hole card in the hand logs")
    p.add_argument("--no-hand-logs", action="store_true", help="don't save hand histories")
    p.add_argument("--in-process", action="store_true", help="no isolation (only for quick local tests)")
    timing(p)
    p.set_defaults(func=_cmd_run)

    def table_options(p, default_speed):
        p.add_argument("bots", nargs="*", help="bots to seat: folders, zips, names in bots/ or a submissions folder (default: every bot in bots/ and my_bots/)")
        p.add_argument("--format", choices=["cash", "sng"], default="cash", help="cash: chips reset every hand (default); sng: last one standing")
        p.add_argument("--stack", type=int, default=10_000, help="starting chips (default 10000)")
        p.add_argument("--blinds", type=_blinds, default=(50, 100), help="SMALL/BIG (default 50/100)")
        p.add_argument("--hands-per-level", type=int, default=10, help="sit-and-go hands between blind increases (default 10)")
        p.add_argument("--speed", choices=["slow", "normal", "fast", "turbo", "instant"], default=default_speed, help=f"how fast bots act (default {default_speed})")
        p.add_argument("--results", default="results", help="folder of tournament results for the Results tab (default results/)")
        p.add_argument("--port", type=int, default=8790, help="local port for the page (default 8790)")
        p.add_argument("--no-browser", action="store_true", help="don't open the browser automatically")
        p.set_defaults(func=_cmd_table)

    p = sub.add_parser("watch", help="watch bots play at a table in your browser")
    table_options(p, "normal")

    p = sub.add_parser("play", help="play against bots in your browser and compare yourself with them")
    table_options(p, "normal")
    p.add_argument("--name", default="You", help="your name at the table (default You)")
    p.add_argument("--log", help="save every hand you play, with all cards, to this .jsonl (or .jsonl.gz) file")

    p = sub.add_parser("results", help="look at tournament leaderboards in your browser")
    table_options(p, "normal")

    p = sub.add_parser("collect", help="fetch submissions listed in a CSV (git repos, zips, folders)")
    p.add_argument("entries", help="CSV with columns name,source[,ref][,subdir]")
    p.add_argument("--out", help="folder to put the bots in (default: the CSV's folder)")
    p.add_argument("--ref", help="tag, branch or commit to use when a row doesn't give one, e.g. round-1")
    p.set_defaults(func=_cmd_collect)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except BotLoadError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
