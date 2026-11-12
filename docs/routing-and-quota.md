# Routing and quota

[Back to the README](../README.md)

## Global ranking: models.toml

On the first `godfather run` or `godfather quota`, Godfather creates
`${XDG_CONFIG_HOME:-$HOME/.config}/godfather/models.toml` with this ranking:

```toml
# Highest priority first. Move entire [[models]] blocks to reorder.

[[models]]
backend = "agy"
model = "gemini-4-argon-high"
effort = "high"

[[models]]
backend = "agy"
model = "claude-opus-5-5-high"
effort = "high"

[[models]]
backend = "codex"
model = "gpt-6.1-sol"
effort = "high"

[[models]]
backend = "grok"
model = "grok-4.7-build-fast"
effort = "high"

[[models]]
backend = "agy"
model = "gemini-3.8-flash-high"
effort = "high"

[[models]]
backend = "agy"
model = "claude-sonnet-5-5-high"
effort = "high"

[[models]]
backend = "agy"
model = "gpt-oss-120b-medium"
effort = "medium"

# Optional fourth backend (move this block to the desired priority):
# [[models]]
# backend = "cursor"
# model = "claude-opus-5-thinking-high"
# effort = "high"               # informational; effort is already in the ID
# fast = true                   # default; use <model>-fast if the catalog has it
# allow_no_zdr = false          # default; explicit opt-in required for NO ZDR
# included_only = true          # optional; skip exhausted included allowance
# included_floor_cents = 500    # skip when active grants have <= $5 remaining
# home = "~/.cursor-company"    # optional independent config AND data directory

```

Move entire `[[models]]` blocks up or down to reorder the global preference list.
You can add supported models or remove blocks. Every entry requires `backend`
(`agy`, `codex`, `grok`, or `cursor`), `model`, and `effort`. Antigravity receives `--effort`,
Codex receives `model_reasoning_effort`, and Grok receives `--reasoning-effort`. Cursor effort is informational.
An optional `min_pct = 20` sets the minimum remaining Antigravity percentage for
that entry (default 0, range 0–100). With the default, any positive quota is eligible;
zero remaining is always exhausted. An optional `label = "Codex · company"`
on any block names its account/pool in quota JSON; the first explicit label wins.
Otherwise labels use the backend and home basename (agy uses its pool name).
`min_pct` also applies to the Codex and Grok TUI probes below. The format supports one field per line, single/double
quoted strings, numeric percentages, and comments; keep this schema on Python 3.8+.

## Multiple Codex and Grok accounts

Log in to each additional account under its own Codex home:

```sh
mkdir ~/.codex-2 && cp ~/.codex/config.toml ~/.codex-2/ && CODEX_HOME=~/.codex-2 codex login
```

Add a ranked entry to `~/.config/godfather/models.toml`:

```toml
[[models]]
backend = "codex"
model = "gpt-6.1-sol"
effort = "high"
home = "~/.codex-2"
```

Repeat for more accounts and order the blocks by preference. `home` is supported
for Codex, Grok, and Cursor and expands `~`; Antigravity rejects it. Omit it to use
`CODEX_HOME` or `~/.codex` for Codex, and `GROK_HOME` or `~/.grok` for Grok.

For another Grok account, log in with:

```sh
mkdir ~/.grok-2 && GROK_HOME=~/.grok-2 grok login
```

Then add its ranked entry:

```toml
[[models]]
backend = "grok"
model = "grok-4.7-build-fast"
effort = "high"
home = "~/.grok-2"
```

Grok checks the selected home's `auth.json` existence and size without reading
it (or uses `XAI_API_KEY`). Sessions live under that home's `sessions` directory.
The installed binary stays on PATH, including installations under
`~/.grok/downloads`; setting `GROK_HOME` does not move the installation.
An explicit home gets its own key, for example `codex:gpt-6.1-sol@codex-2`, in
cooldowns, status and switch logs (or `grok:grok-4.7-build-fast@grok-2`);
custom paths use an escaped absolute path to
avoid collisions. Entries without `home` keep their existing `backend:model` keys.
Login checks and quota probes use the selected home; logged-out homes are skipped.
Quota exhaustion cools down only that ranked account entry. A switch between
homes starts a fresh session with the handoff and shared notes. Codex switches
within one home retain the existing fresh-handoff behavior. Nudges and retries
retain their session's home. Session grants cover the selected home's `sessions`
directory without granting the whole home or other homes' credentials; the
existing sandbox modes and secret-read policy still apply.

Before each attempt, the router checks cooldowns, binary availability, login, and
live Antigravity `/usage`. It uses the same shared weekly, five-hour, and separate
Argon limit checks as `agy-run`. Missing quota fields count as available;
malformed percentages block that model. A failed/empty `/usage` retries up to three times with 1 s and 2 s backoff. If all probes fail, the launcher/router logs a warning and proceeds with unknown quota. Each probe has a 30 s timeout. Codex uses `codex login status`. Grok has no noninteractive
login-status command, so it needs a saved `$GROK_HOME/auth.json` (default
`~/.grok/auth.json`) or `XAI_API_KEY`;
remote credential validity is checked when the task starts. Authentication
failures, including ones discovered during a task, skip that Codex or Grok home
(or the Antigravity backend) and continue with the next ranked entry. Empty final reports and headless tool refusals fail the segment and follow the declarative fallback policy. Other non-quota task failures stop the run.

The installed `codex --help` and `codex exec --help` expose no headless quota
command. Before a Codex or Grok attempt, the router prefers native `rate_limits`
observations saved in recent run metadata for the selected account (up to five
minutes old). Both camelCase and snake_case window fields are accepted. When
that data is unavailable, the router drives the interactive screen in tmux: Grok `/usage` ("Weekly limit … N%", the used share) and Codex
`/status` ("Weekly limit: … N% left"). The result is cached for about five
minutes in the state directory, separately for each Codex or Grok home. Parsing is defensive. An unreadable or timed-out
screen, an empty capture, a missing `tmux`, or `GODFATHER_QUOTA_PROBE=0`
leaves the cooldown clear and the model eligible. `grok usage <session-id>`
reports historical tokens and cost, not remaining allowance, and is not the probe.

## Quota, cooldowns, and handoffs

```sh
godfather quota
godfather quota --json              # all configured accounts/pools, in first-appearance order
godfather quota --json --live       # refresh provider observations
godfather quota --all
godfather quota --reset codex:gpt-6.1-sol
# A bare model name clears matching entries across backends:
godfather quota --reset gemini-4-argon-high
```

Cooldowns are stored atomically under a file lock in
`${XDG_STATE_HOME:-$HOME/.local/state}/godfather/quota.json`, keyed by
`backend:model` (with an account suffix for explicit Codex or Grok homes). Quota/rate-limit failures from routed or low-level runs record
an exhaustion time. The router skips an entry until that time, then retries it
on a later invocation. Explicit reset times from errors or Antigravity `/usage`
take precedence; absent a reset, a five-hour limit cools down for five hours and
an unknown limit for one hour. By default `godfather quota` prints one line per
ranked model from `models.toml` in rank order: backend, model, `eligible=yes|no`,
the applicable Antigravity allowance rows from `agy_limits()` or provider allowance
observations (unknown when unavailable), and any active cooldown with its
`until` timestamp and a single-line truncated reason. `godfather quota --all`
prints the full persisted cooldown map, including expired records.
Positive quota below `min_pct` is skipped without marking the model exhausted.

`godfather quota --json` returns `{"generated_at": "<ISO time>", "accounts": [...]}`.
Each account has `order` (1-based first appearance), `key`, `backend`, `label`,
`models`, `plan`, `window`, `used_percent`, `resets_at`, `status`, `eligible`,
`source`, `observed_at`, `detail`, and `money`. Times are ISO 8601; unavailable
numbers, reset times, plans and observation times are null. Status is `ok`, `low`,
`exhausted`, `cooldown`, or `unknown`. Unknown allowance remains eligible unless
another routing gate blocks it. `eligible` means at least one configured model in
the pool is eligible; `model_eligibility` and `cooldowns` give the same per-model
decisions as text output. Text keeps one row per model in file order; JSON groups
accounts in first-appearance order, including nonadjacent model blocks.
Per-model thresholds can make one model ineligible without changing a usable
pool's status to `low`; that status applies only when no model in the pool is
eligible and an allowance gate failed.

- Codex and Grok group by backend and effective home, e.g. `codex@~/.codex-2`.
  Codex reads the newest `rate_limits` event from `sessions/**/rollout-*.jsonl`
  under that home, preferring a newer app-server observation saved in run metadata.
  `source` is `session-log` or `api`; `credits` includes reported credit flags.
  `windows` preserves both primary and secondary usage and reset times. The top
  level selects the weekly window when present, otherwise the primary window;
  eligibility checks all windows.
- Antigravity groups by its saved `/usage` pool: `Claude and GPT models`,
  `Gemini 4 Argon`, and `Gemini Models`. Default quota reads the latest stored
  snapshot, even if old; `observed_at` identifies its age. `--live` refreshes it.
  Shared Gemini limits still apply to Argon model eligibility.
- Grok uses `/usage`, cached for five minutes; `--live` forces a new capture.
  The weekly panel percentage is used, while `Weekly limit left: N%` is remaining.
  The panel wins if both disagree, and `detail` records the disagreement. Reset
  dates without a year use the next local occurrence. If the probe cannot start,
  the latest quota failure from that home's run logs yields `exhausted` with
  `source: "error-log"`, its observation time, and no invented percentage.
  An unreadable screen or capture timeout stays `unknown` and eligible unless
  an active cooldown blocks it; an older error log cannot override that result.
  A started pane containing only whitespace after ANSI stripping is unreadable,
  including a screen that has not drawn yet.
- Cursor groups by effective account home and reads the existing Dashboard cache
  (`source: "api"`); `--live` refreshes it. `money` is
  `{"used_cents": 2500, "limit_cents": 10000, "kind": "grant"}` when grants are
  reported, otherwise null. Expired grants are excluded. `on_demand` preserves
  reported spend/limit cents. Spent grants do not mark the plan window exhausted;
  grant floors affect only models configured with `included_only`.
  Failed refreshes retain the saved snapshot and its timestamp. Partial refreshes
  merge successful endpoint fields with retained fields and keep the older
  observation time, so failures do not renew the cache TTL. A complete refresh
  replaces the snapshot and updates its time. A degenerate or expired cycle end
  falls back to `GetPlanInfo.planInfo.billingCycleEnd`.

Provider payloads, credentials and email addresses are excluded. The existing
`GODFATHER_QUOTA_PROBE=0` switch also disables forced TUI probes. To reproduce a
complete JSON example using fixtures only:

```sh
python3 tests/test_quota_json.py --sample
```

After a quota failure, the next eligible model follows the global order, including
across CLIs. Adjacent Antigravity attempts resume the exact conversation; if its
ID is missing, the router stops rather than silently restarting it. Across CLIs,
a fresh session receives the original task, current branch, HEAD and merge state,
`git status --short`, working-tree and staged `git diff --stat`, and the complete
last report (or up to 40 lines of partial messages). Writable roots and inspection
rules carry across CLIs. Before the report/messages, the handoff includes the
previous agent's notes, retaining the newest 24,000 bytes if oversized, and tells
the next agent to keep updating the same file. Missing or empty notes retain the
existing report/message fallbacks. Outside a
git repo it receives a list of recently modified files. The handoff explicitly
instructs the next agent to verify existing work and continue rather than restart.
When no fallback model/account remains and a saved session has a known reset,
the run enters `waiting_quota`. `godfather status` shows “waiting for quota,
resumes HH:MM”; JSON includes `resume_at`. The existing persisted queue daemon
resumes that exact session 60 seconds after the reset. Exhausted windows in native
`rate_limits`/Codex snapshots, explicit `resets_at`/`resetsAt`, saved Antigravity
usage rows, and parsable “try again at/in” diagnostics can supply the time. All
blocking windows must reset; non-exhausted windows do not extend the wait.

`GODFATHER_QUOTA_MAX_WAIT` caps the delay in seconds (default 21600, six hours,
including the 60-second buffer). No known reset, no saved session, or a delay
beyond the cap retains the failure behavior. `godfather stop` cancels the wait;
a nudge during the wait is included on resume. Queue/run metadata survive the
conductor exiting. The queue job is persisted before `waiting_quota` is published;
a short shared lock keeps the daemon from reading a partial publication. If the
daemon itself stops, the next Godfather command restarts it, or run `godfather daemon`. Acceptance checks and reviews still run
after the resumed session succeeds. A plan waits for that outcome before
releasing dependent tasks.

The first segment is registered as `<name>`, then `<name>#2`, `<name>#3`, and so
on. Status/watch show each segment's CLI and model. Every selection, skip,
cooldown, handoff, and exit is recorded in `<name>.models.log`.
`godfather watch` / `agents-watch` emit
`SWITCH <name> <from> -> <to> (<reason>)`.
`godfather nudge <name> "instruction"` resolves the latest segment before
invoking `agents-nudge`; you can also address a segment by its explicit name.
Use the final segment's `.last.md` for the completed reply; `.route.json` records
its name, the routing generation, and switches. Generated `.prompt.md` and
`.handoff.md` files retain the exact task passed to the backends.

## Cost and usage ledger

```sh
godfather usage
godfather usage --today --by model
godfather usage --week --by cli --json
godfather usage --by run
```

Every worker invocation appends one JSON line to
`${XDG_STATE_HOME:-$HOME/.local/state}/godfather/ledger.jsonl`, including quota
segments, reviewer runs and resumed sessions. Records contain the logical and
segment names/IDs, CLI, model, start/end times, exit status, known input/cached/output
tokens, quota exhaustion and whether this segment followed a quota fallback.
Unknown counts stay null. Agy conversation history is excluded from resumed
segment counts. The ledger persists when a run name is reused.

`--today` starts at local midnight; `--week` starts at local Monday midnight.
`--by model|cli|run` groups totals; `--json` returns the same structured data as MCP.
Quota burn reports observed exhaustions/fallbacks alongside tokens. For agy the
latest `/usage` snapshot and its timestamp and age (`live` or e.g. `3 days old`)
are included; snapshots older than 10 minutes refresh via `agy_usage()`, and a
failed refresh labels the retained snapshot stale rather than current (`--json`
includes `at`, `age_seconds`, and `refreshed`). `godfather quota --json` supplies
subscription observations for charts: Codex session logs or newer app-server data,
agy's saved snapshot, Grok's TUI probe, and Cursor Dashboard usage. Unreadable
screens leave allowance unknown and do not create cooldowns. Percentages are
provider observations; Godfather does not infer them from token totals.

Prices are optional USD per million tokens in each `models.toml` model block:

```toml
[[models]]
backend = "codex"
model = "your-model"
effort = "high"
input_per_million = 2.0
cached_per_million = 0.5
output_per_million = 8.0
```

These are example prices, not a vendor quote. Defaults say `pricing = "subscription"`
and contain no numeric prices. Estimated USD appears only when usage and prices
are known; otherwise summaries show tokens only. Cached tokens are part of input,
and are subtracted before charging the uncached rate. Without a cached price, the
input rate applies. With a distinct cached rate but unknown cache usage, cost stays
unknown. Each line preserves the prices used; totals count only priced segments
and disclose the number of unpriced segments. Subscription allowance is not a
cash bill.
