#!/usr/bin/env python3
"""Conservative retention planner for terminal Kanban scratch workspaces.

Dry-run is the default. Live execution requires explicit --apply.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

DEFAULT_HOME = Path.home() / ".hermes"
ARCHIVE = Path("/mnt/aux/archive/kanban-workspaces")
RETENTION_SECONDS = 14 * 86400
REGENERABLE_NAMES = {".gitignore", ".keep", "README.md"}


def _git_worktrees(repo: Path) -> set[Path]:
    """Read active worktrees from Git itself, including worktrees outside Kanban."""
    roots: set[Path] = set()
    result = subprocess.run(["git", "-C", str(repo), "worktree", "list", "--porcelain"],
                            check=True, capture_output=True, text=True)
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            roots.add(Path(line.removeprefix("worktree ")))
    return roots


def _inventory(path: Path) -> tuple[int, int]:
    count = size = 0
    for item in path.rglob("*"):
        if item.is_file() and not item.is_symlink():
            count += 1
            size += item.stat().st_size
    return count, size


def _unpushed_commits(path: Path) -> tuple[bool, str | None]:
    if not (path / ".git").exists():
        return False, None
    try:
        probe = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                               capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return False, "git rev-parse timed out"
    except OSError as exc:
        return False, f"git rev-parse could not run: {exc}"
    if probe.returncode:
        return False, probe.stderr.strip() or "git rev-parse failed"
    try:
        result = subprocess.run(["git", "-C", str(path), "log", "--branches", "--not", "--remotes", "--oneline"],
                                capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return False, "git log timed out"
    except OSError as exc:
        return False, f"git log could not run: {exc}"
    if result.returncode:
        return False, result.stderr.strip() or "git log failed"
    return bool(result.stdout.strip()), None


def _only_regenerable(path: Path) -> bool:
    """Use an intentionally tiny allowlist; anything unknown must be preserved."""
    return all(p.name in REGENERABLE_NAMES for p in path.rglob("*") if p.is_file() or p.is_symlink())


def plan(workspace_root: Path, board_root: Path, now: float | None = None,
         active_worktrees: set[Path] | None = None, repo: Path | None = None) -> list[dict]:
    now = time.time() if now is None else now
    worktree_error = None
    if active_worktrees is not None:
        protected = {p.resolve() for p in active_worktrees}
    else:
        try:
            protected = _git_worktrees(repo or Path(__file__).resolve().parents[1])
        except (OSError, subprocess.SubprocessError) as exc:
            detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) and exc.stderr else str(exc)
            protected, worktree_error = set(), detail
    result = []
    for db in sorted(board_root.glob("*/kanban.db")):
        try:
            with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
                tasks = conn.execute("SELECT id,status,workspace_path FROM tasks WHERE workspace_kind='scratch'").fetchall()
        except sqlite3.Error:
            continue
        for task_id, status, declared in tasks:
            path = Path(declared).expanduser() if declared else workspace_root / task_id
            if not path.exists() or not path.is_dir():
                continue
            path = path.resolve()
            files, size = _inventory(path)
            newest = max([path.stat().st_mtime, *(p.stat().st_mtime for p in path.rglob("*"))], default=0)
            age_days = (now - newest) / 86400
            active = any(path == wt or path in wt.parents or wt in path.parents for wt in protected)
            unpushed, git_error = _unpushed_commits(path)
            regenerable = _only_regenerable(path)
            if status not in {"done", "archived"}:
                action, reason = "keep", f"card status is {status}, not done/archived"
            elif worktree_error:
                action, reason = "keep", f"cannot read active git worktrees: {worktree_error}"
            elif active:
                action, reason = "keep", "workspace is an active git worktree"
            elif now - newest < RETENTION_SECONDS:
                action, reason = "keep", f"workspace modified within 14 days ({age_days:.2f} days old)"
            elif git_error:
                action, reason = "keep", f"cannot safely classify git state; git probe error: {git_error}"
            elif unpushed or git_error or not regenerable:
                action, reason = "archive", "terminal and older than 14 days; " + ("has unpushed commits" if unpushed else "contains non-regenerable content")
                if git_error:
                    reason += f"; git probe error: {git_error}"
            else:
                action, reason = "delete", "terminal and older than 14 days; no unpushed commits and only allowlisted regenerable content"
            result.append({"task_id": task_id, "source": str(path), "action": action, "reason": reason,
                           "file_count": files, "bytes": size, "unpushed_commits": unpushed,
                           "only_regenerable": regenerable, "git_probe_error": git_error})
    return result


def execute(rows: list[dict], archive_root: Path, apply: bool = False) -> list[dict]:
    for row in rows:
        print(json.dumps(row, sort_keys=True))
        if not apply or row["action"] not in {"archive", "delete"}:
            continue
        src = Path(row["source"])
        if row["action"] == "delete":
            shutil.rmtree(src)
            row["action"] = "deleted"
            continue
        dst = archive_root / row["task_id"]
        if dst.exists():
            raise FileExistsError(f"archive destination exists: {dst}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst, symlinks=True)
        before, after = _inventory(src), _inventory(dst)
        if before != after:
            shutil.rmtree(dst)
            raise IOError(f"archive verification failed for {src}: {before} != {after}")
        shutil.rmtree(src)
        row["action"] = "archived"
        row["verified_file_count"], row["verified_bytes"] = after
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=DEFAULT_HOME)
    parser.add_argument("--workspace-root", type=Path)
    parser.add_argument("--archive-root", type=Path, default=ARCHIVE)
    parser.add_argument("--apply", action="store_true", help="execute eligible actions (dry-run otherwise)")
    args = parser.parse_args()
    root = args.workspace_root or args.home / "kanban" / "workspaces"
    rows = plan(root, args.home / "kanban" / "boards")
    execute(rows, args.archive_root, args.apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
