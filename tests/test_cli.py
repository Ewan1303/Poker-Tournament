import csv
import subprocess
import zipfile

from poker_tournament.cli import main
from poker_tournament.loader import BUILTIN_BOTS
from poker_tournament.tournament import discover_entries


def test_validate_example_bots():
    assert main(["validate", "random_bot", "ml_example", "--hands", "20"]) == 0


def test_validate_rejects_a_broken_bot(tmp_path):
    folder = tmp_path / "broken"
    folder.mkdir()
    (folder / "bot.py").write_text("class Bot:\n    def act(self, obs)\n        return 'call'\n", encoding="utf-8")
    assert main(["validate", str(folder), "--hands", "5"]) == 1


def test_new_then_package(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["new", "my_bots/alice"]) == 0
    assert (tmp_path / "my_bots" / "alice" / "bot.py").exists()
    assert main(["package", "my_bots/alice", "--skip-validate"]) == 0
    with zipfile.ZipFile(tmp_path / "alice.zip") as archive:
        assert "alice/bot.py" in archive.namelist()


def test_run_from_a_submissions_folder_with_zips(tmp_path, capsys):
    submissions = tmp_path / "round-1"
    submissions.mkdir()
    for name in ("random_bot", "calling_station"):
        with zipfile.ZipFile(submissions / f"{name}.zip", "w") as archive:
            archive.write(BUILTIN_BOTS / name / "bot.py", f"{name}/bot.py")
    code = main(["run", str(submissions), "--name", "r1", "--hands", "10", "--out", str(tmp_path / "results"), "--in-process"])
    assert code == 0
    assert (tmp_path / "results" / "r1" / "leaderboard.md").exists()
    assert "Heads-up round robin" in capsys.readouterr().out


def test_collect_from_git_zip_and_folder(tmp_path):
    repo = tmp_path / "alice_repo"
    repo.mkdir()
    (repo / "bot.py").write_text((BUILTIN_BOTS / "calling_station" / "bot.py").read_text(encoding="utf-8"), encoding="utf-8")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(git + ["-C", str(repo), "add", "."], check=True)
    subprocess.run(git + ["-C", str(repo), "commit", "-q", "-m", "bot"], check=True)
    subprocess.run(git + ["-C", str(repo), "tag", "round-1"], check=True)

    bob_zip = tmp_path / "bob.zip"
    with zipfile.ZipFile(bob_zip, "w") as archive:
        archive.write(BUILTIN_BOTS / "random_bot" / "bot.py", "bot.py")

    out = tmp_path / "round-1"
    entries = tmp_path / "entries.csv"
    with open(entries, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["name", "source", "ref", "subdir"])
        writer.writerow(["alice", (repo / ".git").as_posix(), "round-1", ""])
        writer.writerow(["bob", str(bob_zip), "", ""])
        writer.writerow(["carol", str(BUILTIN_BOTS / "equity_bot"), "", ""])
        writer.writerow(["dave", str(BUILTIN_BOTS), "", "calling_station"])  # several bots: subdir picks one
    assert main(["collect", str(entries), "--out", str(out)]) == 0
    for name in ("alice", "bob", "carol"):
        assert (out / name / "bot.py").exists()

    found = {e.name: e.path for e in discover_entries([out])}
    assert sorted(found) == ["alice", "bob", "carol", "dave"]
    assert found["dave"] == (out / "dave" / "calling_station").resolve()
