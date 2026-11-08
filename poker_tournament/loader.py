"""Finding, unzipping and loading bot folders.

A bot folder holds bot.py, which defines a class called Bot (normally a
subclass of PokerBot), plus any model files and helper modules it needs.
bot.py can also set NAME, STYLE and DESCRIPTION strings at the top: the
name and blurb shown in `poker play`, `poker watch` and `poker bots`.
"""

from __future__ import annotations

import ast
import importlib.util
import itertools
import shutil
import sys
import zipfile
from pathlib import Path
from typing import Iterator

from .api import PokerBot

FRAMEWORK_ROOT = Path(__file__).resolve().parent.parent
BUILTIN_BOTS = FRAMEWORK_ROOT / "bots"
MAX_BOT_MB = 50
MAX_UNZIPPED_MB = 500
IGNORED_PARTS = {
    "__pycache__",
    "__MACOSX",
    ".git",
    ".venv",
    "venv",
    ".ipynb_checkpoints",
    ".pytest_cache",
    ".mypy_cache",
    ".idea",
    ".vscode",
}
IGNORED_SUFFIXES = {".pyc", ".pyo"}
IGNORED_NAMES = {".DS_Store", "Thumbs.db"}


class BotLoadError(Exception):
    pass


def find_bot_root(folder: Path) -> Path | None:
    """The folder that holds bot.py: `folder` itself, or the one subfolder that has it
    (zips often contain a folder rather than the files)."""
    folder = Path(folder)
    if (folder / "bot.py").is_file():
        return folder
    for pattern in ("*/bot.py", "*/*/bot.py"):
        found = [p.parent for p in folder.glob(pattern) if not IGNORED_PARTS.intersection(p.parts)]
        if len(found) == 1:
            return found[0]
        if found:
            return None
    return None


def extract_zip(zip_path: Path, dest: Path) -> Path:
    """Safely unzip a submitted bot into `dest` and return the folder holding bot.py."""
    zip_path, dest = Path(zip_path), Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    root = dest.resolve()
    try:
        with zipfile.ZipFile(zip_path) as archive:
            members = archive.infolist()
            if sum(m.file_size for m in members) > MAX_UNZIPPED_MB * 1024 * 1024:
                raise BotLoadError(f"{zip_path.name} unzips to more than {MAX_UNZIPPED_MB} MB")
            for member in members:
                if not (dest / member.filename).resolve().is_relative_to(root):
                    raise BotLoadError(f"{zip_path.name} contains an unsafe path: {member.filename}")
            archive.extractall(dest)
    except zipfile.BadZipFile:
        raise BotLoadError(f"{zip_path.name} is not a valid zip file") from None
    bot_root = find_bot_root(dest)
    if bot_root is None:
        raise BotLoadError(f"{zip_path.name} has no bot.py (or more than one)")
    return bot_root


def resolve_bot(spec: str | Path) -> Path:
    """A bot folder from a path to a folder or .zip, or the name of a bot in bots/."""
    path = Path(spec)
    if not path.exists() and (BUILTIN_BOTS / str(spec)).exists():
        path = BUILTIN_BOTS / str(spec)
    if not path.exists():
        raise BotLoadError(f"no bot at {spec} (and no built-in bot with that name in bots/)")
    if path.is_file():
        if path.suffix.lower() != ".zip":
            raise BotLoadError(f"{spec} is a file; give the bot's folder or a .zip of it")
        return extract_zip(path, path.parent / ".unzipped" / path.stem).resolve()
    root = find_bot_root(path)
    if root is None:
        raise BotLoadError(f"{spec} has no bot.py")
    return root.resolve()


def bot_info(bot_dir: str | Path) -> dict:
    """A bot's profile: {"id", "name", "style", "description"}.

    Read from NAME, STYLE and DESCRIPTION strings at the top of bot.py without
    running it. The name falls back to the folder name and the description to
    the first paragraph of bot.py's docstring.
    """
    bot_dir = Path(bot_dir)
    info = {"id": bot_dir.name, "name": bot_dir.name, "style": "", "description": ""}
    try:
        tree = ast.parse((bot_dir / "bot.py").read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return info
    docstring = ast.get_docstring(tree)
    if docstring:
        info["description"] = " ".join(docstring.split("\n\n")[0].split())
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            key = node.targets[0].id
            if key in ("NAME", "STYLE", "DESCRIPTION"):
                try:
                    value = ast.literal_eval(node.value)
                except (ValueError, TypeError, SyntaxError):
                    continue
                if isinstance(value, str) and value.strip():
                    info[key.lower()] = " ".join(value.split())
    return info


def iter_bot_files(folder: Path) -> Iterator[Path]:
    """Files that belong to the bot (skips caches, virtualenvs and .git)."""
    folder = Path(folder)
    for path in sorted(folder.rglob("*")):
        rel = path.relative_to(folder)
        if IGNORED_PARTS.intersection(rel.parts) or path.suffix in IGNORED_SUFFIXES or path.name in IGNORED_NAMES:
            continue
        if path.is_file():
            yield path


def folder_size(folder: Path) -> int:
    return sum(path.stat().st_size for path in iter_bot_files(folder))


_module_ids = itertools.count()


def load_bot_class(bot_dir: str | Path, module_name: str | None = None) -> type:
    """Import bot.py from `bot_dir` and return its Bot class."""
    bot_dir = Path(bot_dir).resolve()
    bot_file = bot_dir / "bot.py"
    if not bot_file.is_file():
        raise BotLoadError(f"{bot_dir} has no bot.py")
    # Put the bot's folder first on the path so it imports its own helper modules.
    if str(bot_dir) in sys.path:
        sys.path.remove(str(bot_dir))
    sys.path.insert(0, str(bot_dir))
    shared = module_name is None  # loading into a process that may hold other bots
    before = set(sys.modules)
    name = module_name or f"_poker_bot_{next(_module_ids)}"
    spec = importlib.util.spec_from_file_location(name, bot_file)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    if shared:
        # Forget this bot's helper modules once it has them, so another bot with a
        # helper of the same name (say utils.py) imports its own rather than this one.
        for loaded in set(sys.modules) - before - {name}:
            file = getattr(sys.modules[loaded], "__file__", None)
            if file and Path(file).resolve().is_relative_to(bot_dir):
                del sys.modules[loaded]
    cls = getattr(module, "Bot", None)
    if cls is None:
        found = [
            value
            for value in vars(module).values()
            if isinstance(value, type) and issubclass(value, PokerBot) and value.__module__ == name
        ]
        cls = found[0] if len(found) == 1 else None
    if not isinstance(cls, type):
        raise BotLoadError("bot.py must define a class called Bot")
    if not callable(getattr(cls, "act", None)):
        raise BotLoadError("the Bot class needs an act(self, obs) method")
    return cls


def load_bot(spec: str | Path) -> PokerBot:
    """Create a bot in this process, e.g. an opponent for your training script:

        opponent = load_bot("equity_bot")       # a bot from bots/
        mine = load_bot("my_bots/alice_bot")    # or any bot folder
    """
    return load_bot_class(resolve_bot(spec))()
