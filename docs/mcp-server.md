# MCP server

[Back to the README](../README.md)

```sh
godfather mcp
# Claude Code:
claude mcp add godfather -- godfather mcp
# Codex:
codex mcp add godfather -- godfather mcp
```

For Cursor, add this server to `.cursor/mcp.json` (project) or
`~/.cursor/mcp.json` (user), using an absolute executable path if the editor does
not inherit your shell PATH:

```json
{
  "mcpServers": {
    "godfather": { "command": "godfather", "args": ["mcp"] }
  }
}
```

The server uses the [official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
when available (installer provisions SDK 1.x in a private runtime). Otherwise the
minimal standard-library implementation speaks newline-delimited JSON-RPC over
stdio. Stdout contains protocol messages only. `GODFATHER_MCP_PYTHON` can select an
SDK interpreter explicitly. All other features, including `top`, need no packages.

| Tool | Arguments / result |
| --- | --- |
| `run` | `prompt, name, cwd, isolate?, check?, review_by?`; immediately returns `name, run_id, status`. |
| `status` | `name?`; structured active/recent rows or one logical run report. |
| `events` | `since?`; returns new events and a byte `cursor`; pass it back as `since`. |
| `wait` | `name, timeout_s`; waits for every segment, checks and pending fallback; timeout returns current state with `timed_out`. |
| `answer` | `name, answer`; submits a decision; stopped sessions resume in the background. |
| `nudge` | `name, message`; resumes the exact session in the background. |
| `cancel` | `name`; stops only verified owned processes. |
| `quota` | No arguments; persisted cooldowns. |
| `usage` | `period?` (`today`, `week`, `all`); totals, estimated USD, quota burn and agy snapshot. |

Prefer these tools when available; the CLI remains a fallback. Use unique run
names and separate CWDs or `isolate: true` for concurrent workers. `wait` does not
block other MCP requests. Runs survive an MCP client disconnect; inspect their
registry and `<name>.mcp.log` on reconnect. Poll `events` for persisted decisions,
checks, reviews and lifecycle events; a running `godfather watch` also persists
STALL/SLOW/error alerts. Stale or unknown event cursors are rejected. Tools retain
the CLI's effect policy: asks await a decision; secret and unauthorized self writes remain denied.
