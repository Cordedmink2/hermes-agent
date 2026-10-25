#!/usr/bin/env python3
"""Conservative retention for terminal Kanban scratch workspaces.

Dry-run is the default. Live execution requires explicit --apply.

Policy (t_a74f97ce): only cards that are done/archived, idle >= 3 days (newest mtime
excluding ``.git``, which ``git status`` itself touches). A clean, fully pushed git
workspace is deleted. A dirty, unpushed, stashed, or non-git workspace with
non-regenerable content is first written to ``<archive-root>/<task>.tar`` with a
``.sha256`` sidecar, verified, and only then removed.

``--auto`` is the cron entry point: it runs every few minutes and does work only in
the daily slot or when root free space is below ``--min-free-gb`` (throttled), and
prints nothing otherwise.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
from pathlib import Path

DEFAULT_HOME = Path.home() / ".hermes"
ARCHIVE = Path("/mnt/aux/archive/workspace-archive")
IDLE_SECONDS = 3 * 86400
RETENTION_SECONDS = IDLE_SECONDS  # backwards-compatible alias
MIN_FREE_BYTES = 12 * 10**9
DAILY_HOUR = 3
PRESSURE_THROTTLE_SECONDS = 30 * 60
STATE_FILE = DEFAULT_HOME / "kanban" / "reports" / "retention" / "state.json"
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


def _git(path: Path, *args: str) -> subprocess.CompletedProcess | str:
    """Run git; return the CompletedProcess or an error string (fail closed)."""
    name = " ".join(args[:1])
    try:
        res = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=60,
                             env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    except subprocess.TimeoutExpired:
        return f"git {name} timed out"
    except OSError as exc:
        return f"git {name} could not run: {exc}"
    if res.returncode:
        return res.stderr.strip() or f"git {name} failed"
    return res


CACHE_DIRS = {"node_modules", "__pycache__", ".venv", "venv", ".pytest_cache", ".mypy_cache",
              ".ruff_cache", ".cache", ".tox", "dist", "build", "target"}


def _is_cache_path(rel: str) -> bool:
    """Ignored paths that are regenerable caches; any other ignored file (.env, local state) is kept."""
    return any(part in CACHE_DIRS for part in Path(rel.strip().strip('"')).parts)


def _git_state(path: Path) -> tuple[bool, bool, str | None]:
    """(unpushed, dirty, error). dirty = uncommitted/untracked changes or a stash."""
    if not (path / ".git").exists():
        return False, False, None
    for args in (("rev-parse", "--show-toplevel"),):
        res = _git(path, *args)
        if isinstance(res, str):
            return False, False, res
    # --all covers branches, tags, stash refs and detached HEAD; HEAD is explicit for safety.
    log = _git(path, "log", "--all", "HEAD", "--not", "--remotes", "--oneline")
    if isinstance(log, str):
        return False, False, log
    status = _git(path, "status", "--porcelain", "--untracked-files=all", "--ignored")
    if isinstance(status, str):
        return False, False, status
    status_lines = [ln for ln in status.stdout.splitlines()
                    if not (ln.startswith("!! ") and _is_cache_path(ln[3:]))]
    stash = _git(path, "stash", "list")
    if isinstance(stash, str):
        return False, False, stash
    return bool(log.stdout.strip()), bool(status_lines or stash.stdout.strip()), None


def _unpushed_commits(path: Path) -> tuple[bool, str | None]:
    unpushed, _dirty, err = _git_state(path)
    return unpushed, err


def _newest_mtime(path: Path) -> float:
    """Newest mtime under ``path`` excluding ``.git`` (git status touches .git)."""
    newest = path.stat().st_mtime
    for dirpath, dirs, files in os.walk(path):
        if ".git" in dirs:
            dirs.remove(".git")
        for name in [*dirs, *files]:
            try:
                newest = max(newest, os.lstat(os.path.join(dirpath, name)).st_mtime)
            except OSError:
                pass
    return newest


def _only_regenerable(path: Path) -> bool:
    """Use an intentionally tiny allowlist; anything unknown must be preserved."""
    return all(p.name in REGENERABLE_NAMES for p in path.rglob("*") if p.is_file() or p.is_symlink())


def _is_git(path: Path) -> bool:
    return (path / ".git").exists()


def plan(workspace_root: Path, board_root: Path, now: float | None = None,
         active_worktrees: set[Path] | None = None, repo: Path | None = None,
         extra_dbs: list[Path] | None = None, idle_seconds: float = IDLE_SECONDS) -> list[dict]:
    """``extra_dbs``: boards outside ``board_root`` — notably the default board at
    ``<home>/kanban.db``, whose scratch workspaces were otherwise never planned."""
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
    dbs = [p for p in (extra_dbs or []) if p.is_file()] + sorted(board_root.glob("*/kanban.db"))
    roots = {workspace_root.resolve(), *(d.parent.resolve() / "workspaces" for d in dbs)}
    roots |= {b.resolve() for b in board_root.glob("*/workspaces")}
    # Pass 1: every card of ANY kind referencing a path, across all boards.
    # Only scratch-kind rows are candidates; all rows gate.
    groups: dict[Path, list[tuple[str, str, Path]]] = {}
    allrefs: list[tuple[str, str, Path, Path]] = []
    for db in dbs:
        try:
            with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
                tasks = conn.execute("SELECT id,status,workspace_path,workspace_kind FROM tasks").fetchall()
        except sqlite3.Error:
            continue
        for task_id, status, declared, kind in tasks:
            if declared:
                path = Path(declared).expanduser()
            elif kind == "scratch":
                path = workspace_root / task_id
            else:
                continue
            allrefs.append((task_id, status, path.resolve(), db))
            if kind == "scratch" and path.exists() and path.is_dir():
                groups.setdefault(path.resolve(), []).append((task_id, status, db))
    # Pass 2: one decision per path, gated on every card at, above or below it.
    for path, own in groups.items():
        refs = list(own)
        for t, st, rp, d in allrefs:
            if (t, st, d) not in refs and (rp == path or path in rp.parents or rp in path.parents):
                refs.append((t, st, d))
        statuses = sorted({st for _t, st, _d in refs})
        terminal = [t for t, st, _d in own if st in {"done", "archived"}]
        task_id = (terminal or [own[0][0]])[0]
        status = "done" if not [st for st in statuses if st not in {"done", "archived"}] else \
            next(st for st in statuses if st not in {"done", "archived"})
        files, size = _inventory(path)
        newest = _newest_mtime(path)
        age_days = (now - newest) / 86400
        active = any(path == wt or path in wt.parents or wt in path.parents for wt in protected)
        contained = any(path != r and r in path.parents for r in roots)
        unpushed, dirty, git_error = _git_state(path)
        regenerable = _only_regenerable(path)
        if not contained:
            action, reason = "keep", "workspace is outside the kanban workspace roots; not managed by retention"
        elif status not in {"done", "archived"}:
            action, reason = "keep", f"card status is {status}, not done/archived" + (
                f" (workspace shared by {len(refs)} cards)" if len(refs) > 1 else "")
        elif worktree_error:
            action, reason = "keep", f"cannot read active git worktrees: {worktree_error}"
        elif active:
            action, reason = "keep", "workspace is an active git worktree"
        elif now - newest < idle_seconds:
            action, reason = "keep", f"workspace idle for {age_days:.2f} days, need {idle_seconds / 86400:g}"
        elif git_error:
            action, reason = "keep", f"cannot safely classify git state; git probe error: {git_error}"
        elif unpushed or dirty:
            action, reason = "archive", f"terminal and idle >= {idle_seconds / 86400:g} days; " + (
                "has unpushed commits" if unpushed else "has uncommitted/untracked/ignored changes or stash")
        elif not _is_git(path) and not regenerable:
            action, reason = "archive", f"terminal and idle >= {idle_seconds / 86400:g} days; not a git repo with non-regenerable content"
        else:
            action, reason = "delete", (f"terminal and idle >= {idle_seconds / 86400:g} days; "
                                        "no unpushed commits" + ("" if _is_git(path) else " and only allowlisted regenerable content"))
        result.append({"task_id": task_id, "source": str(path), "action": action, "reason": reason,
                       "file_count": files, "bytes": size, "unpushed_commits": unpushed,
                       "only_regenerable": regenerable, "dirty": dirty, "git_probe_error": git_error,
                       "refs": [[t, str(d)] for t, _s, d in refs]})
    return result


def _verify_tar(src: Path, tar_path: Path) -> bool:
    """The tar must contain exactly the regular files (count and bytes) found under src."""
    count, size = _inventory(src)
    got_count = got_size = 0
    with tarfile.open(tar_path) as tar:
        for member in tar:
            if member.isfile():
                got_count += 1
                got_size += member.size
    return (count, size) == (got_count, got_size)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _archive(src: Path, archive_root: Path, task_id: str) -> tuple[Path, str] | None:
    """Write tar + sha256 sidecar; None (and nothing left behind) if verification fails."""
    archive_root.mkdir(parents=True, exist_ok=True)
    final = archive_root / f"{task_id}.tar"
    if final.exists() or Path(f"{final}.sha256").exists():
        final = archive_root / f"{task_id}-{time.strftime('%Y%m%dT%H%M%S')}.tar"
        n = 1
        while final.exists() or Path(f"{final}.sha256").exists():
            n += 1
            final = archive_root / f"{task_id}-{time.strftime('%Y%m%dT%H%M%S')}-{n}.tar"
    tmp = final.with_name(final.name + ".partial")
    try:
        with tarfile.open(tmp, "w") as tar:
            tar.add(src, arcname=task_id)
        if not _verify_tar(src, tmp):
            return None
        digest = _sha256(tmp)
        tmp.replace(final)
        Path(f"{final}.sha256").write_text(f"{digest}  {final.name}\n")
        return final, digest
    finally:
        tmp.unlink(missing_ok=True)


def _live_refs(refs: list) -> list[str]:
    """Re-read card status right before acting (a card may have been reopened mid-run)."""
    live = []
    for task_id, db in refs:
        try:
            with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
                row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        except sqlite3.Error:
            live.append(f"{task_id}: status unreadable")
            continue
        if row and row[0] not in {"done", "archived"}:
            live.append(f"{task_id}={row[0]}")
    return live


def execute(rows: list[dict], archive_root: Path, apply: bool = False) -> list[dict]:
    for row in rows:
        if apply and row["action"] in {"archive", "delete"}:
            src = Path(row["source"])
            planned = row["action"]
            live = _live_refs(row.get("refs", []))
            if live:
                row["action"], row["reason"] = "keep", f"card reopened before action: {live}"
                print(json.dumps(row, sort_keys=True))
                continue
            try:
                if planned == "archive":
                    done = _archive(src, archive_root, row["task_id"])
                    if done is None:
                        row["action"], row["error"] = "archive_failed", "archive verification failed; source kept"
                    else:
                        row["archive"], row["archive_sha256"] = str(done[0]), done[1]
                        row["verified_file_count"], row["verified_bytes"] = _inventory(src)
                        shutil.rmtree(src)
                        row["action"] = "archived"
                else:
                    shutil.rmtree(src)
                    row["action"] = "deleted"
            except Exception as exc:  # one bad workspace must not stop the rest
                row["action"], row["error"] = f"{planned}_failed", f"{type(exc).__name__}: {exc}"
        print(json.dumps(row, sort_keys=True))
    return rows


def _load_state(state_file: Path) -> dict:
    try:
        return json.loads(state_file.read_text())
    except (OSError, ValueError):
        return {}


def record_run(state_file: Path, now: float) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({"last_run_ts": now,
                                      "last_full_run_date": time.strftime("%Y-%m-%d", time.localtime(now))}))


def decide_run(free_bytes: int, now: float, state_file: Path, min_free_bytes: int = MIN_FREE_BYTES) -> str | None:
    """'pressure' when free space is below the floor (throttled), 'daily' once per day after DAILY_HOUR."""
    state = _load_state(state_file)
    lt = time.localtime(now)
    if free_bytes < min_free_bytes and now - state.get("last_run_ts", 0) >= PRESSURE_THROTTLE_SECONDS:
        return "pressure"
    if lt.tm_hour >= DAILY_HOUR and state.get("last_full_run_date") != time.strftime("%Y-%m-%d", lt):
        return "daily"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=DEFAULT_HOME)
    parser.add_argument("--workspace-root", type=Path)
    parser.add_argument("--archive-root", type=Path, default=ARCHIVE)
    parser.add_argument("--apply", action="store_true", help="execute eligible actions (dry-run otherwise)")
    parser.add_argument("--idle-days", type=float, default=IDLE_SECONDS / 86400)
    parser.add_argument("--auto", action="store_true", help="cron mode: run only in the daily slot or under disk pressure")
    parser.add_argument("--state-file", type=Path)
    parser.add_argument("--min-free-gb", type=float, default=MIN_FREE_BYTES / 10**9)
    parser.add_argument("--free-path", type=Path, default=Path("/"))
    parser.add_argument("--summary", action="store_true", help="print before/after free space and archives")
    args = parser.parse_args(argv)
    floor = int(args.min_free_gb * 10**9)
    state_file = args.state_file or args.home / "kanban" / "reports" / "retention" / "state.json"
    free_before = shutil.disk_usage(args.free_path).free
    trigger = None
    if args.auto:
        trigger = decide_run(free_before, time.time(), state_file, floor)
        if trigger is None:
            return 0
    root = args.workspace_root or args.home / "kanban" / "workspaces"
    rows = plan(root, args.home / "kanban" / "boards", extra_dbs=[args.home / "kanban.db"],
                idle_seconds=args.idle_days * 86400)
    execute(rows, args.archive_root, args.apply)
    free_after = shutil.disk_usage(args.free_path).free
    failed = [r for r in rows if r["action"].endswith("_failed")]
    if args.auto and args.apply:
        record_run(state_file, time.time())
    if args.summary or args.auto:
        counts: dict[str, int] = {}
        for r in rows:
            counts[r["action"]] = counts.get(r["action"], 0) + 1
        print(f"retention {'APPLY' if args.apply else 'DRY-RUN'}"
              + (f" trigger={trigger}" if trigger else "") + f" idle>={args.idle_days:g}d")
        print("plan: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        for r in rows:
            if r.get("archive"):
                print(f"archive: {r['archive']} sha256={r['archive_sha256']}")
            if r.get("error"):
                print(f"FAILED {r['task_id']}: {r['error']}")
        print(f"free before: {free_before / 10**9:.2f} GB, free after: {free_after / 10**9:.2f} GB (floor {floor / 10**9:g} GB)")
        if free_after < floor:
            print(f"ALERT: root free space {free_after / 10**9:.2f} GB is below the {floor / 10**9:g} GB floor "
                  "after retention; manual reclaim needed", file=sys.stderr)
            return 2
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
