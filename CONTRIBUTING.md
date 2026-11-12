# Contributing

Bug reports and pull requests are welcome.

## Run the tests

```sh
./tests/smoke.sh
# Optional, when installed:
shellcheck install.sh bin/codex-run bin/agy-run bin/grok-run bin/cursor-run tests/smoke.sh
```

Tests use temporary homes and fake binaries. They do not contact model services
and need no CLI login.
To exercise the official MCP SDK too, set
`TEST_MCP_PYTHON=/path/to/sdk-venv/bin/python` when running `test_features.py`.

## Ground rules

- Godfather uses Bash and Python's standard library, with no required Python
  packages. Keep it that way: no new dependencies.
- The scripts support Python 3.8+ and macOS's bundled Bash 3.2.
- Add a regression test for each fix.
- When behaviour changes, update the matching page under [docs/](docs/) and
  [ARCHITECTURE.md](ARCHITECTURE.md).
- `install.sh` copies a fixed list of `bin/` files. A new file in `bin/` must be
  added to that list.

## Report a bug

Open an issue at <https://github.com/zhast/godfather/issues> with:

- what you ran and what you expected,
- the output of `godfather doctor`,
- the relevant lines of `<name>.models.log` and `<name>.log` from your runs
  directory (default `~/.agent-runs`).

Run artifacts can contain prompts, source code, shell arguments, and
diagnostics. Remove anything private before you paste them.
