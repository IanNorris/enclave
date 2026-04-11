"""Enclave orchestrator main entry point.

Wires together Matrix client, IPC server, container manager,
and message router into a running orchestrator.
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import sys

import yaml

from enclave.common.config import load_config
from enclave.common.logging import get_logger, setup_logging
from enclave.orchestrator.ipc import IPCServer
from enclave.orchestrator.matrix_client import EnclaveMatrixClient
from enclave.orchestrator.router import MessageRouter
from enclave.orchestrator.session_manager import SessionManager

log = get_logger("main")


def _sd_notify(state: str) -> None:
    """Send a notification to systemd via NOTIFY_SOCKET (no deps required)."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    try:
        if addr.startswith("@"):
            addr = "\0" + addr[1:]
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sock.connect(addr)
        sock.sendall(state.encode())
        sock.close()
    except Exception:
        pass


async def _watchdog_loop() -> None:
    """Periodically ping the systemd watchdog."""
    usec = os.environ.get("WATCHDOG_USEC")
    if not usec:
        return
    # Ping at half the watchdog interval
    interval = int(usec) / 1_000_000 / 2
    while True:
        await asyncio.sleep(interval)
        _sd_notify("WATCHDOG=1")


def _persist_control_room_id(config_path: str | None, room_id: str) -> None:
    """Write the control_room_id back to the YAML config file."""
    if not config_path:
        return
    try:
        with open(config_path) as f:
            data = yaml.safe_load(f) or {}
        data.setdefault("matrix", {})["control_room_id"] = room_id
        with open(config_path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        log.info("Persisted control_room_id to %s", config_path)
    except Exception as e:
        log.warning("Could not persist control_room_id: %s", e)


async def run() -> None:
    """Run the orchestrator."""
    config_path = sys.argv[1] if len(sys.argv) > 1 else None
    config = load_config(config_path)
    setup_logging(config.log_level)

    log.info("Starting Enclave orchestrator")

    # Matrix client
    matrix = EnclaveMatrixClient(
        homeserver=config.matrix.homeserver,
        user_id=config.matrix.user_id,
        password=config.matrix.password,
        store_path=config.matrix.store_path,
        device_name=config.matrix.device_name,
    )

    if not await matrix.login():
        log.error("Matrix login failed — exiting")
        sys.exit(1)

    await matrix.initial_sync()

    # Resolve or create the control room
    control_room_id = config.matrix.control_room_id
    if not control_room_id or control_room_id not in matrix.client.rooms:
        # Check if we're already in a room with the configured name
        for rid, room in matrix.client.rooms.items():
            if room.name == config.matrix.control_room_name:
                control_room_id = rid
                log.info("Found existing control room '%s': %s",
                         config.matrix.control_room_name, rid)
                break
        else:
            # Create the control room and invite configured users
            invite_users = [u.matrix_id for u in config.users] if config.users else []
            control_room_id = await matrix.create_room(
                name=config.matrix.control_room_name,
                topic="Enclave orchestrator control room",
                invite=invite_users,
                space_id=config.matrix.space_id or None,
            )
            if not control_room_id:
                log.error("Failed to create control room — exiting")
                sys.exit(1)
            log.info("Created control room '%s': %s",
                     config.matrix.control_room_name, control_room_id)

            # Invite users (create_room passes invite=[] currently, so do it explicitly)
            for user_id in invite_users:
                await matrix.invite_user(control_room_id, user_id)

        # Persist the room ID so we reuse it next startup
        if control_room_id != config.matrix.control_room_id:
            config.matrix.control_room_id = control_room_id
            _persist_control_room_id(config_path, control_room_id)

    # IPC server
    ipc = IPCServer(socket_dir=config.container.socket_dir)

    # Session manager (owns sessions, coordinates all subsystems)
    sessions = SessionManager(
        config=config.container,
        matrix=matrix,
        ipc=ipc,
    )

    # Message router
    allowed = [u.matrix_id for u in config.users] if config.users else None
    router = MessageRouter(
        matrix=matrix,
        ipc=ipc,
        sessions=sessions,
        control_room_id=config.matrix.control_room_id,
        space_id=config.matrix.space_id,
        allowed_users=allowed,
        user_mappings=config.users,
        data_dir=config.data_dir,
        approval_timeout=config.approval_timeout,
        idle_timeout=config.idle_timeout,
        memory_config=config.memory,
    )
    await router.start()

    log.info("Enclave orchestrator running")

    # Notify systemd that we're ready and start watchdog pings
    _sd_notify("READY=1")
    _sd_notify("WATCHDOG=1")

    # Handle graceful shutdown
    stop_event = asyncio.Event()

    def handle_signal() -> None:
        log.info("Shutdown signal received")
        stop_event.set()
        matrix.stop_sync()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    # Start Matrix sync in background with watchdog
    sync_task = asyncio.create_task(matrix.sync_forever())
    watchdog_task = asyncio.create_task(_watchdog_loop())

    def _on_sync_done(task: asyncio.Task[None]) -> None:
        """Restart sync if it exits unexpectedly."""
        if stop_event.is_set():
            return
        exc = task.exception() if not task.cancelled() else None
        if exc:
            log.error("Sync loop crashed: %s — restarting", exc)
        else:
            log.warning("Sync loop exited unexpectedly — restarting")
        nonlocal sync_task
        sync_task = asyncio.create_task(matrix.sync_forever())
        sync_task.add_done_callback(_on_sync_done)

    sync_task.add_done_callback(_on_sync_done)

    # Wait for shutdown
    await stop_event.wait()

    # Cleanup
    log.info("Shutting down...")
    sync_task.cancel()
    try:
        await sync_task
    except asyncio.CancelledError:
        pass

    await router.stop()
    await ipc.close_all()
    await matrix.close()
    log.info("Enclave orchestrator stopped")


def main() -> None:
    """CLI entry point."""
    asyncio.run(run())


if __name__ == "__main__":
    main()
