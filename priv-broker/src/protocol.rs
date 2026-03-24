//! IPC protocol definitions shared with the Python orchestrator.

use serde::{Deserialize, Serialize};

/// Request types the broker handles.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type")]
pub enum BrokerRequest {
    /// Execute a command as root
    #[serde(rename = "exec")]
    Exec {
        id: String,
        session_id: String,
        command: String,
        #[serde(default)]
        args: Vec<String>,
        #[serde(default)]
        timeout_secs: Option<u64>,
    },

    /// Bind mount a path
    #[serde(rename = "mount")]
    Mount {
        id: String,
        session_id: String,
        source: String,
        target: String,
    },

    /// Unmount a path
    #[serde(rename = "umount")]
    Umount {
        id: String,
        session_id: String,
        target: String,
    },

    /// Set up shared mount propagation
    #[serde(rename = "make_shared")]
    MakeShared {
        id: String,
        session_id: String,
        path: String,
    },

    /// Ping / health check
    #[serde(rename = "ping")]
    Ping { id: String },
}

/// Response from the broker.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BrokerResponse {
    pub id: String,
    pub success: bool,
    pub exit_code: Option<i32>,
    pub stdout: Option<String>,
    pub stderr: Option<String>,
    pub error: Option<String>,
}

impl BrokerResponse {
    pub fn ok(id: String) -> Self {
        Self {
            id,
            success: true,
            exit_code: Some(0),
            stdout: None,
            stderr: None,
            error: None,
        }
    }

    pub fn error(id: String, msg: impl Into<String>) -> Self {
        Self {
            id,
            success: false,
            exit_code: None,
            stdout: None,
            stderr: None,
            error: Some(msg.into()),
        }
    }

    pub fn exec_result(id: String, code: i32, stdout: String, stderr: String) -> Self {
        Self {
            id,
            success: code == 0,
            exit_code: Some(code),
            stdout: Some(stdout),
            stderr: Some(stderr),
            error: None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_serialize_exec_request() {
        let req = BrokerRequest::Exec {
            id: "r1".to_string(),
            session_id: "s1".to_string(),
            command: "apt".to_string(),
            args: vec!["update".to_string()],
            timeout_secs: Some(30),
        };
        let json = serde_json::to_string(&req).unwrap();
        assert!(json.contains("\"type\":\"exec\""));
        assert!(json.contains("\"command\":\"apt\""));
    }

    #[test]
    fn test_serialize_mount_request() {
        let req = BrokerRequest::Mount {
            id: "r2".to_string(),
            session_id: "s1".to_string(),
            source: "/home/ian/code".to_string(),
            target: "/workspace/code".to_string(),
        };
        let json = serde_json::to_string(&req).unwrap();
        assert!(json.contains("\"type\":\"mount\""));
    }

    #[test]
    fn test_deserialize_ping() {
        let json = r#"{"type":"ping","id":"p1"}"#;
        let req: BrokerRequest = serde_json::from_str(json).unwrap();
        match req {
            BrokerRequest::Ping { id } => assert_eq!(id, "p1"),
            _ => panic!("Expected Ping"),
        }
    }

    #[test]
    fn test_response_ok() {
        let resp = BrokerResponse::ok("r1".to_string());
        assert!(resp.success);
        assert_eq!(resp.exit_code, Some(0));
    }

    #[test]
    fn test_response_error() {
        let resp = BrokerResponse::error("r1".to_string(), "denied");
        assert!(!resp.success);
        assert_eq!(resp.error, Some("denied".to_string()));
    }

    #[test]
    fn test_response_exec_result() {
        let resp = BrokerResponse::exec_result(
            "r1".to_string(), 0,
            "output".to_string(), "".to_string(),
        );
        assert!(resp.success);
        assert_eq!(resp.stdout, Some("output".to_string()));
    }
}
