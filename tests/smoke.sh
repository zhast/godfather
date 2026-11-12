#!/bin/bash
# The suite must not launch a model CLI. Tripwire agy/codex/grok sit first on
# PATH, record any invocation, and fail the run. Tests that need a CLI prepend
# their own fakes or pass an absolute fake path.
set -euo pipefail
root=$(cd -- "$(dirname -- "$0")/.." && pwd -P)
export PYTHONDONTWRITEBYTECODE=1
# Worker startup files otherwise reinsert installed shims in child shells,
# even after the GODFATHER variables below have been cleared.
unset BASH_ENV ZDOTDIR CODEX_SANDBOX CODEX_HOME GROK_HOME
clean_path=''
old_ifs=$IFS; IFS=:
for entry in $PATH; do
  case "$entry" in *.shims) continue ;; esac
  clean_path=${clean_path:+$clean_path:}$entry
done
IFS=$old_ifs
export PATH=$clean_path
# A suite launched inside a live run must not inherit its context, paths,
# review callbacks, Cursor settings, or saved PATH. Tests supply their own worker.
for variable in $(compgen -v); do
  case "$variable" in AGENT_*|GODFATHER_*|CURSOR_*) unset "$variable" ;; esac
done
trip=$(mktemp -d)
hits="$trip/hits"
export AGENT_RUNS_DIR="$trip/runs"
export XDG_CONFIG_HOME="$trip/config"
export XDG_STATE_HOME="$trip/state"
mkdir -p "$AGENT_RUNS_DIR" "$XDG_CONFIG_HOME" "$XDG_STATE_HOME"
cleanup() { rm -rf "$trip"; }
trap cleanup EXIT
for cli in agy codex grok cursor-agent; do
  printf '#!/bin/bash\necho "%s $*" >> "%s"\nexit 97\n' "$cli" "$hits" > "$trip/$cli"
  chmod +x "$trip/$cli"
done
# Read-only attachment probes are allowed, but never invoke an installed devbox CLI.
cat > "$trip/devbox" <<EOF
#!/bin/bash
if [[ \$* == 'status --json' ]]; then
  echo 'devbox must be run inside a project' >&2
  exit 1
fi
echo "devbox \$*" >> "$hits"
exit 97
EOF
chmod +x "$trip/devbox"
export PATH="$trip:$PATH"
set +e
python3 -m unittest discover -s "$root/tests" -p 'test_*.py' -v
code=$?
set -e
if [[ -s "$hits" ]]; then
  echo 'suite launched model CLIs:' >&2
  sort "$hits" | uniq -c >&2
  exit 1
fi
exit "$code"
