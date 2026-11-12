# Installation

[Back to the README](../README.md)

## Quick install

```sh
git clone https://github.com/zhast/godfather godfather && cd godfather && ./install.sh
export PATH="$HOME/.local/bin:$PATH"
```

One line, installing from a persistent checkout:

```sh
mkdir -p "$HOME/.local/share" && git clone --depth 1 https://github.com/zhast/godfather "$HOME/.local/share/godfather" && "$HOME/.local/share/godfather/install.sh"
```

The installer copies all twenty-seven `bin/` files into `${PREFIX:-$HOME/.local/bin}`
and marks them read-only (`chmod a-w`). Commands stay executable; Python modules
stay readable. A rerun writes each replacement to a temporary file in the same directory, sets
its mode, and atomically renames it into place. Running launchers keep their old
inode, including across a reinstall. It is safe to rerun, checks dependencies, and reports
installed CLIs and login state. Unrelated or locally modified destination files
cause a clear error. It never edits shell startup files or logs you in. For MCP, it attempts to install the official Python SDK in a private `~/.local/share/godfather/mcp-venv` (honoring `XDG_DATA_HOME`); offline or unsupported installations use the standard-library server. Set `GODFATHER_MCP_SDK=0` to skip this optional installation. System Python packages are left untouched.
`--uninstall` still removes unchanged files.

```sh
PREFIX="$HOME/tools/bin" ./install.sh
PREFIX="$HOME/tools/bin" ./install.sh --uninstall
```

Uninstall removes unchanged installed files and its manifest; it preserves modified
files, run history, and CLI credentials. Use the same prefix and keep this checkout
for uninstall and updates. No symlinks are required.

## Prerequisites

macOS or Linux, **Python 3.8+**, Bash **3.2+**, and ordinary Unix utilities (`ps`,
`awk`, `mktemp`). The scripts work with macOS's bundled Bash 3.2; Bash 4 is not
required. For a newer Bash on macOS, use `brew install bash`. `lsof` is optional
for finding working directories of unregistered CLI processes. Windows needs a
Linux environment such as WSL.

Install and authenticate whichever workers you want. The router skips unavailable workers;
each low-level launcher also works independently.
Status and watch require only Python and `ps`, even if all four CLIs are absent.

| Worker | Official installation and login | After installation |
| --- | --- | --- |
| Codex | [OpenAI CLI guide](https://learn.chatgpt.com/docs/codex/cli), [authentication](https://learn.chatgpt.com/docs/codex/auth) | Run `codex login`; check `codex login status`. The ChatGPT.app bundled binary is also supported. |
| Antigravity | [Google installation and authentication](https://www.antigravity.google/docs/cli/install/) | Run `agy` and complete the sign-in flow. Check quota with `agy -p /usage` outside a project. |
| Grok Build | [xAI getting started and sign-in](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/01-getting-started.md) | Run `grok` or `grok login` and complete browser authentication. |

The installer does not launch Codex, Antigravity, or Grok. It prints a hint to
run `godfather doctor`. That command reads credential and state files
(`~/.codex/auth.json`, `~/.gemini/oauth_creds.json` or `google_accounts.json`,
`~/.gemini/antigravity-cli/settings.json`, `~/.grok/auth.json`, and
`GEMINI_API_KEY` / `XAI_API_KEY`, plus Cursor auth-file presence or `CURSOR_API_KEY`) without starting a model CLI. `godfather doctor --live`
is the only doctor mode that runs `codex login status`, `agy -p /usage`, and Cursor `status`/`about`.
Grok has no headless login-status command, so it stays a file check even with
`--live`. Reports describe a saved file or configured credential, **not remotely
verified validity**. Credential values are never printed.
