"""Running a tournament round.

Main event: many freezeout tournaments, every bot in each one. Everyone
starts with the same chips; a tournament ends when one bot has every chip or
after `max_hands` hands, when the rest are ranked by chips. Bots are ranked
by tournaments won, then by average finishing place.

Also available: a heads-up duplicate round robin. Each pair of bots plays
`hands` deals twice (seats swapped), and bots are ranked by big blinds won
per 100 hands averaged over their opponents.

Finished tournaments and pairings are saved as they go, so an interrupted run
can be picked up again with resume=True.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from .loader import BotLoadError, extract_zip, find_bot_root, resolve_bot
from .match import HandLog, blind_levels, duplicate_result, play_heads_up, play_tournament, write_hand_records
from .players import BotPlayer, BotStats, LocalBot, ProcessBot, kill_all_bots


@dataclass
class Entry:
    name: str
    path: Path


@dataclass
class TournamentConfig:
    name: str
    formats: tuple[str, ...] = ("tournament",)  # "tournament" and/or "heads_up"
    tournaments: int = 100  # freezeout tournaments to play
    max_hands: int | None = 100  # hands per table before a tournament is decided on chips (None: play to the end)
    table_size: int = 10
    hands_per_level: int = 20
    hands: int = 1000  # deals per heads-up pairing; each is played from both seats
    seed: int = 0
    stack: int = 10_000
    small_blind: int = 50
    big_blind: int = 100
    act_timeout: float = 1.0
    startup_timeout: float = 60.0
    max_failures: int = 25
    workers: int = 1
    in_process: bool = False
    hand_logs: bool = True
    full_info_logs: bool = False

    def seed_hash(self) -> str:
        """Identifies the seed without giving it away (the shuffle code is public)."""
        return hashlib.sha256(f"poker-tournament:{self.seed}".encode()).hexdigest()[:16]

    def public_dict(self) -> dict:
        """The settings without the secret seed: safe to write to disk while bots run."""
        return dict(asdict(self), seed=None, seed_hash=self.seed_hash())

    def heads_up_key(self) -> dict:
        """Settings that must match for a saved pairing to be reused."""
        return {"hands": self.hands, "seed_hash": self.seed_hash(), "stack": self.stack, "blinds": [self.small_blind, self.big_blind]}

    def tournament_key(self) -> dict:
        return {
            "seed_hash": self.seed_hash(),
            "stack": self.stack,
            "blinds": [self.small_blind, self.big_blind],
            "table_size": self.table_size,
            "hands_per_level": self.hands_per_level,
            "max_hands": self.max_hands,
        }


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
                    root = path / collected[child.name] if child.name in collected else find_bot_root(child)
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
    # Names become file names, and Windows ignores case in those.
    lowered = [e.name.lower() for e in entries]
    duplicates = sorted({e.name for e in entries if lowered.count(e.name.lower()) > 1})
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
    bots = [e.name for e in entries]
    # Nothing written while bots run may contain the seed: with it, a bot could rebuild every deck.
    run_file = out_dir / "run.json"
    run_file.write_text(json.dumps(dict(config.public_dict(), bots=bots), indent=2), encoding="utf-8")
    summary = {
        "name": config.name,
        "created": datetime.now().isoformat(timespec="seconds"),
        "config": asdict(config),
        "bots": [{"name": e.name, "path": str(e.path)} for e in entries],
    }
    if "tournament" in config.formats:
        summary["tournament"] = _run_tournaments(entries, config, out_dir, resume, echo)
    if "heads_up" in config.formats:
        summary["heads_up"] = _run_heads_up(entries, config, out_dir, resume, echo)
    run_file.write_text(json.dumps(dict(asdict(config), seed_hash=config.seed_hash(), bots=bots), indent=2), encoding="utf-8")
    (out_dir / "results.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (out_dir / "leaderboard.md").write_text(leaderboard_markdown(summary), encoding="utf-8")
    return summary


def summarize_saved(out_dir: Path) -> dict:
    """A summary of a run from the matches and games saved so far: the
    leaderboard of a round still in progress. Finished runs have results.json."""
    data = json.loads((out_dir / "run.json").read_text(encoding="utf-8"))
    names = data.get("bots", [])
    config = TournamentConfig(**{k: v for k, v in data.items() if k in TournamentConfig.__dataclass_fields__})
    key_hash = data.get("seed_hash") or config.seed_hash()
    entries = [Entry(name, Path()) for name in names]
    summary = {
        "name": config.name,
        "created": datetime.fromtimestamp((out_dir / "run.json").stat().st_mtime).isoformat(timespec="seconds"),
        "config": dict(asdict(config), seed=None),
        "bots": [{"name": name} for name in names],
    }
    if "tournament" in config.formats:
        outcomes, stats = {}, {name: BotStats() for name in names}
        for path in (out_dir / "tournaments").glob("*.json"):
            saved = json.loads(path.read_text(encoding="utf-8"))
            if saved.get("key", {}).get("seed_hash") == key_hash and saved.get("bots") == names:
                outcomes.update({o["number"]: o for o in saved["outcomes"]})
                for name, s in saved["stats"].items():
                    stats[name].add(BotStats.from_dict(s))
        summary["tournament"] = _summarize_tournaments(entries, outcomes, stats, config)
        summary["tournament"]["progress"] = [len(outcomes), config.tournaments]
    if "heads_up" in config.formats:
        pairings = {}
        for path in (out_dir / "matches").glob("*.json"):
            saved = json.loads(path.read_text(encoding="utf-8"))
            if saved.get("key", {}).get("seed_hash") == key_hash:
                pairings[(saved["a"], saved["b"])] = saved["halves"]
        summary["heads_up"] = _summarize_heads_up(entries, pairings, config)
        summary["heads_up"]["progress"] = [len(pairings), len(names) * (len(names) - 1) // 2]
    return summary


def _run_tasks(tasks: list, play: Callable, workers: int, on_result: Callable) -> None:
    """Run play(task) on a thread pool, calling on_result(task, result) as each finishes.

    If anything goes wrong (or Ctrl+C), queued tasks are cancelled and running
    bots are stopped straight away, rather than playing out the rest of the round.
    """
    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    try:
        futures = {pool.submit(play, task): task for task in tasks}
        for future in as_completed(futures):
            on_result(futures[future], future.result())
    except BaseException:
        pool.shutdown(wait=False, cancel_futures=True)
        kill_all_bots()
        raise
    pool.shutdown()


# -- heads-up round robin -------------------------------------------------------


def _run_heads_up(entries, config, out_dir, resume, echo) -> dict:
    pairings = list(itertools.combinations(entries, 2))
    results: dict[tuple[str, str], list[dict]] = {}
    todo = []
    for a, b in pairings:
        saved = out_dir / "matches" / f"{a.name}__vs__{b.name}.json"
        if resume and saved.exists():
            data = json.loads(saved.read_text(encoding="utf-8"))
            if data.get("key") == config.heads_up_key() and (data.get("a"), data.get("b")) == (a.name, b.name):
                results[(a.name, b.name)] = data["halves"]
                continue
        todo += [(a, b, 1), (a, b, 2)]
    echo(
        f"Heads-up round robin: {len(entries)} bots, {len(pairings)} pairings, "
        f"{config.hands} deals x 2 seats each ({len(todo)} of {2 * len(pairings)} half-matches to play)"
    )

    finished: dict[tuple[str, str], dict[int, dict]] = {}
    started = time.perf_counter()

    def on_result(task, result):
        a, b, half = task
        done = sum(len(h) for h in finished.values()) + 1
        remaining = (time.perf_counter() - started) / done * (len(todo) - done)
        echo(
            f"  [{done}/{len(todo)}] {a.name} vs {b.name} (half {half}): "
            f"{a.name} {sum(result['a_nets']):+,} chips in {result['seconds']:.0f}s  (about {remaining / 60:.0f} min left)"
        )
        halves = finished.setdefault((a.name, b.name), {})
        halves[half] = result
        if len(halves) == 2:
            results[(a.name, b.name)] = _save_pairing(a, b, halves, config, out_dir)

    _run_tasks(todo, lambda task: _play_half(*task, config), config.workers, on_result)
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


# -- tournaments ----------------------------------------------------------------


def _run_tournaments(entries, config, out_dir, resume, echo) -> dict:
    """Play config.tournaments tournaments in batches; each batch reuses one set of
    bot processes (starting a process per tournament would be slow)."""
    total = config.tournaments
    names = [e.name for e in entries]
    size = max(1, min(20, math.ceil(total / max(1, config.workers * 4))))
    batches = [(start, min(total, start + size)) for start in range(0, total, size)]
    outcomes: dict[int, dict] = {}
    stats = {name: BotStats() for name in names}
    todo = []
    for start, end in batches:
        saved = out_dir / "tournaments" / f"{start + 1:05d}-{end:05d}.json"
        if resume and saved.exists():
            data = json.loads(saved.read_text(encoding="utf-8"))
            if data.get("key") == config.tournament_key() and data.get("bots") == names:
                outcomes.update({o["number"]: o for o in data["outcomes"]})
                for name, s in data["stats"].items():
                    stats[name].add(BotStats.from_dict(s))
                continue
        todo.append((start, end))
    tables = math.ceil(len(entries) / config.table_size)
    seating = "at one table" if tables == 1 else f"starting at {tables} tables that merge into a final table"
    limit = f"up to {config.max_hands} hands each" if config.max_hands else "played to the end"
    remaining = sum(end - start for start, end in todo)
    echo(f"Tournaments: {total} with all {len(entries)} bots {seating}, {limit} ({remaining} to play)")
    started = time.perf_counter()

    def on_result(task, batch):
        start, end = task
        saved = out_dir / "tournaments" / f"{start + 1:05d}-{end:05d}.json"
        saved.parent.mkdir(parents=True, exist_ok=True)
        saved.write_text(json.dumps(batch), encoding="utf-8")
        outcomes.update({o["number"]: o for o in batch["outcomes"]})
        for name, s in batch["stats"].items():
            stats[name].add(BotStats.from_dict(s))
        wins = {name: 0.0 for name in names}
        for outcome in outcomes.values():
            for name, share in _win_shares(outcome).items():
                wins[name] += share
        leaders = ", ".join(f"{name} {wins[name]:g}" for name in sorted(wins, key=wins.get, reverse=True)[:3])
        done = len(outcomes)
        left = (time.perf_counter() - started) / max(1, done - (total - remaining)) * (total - done)
        echo(f"  [{done}/{total}] most wins so far: {leaders}  (about {left / 60:.0f} min left)")

    _run_tasks(todo, lambda task: _play_tournament_batch(*task, entries, config, out_dir), config.workers, on_result)
    return _summarize_tournaments(entries, outcomes, stats, config)


def _play_tournament_batch(start: int, end: int, entries: list[Entry], config: TournamentConfig, out_dir: Path) -> dict:
    label = f"tournaments_{start + 1:05d}-{end:05d}"
    players = [make_player(e, config) for e in entries]
    hand_log = HandLog(out_dir / "hands" / f"{label}.jsonl.gz", config.full_info_logs) if config.hand_logs else None
    outcomes = []
    began = time.perf_counter()
    try:
        for player in players:
            player.start()
        for number in range(start + 1, end + 1):
            for player in players:
                player.new_game()
            outcome = play_tournament(
                players,
                f"{config.seed}:tournament:{number}",
                stack=config.stack,
                levels=blind_levels(config.big_blind),
                hands_per_level=config.hands_per_level,
                table_size=config.table_size,
                max_hands=config.max_hands,
                act_timeout=None if config.in_process else config.act_timeout,
                hand_log=hand_log,
                log_extra={"tournament": number},
            )
            outcomes.append(dict(outcome, number=number))
    finally:
        for player in players:
            player.close()
        if hand_log:
            hand_log.close()
    _write_outputs(players, out_dir / "logs", label)
    return {
        "key": config.tournament_key(),
        "bots": [e.name for e in entries],
        "outcomes": outcomes,
        "seconds": time.perf_counter() - began,
        "stats": {p.name: p.stats.to_dict() for p in players},
    }


def _finish_shares(outcome: dict) -> dict[str, list[float]]:
    """For each player, the share of each finishing position they took: a shared
    place is split, so two players tied for 2nd and 3rd get half of each."""
    places = outcome["places"]
    n = len(places)
    shares = {}
    for name, place in zip(outcome["names"], places):
        tied = sum(1 for p in places if p == place)
        first = round(place - (tied - 1) / 2)
        row = [0.0] * n
        for position in range(first, first + tied):
            row[position - 1] = 1 / tied
        shares[name] = row
    return shares


def _win_shares(outcome: dict) -> dict[str, float]:
    return {name: row[0] for name, row in _finish_shares(outcome).items() if row[0]}


def _summarize_tournaments(entries, outcomes: dict[int, dict], stats: dict[str, BotStats], config) -> dict:
    names = [e.name for e in entries]
    n = len(names)
    rows = {name: {"finishes": [0.0] * n, "place_sum": 0.0, "chips": 0, "count": 0} for name in names}
    for outcome in outcomes.values():
        for name, share in _finish_shares(outcome).items():
            rows[name]["finishes"] = [a + b for a, b in zip(rows[name]["finishes"], share)]
        for name, place, chips in zip(outcome["names"], outcome["places"], outcome["stacks"]):
            rows[name]["place_sum"] += place
            rows[name]["chips"] += chips - config.stack
            rows[name]["count"] += 1
    table = []
    for name in names:
        row, s = rows[name], stats[name]
        played = row["count"]
        if not played:
            continue
        win_rate = row["finishes"][0] / played
        table.append(
            {
                "bot": name,
                "tournaments": played,
                "wins": row["finishes"][0],
                "win_rate": win_rate,
                "win_rate_range": 1.96 * math.sqrt(win_rate * (1 - win_rate) / played),
                "avg_place": row["place_sum"] / played,
                "top3_rate": sum(row["finishes"][:3]) / played,
                "avg_chips": row["chips"] / played,
                "finishes": [count / played for count in row["finishes"]],
                "avg_ms": s.avg_think_ms,
                "failures": s.failures,
                "timeouts": s.timeouts,
                "cut_off": s.cut_off,
                "errors": s.errors[:2],
            }
        )
    table.sort(key=lambda r: (-r["wins"], r["avg_place"], -r["avg_chips"]))
    played = list(outcomes.values())
    return {
        "leaderboard": table,
        "tournaments": len(played),
        "players": n,
        "avg_hands": sum(o["rounds"] for o in played) / len(played) if played else 0,
        "finished_share": sum(1 for o in played if o["finished"]) / len(played) if played else 0,
    }


# -- report -------------------------------------------------------------------


def _ordinal_word(place: int) -> str:
    return f"{place}{'th' if 10 <= place % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(place % 10, 'th')}"


def leaderboard_markdown(summary: dict) -> str:
    config = summary["config"]
    big = config["big_blind"]
    lines = [f"# {summary['name']}", "", f"Run {summary['created'].replace('T', ' ')} with {len(summary['bots'])} bots.", ""]
    tournament = summary.get("tournament")
    if tournament:
        board = tournament["leaderboard"]
        players = tournament["players"]
        tables = math.ceil(players / config["table_size"])
        seating = "at one table" if tables == 1 else f"starting at {tables} tables that merged into a final table"
        limit = (
            f"or after {config['max_hands']} hands, when everyone left is ranked by chips"
            if config.get("max_hands")
            else "and every tournament was played to the end"
        )
        lines += [
            "## Tournaments (main event)",
            "",
            f"{tournament['tournaments']:,} tournaments with all {players} bots {seating}. Everyone started with "
            f"{config['stack']:,} chips, blinds {config['small_blind']}/{big} going up every {config['hands_per_level']} hands. "
            f"A tournament ends when one bot has every chip, {limit}. "
            f"{100 * tournament['finished_share']:.0f}% were played down to one winner; "
            f"they lasted {tournament['avg_hands']:.0f} hands on average.",
            "",
            "| Rank | Bot | Wins | Win % | Avg finish | Top 3 | Avg chips won | Avg decision | Errors |",
            "|---:|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for rank, row in enumerate(board, 1):
            errors = f"{row['failures']}" + (" (cut off)" if row["cut_off"] else "")
            lines.append(
                f"| {rank} | {row['bot']} | {row['wins']:g} | {100 * row['win_rate']:.1f}% ±{100 * row['win_rate_range']:.1f} | "
                f"{row['avg_place']:.2f} | {100 * row['top3_rate']:.0f}% | {row['avg_chips']:+,.0f} | {row['avg_ms']:.1f} ms | {errors} |"
            )
        lines += [
            "",
            "Ranked by tournaments won, then average finishing place. Shared places split a win between the bots that tied.",
            "",
        ]
        if players <= 10:
            positions = [_ordinal_word(p) for p in range(1, players + 1)]
            lines += ["### Finishing positions", "", "| Bot | " + " | ".join(positions) + " |", "|---|" + "---:|" * players]
            for row in board:
                lines.append(f"| {row['bot']} | " + " | ".join(f"{100 * share:.0f}%" for share in row["finishes"]) + " |")
            lines.append("")
    heads_up = summary.get("heads_up")
    if heads_up:
        lines += [
            "## Heads-up round robin",
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
    problems = []
    for section in ("tournament", "heads_up"):
        for row in (summary.get(section) or {}).get("leaderboard", []):
            for error in row["errors"]:
                problems.append(f"- **{row['bot']}**: {error.strip().splitlines()[-1]}")
    if problems:
        lines += ["## Bot errors", ""] + sorted(set(problems)) + [""]
    return "\n".join(lines)
