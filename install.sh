#!/usr/bin/env bash
# Screenium installer.
#
# Installs the `screenium` command into ~/.local/bin (adds it to PATH if
# needed) and sets up the project environment with uv. You can run this
# from a fresh clone of the repository.
#
# Usage:
#   git clone https://github.com/samTheComputerArchitect/Screenium.git
#   cd Screenium
#   ./install.sh
set -euo pipefail

INSTALL_DIR="$HOME/.local/bin"
PROJECT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"

echo "==> Screenium installer"

# 1. Make sure uv is available (install into ~/.local/bin if missing).
if ! command -v uv >/dev/null 2>&1; then
    echo "==> uv not found; installing it to $INSTALL_DIR"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # The installer adds ~/.local/bin to PATH for the current shell session.
    export PATH="$HOME/.local/bin:$PATH"
fi
if ! command -v uv >/dev/null 2>&1; then
    echo "error: uv could not be found after installation." >&2
    echo "       Add $INSTALL_DIR to your PATH and re-run this script." >&2
    exit 1
fi
echo "    using uv: $(command -v uv)"

# 2. Set up the project environment (creates .venv and installs deps).
echo "==> Setting up project environment with uv"
uv --directory "$PROJECT_DIR" sync
echo "    environment ready"

# 3. Install the `screenium` command into ~/.local/bin.
echo "==> Installing 'screenium' command into $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
chmod +x "$PROJECT_DIR/screenium"
ln -sfn "$PROJECT_DIR/screenium" "$INSTALL_DIR/screenium"

# 4. Make sure ~/.local/bin is on PATH.
RC_FILE=""
if ! command -v screenium >/dev/null 2>&1; then
    case ":$PATH:" in
        *":$INSTALL_DIR:"*) : ;;
        *)
            echo "==> Adding $INSTALL_DIR to your PATH"
            SHELL_NAME="$(basename "${SHELL:-bash}")"
            RC_FILE="$HOME/.bashrc"
            [ "$SHELL_NAME" = "zsh" ] && RC_FILE="$HOME/.zshrc"
            if ! grep -qF "export PATH=\"$INSTALL_DIR:\$PATH\"" "$RC_FILE" 2>/dev/null; then
                printf '\nexport PATH="%s:$PATH"\n' "$INSTALL_DIR" >>"$RC_FILE"
            fi
            echo "    added to $RC_FILE (open a new terminal or run: source $RC_FILE)"
            ;;
    esac
fi

echo
echo "==> Done! Run 'screenium' to start recording."
if ! command -v screenium >/dev/null 2>&1; then
    echo "    (restart your shell or run: source ${RC_FILE:-your terminal rc file})"
fi
