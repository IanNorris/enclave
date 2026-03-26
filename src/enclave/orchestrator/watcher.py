"""Workspace file watcher — inotify-based change detection.

Watches workspace directories for file modifications from outside
the container (i.e., from the developer). Sends debounced
notifications to the agent via IPC so it can react to changes.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Callable, Awaitable

from enclave.common.logging import get_logger

log = get_logger("watcher")

# Debounce: collect changes over this window before notifying
_DEBOUNCE_SECS = 2.0

# Directories to ignore
_IGNORE_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "target",
    ".nix-profile", ".cache", ".enclave-memories",
}

# File patterns to ignore
_IGNORE_SUFFIXES = {
    ".pyc", ".pyo", ".swp", ".swo", ".tmp", ".lock",
}


class WorkspaceWatcher:
    """Watch a workspace directory for file changes.

    Uses inotify to monitor the workspace. Changes are debounced
    and batched before being delivered to the callback.
    """

    def __init__(
        self,
        workspace_path: str,
        on_changes: Callable[[list[dict]], Awaitable[None]],
        debounce: float = _DEBOUNCE_SECS,
    ):
        self.workspace_path = Path(workspace_path)
        self.on_changes = on_changes
        self.debounce = debounce
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        """Start watching the workspace."""
        if self._running:
            return

        if not self.workspace_path.exists():
            log.warning("Workspace does not exist: %s", self.workspace_path)
            return

        try:
            from inotify_simple import INotify, flags as iflags
        except ImportError:
            log.warning("inotify_simple not installed — file watching disabled")
            return

        self._running = True
        self._task = asyncio.create_task(self._watch_loop())
        log.info("Watching workspace: %s", self.workspace_path)

    async def stop(self) -> None:
        """Stop watching."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _watch_loop(self) -> None:
        """Main watch loop using inotify."""
        from inotify_simple import INotify, flags as iflags

        try:
            inotify = INotify()
            watch_flags = (
                iflags.CREATE | iflags.MODIFY | iflags.DELETE |
                iflags.MOVED_FROM | iflags.MOVED_TO | iflags.CLOSE_WRITE
            )

            # Watch the root workspace
            wd_map: dict[int, Path] = {}
            self._add_watches(inotify, self.workspace_path, watch_flags, wd_map)

            loop = asyncio.get_running_loop()
            pending: list[dict] = []
            last_batch_time = 0.0

            while self._running:
                # Read events in a thread to avoid blocking
                events = await loop.run_in_executor(
                    None, lambda: inotify.read(timeout=1000)
                )

                for event in events:
                    parent = wd_map.get(event.wd, self.workspace_path)
                    name = event.name
                    full_path = parent / name if name else parent

                    # Skip ignored paths
                    if self._should_ignore(full_path, name):
                        continue

                    # Determine change type
                    if event.mask & (iflags.CREATE | iflags.MOVED_TO):
                        change_type = "created"
                    elif event.mask & (iflags.DELETE | iflags.MOVED_FROM):
                        change_type = "deleted"
                    elif event.mask & (iflags.MODIFY | iflags.CLOSE_WRITE):
                        change_type = "modified"
                    else:
                        continue

                    rel_path = str(full_path.relative_to(self.workspace_path))
                    pending.append({
                        "path": rel_path,
                        "type": change_type,
                        "is_dir": bool(event.mask & iflags.ISDIR),
                    })

                    # If a new directory was created, watch it too
                    if event.mask & iflags.ISDIR and event.mask & iflags.CREATE:
                        if full_path.exists() and not self._should_ignore(full_path, name):
                            self._add_watches(inotify, full_path, watch_flags, wd_map)

                # Debounce: deliver pending changes after quiet period
                now = time.monotonic()
                if pending and (now - last_batch_time) >= self.debounce:
                    # Deduplicate: keep last change per path
                    seen = {}
                    for change in pending:
                        seen[change["path"]] = change
                    batch = list(seen.values())
                    pending.clear()
                    last_batch_time = now

                    try:
                        await self.on_changes(batch)
                    except Exception as e:
                        log.error("Error delivering file changes: %s", e)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.error("Watcher error: %s", e)
        finally:
            try:
                inotify.close()
            except Exception:
                pass

    def _add_watches(self, inotify, root: Path, flags, wd_map: dict) -> None:
        """Recursively add inotify watches for a directory tree."""
        try:
            wd = inotify.add_watch(str(root), flags)
            wd_map[wd] = root
        except OSError as e:
            log.debug("Cannot watch %s: %s", root, e)
            return

        try:
            for child in root.iterdir():
                if child.is_dir() and not self._should_ignore(child, child.name):
                    self._add_watches(inotify, child, flags, wd_map)
        except PermissionError:
            pass

    def _should_ignore(self, path: Path, name: str) -> bool:
        """Check if a path should be ignored."""
        if name in _IGNORE_DIRS:
            return True
        if any(name.endswith(s) for s in _IGNORE_SUFFIXES):
            return True
        # Hidden files starting with .
        if name.startswith(".") and name not in (".env",):
            return True
        return False
