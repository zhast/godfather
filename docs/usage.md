# Running tasks

[Back to the README](../README.md)

## Usage

Write a brief with the task, relevant files, constraints, and acceptance checks:

```sh
printf 'Inspect the parser, fix empty input handling, and run its tests.\n' > /tmp/parser-brief.md
CWD="$PWD" godfather run /tmp/parser-brief.md parser
godfather status
godfather stop parser            # stop its private sessions and process tree; records stopped
godfather watch
```

`CWD` is required. Relative paths resolve from your calling shell. Names must be
single filenames, excluding `.` and `..`; use a unique name for each concurrent
run. A live name is reserved atomically and cannot be overwritten. Isolation is the default: each run gets its own git worktree, or a scratch copy outside git. Caller-provided linked worktrees are adopted in place. Before isolating a git checkout, the devbox CLI named by `[devbox]` in `policy.toml`, if any, is probed with `<binary> status --json`. With an attached devbox, the default runs in CWD, records the workspace and devbox as adopted, and prints an `ADOPT` line. This takes the same CWD lock as `--no-isolate`, including `--wait-lock` behavior. Explicit `--isolate` still refuses an attached devbox; an indeterminate attachment refuses isolation too. No `[devbox]` table, a missing binary, or a non-zero exit whose stderr equals `detached_error` keeps normal isolation. Queued dependents preserve the distinction between default and explicit isolation and recheck at launch. Pass `--no-isolate` to share CWD explicitly. The launching command streams output and waits for completion. A forked owner is reparented to init/launchd so desktop-session descendant cleanup cannot reach the run. The router, model launcher, supervisor, and CLI run in private sessions with no controlling terminal. Durable owners ignore SIGHUP, take stdin from `/dev/null` (Codex receives its prompt from a file), and write output to files. Direct `codex-run`, `agy-run`, `grok-run`, and `cursor-run` use the same durable-launch path. A run keeps running if the launching shell or output pipe disappears. SIGTERM, SIGINT, or SIGHUP to the attached launcher detaches it without forwarding signals; attachment output files are removed when it exits. Use a background shell job to detach immediately, and `godfather stop <name>` to terminate the run explicitly. Stop includes reparented children and separate process groups within the run’s private sessions, escalates TERM to KILL, and verifies exit before recording `stopped`.

### Resource ownership and cleanup

Run records and `godfather status --json` expose a `resources` ledger. Each entry
has `kind` (`worktree`, `branch`, `scratch`, or `devbox`), a `path` or `id`, and
`owned`. Resources created by the run are owned; an existing workspace, branch,
or attached devbox is adopted (`owned: false`). Ownership follows fallback
segments and resumes. The shim of the configured devbox CLI records successful creation evidence
from output matching `created_pattern` and from attachment status. Stop IDs are
read from the status JSON by `id_keys`, in order. Output readers keep
draining if a consumer closes either stream. Its effect policy denies the configured `stop` verbs (by default `stop`, `down`,
`rm`, `delete`, and `destroy`) for adopted or unknown IDs, including an omitted ID
unless the attached box is owned. Global flags before the verb are supported;
help arguments on a wrapping shell do not exempt its commands. User and learned allows cannot override that
rule. Shims still follow the configured audit/enforce mode; set
`GODFATHER_SHIMS=enforce` to block denied commands.

```sh
godfather gc                    # dry-run table; default age 7d
godfather gc --older-than 24h    # durations: s, m, h, d, w
godfather gc --older-than 7d --yes
```

`gc` lists recorded resources with a removal or keep reason. It only removes
owned resources of terminal runs older than the cutoff. A resource still used
by an active run or a more recent run is kept. Worktrees must be clean according
to `git status --porcelain`; every commit must be reachable from the recorded
base branch or a remote-tracking branch. Unmerged, unpushed commits keep both
the worktree and branch. Tags and unrelated local branches do not qualify.
Missing base refs, unavailable repositories and uncertain inspection keep the
resource. Worktree removal and branch deletion share the per-repository Git
lock, and worktree removal never uses force. Remote refs are inspected locally;
`gc` does not fetch.

Old scratch copies can be removed because reports remain in the runs directory.
Cleanup refuses paths outside the corresponding runs `worktrees/` or `scratch/`
pool and symlink components; symlinks within scratch copies are never followed.
Records predating the ledger infer ownership only from these pools and the
`godfather/` branch namespace. Run records and reports remain available. `gc`
never kills processes or touches devboxes; old owned devboxes get a manual
`<binary> <stop verb> <id>` suggestion. The last line reports total bytes reclaimed
(zero in a dry run).

Dependency names in `--after` and `--after-any` may register after the dependent is queued. At enqueue, each dependency binds to its current non-terminal instance, including a running or queued parent. A missing or terminal name waits for its next instance after enqueue; an older completion under a reused name cannot release it. The bound identity is persisted across daemon restarts and follows that instance through fallback segments. Missing or old names wait up to `--dependency-timeout` seconds (default 300), then end as `dependency_failed` with the missing names in the event and queue error.

`godfather nudge <name> "message"` queues a corrective instruction and returns immediately. A live segment finishes naturally before a background worker resumes its exact saved session with the message. If the run finishes on its own (`done`, `failed`, `check_failed`, or another terminal outcome), the queued nudge reopens it. It expires only if the user stops the run (`stopped` or `stop_requested`) or the run instance is replaced: the worker writes a `NUDGE_EXPIRED` line to `<name>.models.log` and emits an important `NUDGE expired: ...` event. You can explicitly nudge a previously stopped run to reopen it. Before a nudge resumes a session, the previous non-empty `.last.md` is copied to `<name>.last.1.md` (then `.last.2.md`, ...), so a resume never loses the earlier report. Use `godfather wait <name>` to track completion, or `godfather nudge --wait <name> "message"` to stay attached; `--wait` exits 1 when the nudge expired. A pending question must be answered before nudging.

### Project lessons

Save project corrections for later runs:

```sh
godfather lessons --cwd /path/to/project add "Use devbox run with ARGV; ENV is not forwarded."
godfather lessons --cwd /path/to/project list
godfather lessons --cwd /path/to/project rm 1
EDITOR=vi godfather lessons --cwd /path/to/project edit
godfather nudge parser "Never stop a devbox you did not start." --remember
```

`--cwd` defaults to `$CWD`, then the current directory. Lessons live in
`$AGENT_RUNS_DIR/lessons/<project-key>.md` (default runs directory:
`~/.agent-runs`). The key is the SHA-256 of the resolved git common directory,
so linked worktrees share lessons. Outside git it hashes the resolved project
CWD; isolated scratch runs and plan tasks retain their source project's identity.
The file header names the project. Each non-empty line is one lesson; Markdown
headings are ignored and bullet/number prefixes are stripped. Files are stored
oldest first; `list` numbers local lessons newest first, and `rm N` uses those
numbers. `edit` shows the same newest-first numbered entries, using `$VISUAL`,
then `$EDITOR`, then `vi`, on a private draft. Failed editors and concurrent
changes leave the live file untouched.

An optional `.godfather/lessons.md` at the repository root supplies shared
lessons. It is only read; these commands never edit it. Its last entry is treated
as newest. Unreadable, non-UTF-8, and directory lesson files are skipped with a
warning; missing optional files are silently skipped. If Git discovery times out
or cannot run, lessons use the resolved CWD identity. Local corrections come
before repository guidance in the injected `Project lessons:` section.
Every CLI launch, resume, and handoff loads the
section anew, capped at about 2 KB (2048 UTF-8 bytes). When it fills, the newest
lesson is retained (truncated if necessary), with the number of older lessons
omitted and their source file paths.

Before saving or reading lessons, Godfather drops lines containing common secret
patterns (API tokens, AWS access keys, private-key headers, `password=`, and long
base64/hex strings) or instruction overrides such as “ignore previous
instructions”, “you are now”, “disregard the policy”, and “system prompt”. Stderr
reports line numbers and reasons without printing rejected text. An `sk-` key
must start a token and have at least 20 body characters; ordinary words such as
`task-type`, `disk-usage`, `ask-before`, and `risk-` are retained. Filtering also
applies to manually edited files and repository lessons. With `nudge --remember`,
the filtered message is both delivered and saved for the **target run's** project;
an entirely rejected message is neither delivered nor saved. Ordinary nudges
keep their existing behavior. Lessons are prompt reminders only: they never
change effect-policy rules or grant an approval.

### Isolation and directory ownership

```sh
CWD="$PWD" godfather run -p "Fix the parser" parser
CWD="$PWD" godfather run --no-isolate -p "Fix the parser in this checkout" parser
```

Isolation is the default. Inside a git repository, create a persistent worktree under
`$AGENT_RUNS_DIR/worktrees/<name>` on branch `godfather/<name>`, starting at HEAD.
Tracked source-directory changes are not copied. The branch and directory
are printed, and the branch is reported again at the end. Existing branch/path
conflicts refuse rather than overwrite. A one-shot run never merges. A plan
integrate task merges task branches, stops on conflict, and runs its check.
Outside git, the run gets a scratch copy under `$AGENT_RUNS_DIR/scratch/<name>`.
Scratch copies refuse the user's home directory, filesystem roots, and directories
directly containing two or more git checkouts; pick the project folder or use
`--no-isolate`. They skip `node_modules`, `.venv`, `venv`, `__pycache__`,
`.pytest_cache`, `.mypy_cache`, `.next`, `dist`, `build`, `target`, `.gradle`,
`.cache`, `DerivedData`, and `.DS_Store` at every depth. A preflight walk stops
and refuses once the retained files exceed `GODFATHER_SCRATCH_MAX_MB` (default
2048 MiB), reporting the measured size and the setting to adjust.

For git isolation, a `.worktreeinclude` in the main checkout can opt local
untracked or ignored files into new worktrees. It uses gitignore patterns, one
per line, with `#` comments and `!` exclusions. Tracked changes, symlinks and
existing destination files are skipped. Files under a `.env*` path are copied
only when that complete file path is named literally (for example,
`config/.env.test`); a wildcard or directory pattern cannot opt them in. Every
copied path is recorded as `WORKTREE_INCLUDE` in the run's `.models.log`.
Without `.worktreeinclude`, worktree contents follow the existing behavior.
Worktree add/remove/prune and their branch creation/deletion hold a shared
per-repository lock keyed by the resolved git common directory. Concurrent
provisioning waits up to 120 seconds before reporting a lock timeout.

`--no-isolate` runs in CWD and holds an exclusive lock keyed by that realpath.
A second shared run refuses clearly; use `run --wait-lock` to wait. Isolated
runs do not take that lock. The router holds a shared-directory lock across
quota switches and check/fix rounds; direct launchers and nudges of a shared
directory also acquire it. Locks release on exit, including errors.
Launching inside the install prefix or the source `bin/` directory refuses
unless `run --self` or `GODFATHER_SELF=1` is explicitly given. `install.sh` marks those files
read-only. The file owner can still change the mode; the working-directory
check is what stops a normal run from using that directory. `--self` (equivalently `GODFATHER_SELF=1`) opts into
those edits without disabling the other policy guards. Updates go through
`install.sh`, which publishes read-only replacements atomically.

### Proof before done

```sh
CWD="$PWD" godfather run -p "Fix the parser" --check "npm test" --max-fix-rounds 2 parser
```

Run the shell check in the task directory after the agent finishes. Emit
`CHECK <name> pass|fail (round N)` to stdout and watch. Round 0 is the initial
check. On failure, resume the same session with the failing command and its last
80 output lines, up to two fix rounds by default. Only a passing check marks the
run `done`; exhausted/failed fixes mark it `check_failed` and return nonzero.
The latest check output is `<name>.check.log`; events and audit retain history.
Pending questions defer verification until the answered session finishes. Later
manual nudges also rerun the stored acceptance check.
Checks are user-supplied shell commands and run with the conductor's environment.

## Dependencies and waiting

```sh
godfather wait parent --timeout 1800
CWD="$PWD" godfather run --after parent,tests -p "Integrate their work" integration
CWD="$PWD" godfather run --after-any parent -p "Inspect the outcome" inspection
CWD="$PWD" godfather run --wait-lock -p "Next task in this directory" next
```

Wait follows every segment (`parent`, `parent#2`, …), checks, review, healing and
pending fallback. A partially written `.last.md` is never a completion signal.
It prints the final status and returns 0 only for `done`; timeout/failure is
nonzero. Nonblocking questions stay pending until answered. When all run owners exit, stale blocking questions expire and exit-code/final-report evidence determines the terminal state. MCP `wait` uses these same rules.

Dependencies are persisted in the state directory's `queue/`. Queued runs show as
`queued` in status and emit `QUEUED`, then `START` when ready. `--after` requires
all predecessors to succeed; failure records `dependency_failed`. A predecessor
whose report starts with `INCOMPLETE`, `BLOCKED`, or `FAILED`, or that exited with
a blocking question, keeps an `--after` dependent queued with that reason so you
can nudge the predecessor to finish. `--after-any` accepts any terminal outcome,
including an incomplete report; use it when the dependent should start once its
parent stops, for example when the parent committed its work but skipped a check. Enqueue starts a small background queue daemon;
subsequent godfather invocations and watch restart it if necessary. You can also
run `godfather daemon` directly. Worker launch environment settings are persisted,
but credential values are not copied into queue files.

## Cross-model review

```sh
CWD="$PWD" godfather run -p "Fix the parser" --check "npm test" --review-by auto parser
CWD="$PWD" godfather run -p "Fix the parser" --review-by codex:gpt-6.1-sol --review-fix parser-fix
CWD="$PWD" godfather run -p "Fix the parser" --review-by auto --review-rounds 2 parser-reviewed
```

After the task and any acceptance check pass, start a separate reviewer model.
`auto` chooses the highest-ranked eligible model on a different CLI. An explicit
model name (or `cli:model`) can choose another model on the same CLI. The worker's
exact model is always excluded. Normal quota, authentication, policy and locking
rules apply to the reviewer; no eligible reviewer fails with `review_failed`.

The review receives the original task, the git diff relative to the run's starting
HEAD (including commits on an isolated branch), and the pre-run diff as context.
Env-file paths are excluded. Outside git, it inspects the task outcome in CWD.
The reviewer returns a fenced JSON block:

```json
{"findings":[{"severity":"high","file":"parser.py","line":12,"problem":"Empty input crashes"}]}
```

Python validates severity, file, positive integer line, and problem text. Missing
or invalid JSON falls back to `- [severity] file:line - concrete problem` parsing.
The report stays in `<name>.review.md`; the run records `review_parse_method`
(`json` or `lines`) and the parsed `review_findings`. Each review emits
`REVIEW <name> round K: X findings (max severity)`, starting at round 1.

`--review-rounds N` requires `--review-by` and allows N worker fix rounds after
the initial review. Findings at or above `--review-threshold` (default `high`;
choices `critical`, `high`, `medium`, `low`) go back to the original worker's
saved session. The worker must fix them or explain why they are wrong; the next reviewer receives
the worker's latest report to assess that explanation. The check,
when set, runs again before another independent review. A clean review finishes
`done`; findings at the threshold after the budget is spent finish
`review_findings` with a nonzero exit, including in `wait` and plan dependencies.
The default 0 keeps report-only behavior: findings do not fail the run. Review
execution failures use `review_failed`. A failed worker resume uses the worker failure
status and `review_resume_exit_status`, preserving the successful review
exit status, findings, and report. Pending questions preserve the loop and
its round budget until answered.

The reviewer is instructed to make no changes by default. `--review-fix` permits
only clear bug fixes and reruns the supplied check afterward. Pending questions
defer completion; answering the reviewer resumes it and updates the original
report. Check failures prevent review. Review logs and ledger segments use
`<name>.review` (plus fallback suffixes); isolated reviews stay in the worker's
worktree. Godfather never merges or pushes.
