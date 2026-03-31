{
  description = "Enclave — AI agent orchestrator with Matrix chat, podman containers, and Copilot SDK";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs {
          inherit system;
          config.permittedInsecurePackages = [
            "olm-3.2.16"
          ];
        };

        python = pkgs.python312;

        # Build the privilege broker (Rust)
        privBroker = pkgs.rustPlatform.buildRustPackage {
          pname = "enclave-priv-broker";
          version = "0.1.0";
          src = ./priv-broker;
          cargoLock.lockFile = ./priv-broker/Cargo.lock;
          meta = with pkgs.lib; {
            description = "Privilege escalation broker for Enclave";
            license = licenses.mit;
            mainProgram = "enclave-priv-broker";
          };
        };

        # Build the Enclave orchestrator as a Python application
        enclave = python.pkgs.buildPythonApplication {
          pname = "enclave";
          version = "0.1.0";
          pyproject = true;

          src = ./.;

          build-system = [ python.pkgs.hatchling ];

          dependencies = with python.pkgs; [
            pyyaml
            aiohttp
            aiohttp-socks
            aiofiles
            h11
            h2
            jsonschema
            pycryptodome
            unpaddedbase64
            pydantic
            python-dateutil
            # matrix-nio and github-copilot-sdk are installed from PyPI
            # via pip in the wrapper below since they're not in nixpkgs
          ];

          # Skip tests during build (run separately)
          doCheck = false;

          # Include prompt markdown files
          postInstall = ''
            prompts=$out/lib/python3.12/site-packages/enclave/agent/prompts
            mkdir -p $prompts
            cp src/enclave/agent/prompts/*.md $prompts/
          '';

          meta = with pkgs.lib; {
            description = "AI agent orchestrator with Matrix chat and podman containers";
            license = licenses.mit;
            maintainers = [ ];
            mainProgram = "enclave";
          };
        };

        # Wrapper that ensures matrix-nio[e2ee] and copilot SDK are available
        # These packages have complex native deps or aren't in nixpkgs
        enclaveWrapped = pkgs.writeShellScriptBin "enclave" ''
          exec ${python}/bin/python3 -m enclave.orchestrator.main "$@"
        '';

        # Full Python environment with all deps (for development and running)
        pythonEnv = python.withPackages (ps: with ps; [
          pyyaml
          aiohttp
          aiohttp-socks
          aiofiles
          h11
          h2
          jsonschema
          pycryptodome
          unpaddedbase64
          pydantic
          python-dateutil
          rich
          textual
          # Dev deps
          pytest
          pytest-asyncio
          pytest-cov
          ruff
          mypy
        ]);

      in {
        packages = {
          default = enclave;
          enclave = enclave;
          priv-broker = privBroker;
        };

        # Development shell — everything you need to work on Enclave
        devShells.default = pkgs.mkShell {
          packages = [
            pythonEnv
            pkgs.podman
            pkgs.olm
            pkgs.pkg-config
            pkgs.gcc
            pkgs.git
            pkgs.jq
            pkgs.curl
          ];

          shellHook = ''
            echo "🔒 Enclave development shell"
            echo "   Python: ${python}/bin/python3"
            echo "   Run tests: python3 -m pytest tests/unit/"
            echo "   Run enclave: python3 -m enclave.orchestrator.main"
            echo ""
            echo "   Install pip deps not in nixpkgs:"
            echo "     pip install --user github-copilot-sdk 'matrix-nio[e2ee]'"
            export PYTHONPATH="$PWD/src:$PYTHONPATH"
          '';

          # Make libolm available for matrix-nio[e2ee] compilation
          LD_LIBRARY_PATH = "${pkgs.olm}/lib";
          PKG_CONFIG_PATH = "${pkgs.olm}/lib/pkgconfig";
        };

        # Overlay for importing into other flakes
        overlays.default = final: prev: {
          enclave = self.packages.${system}.default;
        };
      }
    ) // {
      # NixOS module for the privilege broker system service
      nixosModules.default = { config, lib, pkgs, ... }:
        let
          cfg = config.services.enclave;
          brokerCfg = cfg.broker;
          tomlFormat = pkgs.formats.toml { };
          brokerConfigFile = tomlFormat.generate "priv-broker.toml" ({
            socket_path = brokerCfg.socketPath;
            socket_mode = brokerCfg.socketMode;
            allowed_user = brokerCfg.allowedUser;
            timeout_secs = brokerCfg.timeoutSecs;
            denied_commands = brokerCfg.deniedCommands;
            allowed_mount_paths = brokerCfg.allowedMountPaths;
            denied_mount_paths = brokerCfg.deniedMountPaths;
          } // lib.optionalAttrs (brokerCfg.socketGroup != null) {
            socket_group = brokerCfg.socketGroup;
          });
        in {
          options.services.enclave = {
            enable = lib.mkEnableOption "Enclave AI agent orchestrator";

            broker = {
              enable = lib.mkOption {
                type = lib.types.bool;
                default = true;
                description = "Whether to enable the Enclave privilege broker.";
              };

              package = lib.mkOption {
                type = lib.types.package;
                default = self.packages.${pkgs.system}.priv-broker;
                defaultText = lib.literalExpression "self.packages.\${pkgs.system}.priv-broker";
                description = "The enclave-priv-broker package to use.";
              };

              socketPath = lib.mkOption {
                type = lib.types.str;
                default = "/run/enclave-priv/broker.sock";
                description = "Unix socket path for the broker.";
              };

              socketMode = lib.mkOption {
                type = lib.types.int;
                default = 432; # 0o660
                description = "Socket file permissions as a decimal integer (432 = octal 0660).";
              };

              socketGroup = lib.mkOption {
                type = lib.types.nullOr lib.types.str;
                default = "users";
                description = "Group to own the broker socket (allows non-root access). Set to null to skip.";
              };

              allowedUser = lib.mkOption {
                type = lib.types.str;
                description = "User allowed to connect to the broker socket.";
              };

              timeoutSecs = lib.mkOption {
                type = lib.types.int;
                default = 30;
                description = "Default command timeout in seconds.";
              };

              deniedCommands = lib.mkOption {
                type = lib.types.listOf lib.types.str;
                default = [
                  "^rm\\s+-rf\\s+/\\s*$"
                  "^chmod\\s+777\\s+/"
                  "^dd\\s+.*of=/dev/"
                  "^mkfs"
                  "^fdisk"
                ];
                description = "Denied command patterns (regex). Checked before allowed commands.";
              };

              allowedMountPaths = lib.mkOption {
                type = lib.types.listOf lib.types.str;
                default = [
                  "^/home/"
                  "^/tmp/enclave-"
                ];
                description = "Allowed mount source paths (regex).";
              };

              deniedMountPaths = lib.mkOption {
                type = lib.types.listOf lib.types.str;
                default = [
                  "^/boot"
                  "^/dev"
                  "^/proc"
                  "^/sys"
                  "^/etc"
                ];
                description = "Denied mount paths (regex).";
              };
            };
          };

          config = lib.mkIf cfg.enable (lib.mkMerge [
            (lib.mkIf brokerCfg.enable {
              systemd.services.enclave-priv-broker = {
                description = "Enclave Privilege Broker";
                after = [ "network.target" ];
                wantedBy = [ "multi-user.target" ];
                serviceConfig = {
                  Type = "simple";
                  ExecStart = "${brokerCfg.package}/bin/enclave-priv-broker ${brokerConfigFile}";
                  Restart = "on-failure";
                  RestartSec = 5;
                  RuntimeDirectory = "enclave-priv";
                  RuntimeDirectoryMode = "0755";
                };
              };
            })
          ]);
        };
    };
}
