//! Unix socket server for the privilege broker.

use crate::config::BrokerConfig;
use crate::executor;
use crate::protocol::{BrokerRequest, BrokerResponse};
use std::os::unix::fs::PermissionsExt;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::UnixListener;
use tracing::{error, info, warn};

/// Run the broker server.
pub async fn run(cfg: BrokerConfig) -> Result<(), Box<dyn std::error::Error>> {
    // Remove stale socket
    let _ = tokio::fs::remove_file(&cfg.socket_path).await;

    let listener = UnixListener::bind(&cfg.socket_path)?;

    // Set socket permissions
    let perms = std::fs::Permissions::from_mode(cfg.socket_mode);
    std::fs::set_permissions(&cfg.socket_path, perms)?;

    info!(path = %cfg.socket_path, "Listening for connections");

    loop {
        match listener.accept().await {
            Ok((stream, _addr)) => {
                let cfg = cfg.clone();
                tokio::spawn(async move {
                    if let Err(e) = handle_connection(stream, &cfg).await {
                        error!(error = %e, "Connection handler error");
                    }
                });
            }
            Err(e) => {
                error!(error = %e, "Accept failed");
            }
        }
    }
}

async fn handle_connection(
    stream: tokio::net::UnixStream,
    cfg: &BrokerConfig,
) -> Result<(), Box<dyn std::error::Error>> {
    let (reader, mut writer) = stream.into_split();
    let mut reader = BufReader::new(reader);
    let mut line = String::new();

    info!("Client connected");

    loop {
        line.clear();
        let bytes_read = reader.read_line(&mut line).await?;
        if bytes_read == 0 {
            info!("Client disconnected");
            break;
        }

        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }

        let response = match serde_json::from_str::<BrokerRequest>(trimmed) {
            Ok(req) => {
                info!(request = %trimmed, "Processing request");
                executor::handle_request(cfg, req).await
            }
            Err(e) => {
                warn!(error = %e, input = %trimmed, "Invalid request");
                BrokerResponse::error(
                    "unknown".to_string(),
                    format!("Invalid request: {}", e),
                )
            }
        };

        let mut resp_json = serde_json::to_string(&response)?;
        resp_json.push('\n');
        writer.write_all(resp_json.as_bytes()).await?;
        writer.flush().await?;
    }

    Ok(())
}
