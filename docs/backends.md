# Low-level backends

[Back to the README](../README.md)

The following commands are low-level backends for diagnostics and explicit manual
use. Conductors should delegate through `godfather run`.

## codex-run

```sh
CWD="$PWD" CODEX_MODEL=default codex-run /tmp/parser-brief.md parser
CWD="$PWD" CODEX_MODEL=my-supported-model CODEX_EFFORT=medium CODEX_SERVICE_TIER=default codex-run /tmp/parser-brief.md parser-tuned
CODEX_BIN=/path/to/codex codex-run --help
```

Binary discovery: **`CODEX_BIN` override → `codex` on PATH →
`/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex`**. A bad explicit
override fails rather than silently choosing another binary.

The original defaults are `gpt-6.1-sol`, high effort, default service tier, and
fast mode off. For portability, `CODEX_MODEL=auto` (the default) selects
`gpt-6.1-sol` only when `$CODEX_HOME/models_cache.json` (normally
`~/.codex/models_cache.json`) lists it. Otherwise the launcher omits `-m` so the
installed Codex uses its configured or built-in model. `CODEX_MODEL=default`
always delegates model selection to Codex. An explicit model bypasses discovery.
A cached listing is a hint, not proof of current account access: if it is stale,
set `CODEX_MODEL=default` or choose an available model in Codex's `/model` picker.
There is no automatic retry of a failed task. `CODEX_EFFORT` defaults to `high`;
`CODEX_SERVICE_TIER` defaults to `default`; both must be supported by your CLI/model.
`CODEX_SANDBOX` defaults to `danger-full-access`. Allowed values are
`danger-full-access`, `workspace-write`, and `read-only`. Command shims, the
ask/deny flow, and the audit log are the safety layer. `workspace-write` adds
Codex's own sandbox with network access enabled. Any other value exits 2.
Routed runs and plan tasks honor the same setting; nudges retain it.

## Experimental Codex app-server protocol

The exec backend remains the default. To opt in for a run:

```sh
GODFATHER_CODEX_PROTOCOL=app-server CWD=/path/to/project godfather run -p "Fix the parser" parser
# Also supported by direct codex-run launches.
```

Or add `protocol = "app-server"` to a Codex `[[models]]` row in
`~/.config/godfather/models.toml`. `protocol = "exec"` selects the default;
`GODFATHER_CODEX_PROTOCOL` overrides the model row. Resumes retain the selection.
This experimental adapter targets the Codex 0.167 app-server JSON-RPC protocol.

The bridge starts or resumes a thread in the task CWD, preserves the selected
model, effort, service tier, account home, sandbox and writable roots, and uses
`on-request` approvals in this mode. Approval callbacks use the existing effect
policy regardless of shim audit mode: allow, deny, or a blocking `godfather ask`.
Decisions and policy reasons are recorded in approval events and incidents.
Unrecognized answers and approval-handler exceptions deny the request and still
answer its RPC. Legacy denials include the schema's rejection reason; modern
denials also send their reason as `turn/steer` input when the active turn is known.
Broad or unrecognized approval
actions require a decision. Unsupported client RPC requests receive a JSON-RPC
method error; they are never automatically approved.

Notifications feed the existing JSONL progress parser: status, tool heartbeats,
tokens, final reports, typed errors and rate-limit snapshots use the usual paths.
Sparse quota updates retain earlier windows and reset times when fields are
missing or null. Error notifications marked `willRetry` count as retry heartbeats
and do not prevent a later successful report from completing the run.
Quota probes and wait-for-reset read the saved structured snapshot. The thread ID
is saved in `<name>.app-server.json` and the normal session record; crash recovery
resumes that session with `thread/resume`. Live nudges use `turn/steer` and stop
attempts `turn/interrupt` before terminating the verified process tree. If no live
bridge socket exists, the existing queued nudge behavior applies.
Failed steering, including RPC errors and timeouts, also queues the nudge.

Startup and handshake failures log a reason in `<name>.models.log` and fall back
to the prepared exec command with the same prompt. No fallback replays a turn
once `turn/start` has been sent. `GODFATHER_CODEX_HANDSHAKE_TIMEOUT` controls the
per-request startup timeout (default 15 seconds). Experimental notification types
without a dedicated mapping count as stream heartbeats; rich UI, elicitation and
dynamic-tool client requests are not implemented. Container-wrapped commands retain exec transport through startup fallback.
The adapter does not impose
approval callbacks on commands the server executes without requesting approval;
the existing preamble and command shims still apply.

## agy-run and quota routing

```sh
CWD="$PWD" MIN_PCT=20 agy-run /tmp/parser-brief.md parser
CWD="$PWD" AGY_MODELS='claude-sonnet-5-5-high gemini-3.8-flash-high' agy-run /tmp/parser-brief.md parser-custom
CWD=/tmp DRY_RUN=1 agy-run /dev/null quota-check
```

`AGY_MODELS` is a space-separated preference order. Defaults, in order:

1. `gemini-4-argon-high`
2. `claude-opus-5-5-high`
3. `gemini-3.8-flash-high`
4. `claude-sonnet-5-5-high`
5. `gpt-oss-120b-medium`

The launcher queries `/usage` outside the project and picks the first model with
all applicable limits at or above `MIN_PCT` (default 10, range 0–100). Gemini uses
Gemini weekly/five-hour limits; Claude and GPT use their shared limits; Argon also
checks its separate weekly limit. Missing fields count as available, matching the
original policy; malformed percentages make a model ineligible. An unsuccessful
usage query stops the run. Unsupported model families are skipped.

A task that fails with a quota/rate-limit diagnostic resumes the **same exact
conversation** on the next eligible model. Non-quota errors stop immediately.
If the session ID cannot be discovered, fallback stops rather than starting a
fresh task. Quota is checked again between segments. `DRY_RUN=1` prints the route
without starting a task or writing output files (the runs directory is created).
Model names and quota formats are vendor-specific; override the list when they change.

## grok-run

```sh
CWD="$PWD" GROK_MODEL=grok-4.7-build-fast GROK_EFFORT=high grok-run /tmp/parser-brief.md parser
```

Defaults: `GROK_MODEL=grok-4.7-build-fast`, `GROK_EFFORT=high`. Fast mode is the
same model as `grok-4.7` on the faster servers. The effort must be supported
by the chosen model. `grok-run` itself has no automatic fallback. Routed runs
probe remaining quota as described above. Structured quota failures stop a
direct `grok-run` and cool the model down for `godfather run`.
Streaming events are captured and a terminal `end` event is required for success.
The source launcher was tested with Grok 1.0.62; compatible flags and native
streaming-json format are required. Existing `ADD_DIRS` entries are carried in
the permission profile and native Edit/Write allow rules; there is no native
extra-workspace flag, and the launcher uses sandbox off.
