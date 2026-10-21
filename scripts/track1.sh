#!/bin/zsh
set -euo pipefail

SCRIPT_DIR=${0:A:h}
REPO_DIR=${SCRIPT_DIR:h}

if [[ -x "$HOME/.docker/bin/docker" ]]; then
  export PATH="$HOME/.docker/bin:$PATH"
fi

exec python3 "$SCRIPT_DIR/track1.py" --repo "$REPO_DIR" "$@"
