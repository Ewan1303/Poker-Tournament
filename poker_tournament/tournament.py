"""Running a tournament round: every bot plays every other bot.

Main event: a heads-up duplicate round robin. Each pair of bots plays
`hands` deals twice (seats swapped), and bots are ranked by big blinds won
per 100 hands averaged over their opponents.

Optional side event: sit-and-go games at tables of up to `table_size` bots,
ranked by average points (1 for a win down to 0 for finishing last).

Each pairing or game is saved as it finishes, so an interrupted run can be
picked up again with resume=True.
"""

from __future__ import annotations

import itertools
import json
import math
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from .loader import BotLoadError, extract_zip, find_bot_root, resolve_bot
from .match import SNG_LEVELS, HandLog, duplicate_result, play_heads_up, play_sng, write_hand_records
from .players import BotPlayer, BotStats, LocalBot, ProcessBot


@dataclass
class Entry:
    name: str
    path: Path


@dataclass
class TournamentConfig:
    name: str
    formats: tuple[str, ...] = ("heads_up",)  # "heads_up" and/or "sng"
    hands: int = 1000  # deals per heads-up pairing; each is played from both seats
    seed: int = 0
    stack: int = 10_000
    small_blind: int = 50
    big_blind: int = 100
    games: int = 50  # sit-and-go games
    table_size: int = 6
    hands_per_level: int = 20
    act_timeout: float = 1.0
    startup_timeout: float = 60.0
    max_failures: int = 25
    workers: int = 1
    in_process: bool = False
    hand_logs: bool = True
    full_info_logs: bool = False

    def heads_up_key(self) -> dict:
        """Settings that must match for a saved half-match to be reused."""
        return {"hands": self.hands, "seed": self.seed, "stack": self.stack, "blinds": [self.small_blind, self.big_blind]}

    def sng_key(self) -> dict:
        return {"seed": self.seed, "stack": self.stack, "table_size": self.table_size, "hands_per_level": self.hands_per_level}


def safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._") or "bot"


def discover_entries(paths: list[str | Path]) -> list[Entry]:
    """Bots from a list of paths. Each path can be a bot folder, a .zip, the name of
    a bot in bots/, or a folder of submissions (every bot folder and .zip inside)."""
    entries: list[Entry] = []
    for spec in paths:
        path = Path(spec)
        if path.is_dir() and not (path / "bot.py").exists():
            # A folder of submissions. `poker collect` records where each bot is in manifest.json.
            collected = {}
            if (path / "manifest.json").exists():
                manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
                collected = {name: e["bot_dir"] for name, e in manifest.get("entries", {}).items() if e.get("bot_dir")}
            children = [c for c in sorted(path.iterdir()) if not c.name.startswith((".", "_"))]
            problems = []
            found = 0
            for child in children:
                if child.is_dir():
                    root = Path(collected[child.name]) if child.name in collected else find_bot_root(child)
                    if root is None or not (root / "bot.py").is_file():
                        problems.append(child.name)
                        continue
                    entries.append(Entry(safe_name(child.name), root.resolve()))
                    found += 1
                elif child.suffix.lower() == ".zip":
                    entries.append(Entry(safe_name(child.stem), extract_zip(child, path / ".unzipped" / child.stem).resolve()))
                    found += 1
            if problems:
                raise BotLoadError(
                    f"can't tell where the bot is in {', '.join(problems)} (inside {path}): each needs exactly one "
                    "bot.py, or a subdir column in entries.csv. Fix or remove them so nobody is left out."
                )
            if not found:
                raise BotLoadError(f"no bots found in {path}")
        else:
            root = resolve_bot(spec)
            name = path.stem if path.suffix.lower() == ".zip" else (path.name if path.exists() else str(spec))
            entries.append(Entry(safe_name(name), root))
    names = [e.name for e in entries]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise BotLoadError(f"two bots share a name: {', '.join(duplicates)} (rename a folder)")
    return entries


def make_player(entry: Entry, config: TournamentConfig) -> BotPlayer:
    if config.in_process:
        return LocalBot(entry.path, entry.name, max_failures=config.max_failures)
    return ProcessBot(
        entry.path,
        entry.name,
        act_timeout=config.act_timeout,
        startup_timeout=config.startup_timeout,
        max_failures=config.max_failures,
    )


def _write_outputs(players: list[BotPlayer], logs_dir: Path, label: str) -> None:
    """Save what each bot printed (and its errors) for the organizer."""
    for player in players:
        text = getattr(player, "output", "")
        if player.stats.errors:
            text += "\n\n--- errors ---\n" + "\n\n".join(player.stats.errors)
        if text.strip():
            logs_dir.mkdir(parents=True, exist_ok=True)
            (logs_dir / f"{player.name}__{label}.log").write_text(text, encoding="utf-8")


def run_tournament(
    entries: list[Entry],
    config: TournamentConfig,
    out_dir: Path,
    resume: bool = False,
    echo: Callable[[str], None] = print,
) -> dict:
    """Run every format in config.formats, write results.json and leaderboard.md, return the summary."""
    if len(entries) < 2:
        raise ValueError("a tournament needs at least two bots")
    if config.in_process:
        config.workers = 1  # in-process bots share one interpreter, so run them one at a time
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "run.json").write_text(json.dumps(asdict(config), indent=2), encoding="utf-8")
    summary = {
        "name": config.name,
        "created": datetime.now().isoformat(timespec="seconds"),
        "config": asdict(config),
        "bots": [{"name": e.name, "path": str(e.path)} for e in entries],
    }
    if "heads_up" in config.formats:
        summary["heads_up"] = _run_heads_up(entries, config, out_dir, resume, echo)
    if "sng" in config.formats:
        summary["sng"] = _run_sng(entries, config, out_dir, resume, echo)
    (out_dir / "results.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (out_dir / "leaderboard.md").write_text(leaderboard_markdown(summary), encoding="utf-8")
    return summary


# -- heads-up round robin -------------------------------------------------------


def _run_heads_up(entries, config, out_dir, resume, echo) -> dict:
    pairings = list(itertools.combinations(entries, 2))
    results: dict[tuple[str, str], list[dict]] = {}
    todo = []
    for a, b in pairings:
        saved = out_dir / "matches" / f"{a.name}__vs__{b.name}.json"
        if resume and saved.exists():
            data = json.loads(saved.read_text(encoding="utf-8"))
            if data.get("key") == config.heads_up_key():
                results[(a.name, b.name)] = data["halves"]
                continue
        todo += [(a, b, 1), (a, b, 2)]
    echo(
        f"Heads-up round robin: {len(entries)} bots, {len(pairings)} pairings, "
        f"{config.hands} deals x 2 seats each ({len(todo)} of {2 * len(pairings)} half-matches to play)"
    )

    def play(task):
        a, b, half = task
        return task, _play_half(a, b, half, config)

    finished: dict[tuple[str, str], dict[int, dict]] = {}
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=max(1, config.workers)) as pool:
        futures = [pool.submit(play, task) for task in todo]
        for done, future in enumerate(as_completed(futures), 1):
            (a, b, half), result = future.result()
            elapsed = time.perf_counter() - started
            remaining = elapsed / done * (len(todo) - done)
            echo(
                f"  [{done}/{len(todo)}] {a.name} vs {b.name} (half {half}): "
                f"{a.name} {sum(result['a_nets']):+,} chips in {result['seconds']:.0f}s  (about {remaining / 60:.0f} min left)"
            )
            halves = finished.setdefault((a.name, b.name), {})
            halves[half] = result
            if len(halves) == 2:
                results[(a.name, b.name)] = _save_pairing(a, b, halves, config, out_dir)
    return _summarize_heads_up(entries, results, config)


def _play_half(a: Entry, b: Entry, half: int, config: TournamentConfig) -> dict:
    """Half 1 seats A in seat 0, half 2 swaps them. Fresh processes each time.

    Each pairing gets its own deals, and the hand histories stay in memory until
    both halves are over, so no running bot can read the deals from a log.
    """
    seats = [a, b] if half == 1 else [b, a]
    players = [make_player(e, config) for e in seats]
    hand_log = HandLog(full_info=config.full_info_logs) if config.hand_logs else None
    started = time.perf_counter()
    try:
        for player in players:
            player.start()
        nets = play_heads_up(
            players,
            config.hands,
            f"{config.seed}:{a.name}:{b.name}",
            stack=config.stack,
            blinds=(config.small_blind, config.big_blind),
            act_timeout=None if config.in_process else config.act_timeout,
            hand_log=hand_log,
            log_extra={"match": f"{a.name} vs {b.name}", "half": half},
        )
    finally:
        for player in players:
            player.close()
    return {
        "half": half,
        "a_nets": nets if half == 1 else [-x for x in nets],
        "seconds": time.perf_counter() - started,
        "stats": {p.name: p.stats.to_dict() for p in players},
        "players": players,
        "hands": hand_log.records if hand_log else [],
    }


def _save_pairing(a: Entry, b: Entry, halves: dict[int, dict], config: TournamentConfig, out_dir: Path) -> list[dict]:
    """Once both halves are over: write the hand histories, bot output and result."""
    label = f"{a.name}__vs__{b.name}"
    if config.hand_logs:
        write_hand_records(out_dir / "hands" / f"{label}.jsonl.gz", halves[1]["hands"] + halves[2]["hands"])
    for half in (1, 2):
        _write_outputs(halves[half]["players"], out_dir / "logs", f"{label}__half{half}")
    kept = [{k: v for k, v in halves[half].items() if k not in ("players", "hands")} for half in (1, 2)]
    saved = out_dir / "matches" / f"{label}.json"
    saved.parent.mkdir(parents=True, exist_ok=True)
    saved.write_text(json.dumps({"key": config.heads_up_key(), "a": a.name, "b": b.name, "halves": kept}), encoding="utf-8")
    return kept


def _summarize_heads_up(entries, results, config) -> dict:
    names = [e.name for e in entries]
    stats = {n: BotStats() for n in names}
    head_to_head: dict[str, dict[str, dict]] = {n: {} for n in names}
    matches = []
    for a, b in itertools.combinations(names, 2):
        if (a, b) not in results:
            continue
        first, second = results[(a, b)]
        for half in (first, second):
            for bot, s in half["stats"].items():
                stats[bot].add(BotStats.from_dict(s))
        match = duplicate_result(a, b, first["a_nets"], second["a_nets"], config.big_blind)
        value, error = match.bb_per_100, match.stderr_bb_per_100
        matches.append({"a": a, "b": b, "hands": match.hands, "a_chips": match.chips, "a_bb_per_100": value, "stderr": error})
        head_to_head[a][b] = {"bb_per_100": value, "stderr": error}
        head_to_head[b][a] = {"bb_per_100": -value, "stderr": error}
    table = []
    for name in names:
        opponents = head_to_head[name]
        if not opponents:
            continue
        values = [o["bb_per_100"] for o in opponents.values()]
        s = stats[name]
        table.append(
            {
                "bot": name,
                "bb_per_100": sum(values) / len(values),
                "stderr": math.sqrt(sum(o["stderr"] ** 2 for o in opponents.values())) / len(values),
                "won": sum(v > 0 for v in values),
                "lost": sum(v < 0 for v in values),
                "opponents": len(values),
                "avg_ms": s.avg_think_ms,
                "max_ms": 1000 * s.max_think,
                "failures": s.failures,
                "timeouts": s.timeouts,
                "corrections": s.corrections,
                "cut_off": s.cut_off,
                "errors": s.errors[:2],
            }
        )
    table.sort(key=lambda row: row["bb_per_100"], reverse=True)
    return {"leaderboard": table, "head_to_head": head_to_head, "matches": matches}


# -- sit-and-go ---------------------------------------------------------------


def _run_sng(entries, config, out_dir, resume, echo) -> dict:
    table_size = max(2, min(config.table_size, len(entries)))
    rng = random.Random(f"{config.seed}:sng-schedule")
    played = {e.name: 0 for e in entries}
    schedule = []
    for _ in range(config.games):
        # Bots that have played least go first, so everyone plays about as often.
        order = sorted(entries, key=lambda e: (played[e.name], rng.random()))
        table = order[:table_size]
        rng.shuffle(table)
        for e in table:
            played[e.name] += 1
        schedule.append(table)

    saved_dir = out_dir / "sng"
    results: dict[int, dict] = {}
    todo = []
    for game, table in enumerate(schedule, 1):
        saved = saved_dir / f"game{game:03d}.json"
        if resume and saved.exists():
            data = json.loads(saved.read_text(encoding="utf-8"))
            if data.get("key") == config.sng_key() and data.get("names") == [e.name for e in table]:
                results[game] = data
                continue
        todo.append((game, table))
    echo(f"Sit-and-go: {config.games} games at tables of {table_size} ({len(todo)} to play)")

    def play(task):
        game, table = task
        return game, _play_sng_game(game, table, config, out_dir)

    with ThreadPoolExecutor(max_workers=max(1, config.workers)) as pool:
        futures = [pool.submit(play, task) for task in todo]
        for done, future in enumerate(as_completed(futures), 1):
            game, result = future.result()
            results[game] = result
            winner = result["names"][result["places"].index(min(result["places"]))]
            echo(f"  [{done}/{len(todo)}] game {game}: {winner} won after {result['hands']} hands")
    return _summarize_sng(entries, results)


def _play_sng_game(game: int, table: list[Entry], config: TournamentConfig, out_dir: Path) -> dict:
    label = f"game{game:03d}"
    players = [make_player(e, config) for e in table]
    hand_log = HandLog(out_dir / "hands" / f"sng_{label}.jsonl.gz", config.full_info_logs) if config.hand_logs else None
    started = time.perf_counter()
    try:
        for player in players:
            player.start()
        outcome = play_sng(
            players,
            f"{config.seed}:sng:{game}",
            stack=config.stack,
            levels=SNG_LEVELS,
            hands_per_level=config.hands_per_level,
            act_timeout=None if config.in_process else config.act_timeout,
            hand_log=hand_log,
            log_extra={"game": game},
        )
    finally:
        for player in players:
            player.close()
        if hand_log:
            hand_log.close()
    _write_outputs(players, out_dir / "logs", f"sng_{label}")
    result = dict(outcome, key=config.sng_key(), seconds=time.perf_counter() - started)
    result["stats"] = {p.name: p.stats.to_dict() for p in players}
    saved = out_dir / "sng" / f"{label}.json"
    saved.parent.mkdir(parents=True, exist_ok=True)
    saved.write_text(json.dumps(result), encoding="utf-8")
    return result


def _summarize_sng(entries, results) -> dict:
    rows = {e.name: {"bot": e.name, "games": 0, "wins": 0, "points": 0.0, "place_sum": 0.0, "stats": BotStats()} for e in entries}
    for result in results.values():
        n = len(result["names"])
        for name, place in zip(result["names"], result["places"]):
            row = rows[name]
            row["games"] += 1
            row["wins"] += place == 1
            row["place_sum"] += place
            row["points"] += (n - place) / (n - 1)
        for name, s in result["stats"].items():
            rows[name]["stats"].add(BotStats.from_dict(s))
    table = []
    for row in rows.values():
        if not row["games"]:
            continue
        s = row.pop("stats")
        games = row["games"]
        table.append(
            {
                "bot": row["bot"],
                "games": games,
                "wins": row["wins"],
                "avg_points": row["points"] / games,
                "avg_place": row["place_sum"] / games,
                "avg_ms": s.avg_think_ms,
                "failures": s.failures,
                "cut_off": s.cut_off,
                "errors": s.errors[:2],
            }
        )
    table.sort(key=lambda row: (row["avg_points"], row["wins"]), reverse=True)
    return {"leaderboard": table, "games": len(results)}


# -- report -------------------------------------------------------------------


def leaderboard_markdown(summary: dict) -> str:
    config = summary["config"]
    big = config["big_blind"]
    lines = [f"# {summary['name']}", "", f"Run {summary['created'].replace('T', ' ')} with {len(summary['bots'])} bots.", ""]
    heads_up = summary.get("heads_up")
    if heads_up:
        lines += [
            "## Heads-up round robin (main event)",
            "",
            f"Every pair of bots played {config['hands']:,} deals from both seats ({2 * config['hands']:,} hands), "
            f"starting each hand with {config['stack'] // big} big blinds (blinds {config['small_blind']}/{big}), seed {config['seed']}.",
            "",
            "| Rank | Bot | bb/100 | 95% range | Matches won | Lost | Avg decision | Errors |",
            "|---:|---|---:|---:|---:|---:|---:|---:|",
        ]
        for rank, row in enumerate(heads_up["leaderboard"], 1):
            errors = f"{row['failures']}" + (" (cut off)" if row["cut_off"] else "")
            lines.append(
                f"| {rank} | {row['bot']} | {row['bb_per_100']:+.1f} | ±{1.96 * row['stderr']:.1f} | "
                f"{row['won']} of {row['opponents']} | {row['lost']} | {row['avg_ms']:.1f} ms | {errors} |"
            )
        lines += [
            "",
            "bb/100 is big blinds won per 100 hands, averaged over every opponent. "
            "The 95% range shows how much of that could be luck.",
            "",
            "### Head to head",
            "",
            "bb/100 won by the row bot against the column bot.",
            "",
        ]
        order = [row["bot"] for row in heads_up["leaderboard"]]
        lines.append("| | " + " | ".join(order) + " |")
        lines.append("|---|" + "---:|" * len(order))
        for name in order:
            cells = []
            for other in order:
                cell = heads_up["head_to_head"][name].get(other)
                cells.append("—" if cell is None else f"{cell['bb_per_100']:+.1f}")
            lines.append(f"| **{name}** | " + " | ".join(cells) + " |")
        lines.append("")
    sng = summary.get("sng")
    if sng:
        lines += [
            "## Sit-and-go",
            "",
            f"{sng['games']} games at tables of up to {config['table_size']}, starting stacks {config['stack']:,}, "
            f"blinds up every {config['hands_per_level']} hands. Points: 1 for a win down to 0 for last place.",
            "",
            "| Rank | Bot | Avg points | Wins | Games | Avg place | Avg decision | Errors |",
            "|---:|---|---:|---:|---:|---:|---:|---:|",
        ]
        for rank, row in enumerate(sng["leaderboard"], 1):
            errors = f"{row['failures']}" + (" (cut off)" if row["cut_off"] else "")
            lines.append(
                f"| {rank} | {row['bot']} | {row['avg_points']:.3f} | {row['wins']} | {row['games']} | "
                f"{row['avg_place']:.2f} | {row['avg_ms']:.1f} ms | {errors} |"
            )
        lines.append("")
    problems = []
    for section in ("heads_up", "sng"):
        for row in (summary.get(section) or {}).get("leaderboard", []):
            for error in row["errors"]:
                problems.append(f"- **{row['bot']}**: {error.strip().splitlines()[-1]}")
    if problems:
        lines += ["## Bot errors", ""] + sorted(set(problems)) + [""]
    return "\n".join(lines)
