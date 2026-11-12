# Troubleshooting

[Back to the README](../README.md)

- **Command not found:** add your install prefix to PATH. A missing worker affects
  only its launcher; status/watch and other workers continue to work.
- **Python or Bash missing:** install Python 3.8+ and Bash 3.2+; on macOS newer
  Bash is available with `brew install bash`.
- **Model unavailable:** use `CODEX_MODEL=default`, select an accessible explicit
  model, or update `AGY_MODELS` / `GROK_MODEL`. Cached Codex availability can be stale.
- **Authentication unknown/failed:** use the official login flow above. Installer
  checks can time out or encounter an upstream failure. Grok's saved file alone
  does not verify authentication. No credentials are included in this project.
- **No eligible Antigravity model:** inspect the model log and dry-run route;
  wait for quota reset or choose another supported model. Lower `MIN_PCT` only
  intentionally. Failed `/usage` probes warn and proceed with unknown quota after retries.
- **No live actions/tokens:** CLI event formats may have changed or no exact
  session was discovered. Check the ordinary logs. Unknown values remain `?`.
- **Nudge refused:** wait for a session ID or quota-routing gap to end. Only
  registered, verifiably owned processes can be stopped.
- **Install conflict:** choose a fresh PREFIX or explicitly back up/remove the
  conflicting file yourself. Uninstall preserves modified files.
- **Watcher quiet:** it emits state changes and alerts, not a continuous progress
  stream. Use `agents-status` for the current snapshot.
