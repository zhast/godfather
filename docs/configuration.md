# Configuration and output files

[Back to the README](../README.md)

| Variable | Default / meaning |
| --- | --- |
| `CWD` | Required task directory. |
| `AGENT_RUNS_DIR` | `~/.agent-runs`; shared by all commands. |
| `ADD_DIRS` | Colon-separated extra existing writable directories for every CLI; empty entries ignored. Paths containing colons cannot be represented. |
| `CODEX_BIN`, `CODEX_MODEL`, `CODEX_EFFORT`, `CODEX_SERVICE_TIER`, `CODEX_SANDBOX` | See Codex section. |
| `AGY_MODELS`, `AGY_EFFORT`, `MIN_PCT`, `DRY_RUN` | See Antigravity section. |
| `GROK_MODEL`, `GROK_EFFORT` | See Grok section. |
| `STALL_SECS` | Watch idle threshold, default 120 seconds. |
| `HARD_STALL_SECS` | Hard idle limit for an active model stream, default 900 seconds. |
| `ASK_TIMEOUT` | Policy decision timeout, default 900 seconds. |
| `GODFATHER_SELF` | `1` is equivalent to `run --self`; other guards remain active. |
| `GODFATHER_QUOTA_MAX_WAIT` | Maximum quota wait in seconds, default 21600 (six hours); includes the 60-second reset buffer. |
| `GODFATHER_SCRATCH_MAX_MB` | Maximum retained scratch-copy bytes in MiB; default 2048. |
| `GODFATHER_MCP_SDK`, `GODFATHER_MCP_PYTHON` | Optional SDK installation/runtime; see MCP section. |

Every real run writes `<name>.run.json` (atomic registry), `.last.md` (reply),
`.log` (diagnostics), `.models.log` (routing/settings/exits), and `.run.lock`.
Nudge resumes write `.nudge-<id>.md`, `.nudge.json`, `.nudge.log`, and archive the
prior reply as `.last.N.md`. Safety also writes `.audit.log`, `.events.jsonl`, `.control.json`,
`.control.lock`, `.effective.md`, `.safety.json`, and `.shims/`; checks write
`.check.log`.
Healing writes `.healing.json` and `.healing.log`. Reviews write `.review.md`, `.review-state.json`, `.diff` and `.before.diff`. MCP writes `.job.json`, `.mcp.log` and a cursor journal; watch persists `.monitor.jsonl`.
Antigravity also writes `.segment-N.cli.log`, `.segment-N.stdout` and `.segment-N.stderr`; Grok writes `.stream.jsonl` and
`.json`. Reusing an inactive name overwrites ordinary outputs; save artifacts or
use new names to retain history. Launchers create new files with private permissions.
Do not share run directories or commit them to a public repository.

Progress follows exact session identities: Codex's native `exec --json` stream
(with matching-rollout support for older logs), Antigravity's
matching brain transcript and private segment diagnostics, and Grok's live native
stream. Broad startup/diagnostic chatter does not count as Antigravity progress.
Running notes stay at `<logical-name>.notes.md` across all segments. Nudges
preserve the original model settings and additional directories through registry
metadata. `AGENT_RESUME_ID` and `AGENT_NUDGE` are internal handoff variables.
