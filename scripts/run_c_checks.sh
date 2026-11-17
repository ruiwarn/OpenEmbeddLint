#!/usr/bin/env bash
# Compatibility entry point. The implementation is Python and has no Bash/jq dependency.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for candidate in python3 python /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
    exec "$candidate" "$SCRIPT_DIR/c_verify.py" "$@"
  fi
done
echo 'OpenEmbeddLint requires Python 3.10 or newer.' >&2
exit 2
