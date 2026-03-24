//! Configuration for the privilege broker.

use serde::Deserialize;
use std::path::Path;

#[derive(Debug, Clone, Deserialize)]
pub struct BrokerConfig {
    /// Unix socket path
    #[serde(default = "default_socket_path")]
    pub socket_path: String,

    /// Socket permissions (octal)
    #[serde(default = "default_socket_mode")]
    pub socket_mode: u32,

    /// Allowed user/group to connect
    #[serde(default = "default_allowed_user")]
    pub allowed_user: String,

    /// Command execution timeout in seconds
    #[serde(default = "default_timeout")]
    pub timeout_secs: u64,

    /// Allowed commands (regex patterns)
    #[serde(default)]
    pub allowed_commands: Vec<String>,

    /// Denied commands (regex patterns, checked first)
    #[serde(default = "default_denied_commands")]
    pub denied_commands: Vec<String>,

    /// Allowed mount source paths (regex patterns)
    #[serde(default)]
    pub allowed_mount_paths: Vec<String>,

    /// Denied mount paths (checked first)
    #[serde(default = "default_denied_paths")]
    pub denied_mount_paths: Vec<String>,
}

fn default_socket_path() -> String {
    "/run/enclave-priv/broker.sock".to_string()
}

fn default_socket_mode() -> u32 {
    0o660
}

fn default_allowed_user() -> String {
    "ian".to_string()
}

fn default_timeout() -> u64 {
    30
}

fn default_denied_commands() -> Vec<String> {
    vec![
        r"^rm\s+-rf\s+/\s*$".to_string(),  // rm -rf /
        r"^chmod\s+777\s+/".to_string(),     // chmod 777 /
        r"^dd\s+.*of=/dev/".to_string(),     // dd to devices
    ]
}

fn default_denied_paths() -> Vec<String> {
    vec![
        r"^/boot".to_string(),
        r"^/dev".to_string(),
        r"^/proc".to_string(),
        r"^/sys".to_string(),
    ]
}

impl Default for BrokerConfig {
    fn default() -> Self {
        Self {
            socket_path: default_socket_path(),
            socket_mode: default_socket_mode(),
            allowed_user: default_allowed_user(),
            timeout_secs: default_timeout(),
            allowed_commands: vec![],
            denied_commands: default_denied_commands(),
            allowed_mount_paths: vec![r"^/home/".to_string()],
            denied_mount_paths: default_denied_paths(),
        }
    }
}

impl BrokerConfig {
    pub fn load(path: &str) -> Result<Self, Box<dyn std::error::Error>> {
        let p = Path::new(path);
        if !p.exists() {
            return Err(format!("Config file not found: {}", path).into());
        }
        let content = std::fs::read_to_string(p)?;
        let config: Self = toml::from_str(&content)?;
        Ok(config)
    }

    /// Check if a command is allowed by the allowlist/denylist.
    pub fn is_command_allowed(&self, command: &str) -> bool {
        // Check denylist first
        for pattern in &self.denied_commands {
            if let Ok(re) = regex::Regex::new(pattern) {
                if re.is_match(command) {
                    return false;
                }
            }
        }

        // If allowlist is empty, allow all (that aren't denied)
        if self.allowed_commands.is_empty() {
            return true;
        }

        // Check allowlist
        for pattern in &self.allowed_commands {
            if let Ok(re) = regex::Regex::new(pattern) {
                if re.is_match(command) {
                    return true;
                }
            }
        }

        false
    }

    /// Check if a mount source path is allowed.
    pub fn is_mount_allowed(&self, path: &str) -> bool {
        for pattern in &self.denied_mount_paths {
            if let Ok(re) = regex::Regex::new(pattern) {
                if re.is_match(path) {
                    return false;
                }
            }
        }

        if self.allowed_mount_paths.is_empty() {
            return true;
        }

        for pattern in &self.allowed_mount_paths {
            if let Ok(re) = regex::Regex::new(pattern) {
                if re.is_match(path) {
                    return true;
                }
            }
        }

        false
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_default_config() {
        let cfg = BrokerConfig::default();
        assert_eq!(cfg.socket_path, "/run/enclave-priv/broker.sock");
        assert_eq!(cfg.timeout_secs, 30);
        assert!(!cfg.denied_commands.is_empty());
    }

    #[test]
    fn test_dangerous_commands_denied() {
        let cfg = BrokerConfig::default();
        assert!(!cfg.is_command_allowed("rm -rf /"));
        assert!(!cfg.is_command_allowed("chmod 777 /etc"));
        assert!(!cfg.is_command_allowed("dd if=/dev/zero of=/dev/sda"));
    }

    #[test]
    fn test_safe_commands_allowed() {
        let cfg = BrokerConfig::default();
        assert!(cfg.is_command_allowed("apt update"));
        assert!(cfg.is_command_allowed("mount --bind /home/a /home/b"));
        assert!(cfg.is_command_allowed("systemctl restart nginx"));
    }

    #[test]
    fn test_mount_paths() {
        let cfg = BrokerConfig::default();
        assert!(cfg.is_mount_allowed("/home/ian/projects"));
        assert!(!cfg.is_mount_allowed("/proc/1/mem"));
        assert!(!cfg.is_mount_allowed("/sys/class"));
        assert!(!cfg.is_mount_allowed("/boot/vmlinuz"));
    }

    #[test]
    fn test_allowlist_restricts() {
        let cfg = BrokerConfig {
            allowed_commands: vec![r"^apt\s+".to_string()],
            ..Default::default()
        };
        assert!(cfg.is_command_allowed("apt update"));
        assert!(!cfg.is_command_allowed("systemctl restart nginx"));
    }

    #[test]
    fn test_load_missing_file() {
        let result = BrokerConfig::load("/nonexistent/path.toml");
        assert!(result.is_err());
    }
}
