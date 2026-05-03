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

        cmd = [
            str(self._librarian_bin), "run",
            "--workspace", str(self._canonical_log),
            "--drafts-dir", str(self._drafts_dir),
            "--llm-timeout-secs", str(self._llm_timeout_secs),
            "--max-retries", str(self._max_retries),
        ]
        env = os.environ.copy()
        env.setdefault("MIMIR_LIBRARIAN_LLM", "copilot")
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
                log.warning(
                    "Mimir librarian: run timed out after %ss, killing",
                    self._run_timeout_secs,
                )
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
