#!/bin/bash
# Compatible with macOS Bash 3.2 and newer.
set -euo pipefail
usage() {
  cat <<'HELP'
Usage: [PREFIX="$HOME/.local/bin"] ./install.sh [--uninstall]
Copy bin/* into PREFIX, or remove only unchanged files owned by this install.
Installed commands and helper modules are read-only (chmod a-w). Re-running
publishes same-directory temporary files with atomic renames.
Set GODFATHER_MCP_SDK=0 to skip the optional private MCP SDK installation.
Existing unrelated files are never overwritten. --uninstall still removes
unchanged files. Requires Python 3.8+ and Bash 3.2+. No CLI is required,
and this script never launches one. Run `godfather doctor` afterwards.
HELP
}
if [[ $# == 1 && ( $1 == --help || $1 == -h ) ]]; then usage; exit 0; fi
if [[ $# -gt 1 || ( $# == 1 && $1 != --uninstall ) ]]; then usage >&2; exit 2; fi
if (( BASH_VERSINFO[0] < 3 || (BASH_VERSINFO[0] == 3 && BASH_VERSINFO[1] < 2) )); then
  echo 'Bash 3.2+ required. On macOS: brew install bash' >&2
  exit 1
fi
command -v python3 >/dev/null 2>&1 || { echo 'python3 is required' >&2; exit 1; }
source_dir=$(cd -- "$(dirname -- "$0")" && pwd -P)
prefix=${PREFIX:-$HOME/.local/bin}
python3 - "$source_dir" "$prefix" "${1:-}" <<'PY'
import hashlib
import json
import os
import tempfile
from pathlib import Path
import shutil
import sys

if sys.version_info < (3, 8):
    sys.exit('Python 3.8+ is required')
source, prefix, mode = sys.argv[1:]
root = Path(prefix).expanduser().resolve()
manifest = root / '.godfather-install.json'
try:
    owned = json.loads(manifest.read_text())
except FileNotFoundError:
    owned = {}
except (OSError, ValueError):
    sys.exit('Cannot read installation manifest; leaving files untouched')

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def allow_update(path):
    # A previous install left the file read-only. The owner can still replace it.
    path.chmod(path.stat().st_mode | 0o200)

def publish(path):
    # chmod a-w. Commands stay executable; Python modules stay readable.
    path.chmod((0o755 if path.suffix != '.py' else 0o644) & ~0o222)

names = ('cursor-run', 'godfather_cursor.py', 'godfather_cursor_result.py', 'codex-run', 'agy-run', 'grok-run', 'agents-status', 'agents-watch', 'agents-nudge', 'agent_oversight.py', 'godfather', 'godfather_router.py', 'godfather_safety.py', 'godfather_policy.py', 'godfather_permissions.py', 'godfather_lifecycle.py', 'godfather_usage.py', 'godfather_top.py', 'godfather_mcp.py', 'godfather_store.py', 'godfather_registry.py', 'godfather_stream.py', 'godfather_errors.py', 'godfather_codex_bridge.py', 'godfather_supervisor.py', 'godfather_plan.py', 'godfather_plan_run.py')
files = [Path(source) / 'bin' / name for name in names]
# Always limit operations to the fixed package inventory, never arbitrary manifest paths.
if mode == '--uninstall':
    remaining = dict(owned)
    remaining.pop('_source_bin', None)
    for item in files:
        dest = root / item.name
        if item.name not in owned:
            continue
        if not dest.exists() and not dest.is_symlink():
            remaining.pop(item.name, None)
        elif not dest.is_symlink() and dest.is_file() and digest(dest) == owned[item.name]:
            allow_update(dest)
            dest.unlink()
            remaining.pop(item.name, None)
            print('Removed ' + item.name)
        else:
            print('Kept modified file: ' + item.name, file=sys.stderr)
    if remaining:
        manifest.write_text(json.dumps(remaining, indent=2) + '\n')
    elif manifest.exists():
        manifest.unlink()
    sys.exit(0)

root.mkdir(parents=True, exist_ok=True)
# Preflight all conflicts before copying any file.
for item in files:
    dest = root / item.name
    if dest.exists() or dest.is_symlink():
        if dest.is_symlink() or not dest.is_file() or digest(dest) not in (owned.get(item.name), digest(item)):
            sys.exit('Refusing to overwrite unrelated or modified file: ' + item.name)
def atomic_install(dest, content, mode):
    fd, temp = tempfile.mkstemp(prefix='.' + dest.name + '-', dir=str(root))
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temp, mode)
        os.replace(temp, dest)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)

for item in files:
    dest = root / item.name
    content = item.read_bytes()
    if item.name == 'godfather_safety.py':
        content = item.read_text().replace('SOURCE_BIN = None', 'SOURCE_BIN = ' + repr(str(Path(source) / 'bin')), 1).encode()
    atomic_install(dest, content, (0o755 if item.suffix != '.py' else 0o644) & ~0o222)
    owned[item.name] = digest(dest)
owned['_source_bin'] = str(Path(source) / 'bin')
atomic_install(manifest, (json.dumps(owned, indent=2) + '\n').encode(), 0o600)
commands = sum(1 for item in files if item.suffix != '.py')
print('Installed %d commands and %d shared helpers atomically.' % (commands, len(files) - commands))
PY
if [[ ${1:-} != --uninstall ]]; then
  # Prefer the official MCP SDK in a private runtime; never modify system Python.
  # Offline/minimal installations retain the standard-library protocol server.
  python3 - <<'PYSDK'
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
if os.getenv('GODFATHER_MCP_SDK') == '0':
    print('MCP: standard-library stdio server (SDK installation disabled).')
elif importlib.util.find_spec('mcp'):
    print('MCP: official Python SDK available.')
else:
    root = Path(os.getenv('XDG_DATA_HOME', str(Path.home() / '.local/share'))) / 'godfather/mcp-venv'
    runtime = root / 'bin/python'
    try:
        if not runtime.exists():
            subprocess.run([sys.executable, '-m', 'venv', str(root)], check=True, capture_output=True, timeout=30)
        probe = subprocess.run([str(runtime), '-c', 'import mcp'], capture_output=True, timeout=10)
        if probe.returncode:
            subprocess.run([str(runtime), '-m', 'pip', 'install', '--disable-pip-version-check', 'mcp>=1.12,<2'],
                           check=True, capture_output=True, timeout=60)
        print('MCP: official Python SDK installed in ' + str(root))
    except (OSError, subprocess.SubprocessError):
        print('MCP: SDK unavailable; using standard-library stdio server.')
PYSDK
  printf 'Installed. Run `godfather doctor` for passive CLI checks. `godfather doctor --live` probes codex, agy and cursor.\n'
  printf 'Add to your shell PATH: export PATH="%s:$PATH"\n' "$prefix"
fi
