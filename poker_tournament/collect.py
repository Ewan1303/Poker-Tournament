"""Gathering submissions for a round from git repositories, zips and folders.

entries.csv has a header row and one row per participant:

    name,source,ref
    alice,https://github.com/alice/alice-poker-bot,round-1
    bob,C:/Users/me/Downloads/bob_bot.zip,
    carol,https://github.com/carol/poker,

source is a git URL (cloned, or updated if already there), a .zip or a
folder. ref is an optional tag, branch or commit to check out; the --ref
option sets one for every row that leaves it blank. An optional `subdir`
column says where the bot is inside a repository.
"""

from __future__ import annotations

import csv
import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Callable

from .loader import BotLoadError, extract_zip, find_bot_root
from .tournament import safe_name


def _git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise BotLoadError(f"git {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


def _is_git_url(source: str) -> bool:
    return source.startswith(("https://", "http://", "git@", "ssh://")) or source.endswith(".git")


def collect(entries_csv: str | Path, out_dir: str | Path, ref: str | None = None, echo: Callable[[str], None] = print) -> dict:
    """Fetch every entry into out_dir/<name>/ and write out_dir/manifest.json."""
    entries_csv, out_dir = Path(entries_csv), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(entries_csv, newline="", encoding="utf-8-sig") as f:
        rows = [{(k or "").strip().lower(): (v or "").strip() for k, v in row.items()} for row in csv.DictReader(f)]
    manifest = {"collected": datetime.now().isoformat(timespec="seconds"), "entries": {}}
    failures = 0
    for row in rows:
        if not row.get("name") or not row.get("source"):
            continue
        name, source = safe_name(row["name"]), row["source"]
        wanted = row.get("ref") or ref
        dest = out_dir / name
        info = {"source": source, "ref": wanted}
        try:
            if _is_git_url(source):
                if (dest / ".git").exists():
                    _git("fetch", "--tags", "--force", "--prune", "origin", cwd=dest)
                else:
                    if dest.exists():
                        shutil.rmtree(dest)
                    _git("clone", "--quiet", source, str(dest))
                if wanted:
                    remote_branch = subprocess.run(
                        ["git", "rev-parse", "--verify", "--quiet", f"origin/{wanted}"], cwd=dest, capture_output=True
                    ).returncode == 0
                    _git("checkout", "--quiet", "--detach", f"origin/{wanted}" if remote_branch else wanted, cwd=dest)
                else:
                    default = _git("symbolic-ref", "--short", "refs/remotes/origin/HEAD", cwd=dest)
                    _git("checkout", "--quiet", "--detach", default, cwd=dest)
                info["commit"] = _git("rev-parse", "HEAD", cwd=dest)
                info["commit_date"] = _git("log", "-1", "--format=%cI", cwd=dest)
            elif source.lower().endswith(".zip"):
                extract_zip(Path(source), dest)
            else:
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(source, dest, ignore=shutil.ignore_patterns("__pycache__", ".git", ".venv", "venv"))
            root = find_bot_root(dest / row["subdir"] if row.get("subdir") else dest)
            if root is None:
                raise BotLoadError("no bot.py found (or several: add a subdir column saying which)")
            info["bot_dir"] = str(root)
            detail = f" @ {info['commit'][:8]} ({info['commit_date'][:16]})" if "commit" in info else ""
            echo(f"  OK    {name}: {source}{detail}")
        except (BotLoadError, OSError) as exc:
            failures += 1
            info["error"] = str(exc)
            echo(f"  FAIL  {name}: {exc}")
        manifest["entries"][name] = info
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    echo(f"Collected {len(manifest['entries']) - failures} of {len(manifest['entries'])} entries into {out_dir}")
    return manifest
