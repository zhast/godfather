# Plans

[Back to the README](../README.md)

```sh
godfather plan run examples/plan.toml
godfather plan status example
godfather plan status example --json
```

A plan is TOML in the same subset as `models.toml`: `schema = 1`, a `[plan]` table, and `[[tasks]]` tables. See [examples/plan.toml](../examples/plan.toml) and [ARCHITECTURE.md](../ARCHITECTURE.md).

| Field | Meaning |
| --- | --- |
| `plan.name`, `plan.repo` | Required. `repo` is the shared tree. Paths are relative to the plan file. |
| `plan.merge` | When true in a git repo, append an `integrate` task if the plan does not declare one. It merges task branches with `git merge --no-ff`, stops on conflict, runs its check, and does not push. |
| `plan.max_parallel` | Maximum concurrent ready tasks; default 2. Override with `godfather plan run file.toml --max-parallel N`. Use 1 for serial execution. |
| `plan.container` | Default hard boundary for every task. Off unless set, or the task sets `container = true`. |
| `id`, `brief`, `deps`, `task_type` | Required on each task. `task_type` is `research`, `write`, `edit`, `review`, `integrate`, or `test`. |
| `check` | Shell acceptance check in the task directory, with the conductor's environment. |
| `isolate` | Default true. A git task gets `$AGENT_RUNS_DIR/worktrees/<plan>/<task>` on `godfather/<plan>/<task>`. Outside git it gets a scratch copy. `false` runs in `repo`. |
| `model_hints` | Soft `cli:model` preferences. The router still skips a hint that is missing, unauthenticated, or cooling down. |

Ready tasks run concurrently only when their resolved working directories differ. A task writing `repo` with `isolate = false` also excludes isolated siblings that read it for scratch contents or `.worktreeinclude` files. Integrate tasks always run alone. The scheduler polls SQLite for task status, preserves completed tasks on resume, and reattaches to durable child runs after a scheduler crash. Acceptance-check fixes nudge the same saved CLI session when its session files still exist; otherwise they start the next `.tryN` run.

Run names are `<plan>-<id>`. Questions appear on the plan task in status output. Answer with `godfather answer <plan-task-name> "answer"`; it forwards to the child session. The scheduler keeps the plan open while a question is pending, reserves that task's directories and parallel slot, and continues checks and dependents after the answer.

A second `plan run` of an open, merging, or failed plan continues unfinished tasks. A done plan starts a new generation. `GODFATHER_PLAN_EXEC` replaces the agent launch for a local driver; unset, each task is `godfather run --no-isolate` in the directory the plan already chose. Stdout lines are `PLAN`, `TASK`, and `CHECK`. The same facts are `STATUS` and `CHECK` rows in the store.
