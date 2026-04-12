#!/bin/bash
# Install tb (tokenburn) — Claude Pro Max token dashboard
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="${HOME}/.local/bin"

echo "Installing tb..."

# Check dependencies
python3 -c "import textual, rich" 2>/dev/null || {
    echo "Installing Python dependencies..."
    pip install textual rich
}

# Install launcher
mkdir -p "$BIN_DIR"
cat > "${BIN_DIR}/tb" << EOF
#!/usr/bin/env bash
exec python3 "${SCRIPT_DIR}/tokenburn.py" "\$@"
EOF
chmod +x "${BIN_DIR}/tb"

# Ensure ~/.local/bin is on PATH
if ! echo "$PATH" | grep -q "${BIN_DIR}"; then
    echo ""
    echo "Add to your shell profile:"
    echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

echo "Done. Run 'tb' to launch."
