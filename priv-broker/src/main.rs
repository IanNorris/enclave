//! Enclave Privilege Broker
//!
//! A root-level daemon that accepts privilege escalation requests from the
//! Enclave orchestrator via a Unix socket, executes approved commands,
//! and handles mount operations.

mod config;
mod executor;
mod protocol;
mod server;

use std::path::PathBuf;
use tracing::info;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Initialize logging — try journald first, fall back to stdout
    let subscriber = tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info".into()),
        )
        .finish();
    tracing::subscriber::set_global_default(subscriber)?;

    // Load config
    let config_path = std::env::args()
        .nth(1)
        .unwrap_or_else(|| "/etc/enclave/priv-broker.toml".to_string());

    let cfg = config::BrokerConfig::load(&config_path).unwrap_or_else(|e| {
        info!("Using defaults (config load failed: {})", e);
        config::BrokerConfig::default()
    });

    info!(
        socket = %cfg.socket_path,
        "enclave-priv-broker starting"
    );

    // Ensure socket directory exists
    let socket_dir = PathBuf::from(&cfg.socket_path)
        .parent()
        .map(|p| p.to_path_buf())
        .unwrap_or_else(|| PathBuf::from("/run/enclave-priv"));

    tokio::fs::create_dir_all(&socket_dir).await?;

    // Start the server
    server::run(cfg).await
}
