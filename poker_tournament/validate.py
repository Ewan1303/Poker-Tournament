"""Checking a bot before it is submitted, and zipping it up."""

from __future__ import annotations

import ast
import sys
import textwrap
import zipfile
from pathlib import Path
from typing import Callable

from .loader import MAX_BOT_MB, BotLoadError, folder_size, iter_bot_files, resolve_bot
from .match import SNG_LEVELS, duplicate_result, play_heads_up, play_sng
from .players import BotStats, ProcessBot

# Third-party packages bots may import (keep in step with requirements-bots.txt).
ALLOWED_IMPORTS = {"poker_tournament", "numpy", "scipy", "sklearn", "pandas", "torch", "joblib"}
MB = 1024 * 1024


def format_size(size: int) -> str:
    return f"{size / MB:.1f} MB" if size >= MB else f"{max(1, round(size / 1024))} KB"


def scan_imports(folder: Path) -> tuple[set[str], list[str]]:
    """Top-level modules the bot imports that are not standard library, its own
    files or allowed packages; plus any syntax errors."""
    folder = Path(folder)
    files = [p for p in iter_bot_files(folder) if p.suffix == ".py"]
    local = {p.stem for p in files} | {p.relative_to(folder).parts[0] for p in files}
    unknown: set[str] = set()
    errors = []
    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
        except SyntaxError as exc:
            errors.append(f"{path.relative_to(folder)} line {exc.lineno}: {exc.msg}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules = [node.module]
            else:
                continue
            for module in modules:
                top = module.split(".")[0]
                if top not in sys.stdlib_module_names and top not in local and top not in ALLOWED_IMPORTS:
                    unknown.add(top)
    return unknown, errors


def validate_bot(
    spec: str | Path,
    *,
    hands: int = 100,
    act_timeout: float = 1.0,
    startup_timeout: float = 60.0,
    echo: Callable[[str], None] = print,
) -> bool:
    """Run the checks the organizer will run. Prints a report; returns True if it passed."""
    passed = True

    def ok(message: str) -> None:
        echo(f"  OK    {message}")

    def warn(message: str) -> None:
        echo(f"  WARN  {message}")

    def fail(message: str) -> None:
        nonlocal passed
        passed = False
        echo(f"  FAIL  {message}")

    try:
        path = resolve_bot(spec)
    except BotLoadError as exc:
        echo(f"Checking {spec}")
        fail(str(exc))
        return False
    echo(f"Checking {path}")

    size = folder_size(path)
    if size > MAX_BOT_MB * MB:
        fail(f"the folder is {format_size(size)}; the limit is {MAX_BOT_MB} MB (keep training data out of it)")
    else:
        ok(f"size {format_size(size)} (limit {MAX_BOT_MB} MB)")

    unknown, syntax_errors = scan_imports(path)
    for error in syntax_errors:
        fail(f"syntax error in {error}")
    if unknown:
        warn(
            f"imports {', '.join(sorted(unknown))}, which the tournament may not have installed "
            "(allowed: the standard library, " + ", ".join(sorted(ALLOWED_IMPORTS - {'poker_tournament'})) + ")"
        )
    elif not syntax_errors:
        ok("imports only the standard library and allowed packages")

    stats = BotStats()
    printed = []

    def player(bot_path: Path, name: str) -> ProcessBot:
        return ProcessBot(bot_path, name, act_timeout=act_timeout, startup_timeout=startup_timeout, max_failures=50)

    # Heads-up: a duplicate match against random_bot, fresh processes per half.
    halves = []
    for half in (1, 2):
        mine, other = player(path, "your_bot"), player(resolve_bot("random_bot"), "random_bot")
        seats = [mine, other] if half == 1 else [other, mine]
        try:
            for p in seats:
                p.start()
            if mine.dead:
                fail("the bot did not start:\n" + textwrap.indent("\n".join(mine.stats.errors), "        "))
                return False
            if half == 1:
                ok(f"started in {mine.stats.startup_time:.1f}s (limit {startup_timeout:g}s)")
            nets = play_heads_up(seats, hands, "validate", act_timeout=act_timeout)
        finally:
            for p in seats:
                p.close()
        halves.append(nets if half == 1 else [-x for x in nets])
        stats.add(mine.stats)
        printed.append(mine.output)
    match = duplicate_result("your_bot", "random_bot", halves[0], halves[1], 100)

    # A short three-player sit-and-go with fast blinds, to exercise side pots.
    table = [player(path, "your_bot"), player(resolve_bot("random_bot"), "random_bot"), player(resolve_bot("calling_station"), "calling_station")]
    try:
        for p in table:
            p.start()
        outcome = play_sng(table, "validate:sng", levels=SNG_LEVELS, hands_per_level=5, max_hands=400, act_timeout=act_timeout)
    finally:
        for p in table:
            p.close()
    stats.add(table[0].stats)
    printed.append(table[0].output)
    log_tail = "".join(printed).strip().splitlines()[-15:]

    total_hands = 2 * hands + outcome["hands"]
    if stats.failures:
        fail(f"{stats.failures} errors or timeouts in {total_hands} hands ({stats.timeouts} timeouts). First one:")
        echo(textwrap.indent(stats.errors[0], "        "))
    else:
        ok(f"played {total_hands} hands ({stats.decisions} decisions) with no errors or timeouts")
    if stats.cut_off:
        fail("the bot was cut off after too many errors")
    if stats.corrections:
        example = stats.correction_examples[0]
        requested = example["requested"]
        asked = requested["type"] + (f" to {requested['amount']}" if requested["type"] == "raise" else "")
        warn(
            f"{stats.corrections} illegal action{' was' if stats.corrections == 1 else 's were'} changed by the engine, "
            f"e.g. on the {example['street']} with {example['to_call']} to call "
            f"(legal: {', '.join(example['legal_actions'])}) it asked to {asked} and got '{example['applied']}'"
        )
    avg_ms, max_ms = stats.avg_think_ms, 1000 * stats.max_think
    message = f"decision time: average {avg_ms:.1f} ms, slowest {max_ms:.0f} ms (limit {1000 * act_timeout:.0f} ms)"
    if max_ms > 500 * act_timeout or avg_ms > 100:
        warn(message + "; tournaments run many thousands of hands, so faster is better")
    else:
        ok(message)
    if log_tail and stats.failures:
        echo("  Last lines your bot printed (its stderr/print output):")
        echo(textwrap.indent("\n".join(log_tail), "        "))

    echo(
        f"  INFO  against random_bot: {match.bb_per_100:+.1f} bb/100 over {match.hands} hands; "
        f"3-player sit-and-go: finished {_ordinal(outcome['places'][0])} of 3"
    )
    if match.bb_per_100 < 0:
        warn("your bot lost to random_bot; that usually means a bug")
    echo("PASSED: ready to submit." if passed else "FAILED: fix the problems above before submitting.")
    return passed


def _ordinal(place: float) -> str:
    if place != int(place):
        return f"joint {place:g}"
    place = int(place)
    return f"{place}{'st' if place == 1 else 'nd' if place == 2 else 'rd' if place == 3 else 'th'}"


def package_bot(spec: str | Path, out: str | Path | None = None) -> Path:
    """Zip a bot folder (skipping caches and .git) into <name>.zip. Returns the zip's path."""
    path = resolve_bot(spec)
    if out is None:
        # Default to the current folder, or beside the bot if we're inside it.
        here = Path.cwd().resolve()
        out = (path.parent if here.is_relative_to(path) else here) / f"{path.name}.zip"
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    files = [f for f in iter_bot_files(path) if f.resolve() != out.resolve()]
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for file in files:
            archive.write(file, Path(path.name) / file.relative_to(path))
    size = out.stat().st_size
    if size > MAX_BOT_MB * MB:
        out.unlink()
        raise BotLoadError(f"the zip would be {size / MB:.1f} MB; the limit is {MAX_BOT_MB} MB")
    return out
