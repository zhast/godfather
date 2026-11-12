# Architecture

This is the architecture the `bin/` implementation follows. Artifact logs stay files. Run status lives in the SQLite store.

Godfather is a conductor harness. It routes one task at a time to the Codex, Antigravity (`agy`), Grok, or Cursor CLI, keeps the run's state, and applies timeout, retry, fallback, and permission policy. The conductor does not pick a CLI. Ranking stays in `models.toml`.

JSON files under `~/.agent-runs/` and `${XDG_STATE_HOME:-~/.local/state}/godfather/` (`.run.json`, `.route.json`, `.control.json`, `.events.jsonl`, `queue/*.json`, `incidents.jsonl`, `ledger.jsonl`, `quota.json`) are still written, and each write is mirrored into SQLite. `status`, `wait`, and `watch` read the store. Artifact logs stay files. They are evidence, not state.

Python's standard-library `sqlite3` is the only store. No new packages. The default runs directory uses `${XDG_STATE_HOME:-~/.local/state}/godfather/godfather.sqlite`. Any other `AGENT_RUNS_DIR` uses `<dir>/.godfather.sqlite`. `GODFATHER_DB` overrides the path. The database is WAL mode, `busy_timeout` set, one writer. Every status change is a transaction that appends a row to `transitions`. A second writer that loses the race retries the transaction. It does not write a second copy of the truth.

## Principles

1. A plan is a dependency graph of small tasks. The conductor never sends a mega-brief.
2. One SQLite store holds runs, segments, events, incidents, the queue, and decisions. Each run moves through the state machine below. No other write path changes `runs.status`.
3. Progress is a heartbeat parsed from the CLI's structured stream. File mtime is not progress. A partial line is not an event.
4. Timeout, retry, and fallback are declarative policies keyed by failure class, in the style of a workflow engine. The watcher does not invent a remedy.
5. Plan tasks run in git worktrees. An integrator task merges them. Nothing pushes.
6. Allow, ask, and deny are compiled into each CLI's native permission settings. An optional container is the hard boundary. PATH shims only audit.
7. Every incident in the last section is a replay fixture. The fixture fails if the mechanism regresses.

## Plan file

Plans are TOML, same subset as `models.toml`: one field per line, quoted strings, one-line arrays, `[[tasks]]` tables, comments. A brief path is copied into `runs.prompt` at enqueue time.

```toml
schema = 1

[plan]
name = "phase-d"
repo = "."                 # git working tree the tasks share
merge = true               # append one integrate task
max_parallel = 2           # ready tasks in distinct directories; integrate runs alone
container = false          # default hard boundary for every task

[[tasks]]
id = "d1"
brief = "briefs/d1.md"     # path, or an inline string
deps = []                  # task ids in this plan; --after names are allowed
check = "test -s ARCHITECTURE.md && grep -q \"state machine\" ARCHITECTURE.md"
isolate = true
task_type = "write"        # research, write, edit, review, integrate, test
model_hints = ["grok:grok-4.7"]
container = false
```

| Field | Required | Meaning |
| --- | --- | --- |
| `id` | yes | Stable task id. Unique inside the plan. Becomes the run name `<plan>-<id>` unless `name` is set. |
| `brief` | yes | The whole task. Small enough that one agent can finish it. |
| `deps` | yes | Predecessors. Empty array if none. A task cannot depend on itself. |
| `check` | no | Shell acceptance check, run with the conductor's environment after the segment succeeds. |
| `isolate` | no | Default `true` in a plan. `false` runs in `repo`. |
| `task_type` | yes | Selects effort and heartbeat budgets. |
| `model_hints` | no | Soft preference, `cli:model` in rank order. The router still skips a hint that is missing, unauthenticated, or cooling down, then follows `models.toml`. |

`godfather run` without a plan file is a one-task plan: `isolate` defaults to true, `task_type` defaults to `edit`, and there is no integrator. `--no-isolate` runs in CWD and takes the CWD lock. `--after` and `--after-any` are `deps` plus `any_status`. `--check`, `--isolate`, `--review-by`, and `--max-fix-rounds` map onto the same columns.

When `plan.merge` is true the runner adds one `task_type = "integrate"` task that depends on every other task. Its brief is the merge flow in this document. It is the only task allowed to merge.

## Run state machine

A **run** is one task execution. A **segment** is one CLI attempt inside that run. Segments have their own small status (`starting`, `streaming`, `finishing`, `exited`, `spawn_failed`, `broken_stream`) stored on the segment row. The run status is the only value `wait`, `status`, and the queue read.

Closed set of run statuses:

`queued`, `provisioning`, `running`, `needs_input`, `checking`, `reviewing`, `retrying`, `fallback`, `merging`, `waiting_quota`, `done`, `failed`, `check_failed`, `review_failed`, `review_findings`, `dependency_failed`, `cancelled`, `timed_out`, `stopped`.

Terminal statuses: `done`, `failed`, `check_failed`, `review_failed`, `review_findings`, `dependency_failed`, `cancelled`, `timed_out`, `stopped`.

Legal transitions:

```text
queued          → provisioning | dependency_failed | cancelled
provisioning    → running | failed | cancelled
running         → needs_input | checking | reviewing | merging | retrying
                | fallback | waiting_quota | done | failed | cancelled | timed_out
needs_input     → running | done | failed | cancelled | timed_out
checking        → reviewing | merging | done | retrying | check_failed | cancelled
reviewing       → merging | done | review_failed | review_findings | retrying | cancelled
retrying        → running | fallback | waiting_quota | failed | cancelled | timed_out
fallback        → running | waiting_quota | failed | cancelled
waiting_quota   → retrying | running | failed | cancelled
merging         → done | failed | cancelled
```

`godfather stop <name>` transitions any active state to terminal `stopped` and terminates its verified owners, descendants, and all process groups in its private sessions, including reparented children. Worker session IDs are recorded at spawn; liveness reconciliation includes surviving session members. Stop refuses the caller group/session, escalates TERM to KILL after a short grace, and verifies all captured identities are gone before recording stopped. Status reconciles vanished owners against recorded exit and final-report evidence. The durable router and workers own separate sessions. The attached launcher only streams output: SIGTERM, SIGINT, and SIGHUP detach it without forwarding, and attachment files are unlinked on exit.

Any terminal status except `dependency_failed` may `reopen` to `running`. That is `nudge`: same logical name, `generation + 1`, new segment. `dependency_failed` stays until a new enqueue. A nudge queued while the run was live waits for every owner to exit, then reopens a natural terminal outcome. It expires only on a user stop (`stopped` or `stop_requested`) or run-instance replacement, with a `NUDGE_EXPIRED` model-log line and an important `NUDGE` event. An explicit nudge of a previously stopped run can reopen it. A resume copies the previous non-empty `.last.md` to `.last.N.md` before the launcher truncates it.

Anything not in the list is a bug. `transition(run, to, reason)` is the only writer. It inserts `transitions` in the same transaction as the status update and the public `events` row. A lost compare-and-set (`UPDATE ... WHERE status = ?`) returns the current status and does not emit a second event.

`died` is not a status. A vanished process is failure class `crash` while the run is still `running`, then `retrying` or `failed`. The watch line `DIED` is still emitted so existing conductors keep working.

Decisions are orthogonal. A blocking policy ask leaves the run `running` and sets `pending_decision`. Watch still emits `NEEDS_INPUT`. A nonblocking ask, or a segment that exited with an undelivered answer, moves the run to `needs_input`. The answer transaction clears `pending_decision`. If the segment is still in that process, it keeps going. If it has exited, the transition is `needs_input` → `running` and the saved session resumes. One unanswered question per run. Once the entire process tree exits, undelivered blocking questions expire without granting approval; exit code and final-report evidence determine done or failed. An intentionally exited nonblocking ask stays answerable.

How a successful segment chooses its next status, in order: open decision → `needs_input`; `check` set → `checking`; `review_by` set → `reviewing`; plan merge still ahead and this task is not the integrator → `merging`; otherwise → `done`. The result text is stored in that same transaction. A growing `.last.md` is not a transition.

One-shot dependency names bind at enqueue to the current non-terminal run ID (including queued and provisioning instances). Missing or terminal names instead wait for the next instance after queue creation. Persisted dependency_instances survive daemon restarts and prevent older completions and replacement generations from releasing the dependent. Queued instances retain their ID when the router starts. Logical router IDs cover fallback segments. Missing or stale names have the same registration timeout.

Quota waits keep the real run in `waiting_quota`, with `resume_at` in its payload.
A queue entry with `kind = "quota"` points to that exact segment and run instance;
it never creates a dependency placeholder run. The queue job is written before
the waiting run state; publication and the daemon’s state read share a per-job
file lock. A failed queue write leaves the run outside `waiting_quota`. The daemon
checks the persisted deadline, stop flag, and instance before starting a saved-session nudge worker.
Queued nudges are folded into that resume. No conductor process sleeps until the
reset. Waiting rows are exempt from abandoned-process reconciliation.

Provider errors are classified by `godfather_errors.py`: semantic codes first,
then HTTP status, then text only when no provider fields are present. Stream
heartbeats carry canonical category/source alongside public recovery aliases.
Finished segments persist failure evidence in the run payload and emit `FAILURE`
events; doctor counts classification sources. Native quota windows saved with
an observation time and account home precede tmux scraping.

Queue claim is the same compare-and-set: `queued` → `provisioning` succeeds for one daemon only. A crash while status is `provisioning` or `launching` with a dead pid becomes `failed` for that generation. It does not start a second process for the same generation.

## SQLite schema

`meta(key, value)` holds `schema_version`. Migrations bump `PRAGMA user_version` inside the existing file. They never replace the database inode under a live process.

```sql
CREATE TABLE plans (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  repo TEXT NOT NULL,
  base_sha TEXT NOT NULL,
  integration_branch TEXT NOT NULL,
  status TEXT NOT NULL,            -- open, merging, done, failed
  created_at REAL NOT NULL
);

CREATE TABLE runs (
  id TEXT PRIMARY KEY,             -- uuid, matches today's run_id
  plan_id TEXT REFERENCES plans(id),
  task_id TEXT,
  name TEXT NOT NULL,
  generation INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL,
  pending_decision INTEGER NOT NULL DEFAULT 0,
  cwd TEXT NOT NULL,
  worktree TEXT,
  branch TEXT,
  base_sha TEXT,
  prompt TEXT NOT NULL,
  check_cmd TEXT,
  review_by TEXT,
  isolate INTEGER NOT NULL,
  container INTEGER NOT NULL DEFAULT 0,
  task_type TEXT NOT NULL,
  failure_class TEXT,
  exit_code INTEGER,
  error TEXT,
  artifact_dir TEXT NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  started_at REAL,
  ended_at REAL,
  UNIQUE (name, generation)
);

CREATE TABLE segments (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  ordinal INTEGER NOT NULL,        -- 1 prints as <name>, 2 as <name>#2
  cli TEXT NOT NULL,               -- agy, codex, grok
  model TEXT NOT NULL,
  effort TEXT NOT NULL,
  status TEXT NOT NULL,
  session_id TEXT,
  pid INTEGER,
  pid_identity TEXT,
  launcher_pid INTEGER,
  supervisor_pid INTEGER,
  failure_class TEXT,
  exit_code INTEGER,
  started_at REAL NOT NULL,
  ended_at REAL,
  last_heartbeat_at REAL,
  last_heartbeat_kind TEXT,        -- stream, thought, text, tool, usage, error, end
  last_tool_at REAL,
  tokens_in INTEGER,
  tokens_cached INTEGER,
  tokens_out INTEGER,
  handoff_from TEXT,
  permission_profile TEXT,
  UNIQUE (run_id, ordinal)
);

CREATE TABLE events (
  seq INTEGER PRIMARY KEY,         -- global cursor for watch and MCP
  run_id TEXT,
  segment_id TEXT,
  at REAL NOT NULL,
  kind TEXT NOT NULL,              -- existing names: START, DONE, NEEDS_INPUT, ...
  detail TEXT NOT NULL
);

CREATE TABLE cursors (
  subscriber TEXT PRIMARY KEY,
  seq INTEGER NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE incidents (
  id TEXT PRIMARY KEY,
  run_id TEXT,
  segment_id TEXT,
  at REAL NOT NULL,
  command_json TEXT,
  cwd TEXT,
  targets_json TEXT,
  verdict TEXT NOT NULL,           -- allow, ask, deny, audit
  answer TEXT,
  question_id TEXT,
  effect TEXT,
  source TEXT NOT NULL             -- native, container, shim-audit
);

CREATE TABLE queue (
  run_id TEXT PRIMARY KEY REFERENCES runs(id),
  deps_json TEXT NOT NULL,
  any_status INTEGER NOT NULL,
  env_json TEXT NOT NULL,          -- non-secret launch environment only
  created_at REAL NOT NULL
);

CREATE TABLE decisions (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  segment_id TEXT,
  question TEXT NOT NULL,
  options TEXT,
  effect_json TEXT,
  blocking INTEGER NOT NULL,
  answer TEXT,
  asked_at REAL NOT NULL,
  answered_at REAL,
  delivered INTEGER NOT NULL DEFAULT 0,
  timeout_at REAL
);

CREATE TABLE transitions (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL,
  segment_id TEXT,
  at REAL NOT NULL,
  from_status TEXT NOT NULL,
  to_status TEXT NOT NULL,
  reason TEXT NOT NULL
);

CREATE TABLE remedies (
  run_id TEXT NOT NULL,
  segment_id TEXT NOT NULL,
  action TEXT NOT NULL,
  reason TEXT NOT NULL,
  attempted_at REAL NOT NULL,
  result TEXT NOT NULL,            -- pending, healed, escalate
  PRIMARY KEY (run_id, segment_id, action)
);

CREATE TABLE quota (
  key TEXT PRIMARY KEY,            -- cli:model, account suffix for explicit Codex/Grok home
  until_at REAL NOT NULL,
  reason TEXT,
  updated_at REAL NOT NULL
);

CREATE TABLE ledger (
  id INTEGER PRIMARY KEY,
  run_id TEXT,
  segment_id TEXT,
  cli TEXT,
  model TEXT,
  started_at REAL,
  ended_at REAL,
  exit_status INTEGER,
  tokens_in INTEGER,
  tokens_cached INTEGER,
  tokens_out INTEGER,
  quota_fallback INTEGER NOT NULL DEFAULT 0,
  prices_json TEXT
);
```

One partial unique index keeps a single live generation of a name:

```sql
CREATE UNIQUE INDEX runs_one_live_name ON runs(name)
  WHERE status NOT IN (
    'done', 'failed', 'check_failed', 'review_failed', 'review_findings',
    'dependency_failed', 'cancelled', 'timed_out');
```

`wait` selects `runs.status` and returns only on a terminal status. Timeout returns the current status with `timed_out` set on the response, and does not write `timed_out` onto the run unless the run's own deadline fired. A segment in `starting`, `streaming`, or `finishing` is invisible to `wait` as completion. So is a half-written `.last.md`.

Watch and MCP `events` read `WHERE seq > ?`. A subscriber's cursor lives in `cursors`. Watch atomically leases the lowest available stable slot (`watch`, `watch-2`, ...), recording the watcher PID and process identity in `watch_leases`. Dead holders are reclaimed without losing their cursor or synthesized-event delivery history. `--consumer NAME` creates or resumes an explicit stable consumer and rejects a live holder. `--cursor NAME` also requires an exclusive lease. Switch delivery is scoped to that consumer too. A new watch, and a watch that lost its memory, starts at `MAX(seq)` at subscribe time. `--replay` is the only way to start at 0. Unknown cursors are rejected. Restarting a watcher delivers nothing it has already consumed.

`remedies` is claimed with an insert before the remedy runs. A second watcher sees the primary key and does not repeat it.

Heartbeats are not a table. Every thought chunk in a long Grok stream would dominate the database. The supervisor updates `last_heartbeat_*` and `last_tool_at` on the segment row. Raw frames stay in the segment's stream file for fixtures and doctor.

Quota and the usage ledger move into `quota` and `ledger`. Prices, unknown tokens, and the Antigravity snapshot rules are unchanged.

## Heartbeats

The supervisor reads each CLI stream as newline-delimited JSON. It commits a frame only when the newline has arrived. A partial line updates nothing. A line longer than `stream.max_line_bytes` (default 1 MiB) is spilled to `<artifact>.spill` and recorded as a `tool` heartbeat with detail `oversized`. It is not loaded as one in-memory value, and it is not a successful segment exit. If the CLI then drops, the failure class is `broken_stream`.

| CLI frame | Heartbeat kind | Resets tool timer |
| --- | --- | --- |
| Grok `tool_call`, `tool_call_update` | `tool` | yes |
| Grok `thought` | `thought` | no |
| Grok `text` | `text` | no |
| Grok `usage` | `usage` | no |
| Grok `error` | `error` | no |
| Grok `end` | `end` | terminal frame |
| Grok `available_commands` | `stream` | no |
| Codex `item.started` of command, file, MCP, or web search | `tool` | yes |
| Codex `item.completed` reasoning | `thought` | no |
| Codex `item.completed` agent message | `text` | no |
| Codex `turn.completed`, token counts | `usage` | no |
| Codex `error`, `turn.failed` | `error` | no |
| Antigravity planner response that includes a tool call | `tool` | yes |
| Antigravity thinking or content with no tool call | `thought` | no |
| Antigravity `streamGenerateContent` marker | `stream` | no |

`streamGenerateContent`, cache chatter, and file mtime do not count as a tool and do not count as silence-breakers beyond kind `stream`. Kind `stream` means the socket is alive. It does not mean the agent is working.

Two clocks run from those columns:

- **Silence.** No heartbeat of any kind for `heartbeat.silence_secs`. Class `stall_silent`.
- **Thinking.** Heartbeats are only `thought`, `text`, `usage`, or `stream` for `heartbeat.think_without_tool_secs`. Class `stall_thinking`.

A tool call that has started and not finished is healthy until `heartbeat.tool_secs`.

## Policy

Policy lives in `${XDG_CONFIG_HOME:-~/.config}/godfather/godfather.toml`, next to `[healing]` and `[watch]`. `[watch].optional_mcp` is an array of MCP server host names a run can do without: a diagnostic that names one and reports it unreachable, refused, or timed out is a warning, not a failure. A `[[retry]]` table selects this policy. `healing.enabled` still turns remediation on or off. Stall clocks and retry actions then come from the tables below, and the older mtime stall healer does not run. The shape matches the TOML subset already parsed: `[[retry]]` and `[[effort]]` tables, one field per line.

```toml
[timeouts]
run_secs = 3600
segment_secs = 1800
ask_secs = 1800
policy_ask_secs = 900
check_secs = 600
provision_secs = 60

[heartbeat]
silence_secs = 120
think_without_tool_secs = 180
tool_secs = 600
poll_secs = 5

[stream]
max_line_bytes = 1048576

[[retry]]
class = "auth"
attempts = 1
backoff_secs = 30
action = "resume"            # same session, same model

[[retry]]
class = "broken_stream"
attempts = 1
backoff_secs = 2
action = "resume_chunked"    # resume, instruct pieces under ~400 lines

[[retry]]
class = "stall_thinking"
attempts = 1
backoff_secs = 0
action = "nudge_tool"

[[retry]]
class = "stall_silent"
attempts = 1
backoff_secs = 0
action = "nudge_status"

[[retry]]
class = "crash"
attempts = 1
backoff_secs = 1
action = "restart"           # new process, resume session if the id is known

[[retry]]
class = "check"
attempts = 2                 # today's --max-fix-rounds default
backoff_secs = 0
action = "fix"

[[retry]]
class = "quota"
attempts = 0                 # never the same model again in this run
backoff_secs = 0
action = "fallback"

[fallback]
on = ["quota", "auth", "stall_thinking", "stall_silent", "broken_stream", "crash"]
same_cli = "resume"          # Antigravity keeps the conversation id
cross_cli = "handoff"
stop = ["policy", "cancelled", "other"]

[[effort]]
model = "*"
task_type = "*"
effort = "high"

[[effort]]
model = "*"
task_type = "research"
effort = "medium"

[[effort]]
model = "grok-4.7"
task_type = "research"
effort = "low"
```

`attempts` counts tries after the first failure. When they are spent, a class listed in `fallback.on` moves the run to `fallback` and the next eligible ranked model. A class listed in `fallback.stop` goes to `failed`. `check` is neither: spent fix rounds go to `check_failed` and do not switch model.

Same-CLI Antigravity fallback resumes the exact conversation. A missing conversation id stops the run, as it does today. Cross-CLI fallback writes a handoff (original brief, `git status`, diff stat, running notes, last output) and starts a fresh session. The handoff says to verify existing work and continue.

Codex and Grok ranking rows may select `home`; Antigravity rejects it. Explicit homes add an account suffix to
`model_key`; rows without one keep their historical keys. Login readiness and
quota-probe caches are scoped to the effective home. Each segment saves its
`CODEX_HOME` or `GROK_HOME` and model key for quota recording, transcript discovery, nudges and
recovery. Permissions grant the selected home's sessions tree, not its whole
home. Codex-to-Codex and Grok-to-Grok fallback continue to create a fresh handoff,
including when the homes differ; sessions never resume across accounts. Grok
preflight checks the selected home's `auth.json` existence/size without reading
it. Binary discovery stays on PATH, independently of account home selection.

`godfather_safety.notes_path` defines one `<logical-name>.notes.md` alongside the
run artifacts, shared across segments and resumes. `prepare` creates the private
file if missing, sets `GODFATHER_NOTES`, and adds a short instruction with the
absolute path to every effective prompt. Agents update Done, In progress, Next,
Decisions (and why), Ruled out, Checks run (command and result), and Open questions.
The permission profile records this exact file in `writable_files`; native file
grants and the effect policy permit it without adding a directory grant. Handoffs
place its newest 24,000 bytes before the existing report/message fallbacks, which
remain in use when notes are absent or empty. Containers bind-mount only this
file alongside the repository and pass `GODFATHER_NOTES`; agents update in place.
Explicit Codex `read-only` mode retains its no-write policy: agents read notes
and put progress updates in their final report.

Project lessons are separate from run notes. `godfather_safety.lesson_project`
hashes the resolved git common directory, or resolved CWD outside git, into
`<runs>/lessons/<key>.md`. The router and plan executor preserve source CWD in
`GODFATHER_PROJECT_CWD`, saved in launcher settings for nudges and retries, so
non-git scratch directories keep the source identity. At the CLI boundary,
`prepare` appends filtered local and read-only repository `.godfather/lessons.md`
entries, newest local first, with a 2048-byte budget and omission notice.
Management commands use a per-project file lock and atomic private-file writes;
edit uses a sanitized, newest-first numbered draft with a concurrent-change
check, reversing it back into append order on save. Git discovery errors fall
back to CWD identity; unreadable lesson sources warn and are skipped without
blocking prompt preparation. Remembered nudges
save the filtered correction at enqueue time for the resolved target run.
Content filtering applies on both write and read; diagnostics never echo rejected
values. Lessons have no input to the effect-policy compiler or evaluator.

`other` is any task failure that is not one of the classes above. It stops the run. That keeps today's rule that an ordinary model failure does not hop CLIs.

Effort lookup is the first matching `[[effort]]` row with a specific model, then `model = "*"`, then the `models.toml` effort. `task_type` selects the row. The launched CLI still receives that effort the way it does now: Antigravity `--effort`, Codex `model_reasoning_effort`, Grok `--reasoning-effort`.

Deadlines are also transitions. `segment_secs` without a terminal frame, or `run_secs` from `started_at`, moves the run to `timed_out`. The clock is the heartbeat clock, not a file timestamp.

## Isolation and merge

Isolation is the default. Caller-provided linked worktrees are adopted in place.
An attached devbox also selects in-place execution unless `--isolate` was explicit,
in which case provisioning refuses. Queued dependents preserve this distinction.
`--no-isolate` always runs in place. `godfather run` does not merge. Only a plan integrator merges.

Provisioning, for an isolated run inside a git repo:

1. Record `base_sha` = HEAD. A plan records the plan's integration HEAD.
2. `git worktree add -b godfather/<name> $AGENT_RUNS_DIR/worktrees/<name> HEAD` for a one-shot run, or `godfather/<plan>/<task> $AGENT_RUNS_DIR/worktrees/<plan>/<task> <base_sha>` for a plan task.
3. Refuse an existing branch or path. Do not copy tracked source changes. If the main checkout has `.worktreeinclude`, use Git's ignore-pattern parser to select untracked/ignored files, reject symlinks and existing destinations, and require literal complete paths for `.env*` files. Log every copied path in the run's `.models.log`.
4. Store `worktree`, `branch`, and `base_sha` on the run, then transition `provisioning` → `running`.

Outside git, copy the directory to `$AGENT_RUNS_DIR/scratch/<name>` (plan tasks use `scratch/<plan>/<task>`). Refuse home, filesystem roots and parents of multiple git checkouts. Skip rebuildable directories at every depth and preflight retained bytes against `GODFATHER_SCRATCH_MAX_MB` (2048 MiB by default), stopping early on overflow. Do not merge. A one-shot run refuses an existing scratch path. A resumed plan task reuses its scratch copy.

All worktree add/remove/prune and associated branch mutations hold an `fcntl` lock under the runs directory, keyed by the resolved git common directory. The lock covers existence checks, checkout, includes and rollback; contention waits up to 120 seconds. Agent execution does not hold this provisioning lock.

The integrator is a plan task with `task_type = "integrate"`. `plan.merge = true` appends one, with id `integrate`, when the plan does not declare one. It holds the plan row with a compare-and-set to `merging` and has its own worktree on `godfather/<plan>/<task>`, branched from `base_sha`. For each dependency that finished on a branch, in dependency order then `id` order, it runs `git merge --no-ff` in that worktree. A clean merge marks that task's run `done`. A conflict marks it `failed`, records the conflict paths on the event, stops the queue, and does not run the check. After every dependency merges cleanly, the integrator runs its own check. A failing check is `check_failed` and fails the plan. The integrator does not push, rebase, or delete the task branch.

The CWD lock applies to in-place runs, including adoption: one live run per shared directory realpath. Isolated runs do not take it. The plan scheduler reserves resolved directories before dispatching up to `max_parallel` task threads. Shared-directory tasks stay serial and integrators run alone. SQLite task rows determine readiness and dependency outcomes; local futures only track executing threads. Attempt prompts and child run names are persisted so a restarted scheduler can attach to a live durable child. Check-fix rounds nudge the existing CLI session if its saved files remain and the requested CLI matches; otherwise they create the next `.tryN` run.

Run payloads contain a `resources` ledger with `kind`, `path` or `id`, and `owned`.
Provisioning records creation; pre-existing resources are adopted. The shim of
the configured devbox CLI captures creation evidence and the effect evaluator denies stopping or
destroying IDs absent from the run's owned devboxes. Fallback and resume preserve
the ledger. `gc` reads all generations and plan payloads, defaults to a dry run,
and only applies cleanup with `--yes`. Git inspection and deletion use the same
per-repository lock as provisioning. Terminal age, ownership, clean status and
reachability from the base or remote refs gate removal. Scratch cleanup stays
within its pool without following symlinks. Devboxes are manual suggestions;
garbage collection never signals processes.

A devbox is a remote dev machine provisioned by a CLI that a `[devbox]` table in
`policy.toml` names. Without that table nothing is probed, shimmed, or guarded.

```toml
[devbox]
binary = "devbox"                                   # required: the executable name
stop = ["stop", "down", "rm", "delete", "destroy"]  # verbs that stop or destroy a box
create = ["create", "up", "start"]                  # arguments that may create one
value_flags = []                                    # global flags that consume the next argument
id_keys = ["id", "name"]                            # dotted paths into the status JSON, first match wins
created_pattern = "(?i)\\bcreated\\s+(?:a\\s+)?(?:devbox|box)\\s*[:=]?\\s*([\\w.-]+)"
detached_error = ""                                 # exact stderr of a status probe outside a supported project
```

Only `binary` is required; the other values shown are the defaults. The contract
is `<binary> status --json`, printing a JSON object with a boolean `attached`.
Isolation runs that probe before provisioning: an attached box is adopted in
place, an indeterminate answer refuses isolation, and a non-zero exit whose
stderr equals `detached_error` means the directory has no box. The shim treats a
bare invocation or one carrying a `create` argument as a possible creation. After
exit 0 it reads the box ID from `id_keys`, or from the one capture group of
`created_pattern` in the output; only a `created_pattern` match marks the box as
owned. A `stop` verb, found after global flags and inside `sh -c` strings, may
name only owned IDs; with no ID it applies to the attached box, and `--all` is
denied. `gc` suggests `<binary> <first stop verb> <id>`.

## Permissions

`policy.toml` remains the effect source: allow, ask, deny, by resolved target. A compiler writes a per-segment permission profile and records its path on `segments.permission_profile`. PATH shims default to audit. They log argv, cwd, resolved targets, and the verdict they would have given, then exec the real binary. They do not ask, deny, or delay. `GODFATHER_SHIMS=enforce` keeps today's blocking shims for one migration release.

Help-only invocations are an allow, including the `--help` form of a publish or deploy command and any other argv whose only effect is a help flag. That classification is applied before publish, deploy, or spend rules.

Publish, deploy, and spend commands of common CLIs are built in. `[[outward]]` tables in `policy.toml` declare the same for any other CLI, and add its binary to the shims:

```toml
[[outward]]
binary = "acme"                                   # required: the executable name
commands = ["apps", "payouts"]                    # first operand, after global options
read_only = ["list", "get", "retrieve", "help"]   # second operands that change nothing (default shown)
preview = false                                   # true: `--preview` anywhere in argv is an allow
```

`acme <command> <verb>` is then an ask unless the verb is in `read_only`. A bare `acme <command>` and commands not listed stay an allow. Use one table per group of commands that share `read_only` and `preview`.

| Verdict | Codex | Antigravity | Grok | Container, if enabled |
| --- | --- | --- | --- | --- |
| deny | Effect policy recorded in `<name>.permissions.json`; no native secret read denial or permissions-table override. Enforce-mode shims block denied commands. | Profile deny rules are passed in per-session `--settings` as `permissions.deny`; deny-glob enforcement is unverified. Secret reads are not denied, as with Codex. | `--deny` for `Edit` and `Write` globs, including `**/.env*`; no `Read` denies. | Only the repository and exact run notes file are mounted. |
| ask | `approval_policy="never"`; the preamble directs decisions through `godfather ask`. The shims, ask/deny flow, and audit log are the safety layer. | `--mode accept-edits`; no headless approval callback. The preamble directs decisions through `godfather ask`; command shims and the audit log are the safety layer. | No native approval callback. The preamble tells the agent to ask. The audit row records the command. | The repository and exact run notes file are the only mounts. Network is off for research. Publish and spend calls have no extra block while the network is on. |
| allow | `danger-full-access` by default. `CODEX_SANDBOX` opts into `workspace-write` with network on, or `read-only`. | Per-session `permissions.allow`: reads anywhere via `read_file(/)`, any shell command via `command(*)`, and `write_file(<root>/)` only in writable roots. Settings are merged without changing user settings files. Shell commands can write outside those roots. | `--always-approve` and `--allow` Edit/Write rules inside writable roots. | The repository and exact run notes file are mounted read-write. Temp, other run artifacts, and CLI scratch stay on the host. |

All Codex paths use the same default: direct launches, routed runs, plans, resumes, nudges, fixes, and reviews launch with `danger-full-access` and `approval_policy="never"`. The selected home's `config.toml` loads normally (default `~/.codex/config.toml`). There is no ignored user config, permissions-table injection, or legacy-config provisioning refusal. Secret reads are allowed by default; agents are instructed never to edit secret files. Shims default to audit; `GODFATHER_SHIMS=enforce` enables blocking command checks.

`CODEX_SANDBOX` accepts only `danger-full-access`, `workspace-write`, and `read-only`; invalid values exit 2. Resumes carry `-c sandbox_mode="..."` because `exec resume` does not support `-s` or `--add-dir`. Nudges retain the saved sandbox choice. For `workspace-write`, supported config overrides carry the worktree, git metadata, run artifacts, tool state, session scratch, `$TMPDIR`, and `/tmp`, with network enabled. The supervisor collects process identity with `ps`; Godfather does not nest `sandbox-exec`. Git status diagnostics and exit codes pass through unchanged.

The container is off unless the plan or the task sets `container = true`, or the user passes `--container`. It is the security boundary the shims never were. The runtime is Docker or a compatible `container` CLI. `GODFATHER_CONTAINER_IMAGE` must already contain the CLI. The repository and the exact run notes file are mounted read-write, as the invoking user; the notes directory is not mounted. Home, credential files, `.ssh`, `.env` files outside the repo, other run artifacts, temp, and the docker socket stay on the host. A CLI that needs an auth file under the home directory will not see it. Network is off for `task_type=research` and on for every other task type. A missing runtime or image fails provisioning. The task does not run on the host.

## Incident replay

Fixtures live in `tests/fixtures/<id>/`. They are checked in, redacted, and small. The replayer feeds frames through the production stream parser and the state machine, advances a virtual clock from the fixture, and asserts `transitions`. It does not call a model, sleep, or read `~/.agent-runs/`.

```toml
# tests/fixtures/grok-think-16m/fixture.toml
id = "grok-think-16m"
cli = "grok"
model = "grok-4.7"
incident = "thinking_without_tool"
note = "Recorded as godfather_phaseC.stream.jsonl"

[clock]
mode = "synthetic"
think_span_secs = 960

[policy]
# omitted keys use the defaults under test
```

```toml
# tests/fixtures/grok-think-16m/expect.toml
failure_class = "stall_thinking"
saw_statuses = ["running", "retrying"]
remedy = "nudge_tool"
classified_by_secs = 180
```

`stream.jsonl` is the recorded frames. `classified_by_secs` is virtual time. A fixture that only passes because the test sleeps, or because it greps a log, is not a replay.

## What each incident maps to

| Incident | Evidence | Mechanism that prevents it |
| --- | --- | --- |
| Grok thought for 16 minutes with no tool call. | `~/.agent-runs/godfather_phaseC.stream.jsonl` is a long run of `thought` frames. Those frames reset today's idle timer via `Progress.text`. | `thought` and `text` do not reset `last_tool_at`. `stall_thinking` fires at `think_without_tool_secs` (180). The remedy is one `nudge_tool`, then fallback. Fixture `grok-think-16m`. |
| Argon's stream broke on a huge single file write. | A single tool-call frame carried an entire file and the stream parser died mid-run. | Frames are incremental. Over `max_line_bytes` the line spills to disk and counts as an oversized tool heartbeat. A dropped CLI is `broken_stream`, retried once with `resume_chunked` (write in pieces under ~400 lines). A partial line never completes a segment. Fixture `argon-oversized-write`. |
| An auth token blip hung a run. | The process stayed up after an auth error and no further work happened. Healing waited on a heuristic stall. | The auth frame is class `auth` and a heartbeat of kind `error`. Retry is one resume after 30 seconds, then fallback. Silence after the error is still `stall_silent`. The run cannot sit in `running` past `segment_secs`. Fixture `auth-blip`. |
| A non-atomic install broke running launchers. | Replacing a live script in place changed the inode under a running interpreter. | `install.sh` already publishes by a same-directory temp file, mode bits, and `os.replace`. That is the rule for every publish: never truncate a live path. Schema migrations bump `user_version` inside the open database and do not swap its inode. A new binary that does not understand the version exits 2 before writing. Old launchers keep their old inode. Fixture `atomic-install`. |
| Waiters fired on partial segments. | A short `.last.md` or a mid-file stream looked like completion. | `wait` reads terminal run status only. The result and the terminal transition commit together. Segment status `streaming` or `finishing`, and any prefix of the stream file, is not terminal. Fixture `partial-segment`. |
| Codex's sandbox denied `ps` and `.git`. | `workspace-write` / Seatbelt rejected process listing and git metadata the worker needed. | All Codex runs default to `danger-full-access`. Optional `CODEX_SANDBOX=workspace-write` carries git metadata roots and inspection rules. The supervisor runs `ps`; no nested `sandbox-exec` is added. Fixture `codex-sandbox-ps-git` checks full access, git status, and inspection rules. |
| PATH guard false positive on a deploy command's `--help`. | A help probe matched a deploy rule and blocked or asked. | Shims are audit-only. The classifier treats help-only argv as allow before deploy rules, so `enforce` mode would also allow it. Native rules are generated from that classification. Fixture `deploy-help`. |
| `chmod 755` on a script the run created under `~/.grok` asked as a delete. | The effect model grouped chmod with `rm` and treated CLI config as outside the workspace. | `chmod` and `chown` are metadata. `~/.grok`, Codex `sessions`, and the Antigravity brain directory are writable for files created at or after the run started. A learned approval of that command covers the same binary and flags under the whole tree, and the answer prints the rule. |
| `git restore --staged :/` denied as a secret write. | The pathspec was expanded into worktree files, including secrets. | `--staged` or `-S` without `--worktree` or `-W` is index-only and allowed. A restore that writes the worktree is still a discard. |
| `allow (reason)` on a verdict question was stored as deny. | The shim required the answer to be exactly `allow` or `allow+remember`. | A verdict answer is an exact option, optionally followed by a reason. The reason is stored on the incident. Unrecognized text is an error and leaves the question unanswered. |
| Watcher restarts replayed old events. | A new watch read history from the start of `.events.jsonl` and repeated remedies. | `events.seq` is the cursor. Subscribe and resume start at the stored seq, normally the max at attach. `--replay` is opt-in. `remedies` is inserted before the action, so a second watcher cannot repeat it. Fixture `watch-cursor`. |
| The test suite opened the Antigravity app. | `tests/smoke.sh` ran `install.sh`, and `agent_oversight.py doctor` ran `agy -p /usage`. | `install.sh` only prints a hint. `godfather doctor` reads credential and state files. `--live` is required before `codex login status` or `agy -p /usage`. The suite puts tripwire `agy`, `codex`, and `grok` first on `PATH`. Fixture `hermetic-suite`. |

## Migration from the JSON registry

The public CLI, event names, exit codes, and artifact filenames stay. Segment display names stay `<name>` and `<name>#2`.

1. **Dual-write.** `.run.json`, `.route.json`, `.control.json`, `.events.jsonl`, `queue/*.json`, `incidents.jsonl`, `ledger.jsonl`, and `quota.json` are still written. The same write mirrors into SQLite.
2. **Reads.** `status`, `wait`, and `watch` read the store. The run status column is the state machine. The JSON payload on the row keeps the fields those commands already showed.
3. **Artifacts.** `.log`, `.stream.jsonl`, `.last.md`, `.models.log`, prompts, and diffs stay files, referenced by `artifact_dir`.
4. **Import.** The first `connect` loads existing `~/.agent-runs/*.run.json`, the state-dir queue, `incidents.jsonl`, `ledger.jsonl`, and `quota.json`. Import is idempotent on `run_id`. When all recorded owners and private-session members are gone, import expires stale blocking decisions and derives `done` or `failed` from the exit code and final report; a failure has class `crash`. A truncated JSON line is reported and skipped.
5. **Cursors.** `events.seq` is the cursor. A new watch starts at the max seq. `--replay` starts at 0. An unknown named cursor is rejected. Restarting a watcher delivers nothing it has already consumed.
6. **Shims.** The default is `audit`. `tests/test_permissions.py` and the routed launch check in `tests/test_safety.py` cover the three native profiles, the container refusal, help-only classification, and `GODFATHER_SHIMS=enforce`. Audit mode logs the verdict and execs the real binary.
7. **Plans.** `godfather plan run <plan.toml>` runs the graph. A bare `godfather run` brief is one isolated task and does not merge. `--no-isolate` keeps the shared directory.

There is no `godfather store` or `godfather export` subcommand. The first open of a database imports existing JSON once. A running launcher from before an install keeps its inode and may still write JSON. The mirror covers that window. Import is safe to repeat. Progress is a stream heartbeat.

Idle completion requires a new commit and a report after the last tool, with no open Codex items or Grok tool calls. Codex `turn.completed` or Grok `end` after the report permits `timeouts.final_idle_secs` (30 seconds) of inactivity. Without a verified end signal, including Agy transcripts, `timeouts.silent_final_idle_secs` defaults to 600 seconds. Every stream event resets the inactivity clock.

`watch_leases(subscriber PRIMARY KEY, pid, identity, acquired_at)` references `cursors`; schema version 3 adds this table without resetting existing subscribers or delivery history. Slot allocation and cursor creation share one SQLite write transaction.

Schema version 4 rebuilds the live-name index in place to include terminal `review_findings`. Review rounds validate fenced JSON findings (with legacy line fallback), persist parsing method and findings on the worker, and save loop options in `.review-state.json` for reviewer-question resumes. A bounded loop nudges the worker session, checks it, then reviews again.

Review follow-up corrections: monitor and MCP completion events use the shared terminal set; retrying is active and reports died when its owners vanish. A plan nudge advances child_attempt only after successful delivery, allowing replay after a scheduler crash. Source-tree reads conflict with non-isolated writers. Child questions stay owned by their sessions, are projected onto plan task payloads, and are answerable through the task name; pending questions keep the scheduler and plan open. Worker resume failures preserve successful review evidence and record a separate review_resume_exit_status.

## Cursor backend

`cursor-run` uses print/stream-json (not ACP). `godfather_cursor.py` resolves
catalog IDs with fast enabled by default, rejects NO ZDR labels without explicit
opt-in, and reads Dashboard usage best-effort. Catalog and usage caches live
under the runs directory, partitioned by effective Cursor home and executable,
with one-hour and five-minute TTLs respectively. `home` maps to both
CURSOR_CONFIG_DIR and CURSOR_DATA_DIR; effort stays encoded in the model ID.
An unknown usage signal never blocks routing. Included-only skips on plan usage
>=100% or active grants remaining <= the configured cents floor.

Stream frames confirmed from the 2026.11.11 bundled print implementation:
system/init carries session_id; assistant.message.content carries text;
thinking delta/completed carries text; tool_call started/completed carries
call_id and tool_call.<toolName>.args/result; usage/model_call carries
model_call_id and usage; result success/error carries is_error, result, session_id
and optional usage. The fixture tests/fixtures/cursor-stream.jsonl exercises
these shapes. Unknown events are stream heartbeats. A successful terminal result
is required for completion. Only structured errors and diagnostics are failure
evidence, not assistant text or nested tool results.

The common safety.prepare path installs command shims and adds the oversight and
notes preamble before Cursor invocation. --force/--sandbox disabled does not
provide a native effect callback or secret file-tool denial. Shims retain their
configured audit/enforce mode, and the optional container remains the hard boundary.
Cursor is excluded from review-only selection because team controls can disable
its sandbox. --review-fix permits Cursor. Direct ask/plan runs omit --force and
request --sandbox enabled. All Cursor prompts use the effective prompt file on
stdin. Stderr-only failures are preserved and classified before finalization; no
synthetic error obscures provider evidence. Doctor is passive unless --live. Actual chat IDs
are discovered from the stream; cross-backend fallback gets a fresh handoff.
