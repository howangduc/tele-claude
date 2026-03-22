{
  description = "tele-claude - Telegram bot for forwarding messages to Claude Code tmux panes";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";

    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      nixpkgs,
      pyproject-nix,
      uv2nix,
      pyproject-build-systems,
      ...
    }:
    let
      inherit (nixpkgs) lib;
      forAllSystems = lib.genAttrs lib.systems.flakeExposed;

      # Load the uv workspace from uv.lock and pyproject.toml
      workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };

      # Production overlay (immutable builds, wheel preference)
      overlay = workspace.mkPyprojectOverlay {
        sourcePreference = "wheel";
      };

      # Development overlay (editable installs, source changes reflected immediately)
      editableOverlay = workspace.mkEditablePyprojectOverlay {
        root = "$REPO_ROOT";
      };

      # Build the Python package sets for each system
      pythonSets = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          python = pkgs.python312;
        in
        (pkgs.callPackage pyproject-nix.build.packages {
          inherit python;
        }).overrideScope
          (
            lib.composeManyExtensions [
              pyproject-build-systems.overlays.wheel
              overlay
            ]
          )
      );

    in
    {
      # Production package: `nix build`
      packages = forAllSystems (system: {
        default = pythonSets.${system}.mkVirtualEnv "tele-claude-env" workspace.deps.default;
      });

      # Development shell: `nix develop`
      devShells = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          # Apply editable overlay on top of the production set for dev
          editablePythonSet = pythonSets.${system}.overrideScope editableOverlay;
          virtualenv = editablePythonSet.mkVirtualEnv "tele-claude-dev-env" workspace.deps.all;
        in
        {
          default = pkgs.mkShell {
            packages = [
              virtualenv
              pkgs.uv
            ];
            env = {
              # Prevent uv from managing the virtualenv (Nix handles it)
              UV_NO_SYNC = "1";
              # Use the Nix-provided Python interpreter
              UV_PYTHON = editablePythonSet.python.interpreter;
              # Never download Python interpreters (use Nix's)
              UV_PYTHON_DOWNLOADS = "never";
            };
            shellHook = ''
              unset PYTHONPATH
              export REPO_ROOT=$(git rev-parse --show-toplevel)
            '';
          };
        }
      );
    };
}
