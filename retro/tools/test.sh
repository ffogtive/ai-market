#!/usr/bin/env bash
# Test runner: automatically selects Python 3.9+ for running retro tests
set -eu

# Calculate repo root (two directories above this script)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$(dirname "$SCRIPT_DIR")")"

# Function to check if a python command has version >= 3.9
check_python_version() {
  local py="$1"
  if $py -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' 2>/dev/null; then
    return 0
  fi
  return 1
}

# Try candidates in order of preference
CHOSEN_PY=""

# 1. Environment variable
if [ -n "${RETRO_PYTHON:-}" ] && check_python_version "$RETRO_PYTHON"; then
  CHOSEN_PY="$RETRO_PYTHON"
fi

# 2-5. Specific python versions
if [ -z "$CHOSEN_PY" ]; then
  for version in 3.12 3.11 3.10 3.9; do
    if check_python_version "python$version" 2>/dev/null; then
      CHOSEN_PY="python$version"
      break
    fi
  done
fi

# 6. Miniconda
if [ -z "$CHOSEN_PY" ] && [ -x "$HOME/miniconda3/bin/python3" ]; then
  if check_python_version "$HOME/miniconda3/bin/python3"; then
    CHOSEN_PY="$HOME/miniconda3/bin/python3"
  fi
fi

# 7. uv (if available)
if [ -z "$CHOSEN_PY" ] && command -v uv >/dev/null 2>&1; then
  if check_python_version "uv run --python 3.11 python"; then
    CHOSEN_PY="uv run --python 3.11 python"
  fi
fi

# 8. Fallback to system python3
if [ -z "$CHOSEN_PY" ] && check_python_version "python3"; then
  CHOSEN_PY="python3"
fi

if [ -z "$CHOSEN_PY" ]; then
  echo "테스트에는 Python 3.9+가 필요합니다."
  exit 1
fi

# Get the version and display info
VERSION=$($CHOSEN_PY -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')
echo "Using Python $VERSION ($CHOSEN_PY)"

# Run tests from repo root
cd "$REPO_ROOT"
$CHOSEN_PY -m unittest discover retro/tests "$@"
