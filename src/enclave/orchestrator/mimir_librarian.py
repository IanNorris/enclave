"""Mimir librarian worker — drains pending drafts on demand.

Runs `mimir-librarian run` whenever an agent emits a compaction-complete
event or a successful mimir_record tool call. A single asyncio task with
debouncing serializes invocations across all agents (the librarian is
the only writer to canonical.log, so concurrent runs would race).

Design:
- Single long-lived task per orchestrator process.
- ``trigger()`` is fire-and-forget; sets an event and returns.
- After being signalled, the worker waits ``debounce_secs`` to coalesce
  bursts (e.g. 9 compactions in quick succession should produce one run,
  not nine).
- ``MIMIR_LIBRARIAN_LLM`` env (default ``copilot``) determines which
  classifier the librarian invokes. Inherited from the orchestrator
  process; operator can override in systemd unit / shell env.
- Failures are logged but don't crash the task — drafts simply remain
  in pending and will be retried on the next trigger.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
from pathlib import Path

log = logging.getLogger("enclave.mimir.librarian")


class MimirLibrarianWorker:
    """Single-task debounced runner for mimir-librarian."""

    def __init__(
        self,
        librarian_bin: str,
        canonical_log: str | os.PathLike,
        drafts_dir: str | os.PathLike,
        *,
        debounce_secs: float = 10.0,
        run_timeout_secs: float = 600.0,
        llm_timeout_secs: int = 120,
        max_retries: int = 2,
    ) -> None:
        self._librarian_bin = librarian_bin
        self._canonical_log = Path(canonical_log)
        self._drafts_dir = Path(drafts_dir)
        self._debounce_secs = debounce_secs
        self._run_timeout_secs = run_timeout_secs
        self._llm_timeout_secs = llm_timeout_secs
        self._max_retries = max_retries
        self._event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(
                self._loop(), name="mimir-librarian-worker",
            )
            log.info(
                "Mimir librarian worker started (bin=%s, log=%s, drafts=%s)",
                self._librarian_bin, self._canonical_log, self._drafts_dir,
            )

    async def stop(self) -> None:
        self._stopping = True
        self._event.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except asyncio.TimeoutError:
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):
                    pass
            self._task = None

    def trigger(self, reason: str = "") -> None:
        """Request a librarian run. Coalesces with concurrent triggers."""
        if self._stopping:
            return
        if reason:
            log.debug("Mimir librarian trigger: %s", reason)
        self._event.set()

    async def _loop(self) -> None:
        while not self._stopping:
            try:
                await self._event.wait()
                if self._stopping:
                    break
                # Debounce: wait a quiet period; if more triggers arrive
                # we keep extending. This batches 9-compactions-in-a-row
                # into a single librarian run.
                while not self._stopping:
                    self._event.clear()
                    try:
                        await asyncio.wait_for(
                            self._event.wait(),
                            timeout=self._debounce_secs,
                        )
                    except asyncio.TimeoutError:
                        break
                if self._stopping:
                    break
                await self._run_once()
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("Mimir librarian worker loop iteration failed")
                # Avoid hot-spin on persistent failure
                await asyncio.sleep(5)

    def _break_stale_lock(self) -> None:
        """Remove the workspace write lock iff its owner process is dead.

        The lock file (``<canonical_log>.lock``) records the owning pid as a
        ``pid=<n>`` line. If that process no longer exists the lock is stale
        (the owner was killed without releasing it) and we delete it so the
        next run can proceed. A live owner is left strictly untouched, so this
        never races a legitimately-running librarian.
        """
        lock_path = Path(str(self._canonical_log) + ".lock")
        try:
            text = lock_path.read_text()
        except (OSError, ValueError):
            return  # no lock, or unreadable — nothing to break
        pid = None
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("pid="):
                try:
                    pid = int(line[4:].strip())
                except ValueError:
                    pid = None
                break
        if pid is None:
            log.warning(
                "Mimir librarian: lock file %s has no parseable pid — "
                "leaving it in place (manual inspection needed)", lock_path,
            )
            return
        # os.kill(pid, 0) raises ProcessLookupError if the pid is dead,
        # PermissionError if it exists but we can't signal it (still alive).
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            try:
                lock_path.unlink()
                log.warning(
                    "Mimir librarian: removed stale write lock %s "
                    "(owner pid %d is dead)", lock_path, pid,
                )
            except OSError as e:
                log.warning(
                    "Mimir librarian: failed to remove stale lock %s: %s",
                    lock_path, e,
                )
        except PermissionError:
            log.debug(
                "Mimir librarian: lock owner pid %d still alive — not breaking",
                pid,
            )

    async def _run_once(self) -> None:
        if not Path(self._librarian_bin).is_file():
            log.warning(
                "Mimir librarian binary missing: %s — skipping run",
                self._librarian_bin,
            )
            return
        # Check there's actually something to do; cheap guard avoids
        # spawning an LLM-bound subprocess on a no-op trigger.
        pending = self._drafts_dir / "pending"
        if pending.is_dir():
            try:
                if not any(pending.iterdir()):
                    log.debug("Mimir librarian: pending empty, skipping run")
                    return
            except OSError:
                pass

        # Break a stale workspace write lock before running. The librarian
        # acquires an advisory lock file (<canonical_log>.lock) for the run and
        # is supposed to release it on exit, but if a previous run was SIGKILLed
        # mid-flight (e.g. our own run-timeout kill below, or an orchestrator
        # crash) the lock file is orphaned and every subsequent run fails with
        # "workspace write lock already held" (rc=70) — a permanent stall. We
        # only break it when the recorded owner pid is provably dead, so a live
        # run is never disturbed.
        self._break_stale_lock()

        cmd = [
            str(self._librarian_bin), "run",
            "--workspace", str(self._canonical_log),
            "--drafts-dir", str(self._drafts_dir),
            "--llm-timeout-secs", str(self._llm_timeout_secs),
            "--max-retries", str(self._max_retries),
        ]
        env = os.environ.copy()
        env.setdefault("MIMIR_LIBRARIAN_LLM", "copilot")
        # The librarian shells out to an LLM CLI (default `copilot`). The
        # systemd unit's PATH can omit the user bin dirs where it's installed
        # (e.g. ~/.npm-global/bin), causing "failed to spawn copilot: No such
        # file or directory". Prepend the usual user bin dirs so it resolves.
        _extra_paths = [
            str(Path.home() / ".npm-global" / "bin"),
            str(Path.home() / ".local" / "bin"),
        ]
        _existing = env.get("PATH", "")
        _existing_parts = _existing.split(os.pathsep) if _existing else []
        env["PATH"] = os.pathsep.join(
            [p for p in _extra_paths if p not in _existing_parts] + _existing_parts
        )
        log.info("Mimir librarian: running %s", shlex.join(cmd))
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            try:
                _stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=self._run_timeout_secs,
                )
            except asyncio.TimeoutError:
                # Terminate gracefully first (SIGTERM) so the librarian can run
                # its lock-release/cleanup path; only SIGKILL if it ignores the
                # request. A bare SIGKILL here was the original stall source: it
                # left the workspace write lock orphaned, wedging every later
                # run. The dead-pid lock breaker above now recovers from that
                # too, but a clean shutdown avoids the failed run entirely.
                log.warning(
                    "Mimir librarian: run timed out after %ss, terminating",
                    self._run_timeout_secs,
                )
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=10.0)
                except asyncio.TimeoutError:
                    log.warning("Mimir librarian: did not exit on SIGTERM, killing")
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    await proc.wait()
                return
            if proc.returncode == 0:
                # Extract the final summary line from stderr (the librarian
                # logs to stderr via tracing). Tail is usually concise.
                tail = (stderr or b"").decode("utf-8", "replace").splitlines()
                summary = tail[-1] if tail else "ok"
                log.info("Mimir librarian: run done (rc=0): %s", summary[:300])
            else:
                tail = (stderr or b"").decode("utf-8", "replace")
                log.warning(
                    "Mimir librarian: run failed rc=%s: %s",
                    proc.returncode, tail[-500:],
                )
        except FileNotFoundError:
            log.warning(
                "Mimir librarian: binary not found at %s",
                self._librarian_bin,
            )
        except Exception:
            log.exception("Mimir librarian: subprocess failed")
