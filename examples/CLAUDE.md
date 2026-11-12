## Godfather conductor

Claude is the conductor. Delegate implementation (code, edits, scripts, tests,
and grading runs) to a CLI agent. Write a clear brief, review its output and the
actual diff, run appropriate checks, and commit only when the user asks.
Use the same pattern with any orchestrating agent that can run shell commands.

- Prefer Godfather MCP tools when available: `run(prompt, name, cwd, isolate?,
  check?, review_by?)` starts immediately and returns a run ID. Use `status`,
  `events(since?)` and `wait(name, timeout_s)` to supervise. Save each events
  cursor for the next poll. A timeout is not completion. Status comes from the
  SQLite store (`events.seq`), not from file mtimes or a growing `.last.md`.
- Use `answer(name, answer)` for NEEDS_INPUT, `nudge(name, message)` for a concrete
  correction, and `cancel(name)` only after a human decision when needed.
  `quota()` and `usage(period?)` expose availability and usage accounting.
- Set `review_by: "auto"` for an independent review on a different CLI. Inspect
  `<name>.review.md`, the actual diff, and the acceptance-check result before
  declaring completion. Review findings require judgment even when execution
  succeeds. For clear bug fixes, use CLI `--review-by auto --review-fix`.
- Fall back to the CLI below when MCP tools are unavailable. Never choose the
  worker CLI/model yourself; Godfather's global ranking owns selection.
- Write each brief to a prompt file. Include the working directory, intended
  behavior, constraints, relevant files, and acceptance checks.
- Conductor rule: delegate with `godfather run`; never pick a CLI yourself.
  Use `CWD=/path/to/project godfather run /path/to/prompt.md unique-name` or
  `CWD=/path/to/project godfather run -p "task" unique-name`.
  Godfather follows the global `models.toml` ranking, checks quota, and handles
  conversation resume or a cross-CLI handoff when quota is exhausted.
- Give concurrent runs distinct names. Isolation is the default, so each run has its own worktree or scratch copy.
  Launch long tasks as background shell jobs so supervision can continue.
- Read `~/.agent-runs/<name>.models.log` and `.route.json` for routing and the
  latest segment name. Read that segment's `.last.md` and `.log`; inspect the
  real changes. Honor `AGENT_RUNS_DIR` when overridden.
- Run `godfather status` for a snapshot and `godfather watch --important` through the Monitor tool
  for actionable NEEDS_INPUT, DENY, ESCALATE, CHECK fail, unhealed DIED, final QUOTA, and DONE events. Use verbose watch for diagnostics.
- Investigate an alert before intervening. Use
  `godfather nudge unique-name "A concrete corrective instruction"` to stop and
  resume the exact registered session. Never kill agents by process name.
- Keep one grader throughout a comparison so scores remain comparable.
- These launchers allow unattended edits and shell commands with broad access.
  Delegate only tasks the user has authorized; approval for a task does not
  expand permission to publish, spend money, or send messages.

- Watch for `NEEDS_INPUT <name>: ...` and `DENY <name>: ...`. Inspect the
  question, policy denial, run registry, and audit log. Answer decisions with
  `godfather answer <name> "answer"`; stopped workers resume their saved session.
  A live blocking question receives the answer without restarting.
- Escalate anything outward-facing to the human: pushes, deploys, publishing,
  sending messages, or spending money. Also escalate destructive/ambiguous choices
  beyond the user's authorization. Never loosen a policy rule merely to finish.
  Policy asks accept an exact option (`allow`, `deny`, or `allow+remember`), optionally followed by a reason. Any other text is an error and leaves the question unanswered. Use `godfather answer <name> allow --remember` for reviewed safe recurring effects. When the targets sit under one CLI-owned tree (`~/.grok`, Codex sessions, or the Antigravity brain directory), the learned rule covers that binary and those flags for the whole tree, and the answer prints the rule. Inspect `godfather policy` and forget rules when they are no longer appropriate. `chmod` on a file this run created in those trees is a metadata change and is allowed. `git restore --staged` without `--worktree` only touches the index and is allowed.
- Isolation is the default. Inspect the reported `godfather/<name>` branch and
  worktree. A one-shot run never merges; a plan integrate task does, and it stops
  on conflict. Pass `--no-isolate` only to share a checkout; that takes the CWD
  lock. Leave godfather's installed scripts alone unless `--self` was explicitly
  authorized.
- Supply `--check "appropriate test command"` when delegating. Only `done` after
  a passing CHECK is verified completion; inspect `check_failed` after exhausted
  fixes, and handle `needs_input` before treating a run as complete. Once that
  check has passed and a commit exists, the run is done. The final report is a
  separate short turn (default cap 2 minutes). If it runs long, the supervisor
  writes the report from the commit and the events.

Use `godfather top` for interactive oversight (questions are highlighted), and
`godfather usage --today --by model` to inspect tokens, optional estimated costs
and quota burn. An in-flight tool, a CPU-active child, or a declared `sleep` is
alive, so it is not a STALL and a nudge must not kill that tree. A kill/restart
heal needs high confidence. SLOW is an active long model response. A quota probe
that cannot be read leaves the model eligible; a probe that fails to parse cools
the CLI down. Task auth failures fall through to the next CLI.

Use `godfather plan run <plan.toml>` for a dependency graph (`examples/plan.toml`).
`godfather plan status` reads the store. `godfather wait <name>` and `run --after <name>` still queue one run. Never poll
`.last.md` because fallback segments can leave partial replies. Use `--no-isolate --wait-lock`
when work must wait for a shared directory. `godfather watch` is the one healer;
a second watch only observes, and the healer exits when its stdout closes or its
parent dies. Run `godfather doctor --since 24h`
periodically and inspect its suggestions. `godfather doctor` reads credential
files; add `--live` only when a real `codex` or `agy` probe is intended. Use `godfather improve --dry-run` to
review its brief, then `godfather improve` for tested fixes on an isolated review
branch. Inspect and merge manually; improvement never merges automatically.
