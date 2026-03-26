{
  description = "Enclave — AI agent orchestrator with Matrix chat, podman containers, and Copilot SDK";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };

        python = pkgs.python312;

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
      # NixOS module (Phase 2 — placeholder)
      nixosModules.default = { config, lib, pkgs, ... }: {
        options.services.enclave = {
          enable = lib.mkEnableOption "Enclave AI agent orchestrator";
        };
        config = lib.mkIf config.services.enclave.enable {
          # Phase 2: Full NixOS service configuration
          # systemd.services.enclave = { ... };
        };
      };
    };
}
