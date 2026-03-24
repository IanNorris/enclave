//! Enclave Privilege Broker
//!
//! A root-level daemon that accepts privilege escalation requests from the
//! orchestrator, forwards them to Matrix for user approval, and executes
//! approved commands.
//!
//! This is a placeholder — implementation comes in Phase 3.

use tracing::info;

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt::init();
    info!("enclave-priv-broker starting (placeholder)");

    // TODO Phase 3:
    // 1. Listen on Unix socket at /run/aeon-priv/broker.sock
    // 2. Accept privilege request messages from orchestrator
    // 3. Forward approval requests to Matrix via orchestrator
    // 4. Wait for user approval (with timeout)
    // 5. Execute approved commands as root
    // 6. Return stdout/stderr/exit code
    // 7. Log everything to journald

    info!("enclave-priv-broker placeholder — no-op, exiting");
}
