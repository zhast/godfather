# Supervision

[Back to the README](../README.md)

## Self-healing and actionable supervision

```sh
godfather watch --important
godfather watch                 # verbose diagnostics
godfather watch --replay        # include previously finished runs
godfather watch --no-heal       # observation only
```

Watch resumes a persisted cursor per consumer. Default watchers lease stable slots: `watch`, then `watch-2`, and so on for concurrent monitors. Each lease records the watcher PID and process identity. A restarted watcher reclaims the lowest slot whose holder is dead and resumes its cursor and delivery history. `--consumer NAME` selects an explicit stable name and is refused while another live watcher holds it. Concurrent consumers each receive every new event once. Restarting `agents-watch` or `godfather watch` emits only undelivered events; synthesized diagnostics also retain delivery IDs. `--cursor NAME` resumes an existing named consumer. `--replay` explicitly includes historical events and diagnostics. Inspect current questions with `godfather status --json`. `--important` emits only `NEEDS_INPUT`, real `DENY`, `ESCALATE`,
`CHECK fail`, unhealed `DIED`, `QUOTA` when no fallback remains (including a quota
wait), `QUOTA_WAIT` with its scheduled resume time, and final `DONE`.
Built-in noise patterns (`cs path`, `GeminiDir`, silent startup auth, cache/git
startup logs, analytics and in-loop exits) are filtered; verbose mode retains
other diagnostics.
Each `DONE <name>` event line or notification appends `report="..."` with the
first non-empty line of `<name>.last.md`, limited to 160 characters. Existing
event names and leading status/exit fields stay the same; missing or empty
reports add nothing.

Remedies are claimed in the store before they run, so a second watcher cannot
repeat one. With a `[[retry]]` table in `godfather.toml`, the supervisor applies
that policy and the older mtime stall healer does not run.

An in-flight tool is alive. A command still running, a child process that is
using CPU, or a declared `sleep N` is not a stall. The tool clock is
`heartbeat.tool_secs` (default 600), or longer when the command declares a wait.
Silence with no heartbeat is `stall_silent` at `heartbeat.silence_secs` (default
120). Thought, text, usage, or stream heartbeats with no tool call are
`stall_thinking` at `heartbeat.think_without_tool_secs` (default 180). A nudge
or restart refuses to signal a child tree that is doing work. A kill/restart
runs only at high confidence, and only for a process that is already gone or a
`crash` retry that is allowed to restart.

Once a snapshot shows a passing acceptance check and a commit, the run is done.
Automatic idle completion requires a new commit, a final report after the last tool, no open items or tools, and the CLI end-of-turn signal after the report (`turn.completed` for Codex, `end` for Grok), followed by `timeouts.final_idle_secs` (default 30) without stream activity. Without that signal, `timeouts.silent_final_idle_secs` (default 600) of silence is required; Agy uses this conservative limit because its transcript has no verified equivalent. A final-report turn is separate. Past `timeouts.report_secs` (default 120
seconds) the supervisor writes the report from the commit and the recorded
events. Thought during that window is not a stall.

Default classes, attempts after the first failure, and actions:

| Class | Attempts | Action |
| --- | --- | --- |
| `auth` | 1, after 30 s | resume the same session |
| `broken_stream` | 1 | resume, and tell the agent to write files in pieces under about 400 lines |
| `stall_thinking` | 1 | nudge it to use a tool |
| `stall_silent` | 1 | nudge it to report status and continue |
| `crash` | 1 | restart the process when confidence is high |
| `check` | 2 | resume with the failing check output |
| `quota` | 0 | fall back to the next ranked model |
| `no_output` | 0 | fail an empty final report and fall back |
| `tool_denied` | 0 | fail a headless tool refusal and fall back |
| `capacity` | 2 | back off 15 seconds and retry the same model, then fall back |

Classes in `fallback.on` move to the next model after attempts are spent.
`check` becomes `check_failed` and stays on the same model. `policy`,
`cancelled`, and `other` stop the run. Quota fallback within Antigravity resumes
the conversation. Output failures and cross-CLI fallback write a handoff and start a fresh
session. Terminal capacity/overload errors outrank earlier tool denials and do
not create quota cooldowns. Same-model capacity retries preserve the session
when available and carry a workspace handoff otherwise.

Failure classification uses one canonical taxonomy. Native `codexErrorInfo`, HTTP
status, and provider `code`/`status`/`type` fields take precedence over message
patterns. Generic envelopes such as `type: error` do not supply a provider code.
Unknown explicit codes remain `other`; they do not trigger a guess from the
message. Grok provider JSON serialized inside an “Internal error:” message is
decoded before applying the HTTP mapping. Each failure keeps its raw evidence, `category`, `source` (`structured`
or `text`), and `retryable`. `FAILURE` events record category and source;
`godfather doctor` counts structured classifications and text fallbacks. Current
failing-segment results outrank discovery/history diagnostics; recognized failures
are retained over later unrelated lines. Recoverable Antigravity discovery auth
and cache warnings share the progress parser’s noise filter.

| Canonical category | Public recovery class | Meaning |
| --- | --- | --- |
| `auth` | `auth` | Invalid credentials; HTTP 401/403. |
| `quota` | `quota` | Usage/rate limit or depleted balance; HTTP 402/429; optional `resets_at`. |
| `capacity` | `capacity` | Overloaded/model at capacity; HTTP 503/529. |
| `context_window` | `context_window` | Context limit exceeded. |
| `model_not_found` | `model_not_found` | Unknown/unavailable model identifier. |
| `policy` | `policy` | Provider policy refusal. |
| `permission_denied` | `tool_denied` | Sandbox/tool approval denial, including incidental 401/403 text. |
| `bad_request` | `bad_request` | Invalid request; other HTTP 4xx. |
| `network` | `crash` | Connection, stream disconnect, timeout. |
| `broken_stream` | `broken_stream` | Broken or interrupted stream. |
| `internal` | `crash` | Provider internal error; other HTTP 5xx. |
| `other` | `other` | Unrecognized failure; stops the run. |

Codex `Reconnecting... N/5` frames are transient network diagnostics. They do
not fail an active turn, including at N=5; a terminal failed turn or failed
process exit establishes the failure. Tool approval denials never become auth
failures because their message contains an HTTP-looking number.

Successful remedies emit `HEALED <name> <action> (<reason>)`; failed remedies emit
`ESCALATE <name> <what failed>`. Watch never kills by process name; it verifies
registered process ownership.
Supervisor recovery prompts identify the supervisor as the source of the
interruption, give its reason class, and name the unfinished command or tool
when the stream records it. They instruct the agent to check its effects before
rerunning it and preserve completed steps. If the unfinished call is unknown,
the prompt says to check state first. Capacity/auth retries and fallback
handoffs carry the same context.

`healing.enabled` still turns remediation on or off. The stall numbers in
`[healing]` apply only when `godfather.toml` has no `[[retry]]` table. Prefer
the declarative tables:

```toml
[healing]
enabled = true

[watch]
ignore = ["my known harmless diagnostic"]

[timeouts]
run_secs = 3600
segment_secs = 1800
ask_secs = 1800
policy_ask_secs = 900
check_secs = 600
report_secs = 120

[heartbeat]
silence_secs = 120
think_without_tool_secs = 180
tool_secs = 600
poll_secs = 5

[[retry]]
class = "stall_silent"
attempts = 1
backoff_secs = 0
action = "nudge_status"
```

One `[[retry]]` table selects the policy. Classes you omit keep the defaults
above. `[watch].ignore` still filters diagnostic lines.

## Run store

The state machine lives in SQLite. The default runs directory uses `${XDG_STATE_HOME:-~/.local/state}/godfather/godfather.sqlite`. Any other `AGENT_RUNS_DIR` uses `<dir>/.godfather.sqlite`. `GODFATHER_DB` overrides the path. WAL mode, one writer, `busy_timeout` set.

`status`, `wait`, and `watch` read `runs.status`. Statuses are `queued`, `provisioning`, `running`, `needs_input`, `checking`, `reviewing`, `retrying`, `fallback`, `merging`, `done`, `failed`, `check_failed`, `review_failed`, `review_findings`, `dependency_failed`, `cancelled`, `timed_out`, and `stopped`. Terminal statuses are `done`, `failed`, `check_failed`, `review_failed`, `review_findings`, `dependency_failed`, `cancelled`, `timed_out`, and `stopped`. `wait` returns only on a terminal status. A streaming segment and a half-written `.last.md` are not completion.

JSON registry files are still written and mirrored into the store, so older launchers and a human reading `.run.json` keep working. The first open of a database imports existing JSON once. Artifact logs (`.log`, `.stream.jsonl`, `.last.md`, `.models.log`, prompts, diffs) stay files.

`events.seq` is the watch and MCP cursor. A new watch starts at the current max. `--replay` is the only start at zero. An unknown `--cursor` name is rejected. One watcher holds `watch.lock` and may heal. Extra watchers only print events. The healer exits when stdout closes or its parent dies, and it re-reads `godfather.toml` when that file changes.

## Doctor and improvement branches

```sh
godfather doctor --since 24h
godfather doctor --since 24h --live
godfather doctor --fix  # expire asks of terminal runs after all owners exit
godfather improve --since 24h --dry-run
godfather improve --since 24h
```

Doctor summarizes incidents, successful heals, escalations, always-allowed asks
that may be false positives, recurring stalls by model, quota exhaustion and
fallback switches. It then reports CLI auth from files on disk. `--live` adds
`codex login status` and `agy -p /usage`; without that flag no model CLI starts.
It also compares the installed scripts listed in `.godfather-install.json`
against the recorded source checkout, ignoring only installer stamps such as
`SOURCE_BIN`. It reports each differing or unavailable file, or prints
`install matches source` when clean. This comparison is read-only; new installs
record the source directory in the manifest, and older installs use the saved
`SOURCE_BIN` stamp.
It prints concrete suggestions. Improve puts that report into
a brief and runs godfather on its own source with `--self --isolate --check
"./tests/smoke.sh"`. The task asks for root-cause fixes with regression tests.
Dry-run prints the brief without launching. Every improvement reports its
`godfather/improve-…` branch for review; it never merges or pushes automatically.

## godfather top

```sh
godfather top
godfather top --once
```

A standard-library terminal dashboard refreshes every 2 seconds. Each active or
recent run shows model, CLI, status, elapsed/idle time, input/cached/output tokens
and last action. Pending NEEDS_INPUT questions are highlighted; recent events
appear below the runs. Up/down selects a row; `q` quits, `a` answers its question,
`n` nudges, `k` kills after typing `yes`, and `l` opens its log with `$PAGER`
(default `less`). Unregistered processes are displayed but cannot be controlled.
Without a TTY, output becomes a plain refresh loop; `--once` prints one snapshot.

## agents-status

```sh
agents-status
agents-status --json
```

Shows active runs and runs ended in the last hour, including model, status,
elapsed/idle time, latest action, steps, tokens, and errors. It also discovers
unregistered CLI processes on a best-effort basis, without adopting or signaling
them. Unknown token counts are `?`. No CLI binary or authentication is needed.

## agents-watch

```sh
agents-watch
agents-watch --stall-secs 120 --poll-secs 5
AGENT_RUNS_DIR=/tmp/my-runs agents-watch --stall-secs 30
```

Prints flushed, one-line events until stopped. `STALL_SECS` also sets the default
idle threshold when `godfather.toml` has no `[[retry]]` table. One watcher holds
the heal lock. Another watch in the same state directory only observes. The
process exits when stdout closes or its parent dies, and it reloads
`godfather.toml` when the file changes. Use `--no-heal` for observation only.
An in-flight tool, a CPU-active child, or a declared wait does not emit `STALL`.

| Event | Meaning |
| --- | --- |
| `NEEDS_INPUT` | A worker needs a decision; answer with `godfather answer`. |
| `DENY` | A real policy denial, rejected approval or timeout. |
| `HEALED` | A watchdog remediation succeeded. |
| `ESCALATE` | A remediation failed and needs attention. |
| `QUEUED` | A persisted run is waiting for dependencies. |
| `BLOCKED` | A legacy policy event retained in incident history. |
| `CHECK` | Acceptance check pass/fail and verification round. |
| `SWITCH` | The global router selected the next model after exhaustion. |
| `START` | A registered run generation was first observed. |
| `SLOW` | An agy model stream is active but no transcript step has completed; suppressed at the hard idle limit. |
| `REVIEW` | Independent review finding count and maximum severity. |
| `STALL` | No heartbeat while a process is alive. An in-flight tool, a CPU-active child, or a declared `sleep` is not a stall. With `[[retry]]`, silence is `stall_silent` and thought-only time is `stall_thinking`. |
| `ERROR` | A CLI diagnostic/stream failure, or an Antigravity/Grok failed tool event. Codex shell-command failures within the agent tool loop are routine and do not emit ERROR. |
| `QUOTA` | A quota/rate-limit diagnostic or terminal quota status. |
| `FAILURE` | Canonical `category` and classification `source` for a failed segment. |
| `QUOTA_WAIT`, `QUOTA_RESUME` | A persisted quota wait and its saved-session resume. |
| `LOOP` | Five identical consecutive tool actions, or 16,000+ characters of text/thinking without another tool call. |
| `DONE` | The launcher finalized, with exit code, elapsed time, and known tokens. Check the exit code: DONE also includes failures. |
| `DIED` | A registered child disappeared for a full poll without a recorded segment exit; expected routing gaps are excluded. |

Routine Antigravity path, workspace-git, and background-cache startup failures are
suppressed. Its silent-auth warning is reported only if the run ultimately fails.
Repeated diagnostics are deduplicated. Healing attempts persist across watchers. Restarting watch omits already finished
runs unless `--replay` is given. A late attachment emits one current stall alert rather
than all missed thresholds. With `[[retry]]`, silence is 120 seconds and thought-only time is 180; an open tool is allowed 600 seconds or its declared wait. Agy progress comes from
its exact conversation transcript; stderr/cache chatter and quota probes never
reset idle. Segment stdout/stderr paths persist instead of disappearing with
launcher temp directories. A recent `streamGenerateContent` marker is a stream heartbeat. It keeps the socket from looking silent, and it does not reset the tool clock. Without `[[retry]]`, that marker produces SLOW during a long response, then STALL after the hard
idle limit (default 900 s, `HARD_STALL_SECS` or `--hard-stall-secs`).

## agents-nudge

```sh
agents-nudge parser "Stop widening the scope; handle empty input and rerun the parser tests."
```

Requires a registered run and known exact session. A live segment and its owners
finish naturally before the queued message resumes the same session. Nudging
never signals the live process tree. Stop cancels queued delivery; replacing the
registered instance also cancels it. A nudge queued while the run was live reopens
a natural terminal outcome; user stops and replaced instances expire with a
`NUDGE_EXPIRED` model-log line. It can resume
an ended registered run, keeps the model log, archives the previous `.last.md` as
`.last.N.md`, and starts a new registry generation. Unknown sessions fail before
delivery. Use `--wait` to wait for the resumed task.

## Claude Code Monitor integration

Paste [examples/CLAUDE.md](../examples/CLAUDE.md) into your global
`~/.claude/CLAUDE.md`. Ask Claude to use its **Monitor tool**, when available, with
`agents-watch --stall-secs 120 --poll-secs 5` as the long-running command. A useful
instruction is:

> Start a Monitor running `godfather watch`. Delegate with `godfather run`, never
> pick a CLI yourself. Use a unique run
> name in the background. Use `godfather watch --important`. On NEEDS_INPUT, review and answer with
> `godfather answer`; escalate anything outward-facing to the human. On STALL, ERROR, QUOTA, LOOP, or DIED, inspect
> `agents-status` and the affected logs, then intervene if needed. On DONE,
> check the exit code and review the resulting files and tests.

The Monitor should receive stdout event lines as they arrive; do not buffer them
through a pipeline. Use the same `AGENT_RUNS_DIR` for workers and monitor. Tool
availability and arguments vary between Claude Code versions, so this example is
an instruction rather than a fixed tool-call schema. If Monitor is unavailable,
run `agents-watch` as a background Bash task or in another terminal and inspect
its output. Stop the watcher when supervision ends; workers are unaffected.
