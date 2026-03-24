"""Enclave orchestrator main entry point.

Wires together Matrix client, IPC server, container manager,
and message router into a running orchestrator.
"""

from __future__ import annotations

import asyncio
import signal
import sys

from enclave.common.config import load_config
from enclave.common.logging import get_logger, setup_logging
from enclave.orchestrator.container import ContainerManager
from enclave.orchestrator.ipc import IPCServer
from enclave.orchestrator.matrix_client import EnclaveMatrixClient
from enclave.orchestrator.router import MessageRouter

log = get_logger("main")


async def run() -> None:
    """Run the orchestrator."""
    config = load_config()
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

    # IPC server
    ipc = IPCServer(socket_dir=config.container.socket_dir)

    # Container manager
    containers = ContainerManager(config=config.container)

    # Message router
    allowed = [u.matrix_id for u in config.users] if config.users else None
    router = MessageRouter(
        matrix=matrix,
        ipc=ipc,
        containers=containers,
        control_room_id=config.matrix.control_room_id,
        space_id=config.matrix.space_id,
        allowed_users=allowed,
    )
    await router.start()

    log.info("Enclave orchestrator running")

    # Handle graceful shutdown
    stop_event = asyncio.Event()

    def handle_signal() -> None:
        log.info("Shutdown signal received")
        stop_event.set()
        matrix.stop_sync()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    # Start Matrix sync in background
    sync_task = asyncio.create_task(matrix.sync_forever())

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
