# Policy and safety

[Back to the README](../README.md)

## Native CLI permissions and containers


A routed run (`godfather run`, or a plan task) compiles one effect policy into `<name>.permissions.json` and passes what each CLI can actually enforce.

Every Codex run defaults to `-s danger-full-access` and `approval_policy="never"`: direct launches, routed runs, plan tasks, resumes, nudges, fix rounds, and reviews. `CODEX_SANDBOX` opts into `workspace-write` (network stays on) or `read-only`; any other value exits 2. Resumes use supported `-c sandbox_mode="..."` overrides. The selected home's `config.toml` loads normally (default `~/.codex/config.toml`). Godfather injects no Codex permissions table, and secret reads are allowed by default. Godfather does not nest `sandbox-exec`.

Command shims, the `godfather ask`/deny flow, and the audit log are the safety layer. The preamble directs agents to ask before destructive or outward actions, never edit secret files, and use headless Playwright for browser work instead of the user's own Chrome or an extension or MCP attached to it. Shims default to audit; enable `GODFATHER_SHIMS=enforce` for blocking command checks. Prefer `git status -- <paths>` in runs; git diagnostics and exit status pass through unchanged.

Grok on a routed run keeps `--always-approve --no-plan --sandbox off` and adds `--deny` and `--allow` rules for `Edit` and `Write`, including `**/.env*`. Direct launches also deny `Edit(**/.env*)` and `Write(**/.env*)`. No `Read` denies or SSH/CA read exceptions are added. Grok has no native approval callback. `chmod` is outside Edit/Write checks; the effect model classifies it and the audit log records it.

Antigravity on a routed run uses `--mode accept-edits` with per-session `--settings` and strips `--dangerously-skip-permissions`. The allow rules grant `read_file(/)` (reads anywhere), `command(*)` (any shell command), and `write_file(<root>/)` for each writable root. Existing session settings are merged; the user's settings file stays untouched. Auto mode is account-restricted and can silently fall back to prompting, which headless runs cannot answer. Profile deny rules are passed as `permissions.deny`, but deny-glob enforcement is unverified; secret reads are not denied, as with Codex. The write rules scope the `write_file` tool, while shell commands can write elsewhere. The preamble directs decisions through `godfather ask`; command shims and the audit log are the safety layer, with shims retaining their configured guard mode. Only the optional container is a hard boundary. A headless tool refusal is classified as `tool_denied` and falls back according to the retry policy without blacklisting the other Antigravity models as authentication failures.

`godfather run --container`, or `container = true` on a plan or task, runs the CLI in Docker or a compatible `container` CLI. `GODFATHER_CONTAINER_IMAGE` must name an image that already contains the CLI. The repository and the exact run notes file are mounted read-write, as the invoking user; the notes directory is not mounted. Home, credential files, `.ssh`, `.env` files outside the repo, other run artifacts, temp, and the docker socket stay on the host. A CLI that needs an auth file under the home directory will not see it, and files it writes only under the home directory stay in the container. Network is off for `task_type=research` and on for every other task type. Stdout stays attached to the supervisor. A missing runtime or image fails the task and does not fall back to the host.

PATH shims default to audit. They log argv, cwd, resolved targets, and the verdict they would have given, then exec the real binary. They do not ask, deny, or delay. `GODFATHER_SHIMS=enforce` restores the blocking ask/deny path. Audit mode is not a security boundary. Help-only argv, including the `--help` form of a publish or deploy command, is an allow in both modes. Installed godfather files stay read-only. Run artifacts can contain prompts, source code, shell arguments, and diagnostics; keep them private.

## Decisions: ask and answer

Inside any launched agent session:

```sh
godfather ask "Which migration approach?" --options "small|full"
godfather ask "May I delete the old export?" --blocking --timeout 1800
```

`ask` records the question, choices, and answer in the run registry and emits
`NEEDS_INPUT <name>: <question> [options]` through `godfather watch`. Only one
unanswered question is allowed at a time. Without `--blocking`, finish independent
work and stop; the run is `needs_input`. With it, poll until answered and print
only the answer to stdout. The default timeout is 30 minutes; timeout exits 1 and
leaves the question available to the conductor.

```sh
godfather answer migration "small"
```

An answer to a live blocking question stays in the same CLI invocation. If the
agent stopped, `answer` resumes its exact saved session like `nudge`. If a
nonblocking agent is still finishing independent work, `answer` saves the answer
and waits for it to stop before resuming. Logical router names resolve to the
latest segment. A verdict question accepts exactly `allow`, `deny`, or `allow+remember`. A reason may follow the option, after a space or `(`, and is stored on the incident. Any other text is an error and leaves the question unanswered. `allow+remember`, or `allow --remember`, records a learned rule and prints that rule on stderr.

Every launch, resume, and quota handoff prepends a standard instruction: ask before
anything destructive, outward-facing, or ambiguous, including pushes, deploys,
deleting files the run did not create, or spending money; never edit `.env` files
or other secret files; otherwise proceed. `godfather` is placed on the child's
PATH and the registry is writable. Fresh Codex and Antigravity runs use
`--add-dir`; Codex resume uses supported `-c` writable-root overrides, and
Grok uses native Edit/Write allow rules.

Every launch also names `$GODFATHER_NOTES`: the absolute path to
`$AGENT_RUNS_DIR/<logical-name>.notes.md`, outside the workspace and shared by
all segments and resumes of the run. Agents keep brief Done, In progress, Next,
Decisions (and why), Ruled out, Checks run (command and result), and Open questions
sections there. The launcher creates the private file if missing and preserves
existing notes. Native profiles add this exact file: Antigravity `write_file(path)`,
Grok `Edit(path)` / `Write(path)`, and Codex workspace-write's writable paths
(including resume). The effect policy also allows this exact file across segments. Explicit Codex
`read-only` mode remains a no-write exception: the agent reads notes and puts
progress updates in its final report. Containers bind-mount only the notes file
alongside the repo; agents update it in place.

## Effect policy and audit

Shims resolve the effects of commands, including real paths and expanded globs,
before choosing a verdict:

- **allow:** read-only commands and writes inside the run's working directory
  (its worktree or scratch copy), `$TMPDIR`, `/tmp`, `/var/folders/*/T`, its own
  `$AGENT_RUNS_DIR/<name>*` artifacts, and its own CLI session/brain scratch paths.
  Preexisting workspace files are writable too. `rm -rf /tmp/my-scratch` is safe.
  `chmod` and `chown` are a metadata change. A mode change on a file this run
  created under `~/.grok`, `$CODEX_HOME/sessions` (default `~/.codex/sessions`),
  or `~/.gemini/antigravity-cli/brain` is allowed, including a path that does
  not exist yet. A preexisting file in those trees still asks. `git restore
  --staged` or `-S`, without `--worktree` or `-W`, updates only the index and
  is allowed. A pathspec of `:/` is not expanded into worktree files.
- **ask:** destructive effects outside these roots, pushes, repo creation/deletion,
  PR merges, publishing, production mutations, deployments without `--preview`,
  infrastructure apply/delete, spending, and `curl | sh`. An ask waits for a
  decision; it is not a hard policy failure.
- **deny:** writes to `.env*`, keys, PEM files, credentials and other recognized
  secret paths, or changes to godfather's install/source without `--self`.
  Learned/user allow rules cannot override these protections.

Curl checks body, header, cookie, trace, stderr, generated-code, write-out, ETag, HSTS and alt-svc cache destinations, including combined short flags and `--option=value`. Relative body outputs resolve under `--output-dir` regardless of option order; `--create-dirs` also checks missing parent directories. Cache and header paths remain relative to CWD. Only stream sinks (`/dev/null`, `/dev/stdout`, `/dev/stderr`, or `-` for options that support it) are allowed by default; other destinations, implicit remote filenames and `--config` ask even alongside `-o /dev/null`. Cache flags conservatively treat `-` as a filename; empty HSTS and alt-svc cache names keep the cache in memory.

Resolution covers `rm`, both sides of `mv`, `cp` destinations, `git clean`,
`reset`, `checkout -- <path>`, `restore`, `truncate`, common writing utilities,
and detectable shell redirects. `git restore` that also names `--worktree`, and a default `git restore`, rewrites the worktree and is classified as discarding files. Symlinks resolve to their actual target.
Shells reading piped stdin conservatively ask as well, so `curl | sh` is covered
even when the calling shell uses an absolute binary. Read-only help probes remain allowed. Internal cleanup, quota probes, registry
writes and acceptance checks use the original conductor PATH and bypass shims.

A policy ask emits `NEEDS_INPUT` with an automatically generated question:

```text
Allow `git push origin main`? It would publish commits to a remote. [allow|deny|allow+remember]
```

The shim uses the blocking `godfather ask` path. `ASK_TIMEOUT` defaults to 900
seconds (15 minutes). Allow executes the command; deny or timeout returns 126
with an explanation. Answer from the conductor:

```sh
godfather answer task deny
godfather answer task allow --remember
godfather policy
godfather policy forget <rule-id>
```

Configuration lives in `${XDG_CONFIG_HOME:-~/.config}/godfather/policy.toml`.
The top-level `writable_roots` array records tool state outside the workspace
and grants access when `CODEX_SANDBOX=workspace-write` is selected. Paths may use `~/` and environment variables;
they must resolve to absolute directories, without globs. Missing directories
are created at launch so the native CLI can grant access on first use. For example,
on macOS:

```toml
writable_roots = [
  "~/.config/devbox",
  "~/Library/Application Support/acme-dev",
  "~/Library/Caches/acme-dev",
  "~/.acme-dev",
]
```

Use the tool's state/cache paths on other platforms (or its configured overrides).
These roots do not approve commands: the effect policy and configured shim mode
still apply. With `CODEX_SANDBOX=workspace-write`, supported config overrides
grant these roots and temporary directories (`TMPDIR` and `/tmp`) on launch and resume.
Git's common and per-worktree metadata directories are discovered
from the run's checkout at launch and included automatically.

`process_inspection = true` (the default) permits read-only `ps`, `pgrep`,
`lsof -p`, and the noninteractive macOS `top -l 1`, including their system paths.
Native Codex launches publish these prefixes to
`${CODEX_HOME:-~/.codex}/rules/godfather-process-inspection.rules`, the
[supported Codex rules location](https://developers.openai.com/codex/rules/).
Set the policy key to `false` to clear those grants. Other command guards,
effect policy checks, and any opted-in filesystem sandbox still apply.

Rules match a binary, argv/flags, resolved target globs and optionally CWD:

```toml
[[rules]]
id = "reviewed-cleanup"
binary = "rm"
args = ["-f", "/approved/build/*"]
targets = ["/approved/build/*"]
verdict = "allow"
```

The effective defaults are always present. Explicit rules apply afterward, in
file order; learned rules follow user rules. Old command-glob configurations
are accepted for migration, with old denials converted into asks. New defaults
use effects. `godfather policy` labels default, user and learned sources.
Learned approvals go only into `policy.learned.toml`, with their originating run
and question in a comment. When every target sits under one CLI-owned tree, the
rule keeps the binary and the flags and covers that whole tree, for example
`chmod +x under /Users/me/.grok/**`. `godfather answer` prints the rule, so a
wide tree is visible before the next command relies on it. Targets outside
those trees stay narrow: same parent, same flags, same cwd, with only the
basename generalized. Secrets and godfather's own files stay denied either way.
Learned rules never rewrite user configuration.

Every ask, deny and legacy `BLOCKED` event appends to
`${XDG_STATE_HOME:-~/.local/state}/godfather/incidents.jsonl` with run, command,
CWD, resolved targets, verdict, answer and timestamp. Ask creation and resolution
are separate records tied by question ID. `<name>.audit.log` additionally records
launches, checks, shim argv and commands visible in native CLI logs. All run files
are private; avoid secrets in command arguments.

PATH wrappers in audit mode are a log, not a security boundary. Absolute
binaries, a changed PATH, arbitrary programs, and direct file-tool writes are
outside what a shim can stop. Enforce mode can still ask or deny the commands
it classifies. Grok adds native Edit/Write denies for secret paths. Codex
uses full access by default, with no native secret deny profile. The container is the hard boundary; its host mounts contain only the repository and the exact run notes file.

## Devbox and outward CLIs

Two optional `policy.toml` tables extend the effect policy to your own tools.
`[devbox]` names a CLI that provisions a remote dev machine, so a run can stop
only the boxes it created. `[[outward]]` declares the publish, deploy, or spend
commands of any other CLI, so they ask before running. Both are inactive when
absent. The keys and defaults are in
[ARCHITECTURE.md](../ARCHITECTURE.md#permissions).
