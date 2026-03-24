//! Command and mount executor.

use crate::config::BrokerConfig;
use crate::protocol::{BrokerRequest, BrokerResponse};
use std::time::Duration;
use tokio::process::Command;
use tracing::{error, info, warn};

/// Handle a broker request and return a response.
pub async fn handle_request(cfg: &BrokerConfig, req: BrokerRequest) -> BrokerResponse {
    match req {
        BrokerRequest::Exec {
            id,
            session_id,
            command,
            args,
            timeout_secs,
        } => {
            info!(
                id = %id,
                session = %session_id,
                command = %command,
                "Executing command"
            );

            // Check allowlist/denylist
            let full_cmd = if args.is_empty() {
                command.clone()
            } else {
                format!("{} {}", command, args.join(" "))
            };

            if !cfg.is_command_allowed(&full_cmd) {
                warn!(command = %full_cmd, "Command denied by policy");
                return BrokerResponse::error(id, "Command denied by policy");
            }

            let timeout = Duration::from_secs(timeout_secs.unwrap_or(cfg.timeout_secs));
            exec_command(&id, &command, &args, timeout).await
        }

        BrokerRequest::Mount {
            id,
            session_id,
            source,
            target,
        } => {
            info!(
                id = %id,
                session = %session_id,
                source = %source,
                target = %target,
                "Mount request"
            );

            if !cfg.is_mount_allowed(&source) {
                warn!(source = %source, "Mount path denied by policy");
                return BrokerResponse::error(id, "Mount path denied by policy");
            }

            exec_mount(&id, &source, &target).await
        }

        BrokerRequest::Umount {
            id,
            session_id,
            target,
        } => {
            info!(
                id = %id,
                session = %session_id,
                target = %target,
                "Umount request"
            );
            exec_umount(&id, &target).await
        }

        BrokerRequest::MakeShared {
            id,
            session_id,
            path,
        } => {
            info!(
                id = %id,
                session = %session_id,
                path = %path,
                "Make shared request"
            );
            exec_make_shared(&id, &path).await
        }

        BrokerRequest::Ping { id } => {
            info!(id = %id, "Ping");
            BrokerResponse::ok(id)
        }
    }
}

async fn exec_command(
    id: &str,
    command: &str,
    args: &[String],
    timeout: Duration,
) -> BrokerResponse {
    let result = tokio::time::timeout(timeout, async {
        Command::new(command)
            .args(args)
            .output()
            .await
    })
    .await;

    match result {
        Ok(Ok(output)) => {
            let stdout = String::from_utf8_lossy(&output.stdout).to_string();
            let stderr = String::from_utf8_lossy(&output.stderr).to_string();
            let code = output.status.code().unwrap_or(-1);

            info!(
                id = %id,
                exit_code = code,
                stdout_len = stdout.len(),
                stderr_len = stderr.len(),
                "Command completed"
            );

            BrokerResponse::exec_result(id.to_string(), code, stdout, stderr)
        }
        Ok(Err(e)) => {
            error!(id = %id, error = %e, "Command execution failed");
            BrokerResponse::error(id.to_string(), format!("Execution failed: {}", e))
        }
        Err(_) => {
            error!(id = %id, "Command timed out");
            BrokerResponse::error(id.to_string(), "Command timed out")
        }
    }
}

async fn exec_mount(id: &str, source: &str, target: &str) -> BrokerResponse {
    // Create target directory if needed
    if let Err(e) = tokio::fs::create_dir_all(target).await {
        return BrokerResponse::error(id.to_string(), format!("mkdir failed: {}", e));
    }

    let output = Command::new("mount")
        .args(["--bind", source, target])
        .output()
        .await;

    match output {
        Ok(out) if out.status.success() => {
            info!(source = %source, target = %target, "Mount successful");
            BrokerResponse::ok(id.to_string())
        }
        Ok(out) => {
            let stderr = String::from_utf8_lossy(&out.stderr);
            error!(source = %source, target = %target, stderr = %stderr, "Mount failed");
            BrokerResponse::error(id.to_string(), format!("mount failed: {}", stderr))
        }
        Err(e) => BrokerResponse::error(id.to_string(), format!("mount exec failed: {}", e)),
    }
}

async fn exec_umount(id: &str, target: &str) -> BrokerResponse {
    let output = Command::new("umount")
        .arg(target)
        .output()
        .await;

    match output {
        Ok(out) if out.status.success() => {
            info!(target = %target, "Umount successful");
            BrokerResponse::ok(id.to_string())
        }
        Ok(out) => {
            let stderr = String::from_utf8_lossy(&out.stderr);
            BrokerResponse::error(id.to_string(), format!("umount failed: {}", stderr))
        }
        Err(e) => BrokerResponse::error(id.to_string(), format!("umount exec failed: {}", e)),
    }
}

async fn exec_make_shared(id: &str, path: &str) -> BrokerResponse {
    // Step 1: bind mount to self
    let out1 = Command::new("mount")
        .args(["--bind", path, path])
        .output()
        .await;

    if let Ok(o) = &out1 {
        if !o.status.success() {
            let stderr = String::from_utf8_lossy(&o.stderr);
            return BrokerResponse::error(
                id.to_string(),
                format!("bind mount failed: {}", stderr),
            );
        }
    } else if let Err(e) = out1 {
        return BrokerResponse::error(id.to_string(), format!("bind mount exec failed: {}", e));
    }

    // Step 2: make shared
    let out2 = Command::new("mount")
        .args(["--make-shared", path])
        .output()
        .await;

    match out2 {
        Ok(o) if o.status.success() => {
            info!(path = %path, "Made shared mount");
            BrokerResponse::ok(id.to_string())
        }
        Ok(o) => {
            let stderr = String::from_utf8_lossy(&o.stderr);
            BrokerResponse::error(id.to_string(), format!("make-shared failed: {}", stderr))
        }
        Err(e) => BrokerResponse::error(id.to_string(), format!("make-shared exec failed: {}", e)),
    }
}
