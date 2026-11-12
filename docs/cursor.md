# Cursor CLI backend

[Back to the README](../README.md)

Authenticate yourself with `cursor-agent login`, then add an uncommented Cursor
block from the models.toml example at your desired priority. Use exact IDs from
`cursor-agent --list-models`; `effort` is informational. Godfather always invokes
`cursor-agent` (or `CURSOR_AGENT_BIN`), never the unrelated `agent` executable.
`fast` defaults to true and selects a catalog-listed `-fast` variant; false keeps
the configured ID. Both original and resolved model labels are checked for
`NO ZDR`. Routing refuses these unless `allow_no_zdr = true`, and refuses an
unavailable catalog or missing model. Catalogs cache for one hour under the runs
directory, per account and executable. The resolved ID appears in models.log and
status. Cursor counts as a separate CLI for `--review-by auto` with `--review-fix`.
Review-only selection excludes Cursor: team controls can disable its sandbox, so
Godfather cannot guarantee read-only execution even with `--mode ask --sandbox enabled`.

`home` maps to the CLI's `CURSOR_CONFIG_DIR` and `CURSOR_DATA_DIR` (confirmed in
2026.11.11); `CURSOR_HOME` is the equivalent launcher override. Sessions and
nudges retain that account. The default is `~/.cursor`, or an existing
`CURSOR_CONFIG_DIR`. `CWD`, `ADD_DIRS`, `AGENT_RUNS_DIR`, `AGENT_RESUME_ID` and the
usual artifact names also work with `cursor-run`. Direct launcher overrides are
`CURSOR_MODEL`, `CURSOR_EFFORT`, `CURSOR_FAST=false`, `CURSOR_ALLOW_NO_ZDR=true`,
and `CURSOR_MODE=ask|plan`.

Cursor edit runs use `--force --trust --sandbox disabled`. Direct `CURSOR_MODE=ask|plan`
runs omit `--force` and request `--sandbox enabled`; team settings can still disable
the sandbox. Prompts travel through stdin, including long reviews and handoffs. The standard oversight preamble,
notes, effect policy, command shims, and audit log still apply. Shims retain the
configured audit/enforce mode (`GODFATHER_SHIMS=enforce` blocks command checks).
There is no injected native secret file-tool denial; the preamble prohibits
secret edits. The optional container remains the hard boundary.

`godfather doctor` checks Cursor auth-file presence and configured API credentials
without starting Cursor; keychain-only login remains unverified. `godfather doctor --live`
runs Cursor `status` and `about` and shows the subscription tier when available.
Cursor login is never automated.
`godfather quota` reads plan, cycle end, plan usage percentage, on-demand spend
and limits, and active grant dollars where reported. Dashboard reads cache for
five minutes under the runs directory. Credentials come from the macOS keychain
(default account) or the selected home's auth.json; tokens are never printed.
A failed or unauthorized usage read reports `usage unknown` and does not block
routing. `included_only = true` skips at 100% plan usage or when active grants'
combined remaining cents are at or below `included_floor_cents` (default 500).
With neither signal, routing proceeds and quota says `included usage not reported`.
A real usage/spend refusal still classifies as quota and triggers fallback.
