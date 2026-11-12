"""Global ranked routing and persistent quota state. Python standard library only."""
import argparse
import contextlib
import datetime as dt
import copy
import email.utils
import fcntl
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.parse import quote

import agent_oversight as a
import godfather_safety as safety

DEFAULTS = (
    ('agy', 'gemini-4-argon-high', 'high'),
    ('agy', 'claude-opus-5-5-high', 'high'),
    ('codex', 'gpt-6.1-sol', 'high'),
    ('grok', 'grok-4.7-build-fast', 'high'),
    ('agy', 'gemini-3.8-flash-high', 'high'),
    ('agy', 'claude-sonnet-5-5-high', 'high'),
    ('agy', 'gpt-oss-120b-medium', 'medium'),
)
DEFAULT_TOML = '# Highest priority first. Move entire [[models]] blocks to reorder.\n\n' + '\n'.join(
    '[[models]]\nbackend = "{}"\nmodel = "{}"\neffort = "{}"\npricing = "subscription"\n'.format(*m) for m in DEFAULTS)
from godfather_errors import REGEX
AUTH = REGEX['auth']


def config_path():
    return Path(os.getenv('XDG_CONFIG_HOME', str(Path.home() / '.config'))).expanduser() / 'godfather/models.toml'


def state_path():
    return Path(os.getenv('XDG_STATE_HOME', str(Path.home() / '.local/state'))).expanduser() / 'godfather/quota.json'


def parse_models(text):
    # A deliberately small TOML schema also works on Python 3.8 without packages.
    # Accept single/double quoted strings, numbers, comments and [[models]] tables.
    models = []
    skipping_effort = False
    for number, raw in enumerate(text.splitlines(), 1):
        line = re.sub(r'''("(?:[^"\\]|\\.)*"|'[^']*')|(#.*)''',
                      lambda m: m[1] or '', raw).strip()
        if not line:
            continue
        if line == '[[effort]]':
            skipping_effort = True
            continue
        if line == '[[models]]':
            skipping_effort = False
            models.append({})
            continue
        if skipping_effort:
            continue
        match = re.fullmatch(r'(backend|model|effort|home|label|protocol|min_pct|pricing|input_per_million|cached_per_million|output_per_million|fast|allow_no_zdr|included_only|included_floor_cents)\s*=\s*(.+)', line)
        if not models or not match or match[1] in models[-1]:
            raise ValueError('models.toml line {}: expected [[models]] or a unique backend/model/effort/min_pct field'.format(number))
        key, value = match.groups()
        if value.startswith("'") and value.endswith("'"):
            value = value[1:-1]
        else:
            value = json.loads(value)
        models[-1][key] = value
    if not models:
        raise ValueError('models.toml must contain at least one [[models]] block')
    seen = set()
    for m in models:
        if 'label' in m and (not isinstance(m['label'], str) or not m['label'].strip()):
            raise ValueError('label must be a nonempty string')
        if m.get('backend') not in ('agy', 'codex', 'grok', 'cursor'):
            raise ValueError('backend must be agy, codex, grok or cursor')
        if m.get('protocol', 'exec') not in ('exec', 'app-server') or ('protocol' in m and m['backend'] != 'codex'):
            raise ValueError('protocol must be exec or app-server and is supported only for codex')
        if 'home' in m:
            if m['backend'] not in ('codex', 'grok', 'cursor'):
                raise ValueError('home is supported only for the codex, grok and cursor backends; agy does not support home')
            if not isinstance(m['home'], str) or not m['home'].strip():
                raise ValueError('home must be a nonempty backend home path string')
            m['home'] = str(a.backend_home(m['backend'], {m['backend'] + '_home': m['home']}))
        for key in ('model', 'effort'):
            if not isinstance(m.get(key), str) or not re.fullmatch(r'[A-Za-z0-9_.:/-]+', m[key]):
                raise ValueError('each model needs a valid {} string'.format(key))
        for flag in ('fast', 'allow_no_zdr', 'included_only'):
            if flag in m and not isinstance(m[flag], bool):
                raise ValueError(flag + ' must be a boolean')
        floor = m.get('included_floor_cents', 500)
        if isinstance(floor, bool) or not isinstance(floor, int) or floor < 0:
            raise ValueError('included_floor_cents must be a nonnegative integer')
        pct = m.get('min_pct', 0)
        if isinstance(pct, bool) or not isinstance(pct, (int, float)) or not math.isfinite(pct) or not 0 <= pct <= 100:
            raise ValueError('min_pct must be a percentage from 0 to 100')
        if 'pricing' in m and m['pricing'] not in ('subscription', ''):
            raise ValueError('pricing must be empty or subscription; use *_per_million for USD prices')
        for key in ('input_per_million', 'cached_per_million', 'output_per_million'):
            if key in m and (isinstance(m[key], bool) or not isinstance(m[key], (int, float)) or not math.isfinite(m[key]) or m[key] < 0):
                raise ValueError(key + ' must be a finite nonnegative USD price')
        if model_key(m) in seen:
            raise ValueError('duplicate model: ' + model_key(m))
        seen.add(model_key(m))
    return models


def parse_effort_rows(text):
    """[[effort]] rows in models.toml: model, task_type, effort. model may be *."""
    rows, current = [], None
    for number, raw in enumerate(text.splitlines(), 1):
        line = re.sub(r'''("(?:[^"\\]|\\.)*"|'[^']*')|(#.*)''',
                      lambda m: m[1] or '', raw).strip()
        if not line:
            continue
        if line == '[[models]]':
            current = None
            continue
        if line == '[[effort]]':
            current = {}
            rows.append(current)
            continue
        if current is None:
            continue
        match = re.fullmatch(r'(model|task_type|effort)\s*=\s*(.+)', line)
        if not match or match[1] in current:
            raise ValueError('models.toml line {}: effort rows need model, task_type, and effort'.format(number))
        value = match[2]
        if value.startswith("'") and value.endswith("'"):
            value = value[1:-1]
        else:
            value = json.loads(value)
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:/*-]+', value):
            raise ValueError('models.toml line {}: invalid effort {}'.format(number, match[1]))
        current[match[1]] = value
    for row in rows:
        if not all(key in row for key in ('model', 'task_type', 'effort')):
            raise ValueError('each [[effort]] row needs model, task_type, and effort')
    return rows


class Models(list):
    """Ranked model list plus [[effort]] rows from the same file."""

    effort_rows = ()


def load_models():
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open('x') as f:
            f.write(DEFAULT_TOML)
    except FileExistsError:
        pass
    text = path.read_text()
    models = Models(parse_models(text))
    models.effort_rows = parse_effort_rows(text)
    return models


def model_key(m):
    key = m['backend'] + ':' + m['model']
    if 'home' in m:
        home = a.backend_home(m['backend'], {m['backend'] + '_home': m['home']})
        # Conventional sibling homes have short labels; full paths keep custom
        # homes with the same basename distinct. No credentials are inspected.
        label = home.name[1:] if home.parent == Path.home() and home.name.startswith('.' + m['backend']) else quote(str(home), safe='')
        key += '@' + label
    return key


@contextlib.contextmanager
def quota_state():
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                raise ValueError('expected an object')
        except FileNotFoundError:
            data = {}
        except (ValueError, OSError) as exc:
            raise ValueError('Cannot read quota state {}: {}'.format(path, exc))
        yield data
        a.write(path, data)


def cooldown(key, clock=None):
    with quota_state() as data:
        entry = data.get(key, {})
        return entry if a.epoch(entry.get('until', 0)) > (time.time() if clock is None else clock) else None


def reset_time(text, clock=None, default=True):
    """Honor explicit reset timestamps/Retry-After/durations; otherwise 5h or 1h."""
    clock = time.time() if clock is None else clock
    candidates = []
    # Restrict parsing to reset/retry phrases, not arbitrary log timestamps.
    for match in re.finditer(r'(?:reset(?:s_at|_at| time|s)?|retry[_ -]?after|try again(?: at| in)?|available(?: at| in)?)\s*[\"\s:=]*(.{1,100})', text, re.I):
        value = re.sub(r'^(?:at|in|on)\s+', '', match[1].strip(), flags=re.I)
        iso = re.match(r'(\d{4}-\d\d-\d\d[T ]\d\d:\d\d(?::\d\d(?:\.\d+)?)?(?:Z|[+-]\d\d:\d\d)?)', value)
        if iso:
            try:
                stamp = dt.datetime.fromisoformat(iso[1].replace('Z', '+00:00'))
                candidates.append(stamp.timestamp())
                continue
            except ValueError:
                pass
        numeric = re.match(r'(\d{10,13})(?:\D|$)', value)
        if numeric:
            stamp = float(numeric[1])
            candidates.append(stamp / 1000 if stamp > 1e12 else stamp)
            continue
        # HTTP Retry-After dates and the local date format used by Codex errors.
        try:
            candidates.append(email.utils.parsedate_to_datetime(value).timestamp())
            continue
        except (ValueError, TypeError, OverflowError):
            pass
        local_date = re.match(r'([A-Za-z]+ \d{1,2}(?:st|nd|rd|th)?,? \d{4}[, ]+\d{1,2}:\d\d\s*[AP]M)', value, re.I)
        if local_date:
            normalized = re.sub(r'(\d)(?:st|nd|rd|th)', r'\1', local_date[1])
            normalized = re.sub(r',\s*', ' ', normalized)
            for fmt in ('%b %d %Y %I:%M %p', '%B %d %Y %I:%M %p'):
                try:
                    candidates.append(dt.datetime.strptime(normalized, fmt).timestamp())
                    break
                except ValueError:
                    pass
            continue
        local_time = re.match(r'(\d{1,2}:\d\d(?:\s*[AP]M)?)', value, re.I)
        if local_time:
            for fmt in ('%I:%M %p', '%H:%M'):
                try:
                    parsed = dt.datetime.strptime(local_time[1], fmt)
                    today = dt.datetime.fromtimestamp(clock).replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)
                    if today.timestamp() <= clock:
                        today += dt.timedelta(days=1)
                    candidates.append(today.timestamp())
                    break
                except ValueError:
                    pass
            continue
        duration = re.match(r'(?:in\s+)?((?:\d+(?:\.\d+)?\s*(?:hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s|days?|d)\s*[, ]*?)+)(?=$|[^a-z])', value, re.I)
        if duration:
            seconds = sum(float(n) * {'h': 3600, 'm': 60, 's': 1, 'd': 86400}[unit[0].lower()]
                          for n, unit in re.findall(r'(\d+(?:\.\d+)?)\s*([a-z]+)', duration[1], re.I))
            candidates.append(clock + seconds)
            continue
        if re.search(r'retry[_ -]?after', match[0], re.I):
            seconds = re.match(r'(\d+(?:\.\d+)?)', value)
            if seconds:
                candidates.append(clock + float(seconds[1]))
    future = [stamp for stamp in candidates if stamp > clock]
    if future:
        return max(future)  # All blocking windows must have reset.
    if not default:
        return None
    return clock + (5 * 3600 if re.search(r'(?:five|5)[ _-]*(?:hour|hr|h)\b', text, re.I) else 3600)


def mark_exhausted(key, detail, clock=None):
    clock = time.time() if clock is None else clock
    until = reset_time(detail, clock)
    known = reset_time(detail, clock, default=False) is not None
    with quota_state() as data:
        old = data.get(key, {})
        if old.get('reset_known') and a.epoch(old.get('until', 0)) > clock and not known:
            until = max(clock, a.epoch(old.get('until', 0)))
            known = True
        elif known == bool(old.get('reset_known')):
            until = max(until, a.epoch(old.get('until', 0)))
        data[key] = dict(until=dt.datetime.fromtimestamp(until, dt.timezone.utc).isoformat(),
                         marked_at=dt.datetime.fromtimestamp(clock, dt.timezone.utc).isoformat(),
                         reset_known=known,
                         reason=a.compact(detail, 500))
    return data[key]


def agy_limits(usage, model, minimum=10):
    """Same tabular limits and missing-field policy as the low-level agy launcher."""
    group = ('Gemini Models' if model.startswith('gemini-') else
             'Claude and GPT models' if model.startswith(('claude-', 'gpt-')) else None)
    if group is None:
        return False, 'unsupported model family', ''
    keys = [(group, 'Weekly Limit Remaining'), (group, 'Five Hour Limit Remaining')]
    if model.startswith('gemini-4-argon-'):
        keys.append(('Gemini 4 Argon', 'Gemini 4 Argon Weekly Limit'))
    rows = {}
    for line in usage.splitlines():
        cols = line.split('\t')
        if len(cols) >= 3:
            rows[tuple(cols[:2])] = cols
    reasons, blocking = [], []
    for key in keys:
        row = rows.get(key)
        if not row:
            reasons.append(key[1] + ' missing (available)')
            continue
        value = row[2].rstrip('%')
        valid = bool(re.fullmatch(r'\d+(?:\.\d+)?', value))
        # 0 is exhausted even when min_pct is explicitly 0.
        bad = not valid or float(value) <= 0 or float(value) < minimum
        reasons.append(key[1] + '=' + row[2])
        if bad:
            blocking.append('\t'.join(row[:3]) + ('\tresets_at=' + row[3] if len(row) > 3 else ''))
    return not blocking, '; '.join(reasons) + ' (minimum {}%)'.format(minimum), '\n'.join(blocking)


def query(command, cwd=None, timeout=15, env=None):
    return subprocess.run(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, errors='replace', timeout=timeout)


def agy_usage(binary):
    if not binary:
        return ''
    from godfather_usage import save_snapshot
    for attempt in range(3):
        try:
            with tempfile.TemporaryDirectory(prefix='godfather-usage-') as temp:
                result = query([binary, '-p', '/usage'], cwd=temp, timeout=30)
            if result.returncode == 0 and result.stdout.strip():
                save_snapshot(result.stdout)
                return result.stdout
        except (OSError, subprocess.TimeoutExpired):
            pass
        if attempt < 2:
            time.sleep(2 ** attempt)
    print('WARN /usage unavailable after 3 attempts; proceeding with unknown quota.', file=sys.stderr)
    return ''


def backend_ready(backend, home=None):
    try:
        binary = a.cli_binary(backend)
        if not binary:
            return None, 'CLI missing'
        if backend == 'codex':
            result = query([binary, 'login', 'status'], env=dict(os.environ, CODEX_HOME=str(a.codex_home({'codex_home': home}))))
            if result.returncode:
                return None, 'not logged in' if AUTH.search(result.stdout + result.stderr) else 'login status unavailable'
        elif backend == 'cursor':
            from godfather_cursor import environment
            result = query([binary, 'status'], env=environment(home))
            if result.returncode or 'not logged in' in (result.stdout + result.stderr).lower():
                return None, 'not logged in'
        elif backend == 'grok':
            # There is no noninteractive login-status API. Never read/print secrets.
            saved = a.grok_home({'grok_home': home}) / 'auth.json'
            if not os.getenv('XAI_API_KEY') and not (saved.is_file() and saved.stat().st_size > 2):
                return None, 'no saved login or XAI_API_KEY'
        return binary, 'logged in' if backend == 'codex' else 'installed'
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, 'preflight unavailable ({})'.format(type(exc).__name__)


def tail(path, lines=40):
    try:
        with Path(path).open('rb') as f:
            f.seek(max(0, Path(path).stat().st_size - 64000))
            return '\n'.join(f.read().decode(errors='replace').splitlines()[-lines:])
    except OSError:
        return ''


NOTES_HANDOFF_BYTES = 24000


def handoff(original, cwd, previous_base, run):
    notes_path = safety.notes_path(previous_base, run)
    try:
        with notes_path.open('rb') as source:
            source.seek(0, os.SEEK_END)
            size = source.tell()
            source.seek(max(0, size - NOTES_HANDOFF_BYTES))
            notes = source.read(NOTES_HANDOFF_BYTES).decode('utf-8', errors='replace').strip()
    except OSError:
        notes, size = '', 0
    notes_section = ('\n## Previous agent\'s notes\n' +
                     ('[Truncated; newest notes retained]\n' if size > NOTES_HANDOFF_BYTES else '') +
                     notes + '\n') if notes else ''
    result = query(['git', '-C', str(cwd), 'rev-parse', '--is-inside-work-tree']) if shutil.which('git') else None
    if result and result.returncode == 0 and result.stdout.strip() == 'true':
        branch = git_output(cwd, 'symbolic-ref', '--short', '-q', 'HEAD').strip() or '(detached HEAD)'
        merge_head = git_output(cwd, 'rev-parse', '--verify', 'MERGE_HEAD').strip()
        parts = ['Branch: ' + branch, 'HEAD: ' + git_output(cwd, 'rev-parse', 'HEAD').strip(),
                 'Merge in progress: ' + (merge_head or 'no')]
        for label, flags in [('git status --short', ['status', '--short']),
                             ('git diff --stat', ['diff', '--stat']),
                             ('git diff --cached --stat', ['diff', '--cached', '--stat'])]:
            result = query(['git', '-C', str(cwd)] + flags)
            parts.append(label + ':\n' + result.stdout[:24000])
        progress = '\n'.join(parts)
    else:
        recent = []
        for directory, dirs, files in os.walk(str(cwd)):
            dirs[:] = [d for d in dirs if d not in ('.git', 'node_modules', '.venv', '__pycache__') and not Path(directory, d).is_symlink()]
            for name in files:
                path = Path(directory, name)
                try:
                    recent.append((path.stat().st_mtime, str(path.relative_to(cwd))))
                except OSError:
                    pass
        progress = 'Recently modified files (newest first):\n' + '\n'.join(
            '{} {}'.format(dt.datetime.fromtimestamp(stamp).isoformat(), name)
            for stamp, name in sorted(recent, reverse=True)[:40])
    try:
        messages = Path(str(previous_base) + '.last.md').read_text(errors='replace').strip()
    except OSError:
        messages = ''
    has_report = bool(messages)
    if not messages and run.get('cli') == 'grok':
        # Grok retains partial text in its result even when an error prevents
        # publishing a successful .last.md reply.
        result = a.read(str(previous_base) + '.json')
        messages = '\n'.join(str(result.get('text', '')).splitlines()[-40:])
    if not messages:
        # Prefer exact-session assistant messages over raw CLI diagnostics.
        transcript_messages = []
        for path in run.get('progress_paths', []):
            if (not path.endswith('.jsonl') and not run.get('codex_stream')) or path.endswith('.stream.jsonl'):
                continue
            for line in tail(path, 300).splitlines():
                try:
                    event = json.loads(line)
                    p = event.get('payload', {})
                    if event.get('type') == 'PLANNER_RESPONSE':
                        transcript_messages.append(event.get('content', ''))
                    elif p.get('type') == 'message' and p.get('role') == 'assistant':
                        transcript_messages.extend(c.get('text', '') for c in p.get('content', []))
                    elif event.get('type') == 'item.completed' and event.get('item', {}).get('type') == 'agent_message':
                        transcript_messages.append(event['item'].get('text', ''))
                except (ValueError, AttributeError):
                    pass
        messages = '\n'.join('\n'.join(transcript_messages).splitlines()[-40:]) or tail(str(previous_base) + '.log')
    profile = a.read(run['permission_profile']) if run.get('permission_profile') else {}
    access = ('Writable roots: ' + json.dumps(profile.get('writable_roots', [])) +
              '\nRead-only process inspection allowed: ' + str(profile.get('process_inspection', True)) +
              ('\nUse ps -p <pid> -o pid,comm and pgrep -f godfather for inspection. ' if
               profile.get('process_inspection', True) else '\nProcess inspection requires Godfather approval. ') +
              'Ask Godfather before signaling any processes; never bypass a policy denial.\n')
    return ('Continue the task rather than restart. Verify the existing work first, preserve valid changes, '
            'and finish the remaining work and checks. The previous CLI could not continue; its session cannot move here.\n\n'
            + safety.notes_preamble(notes_path) +
            '## Original task\n' + original + '\n\n## Workspace progress\n' + progress +
            '\n\n## Access and inspection rules\n' + access +
            notes_section +
            ('\n## Last report from previous run\n' if has_report else
             '\n## Last messages from previous run (up to 40 lines)\n') + messages + '\n')


def quota_main(args):
    models = load_models()
    now_ts = time.time()
    if args.reset:
        with quota_state() as data:
            keys = [k for k in data if k == args.reset or k.split(':', 1)[-1] == args.reset or
                    k.split(':', 1)[-1].split('@', 1)[0] == args.reset]
            for key in keys:
                del data[key]
            print('Cleared ' + (', '.join(keys) or args.reset + ' (no cooldown)'))
        from godfather_supervisor import _probe_cache
        path, cache = _probe_cache(now_ts)
        for m in models:
            key = model_key(m)
            if key == args.reset or key in keys or m['model'] == args.reset:
                cli = m['backend']
                home = a.backend_home(cli, {cli + '_home': m.get('home')})
                probe_key = cli + ('@' + str(home) if cli in ('codex', 'grok') and home != Path.home() / ('.' + cli) else '')
                cache.pop(probe_key, None)
        if path.exists():
            a.write(path, cache)
        return 0
    with quota_state() as data:
        persisted = dict(data)
    if getattr(args, 'all', False):
        shown = {key: dict(value, active=a.epoch(value.get('until', 0)) > now_ts) for key, value in persisted.items()}
        print(json.dumps(shown, indent=2))
        return 0
    from godfather_usage import quota_report
    report, rows = quota_report(models, persisted, live=getattr(args, 'live', False), now=now_ts)
    if getattr(args, 'json', False):
        print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        for row in rows:
            print(row)
    return 0


def _inside_git(cwd):
    try:
        repo = query(['git', '-C', str(cwd), 'rev-parse', '--show-toplevel'])
    except (OSError, subprocess.TimeoutExpired):
        return False
    return repo.returncode == 0


def _linked_worktree(cwd, root=None):
    """True when cwd is inside a caller-created linked git worktree, not the main checkout."""
    target = Path(cwd).resolve()
    pool = (Path(root) if root is not None else a.runs_dir()).resolve() / 'worktrees'
    if safety.inside(target, pool):
        return False
    try:
        result = query(['git', '-C', str(target), 'rev-parse', '--path-format=absolute', '--git-common-dir', '--git-dir'])
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode:
        return False
    dirs = [str((target / line).resolve()) for line in result.stdout.splitlines() if line.strip()]
    return len(dirs) >= 2 and dirs[0] != dirs[1]


def worktree_note(cwd, origin_cwd=None):
    source = Path(origin_cwd).resolve() if origin_cwd else Path(os.getenv('CWD', str(cwd))).resolve()
    return 'Your working tree is {}; paths to {} in the brief refer to it; do not write elsewhere.'.format(
        Path(cwd).resolve(), source)


def explicit_env_include(path, patterns):
    """Broad globs cannot opt secrets in; require an exact, positive path."""
    return not any(part.startswith('.env') for part in Path(path).parts) or any(
        line.lstrip('/') == path and not line.startswith(('!', '#')) and
        not any(char in line for char in '*?[\\') for line in patterns)


def copy_worktree_includes(cwd, worktree, models_log):
    # Git lists the main checkout first, even when cwd is a linked worktree.
    result = query(['git', '-C', str(cwd), 'worktree', 'list', '--porcelain', '-z'])
    if result.returncode:
        raise ValueError('Cannot locate main checkout: ' + result.stderr.strip())
    source = Path(result.stdout.split('\0', 1)[0][len('worktree '):])
    include = source / '.worktreeinclude'
    if not include.is_file():
        return
    patterns = include.read_text().splitlines()
    # Use git's own ignore-pattern parser as an inclusion list. --others
    # excludes tracked files, including dirty ones, without standard ignores.
    result = query(['git', '-C', str(source), 'ls-files', '--others', '--ignored',
                    '--exclude-from=.worktreeinclude', '-z'], timeout=None)
    if result.returncode:
        raise ValueError('Cannot list .worktreeinclude files: ' + result.stderr.strip())
    for name in filter(None, result.stdout.split('\0')):
        if not explicit_env_include(name, patterns):
            continue
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts or '.git' in relative.parts:
            continue
        src, dest = source / relative, worktree / relative
        # Do not follow source/destination symlinks outside either checkout,
        # or overwrite files already materialized from the target commit.
        if (any((source / parent).is_symlink() for parent in (relative, *relative.parents))
                or not src.is_file() or dest.exists() or dest.is_symlink()
                or not safety.inside(dest.resolve(), worktree.resolve())):
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        with src.open('rb') as incoming, dest.open('xb') as outgoing:
            shutil.copyfileobj(incoming, outgoing)
        shutil.copystat(src, dest)
        with Path(models_log).open('a') as log:
            log.write('[{}] WORKTREE_INCLUDE {}\n'.format(a.now(), json.dumps(name)))


def create_worktree(cwd, worktree, branch, base_sha='HEAD', models_log=None, resources=None):
    with safety.git_lock(cwd):
        result = _create_worktree(cwd, worktree, branch, base_sha, models_log)
        safety.add_resource(resources, 'worktree', worktree, True)
        safety.add_resource(resources, 'branch', branch, True)
        return result


def _create_worktree(cwd, worktree, branch, base_sha, models_log):
    """Stream checkout progress without a small-repository deadline; undo our failed attempt."""
    worktree = Path(worktree)
    if worktree.exists() or query(['git', '-C', str(cwd), 'show-ref', '--verify', '--quiet', 'refs/heads/' + branch]).returncode == 0:
        raise ValueError('Cannot create worktree: refusing existing path or branch: ' + branch)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(cwd),
                                 'worktree', 'add', '-b', branch, str(worktree), base_sha],
                                env=safety.original_environment(), stdin=subprocess.DEVNULL)
        if result.returncode:
            raise ValueError('Cannot create worktree: git exited ' + str(result.returncode))
        copy_worktree_includes(cwd, worktree, models_log or a.runs_dir() / (worktree.name + '.models.log'))
    except BaseException:
        # These names did not exist before this attempt. Remove locked/partial registrations too.
        query(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(cwd),
               'worktree', 'remove', '--force', '--force', str(worktree)], timeout=None)
        if worktree.exists():
            shutil.rmtree(worktree)
        query(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(cwd), 'worktree', 'prune'], timeout=None)
        query(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(cwd), 'branch', '-D', branch], timeout=None)
        raise
    return worktree, branch


def check_devbox_isolation(cwd, explicit=True):
    box = safety.devbox_tool()
    env = safety.original_environment()
    binary = box and shutil.which(box['binary'], path=env['PATH'])
    if not binary:
        return
    refusal = 'Cannot determine devbox attachment; refusing isolation. Use --no-isolate to keep CWD.'
    try:
        result = subprocess.run([binary, 'status', '--json'], cwd=cwd, env=env,
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(refusal) from exc
    if result.returncode and box['detached_error'] and result.stderr.strip() == box['detached_error']:
        return
    try:
        status = json.loads(result.stdout)
    except ValueError as exc:
        raise ValueError(refusal) from exc
    if isinstance(status, dict) and status.get('attached') is True:
        if explicit:
            raise ValueError('CWD has an attached devbox; refusing isolation into a second worktree. '
                             'Use --no-isolate to keep the existing devbox binding.')
        return status
    if result.returncode or not isinstance(status, dict) or status.get('attached') is not False:
        raise ValueError(refusal)


def provision_run(cwd, name, root, explicit=True, resources=None):
    """Give one run its own directory. Git checkouts get a worktree; other directories get a scratch copy."""
    root = Path(root)
    if _inside_git(cwd):
        attached = check_devbox_isolation(cwd, explicit=explicit)
        if attached:
            safety.adopt_workspace(cwd, resources, attached)
            print('ADOPT {} CWD has an attached devbox; running in place with the CWD lock.'.format(name), flush=True)
            return Path(cwd).resolve(), None
        if _linked_worktree(cwd, root):
            safety.adopt_workspace(cwd, resources)
            return Path(cwd).resolve(), None
        branch = 'godfather/' + name
        worktree = root / 'worktrees' / name
        result = create_worktree(cwd, worktree, branch)
        safety.add_resource(resources, 'worktree', worktree, True)
        safety.add_resource(resources, 'branch', branch, True)
        return result
    scratch = root / 'scratch' / name
    safety.copy_scratch(cwd, scratch)
    safety.add_resource(resources, 'scratch', scratch, True)
    return scratch, None


def route(args):
    if not os.getenv('CWD') or not Path(os.environ['CWD']).is_dir():
        raise ValueError('CWD must name an existing task directory')
    cwd = Path(os.environ['CWD']).resolve()
    origin_cwd = cwd
    args.origin_cwd = origin_cwd
    args.self = args.self or os.getenv('GODFATHER_SELF') == '1'
    safety.protect_cwd(cwd, args.self)
    if args.max_fix_rounds < 0:
        raise ValueError('--max-fix-rounds must be nonnegative')
    if not args.name or '/' in args.name or args.name in ('.', '..'):
        raise ValueError('name must be a filename without slashes')
    task_type = getattr(args, 'task_type', None) or os.getenv('GODFATHER_TASK_TYPE') or 'edit'
    if not isinstance(task_type, str) or not re.fullmatch(r'[A-Za-z0-9_.:/-]+', task_type):
        raise ValueError('--task-type must be a token')
    args.task_type = task_type
    if getattr(args, 'review_rounds', 0) < 0:
        raise ValueError('--review-rounds must be nonnegative')
    if getattr(args, 'review_rounds', 0) and not args.review_by:
        raise ValueError('--review-rounds requires --review-by')
    if args.review_fix and not args.review_by:
        raise ValueError('--review-fix requires --review-by')
    if not hasattr(args, 'isolate'):
        args.isolate = True
    root = a.runs_dir().resolve()
    root.mkdir(parents=True, exist_ok=True)
    branch = None
    scratch = None
    args.resources = []
    args.resource_context = dict(repo=str(origin_cwd),
        base_branch=git_output(cwd, 'symbolic-ref', '--short', '-q', 'HEAD').strip(),
        base_sha=git_output(cwd, 'rev-parse', 'HEAD').strip()) if _inside_git(cwd) else dict(repo=str(origin_cwd))
    args._run_started = False
    try:
        if args.isolate:
            cwd, branch = provision_run(cwd, args.name, root,
                explicit=getattr(args, 'isolate_explicit', False), resources=args.resources)
            if branch:
                print('ISOLATE {} branch={} directory={}'.format(args.name, branch, cwd), flush=True)
            elif cwd != origin_cwd:
                scratch = cwd
                print('ISOLATE {} scratch directory={}'.format(args.name, cwd), flush=True)
            else:
                args.isolate = False
        else:
            safety.adopt_workspace(cwd, args.resources)
        args.run_cwd, args.branch = cwd, branch
        # Isolated runs each have a private directory, so they do not take the shared CWD lock.
        if args.isolate:
            return _route(args)
        with safety.cwd_lock(cwd, wait=getattr(args, 'wait_lock', False)):
            return _route(args)
    finally:
        # Until a launcher starts, a scratch copy belongs to setup. Preserve
        # agent work from all launched runs, including unsuccessful ones.
        if scratch is not None and not args._run_started:
            shutil.rmtree(scratch)
        if branch:
            print('BRANCH {} {} (never merged)'.format(args.name, branch), flush=True)


def verify(args, base):
    for round_number in range(args.max_fix_rounds + 1):
        run = a.read(str(base) + '.run.json')
        run['status'] = 'checking'
        a.write(str(base) + '.run.json', run)
        safety.audit(base, 'check', args.check)
        check_log = Path(str(base) + '.check.log')
        # File-backed output keeps verbose check failures out of memory.
        cancelled = False
        with check_log.open('w') as output:
            result = subprocess.Popen(args.check, shell=True, cwd=args.run_cwd,
                                      stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            def stop_check(sig, frame):
                nonlocal cancelled
                cancelled = True
                try:
                    os.killpg(result.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            handlers = {sig: signal.signal(sig, stop_check) for sig in (signal.SIGTERM, signal.SIGINT)}
            try:
                while True:
                    try:
                        result.wait(timeout=.2)
                        break
                    except subprocess.TimeoutExpired:
                        if cancelled:
                            try:
                                result.wait(timeout=8)
                            except subprocess.TimeoutExpired:
                                os.killpg(result.pid, signal.SIGKILL)
                                result.wait()
                            break
            finally:
                for sig, handler in handlers.items():
                    signal.signal(sig, handler)
        if cancelled:
            run.update(status='killed', exit_code=143, ended_at=a.now())
            a.write(str(base) + '.run.json', run)
            safety.event(base, 'CHECK', 'cancelled')
            print('CHECK {} cancelled'.format(base.name), flush=True)
            return 143
        passed = result.returncode == 0
        detail = '{} (round {})'.format('pass' if passed else 'fail', round_number)
        safety.event(base, 'CHECK', detail)
        print('CHECK {} {}'.format(base.name, detail), flush=True)
        run.update(status='done' if passed else 'check_failed' if round_number == args.max_fix_rounds else 'checking',
                   exit_code=0 if passed else 1 if round_number == args.max_fix_rounds else None, ended_at=a.now())
        a.write(str(base) + '.run.json', run)
        if passed:
            return 0
        if round_number == args.max_fix_rounds:
            return 1
        message = 'The acceptance check failed: ' + args.check + '\nFix the failure and verify your changes. Last 80 output lines:\n' + tail(check_log, 80)
        # nudge uses the exact stored session/settings. Keep the outer CWD lock.
        prior = os.environ.get('GODFATHER_LOCK_HELD')
        os.environ['GODFATHER_LOCK_HELD'] = str(args.run_cwd)
        try:
            try:
                rc = a.nudge_main([base.name, message], run_checks=False)
            except SystemExit as exc:
                rc = exc.code or 1
        finally:
            if prior is None:
                os.environ.pop('GODFATHER_LOCK_HELD', None)
            else:
                os.environ['GODFATHER_LOCK_HELD'] = prior
        if rc:
            failed = a.read(str(base) + '.run.json')
            failed.update(status='check_failed', exit_code=rc, ended_at=a.now())
            a.write(str(base) + '.run.json', failed)
            return rc
        current = a.read(str(base) + '.run.json')
        if current.get('status') == 'needs_input':
            return 0


def _route(args):
    if not os.getenv('CWD') or not Path(os.environ['CWD']).is_dir():
        raise ValueError('CWD must name an existing task directory')
    if not args.name or '/' in args.name or args.name in ('.', '..') or '\0' in args.name:
        raise ValueError('name must be a filename without slashes')
    if not getattr(args, 'task_type', None):
        args.task_type = os.getenv('GODFATHER_TASK_TYPE') or 'edit'
    cwd = args.run_cwd
    if not hasattr(args, 'resources'):
        args.resources = []
        safety.adopt_workspace(cwd, args.resources)
    origin_cwd = getattr(args, 'origin_cwd', None) or Path(os.environ['CWD']).resolve()
    original = args.prompt if args.prompt is not None else Path(args.prompt_file).read_text()
    models = load_models()
    if getattr(args, 'review_models', None) is not None:
        models = args.review_models
    root = a.runs_dir().resolve()
    root.mkdir(parents=True, exist_ok=True)
    base = root / args.name
    with open(str(base) + '.router.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Run name already active: ' + args.name)
        old = a.read(str(base) + '.run.json')
        if old.get('status') == 'waiting_quota':
            raise ValueError('Run name already active: ' + args.name + ' (waiting for quota; stop it first)')
        if old.get('status') == 'running' and (a.run_alive(old) or a.alive(old.get('launcher_pid'), old.get('launcher_identity'))):
            raise ValueError('Run name already active: ' + args.name)
        route_id = (os.getenv('GODFATHER_ROUTE_ID') if not hasattr(args, 'review_models') else None) or str(uuid.uuid4())
        route_file = str(base) + '.route.json'
        route_state = dict(run_id=route_id, name=args.name, active_name=None, status='running', switches=[], started_at=a.now())
        route_state.update(getattr(args, 'resource_context', {}))
        route_state.update(resources=args.resources, cwd=str(cwd))
        route_state.update(router_pid=os.getpid(), router_identity=a.processes().get(os.getpid(), {}).get('identity'))
        a.write(route_file, route_state)
        log_path = str(base) + '.models.log'
        # Provisioning precedes routing (including plan workers); retain its
        # copied-path audit entries when resetting the model routing log.
        prior = Path(log_path).read_text() if Path(log_path).exists() else ''
        Path(log_path).write_text(''.join(line + '\n' for line in prior.splitlines()
                                        if '] WORKTREE_INCLUDE ' in line))
        def log(message):
            with open(log_path, 'a') as f:
                f.write('[{}] {}\n'.format(a.now(), message))
        prompt_file = Path(str(base) + '.prompt.md')
        prompt_file.write_text(original)
        if args.review_by:
            args.review_head = git_output(cwd, 'rev-parse', 'HEAD').strip() or 'HEAD'
            args.review_before = git_output(cwd, 'diff', '--no-ext-diff', args.review_head, '--', '.', ':(exclude)**/.env*', ':(exclude).env*')
            Path(str(base) + '.before.diff').write_text(args.review_before)
        log('ROUTE config=' + str(config_path()))
        previous = None
        quota_unavailable = False
        quota_candidates = []
        segment = 0
        ready = {}
        auth_retries_used = {}
        last_rc = 1
        child = None
        stopped = False
        def stop(sig, frame):
            nonlocal stopped
            stopped = True
            if child and child.poll() is None:
                # Bash defers traps while waiting for its supervisor. Signal that
                # owned supervisor too so it stops the model process group now.
                active = route_state.get('active_name')
                run = a.read(str(root / active) + '.run.json') if active else {}
                supervisor = run.get('supervisor_pid')
                procs = a.processes()
                if (run.get('launcher_pid') == child.pid and supervisor and
                        procs.get(supervisor, {}).get('ppid') == child.pid):
                    try:
                        os.kill(supervisor, sig)
                    except ProcessLookupError:
                        pass
                child.send_signal(sig)
        handlers = {s: signal.signal(s, stop) for s in (signal.SIGINT, signal.SIGTERM)}
        import godfather_supervisor as supervisor
        try:
            for m in models:
                if stopped:
                    return 143
                key = model_key(m)
                entry = cooldown(key)
                if entry:
                    quota_unavailable = True
                    log('SKIP {} cooldown until={} ({})'.format(key, entry['until'], entry['reason']))
                    continue
                backend = m['backend']
                home = str(a.backend_home(backend, {backend + '_home': m.get('home')})) if backend in ('codex', 'grok', 'cursor') else None
                account = (backend, home)
                if account not in ready:
                    ready[account] = backend_ready(backend, home)
                    binary, preflight_reason = ready[account]
                    if not binary and AUTH.search(preflight_reason):
                        attempts, backoff = supervisor.auth_retry_budget()
                        for retry in range(attempts):
                            auth_retries_used[account] = retry + 1
                            safety.event(base, 'AUTH_RETRY', key + ' preflight')
                            deadline = time.monotonic() + backoff
                            while time.monotonic() < deadline and not stopped:
                                time.sleep(min(.2, max(0, deadline - time.monotonic())))
                            if stopped:
                                return 143
                            ready[account] = backend_ready(backend, home)
                            if ready[account][0]:
                                safety.event(base, 'HEALED', 'auth retry (' + key + ' preflight)')
                                break
                        if auth_retries_used.get(account) and not ready[account][0]:
                            safety.event(base, 'ESCALATE', 'auth retry failed (' + key + ' preflight)')
                binary, reason = ready[account]
                if not binary:
                    log('SKIP {} {}'.format(key, reason))
                    continue
                resolved_model = m['model']
                if backend == 'cursor':
                    from godfather_cursor import resolve, usage as cursor_usage, included
                    try:
                        resolved_model = resolve(m, binary)
                    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
                        log('SKIP {} {}'.format(key, exc))
                        continue
                    if m.get('included_only') and not included(m, cursor_usage(m.get('home'))):
                        quota_unavailable = True
                        log('SKIP {} included allowance exhausted or below floor'.format(key))
                        continue
                if backend == 'agy':
                    try:
                        usage = agy_usage(binary)
                    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
                        log('SKIP {} {}'.format(key, exc))
                        continue
                    if not usage:
                        log('WARN {} /usage unavailable after 3 attempts; proceeding with unknown quota'.format(key))
                    eligible, reason, blocked = agy_limits(usage, m['model'], m.get('min_pct', 0))
                    if not eligible:
                        log('SKIP {} /usage: {}'.format(key, reason))
                        # A low-but-positive threshold is ineligible, not exhausted.
                        exhausted = [line for line in blocked.splitlines() if re.search(r'\t0(?:\.0+)?%(?:\t|$)', line)]
                        if exhausted:
                            quota_unavailable = True
                            entry = mark_exhausted(key, '\n'.join(exhausted))
                            log('COOLDOWN {} until={}'.format(key, entry['until']))
                        continue
                else:
                    probe_args = {'home': home} if home is not None else {}
                    probe = supervisor.quota_preflight(backend, binary, m.get('min_pct', 0), key, **probe_args)
                    if probe['status'] == 'cooldown':
                        quota_unavailable = True
                        entry = mark_exhausted(key, probe.get('detail') or 'quota probe')
                        log('SKIP {} {}'.format(key, probe.get('detail') or 'quota'))
                        log('COOLDOWN {} until={}'.format(key, entry['until']))
                        continue
                    if probe['status'] == 'ineligible':
                        log('SKIP {} {}'.format(key, probe.get('detail') or 'below minimum'))
                        continue
                    reason = probe.get('detail') or 'cooldown clear; headless remaining-quota query unavailable'
                segment += 1
                name = args.name if segment == 1 else args.name + '#' + str(segment)
                segment_base = root / name
                env = dict(os.environ, CWD=str(cwd), AGENT_RUNS_DIR=str(root), AGENT_ROUTED='1',
                           GODFATHER_PROJECT_CWD=os.getenv('GODFATHER_PROJECT_CWD') or str(origin_cwd),
                           AGENT_ROUTER_NAME=args.name, AGENT_ROUTER_ID=route_id,
                           GODFATHER_NATIVE='1', GODFATHER_TASK_TYPE=args.task_type,
                           GODFATHER_CONTAINER='1' if getattr(args, 'container', False) else '',
                           GODFATHER_LOCK_HELD=str(cwd), GODFATHER_SELF='1' if args.self else '0',
                           GODFATHER_CHECK=args.check or '', GODFATHER_BRANCH=args.branch or '',
                           GODFATHER_MAX_FIX_ROUNDS=str(args.max_fix_rounds), GODFATHER_CONTEXT=str(base) + '.safety.json')
                resources = getattr(args, 'resources', [])
                if previous:
                    resources = a.read(str(previous['base']) + '.run.json').get('resources', resources)
                env['GODFATHER_RESOURCES'] = json.dumps(resources)
                env['GODFATHER_RESOURCE_CONTEXT'] = json.dumps(getattr(args, 'resource_context', {}))
                env['GODFATHER_MODEL_KEY'] = key
                if home is not None:
                    env[backend.upper() + '_HOME'] = home
                env['GODFATHER_QUOTA_FALLBACK'] = '1' if previous and previous.get('quota') else '0'
                env.update(GODFATHER_REVIEW_BY=args.review_by or '', GODFATHER_REVIEW_FIX='1' if args.review_fix else '0',
                           GODFATHER_REVIEW_ROUNDS=str(getattr(args, 'review_rounds', 0)),
                           GODFATHER_REVIEW_ROUND=str(getattr(args, 'review_round', 0)),
                           GODFATHER_REVIEW_THRESHOLD=getattr(args, 'review_threshold', 'high'),
                           GODFATHER_REVIEW_HEAD=getattr(args, 'review_head', ''), GODFATHER_REVIEW_TASK=str(prompt_file),
                           GODFATHER_REVIEW_PARENT=str(getattr(args, 'review_parent', '')),
                           GODFATHER_REVIEW_SEGMENT=str(getattr(args, 'review_segment', '')))
                # A caller's low-level resume/model overrides cannot defeat the router.
                for variable in ('AGENT_RESUME_ID', 'AGENT_NUDGE', 'AGENT_RETRY', 'DRY_RUN'):
                    env.pop(variable, None)
                task = prompt_file
                if previous:
                    if backend == 'agy' and previous['model']['backend'] == 'agy' and previous.get('quota'):
                        session = previous['run'].get('conversation_id')
                        if not session:
                            log('STOP cannot resume agy: previous conversation ID unknown')
                            return last_rc
                        env['AGENT_RESUME_ID'] = session
                        env['AGENT_RETRY'] = '1'
                        task = Path(str(segment_base) + '.resume.md')
                        task.write_text(supervisor.interruption_prompt(previous['run'], 'quota', previous.get('reason')) + original)
                        log('RESUME {} conversation={}'.format(key, session))
                    else:
                        task = Path(str(segment_base) + '.handoff.md')
                        handoff_body = (supervisor.interruption_prompt(previous['run'],
                                        previous['run'].get('failure_class') or 'quota', previous.get('reason')) +
                                        handoff(original, cwd, previous['base'], previous['run']))
                        if 'write_file auto-denied in headless mode' in (previous.get('reason') or ''):
                            handoff_body = worktree_note(cwd, origin_cwd) + '\n\n' + handoff_body
                        task.write_text(handoff_body)
                        log('HANDOFF {} prompt={}'.format(key, task))
                    switch = dict(name=args.name, source=model_key(previous['model']), target=key,
                                  reason=(previous.get('reason') or 'previous segment failed') + '; ' + reason, segment=name)
                    route_state['switches'].append(switch)
                    safety.event(base, 'SWITCH', switch['source'] + ' -> ' + key + ' (' + switch['reason'] + ')')
                    log('SWITCH {} {} -> {} ({})'.format(args.name, switch['source'], key, switch['reason']))
                env['GODFATHER_CODEX_PROTOCOL'] = os.getenv('GODFATHER_CODEX_PROTOCOL') or m.get('protocol', 'exec')
                effort = supervisor.resolve_effort(m['model'], args.task_type, m['effort'], getattr(models, 'effort_rows', None))
                env.update(CURSOR_ALLOW_NO_ZDR='true' if m.get('allow_no_zdr') else 'false', CURSOR_FAST='false', CURSOR_MODEL=resolved_model, CURSOR_EFFORT=effort,
                           CURSOR_MODE='ask' if getattr(args, 'review_read_only', False) else '',
                           AGY_MODELS=m['model'], AGY_EFFORT=effort, MIN_PCT=str(m.get('min_pct', 0)),
                           CODEX_MODEL=m['model'], CODEX_EFFORT=effort,
                           GROK_MODEL=m['model'], GROK_EFFORT=effort)
                log('SELECT {} segment={} effort={} ({})'.format(key, name, effort, reason))
                route_state['active_name'] = name
                a.write(route_file, route_state)
                launcher = Path(__file__).with_name(backend + '-run')
                from godfather_lifecycle import launcher_process
                with open(str(segment_base) + '.launcher.log', 'ab') as output:
                    child = launcher_process([str(launcher), str(task), name], env=env,
                                             stdout=output, stderr=output)
                args._run_started = True
                while True:
                    try:
                        last_rc = child.wait(timeout=.2)
                        break
                    except subprocess.TimeoutExpired:
                        if stopped:
                            stop(signal.SIGTERM, None)
                run = a.read(str(segment_base) + '.run.json')
                if segment > 1:
                    with open(log_path, 'a') as f:
                        f.write(tail(str(segment_base) + '.models.log', 200) + '\n')
                exit_reason = (' (' + run['failure_reason'] + ')') if run.get('failure_reason') else ''
                log('EXIT {} segment={} exit={} status={}{}'.format(key, name, last_rc, run.get('status', 'unknown'), exit_reason))
                if stopped or last_rc in (130, 137, 143, -signal.SIGINT, -signal.SIGTERM):
                    return 143 if stopped else last_rc
                if last_rc == 0:
                    if args.check and run.get('status') != 'needs_input':
                        last_rc = verify(args, segment_base)
                        run = a.read(str(segment_base) + '.run.json')
                    if last_rc == 0 and args.review_by and run.get('status') != 'needs_input':
                        return cross_review(args, segment_base, original)
                    return last_rc
                detail = failure_detail(segment_base, run)
                if 'write_file auto-denied in headless mode' in (run.get('failure_reason') or ''):
                    note = worktree_note(cwd, origin_cwd)
                    log('RETRY write_file_denied {} attempt=1/1 ({})'.format(key, run['failure_reason']))
                    safety.event(base, 'WRITE_RETRY', key + ' (' + run['failure_reason'] + ')')
                    retry_task = Path(str(segment_base) + '.write-retry-1.md')
                    retry_task.write_text(supervisor.interruption_prompt(run, 'tool_denied') + note + '\n\n' + task.read_text())
                    retry_env = dict(env, AGENT_RETRY='1')
                    session = run.get('conversation_id' if backend == 'agy' else 'session_id')
                    if session:
                        retry_env['AGENT_RESUME_ID'] = session
                    with open(str(segment_base) + '.launcher.log', 'ab') as output:
                        child = launcher_process([str(launcher), str(retry_task), name], env=retry_env,
                                                 stdout=output, stderr=output)
                    while True:
                        try:
                            last_rc = child.wait(timeout=.2)
                            break
                        except subprocess.TimeoutExpired:
                            if stopped:
                                stop(signal.SIGTERM, None)
                    run = a.read(str(segment_base) + '.run.json')
                    detail = failure_detail(segment_base, run)
                    exit_reason = (' (' + run['failure_reason'] + ')') if run.get('failure_reason') else ''
                    log('EXIT {} segment={} exit={} status={}{}'.format(key, name, last_rc, run.get('status', 'unknown'), exit_reason))
                    if stopped or last_rc in (130, 137, 143, -signal.SIGINT, -signal.SIGTERM):
                        return 143 if stopped else last_rc
                    if last_rc == 0:
                        safety.event(base, 'HEALED', 'write_file retry (' + key + ')')
                        if args.check and run.get('status') != 'needs_input':
                            last_rc = verify(args, segment_base)
                            run = a.read(str(segment_base) + '.run.json')
                        if last_rc == 0 and args.review_by and run.get('status') != 'needs_input':
                            return cross_review(args, segment_base, original)
                        return last_rc
                capacity_used = 0
                recovery_class = run.get('failure_class')
                capacity_rule = next((row for row in supervisor.load_policies()['retry'] if row['class_'] == recovery_class),
                                     dict(attempts=0, action='fallback', backoff_secs=0))
                while (recovery_class in ('capacity', 'crash', 'broken_stream') and run.get('failure_class') == recovery_class and capacity_rule['action'] != 'fallback'
                       and capacity_rule['action'] != 'escalate' and capacity_used < capacity_rule['attempts']):
                    capacity_used += 1
                    log('RETRY {} {} attempt={}/{} backoff={}s'.format(
                        recovery_class, key, capacity_used, capacity_rule['attempts'], capacity_rule['backoff_secs']))
                    safety.event(base, recovery_class.upper() + '_RETRY', key + ' attempt=' + str(capacity_used))
                    # Keep partial work and the last report before the launcher
                    # truncates its outputs, including when no session was created.
                    retry_task = Path(str(segment_base) + '.' + recovery_class + '-' + str(capacity_used) + '.md')
                    retry_task.write_text(supervisor.interruption_prompt(run, recovery_class) + handoff(original, cwd, segment_base, run))
                    session = run.get('conversation_id' if backend == 'agy' else 'session_id')
                    if session:
                        env['AGENT_RESUME_ID'] = session
                    env['AGENT_RETRY'] = '1'
                    deadline = time.monotonic() + capacity_rule['backoff_secs']
                    while time.monotonic() < deadline and not stopped:
                        time.sleep(min(.2, max(0, deadline - time.monotonic())))
                    if stopped:
                        return 143
                    with open(str(segment_base) + '.launcher.log', 'ab') as output:
                        child = launcher_process([str(launcher), str(retry_task), name], env=env,
                                                 stdout=output, stderr=output)
                    while True:
                        try:
                            last_rc = child.wait(timeout=.2)
                            break
                        except subprocess.TimeoutExpired:
                            if stopped:
                                stop(signal.SIGTERM, None)
                    run = a.read(str(segment_base) + '.run.json')
                    detail = failure_detail(segment_base, run)
                    if stopped or last_rc in (130, 137, 143, -signal.SIGINT, -signal.SIGTERM):
                        return 143 if stopped else last_rc
                    if last_rc == 0:
                        safety.event(base, 'HEALED', recovery_class + ' retry (' + key + ')')
                        if args.check and run.get('status') != 'needs_input':
                            last_rc = verify(args, segment_base)
                        if last_rc == 0 and args.review_by and run.get('status') != 'needs_input':
                            return cross_review(args, segment_base, original)
                        return last_rc
                if auth_failure(run, detail):
                    attempts, backoff = supervisor.auth_retry_budget()
                    remaining = max(0, attempts - auth_retries_used.get(account, 0))
                    attempted = False
                    for retry in range(remaining):
                        attempted = True
                        auth_retries_used[account] = auth_retries_used.get(account, 0) + 1
                        safety.event(base, 'AUTH_RETRY', key)
                        deadline = time.monotonic() + backoff
                        while time.monotonic() < deadline and not stopped:
                            time.sleep(min(.2, max(0, deadline - time.monotonic())))
                        if stopped:
                            return 143
                        retry_task = Path(str(segment_base) + '.auth-' + str(retry + 1) + '.md')
                        retry_task.write_text(supervisor.interruption_prompt(run, 'auth') + handoff(original, cwd, segment_base, run))
                        session = run.get('conversation_id' if backend == 'agy' else 'session_id')
                        if session:
                            env['AGENT_RESUME_ID'] = session
                        env['AGENT_RETRY'] = '1'
                        with open(str(segment_base) + '.launcher.log', 'ab') as output:
                            child = launcher_process([str(launcher), str(retry_task), name], env=env,
                                                     stdout=output, stderr=output)
                        last_rc = child.wait()
                        run = a.read(str(segment_base) + '.run.json')
                        detail = failure_detail(segment_base, run)
                        if last_rc == 0:
                            safety.event(base, 'HEALED', 'auth retry (' + key + ')')
                            if args.check and run.get('status') != 'needs_input':
                                last_rc = verify(args, segment_base)
                            if last_rc == 0 and args.review_by and run.get('status') != 'needs_input':
                                return cross_review(args, segment_base, original)
                            return last_rc
                        if not auth_failure(run, detail):
                            break
                    if attempted and auth_failure(run, detail):
                        safety.event(base, 'ESCALATE', 'auth retry failed (' + key + ')')
                from godfather_errors import classify
                provider_error = run.get('failure') or classify(backend, detail)
                is_quota = provider_error['category'] == 'quota' or (provider_error['category'] == 'other' and run.get('status') == 'quota')
                if not is_quota:
                    if auth_failure(run, detail):
                        ready[account] = (None, 'authentication failed in previous segment')
                        log('SKIP {} authentication failed; trying next account/backend'.format(key))
                    elif supervisor.outcome_fallback(run.get('failure_class'), {recovery_class: capacity_used}):
                        log('FALLBACK {} ({})'.format(run['failure_class'], run.get('failure_reason')))
                    else:
                        log('STOP non-quota CLI failure')
                        return last_rc
                else:
                    if backend == 'agy':
                        try:
                            usage = agy_usage(binary)
                            _, _, blocked = agy_limits(usage, m['model'], 0)
                            if blocked:
                                detail += '\n' + blocked
                        except (ValueError, OSError, subprocess.TimeoutExpired):
                            log('Quota reset refresh unavailable; using error/cooldown')
                    entry = mark_exhausted(key, detail)
                    quota_candidates.append((segment_base, run, entry))
                    log('COOLDOWN {} until={} ({})'.format(key, entry['until'], entry['reason']))
                if run.get('pid'):
                    previous = dict(model=m, run=run, base=segment_base, quota=is_quota,
                                    reason=run.get('failure_reason') or ('quota/rate limit exhausted' if run.get('status') == 'quota' else
                                           'authentication failed' if auth_failure(run, detail) else 'CLI failure'))
            log('STOP no eligible models remain')
            if quota_candidates or quota_unavailable and not route_state.get('active_name'):
                route_state['result'] = 'quota'
                safety.event(base, 'QUOTA', 'no fallback model remains')
                from godfather_lifecycle import schedule_quota
                # Earlier accounts can still recover after later fallbacks fail auth.
                def reset_for(item):
                    _, candidate, entry = item
                    return ((candidate.get('failure') or {}).get('resets_at') or
                            (a.epoch(entry.get('until')) if entry.get('reset_known') else None))
                for candidate_base, candidate, entry in sorted(quota_candidates,
                        key=lambda item: reset_for(item) or float('inf')):
                    if schedule_quota(candidate_base, candidate, reset_for((candidate_base, candidate, entry))):
                        route_state.update(active_name=candidate_base.name, status='waiting_quota',
                                           result='waiting_quota', resume_at=candidate['resume_at'])
                        return 0
            return last_rc or 1
        finally:
            for sig, handler in handlers.items():
                signal.signal(sig, handler)
            route_state['status'] = 'stopped' if stopped else ('waiting_quota' if route_state['status'] == 'waiting_quota' else 'finished')
            route_state['ended_at'] = a.now()
            a.write(route_file, route_state)


def auth_failure(run, detail):
    from godfather_errors import classify
    failure = run.get('failure') or classify(run.get('cli', ''), detail)
    return run.get('failure_class') != 'tool_denied' and failure['category'] == 'auth'


def failure_evidence(base, run):
    """Return native errors with result (2), saved (1), discovery (0), or history (-1) authority."""
    evidence = [(error, 1) for error in run.get('errors', [])]
    discovery = run.get('discovery_path')
    paths = list(dict.fromkeys([str(base) + '.log'] + run.get('diagnostic_paths', [])))
    current_results = [path for path in run.get('diagnostic_paths', [])
                       if path != discovery and path != str(base) + '.log' and Path(path).is_file()]
    for path in paths:
        history = run.get('cli') == 'agy' and current_results and path == str(base) + '.log'
        discovery_log = run.get('cli') == 'agy' and path == discovery
        authority = -1 if history else 0 if discovery_log else 2
        for line in tail(path, 300).splitlines():
            if line.lstrip().startswith('{'):
                try:
                    event = json.loads(line)
                    if event.get('type') in ('error', 'turn.failed', 'ERROR_MESSAGE') or event.get('is_error') or event.get('status') in ('ERROR', 'FAILED'):
                        evidence.append((line, authority))
                except (ValueError, AttributeError):
                    pass
            if re.match(r'(?:Error:|error:|ERROR:|E\d{4}\s|\d{4}-\d\d-\d\dT\S+\s+ERROR)', line):
                evidence.append((line, authority))
    if run.get('cli') in ('grok', 'cursor'):
        result = a.read(str(base) + '.json')
        if result.get('is_error'):
            evidence.extend((json.dumps(error), 2) for error in result.get('errors', []))
    return evidence


def failure_detail(base, run):
    # Structured/CLI errors only; never classify assistant prose or tool output.
    return '\n'.join(error if isinstance(error, str) else json.dumps(error)
                     for error, _authority in failure_evidence(base, run))


def git_output(cwd, *args):
    if not shutil.which('git'):
        return ''
    result = query(['git', '-C', str(cwd)] + list(args))
    return result.stdout if result.returncode == 0 else ''


def cross_review(args, segment_base, original):
    while True:
        rc = _review_once(args, segment_base, original)
        if rc is not None:
            return rc
        args.review_round = getattr(args, 'review_round', 0) + 1


def _review_once(args, segment_base, original):
    run = a.read(str(segment_base) + '.run.json')
    run.update(status='reviewing')
    a.write(str(segment_base) + '.run.json', run)
    configured_key = run.get('model_key')
    models = [m for m in load_models() if (model_key(m) != configured_key if configured_key else
              (m['backend'], m['model']) != (run['cli'], run['model']))]
    if not args.review_fix:
        # Team controls can disable Cursor's sandbox even with --sandbox enabled.
        # Refuse review-only use until the CLI can guarantee read-only execution.
        if any(m['backend'] == 'cursor' for m in models):
            with Path(str(segment_base) + '.models.log').open('a') as log:
                log.write('REVIEW_SKIP cursor: read-only execution cannot be guaranteed; use --review-fix\n')
        models = [m for m in models if m['backend'] != 'cursor']
    if args.review_by == 'auto':
        models = [m for m in models if m['backend'] != run['cli']]
    else:
        models = [m for m in models if args.review_by in (m['model'], model_key(m))]
    root = a.runs_dir().resolve()
    base = root / args.name
    review_round = getattr(args, 'review_round', 0)
    context = {key: getattr(args, key, None) for key in (
        'name', 'check', 'max_fix_rounds', 'review_by', 'review_fix', 'review_head',
        'review_before', 'branch', 'self', 'task_type', 'container')}
    context.update(run_cwd=str(args.run_cwd), review_round=review_round,
                   review_rounds=getattr(args, 'review_rounds', 0),
                   review_threshold=getattr(args, 'review_threshold', 'high'))
    previous = a.read(str(base) + '.review-state.json') if review_round else {}
    a.write(str(base) + '.review-state.json', dict(options=context, original=original, history=previous.get('history', [])))
    diff = git_output(args.run_cwd, 'diff', '--no-ext-diff', getattr(args, 'review_head', 'HEAD'), '--', '.', ':(exclude)**/.env*', ':(exclude).env*')
    Path(str(base) + '.diff').write_text(diff)
    fixing = ('Fix only clear bugs. Preserve unrelated work. Run relevant checks for any fix.' if args.review_fix else
              'Review only. Do not edit any files or run mutating commands.')
    prompt = ('Review this completed task independently. ' + fixing + '\n'
              'Return a fenced ```json block with this schema:\n'
              '{"findings":[{"severity":"critical|high|medium|low","file":"path","line":1,"problem":"concrete problem"}]}\n'
              'If there are no findings, return {"findings":[]}. Do not invent problems.\n\n'
              '## Original task\n' + original + '\n\n## Existing diff before the run (context only)\n' + getattr(args, 'review_before', '') +
              '\n\n## Final git diff since the run started\n' + diff +
              '\nCompare the before and final diff; report problems introduced by this run. If no git diff is available, inspect the task outcome in the working directory.\n')
    if review_round:
        prompt += ('\n## Worker response to the previous review\n' + tail(str(segment_base) + '.last.md', 10000) +
                   '\nEvaluate any explanation that a finding was wrong against the code and checks.\n')
    options = copy.copy(args)
    options.name = args.name + '.review'
    options.prompt, options.prompt_file = prompt, None
    options.review_by, options.review_fix, options.isolate = None, False, False
    options.review_rounds = 0
    options.review_models = models
    options.review_read_only = not args.review_fix
    options.review_parent, options.review_segment = base, segment_base
    options.check = args.check if args.review_fix else None
    options.branch = None
    # Retain the outer directory lock across work, verification and review.
    rc = _route(options)
    return complete_review(base, segment_base, options.name, rc, continue_review=False)


SEVERITIES = ('critical', 'high', 'medium', 'low')


def parse_findings(report):
    """Accept only a validated JSON object; retain the historical line fallback."""
    for block in re.findall(r'```json\s*\n(.*?)```', report, re.I | re.S):
        try:
            data = json.loads(block)
            findings = data['findings']
            if not isinstance(findings, list):
                continue
            if all(isinstance(item, dict) and item.get('severity') in SEVERITIES
                   and isinstance(item.get('file'), str) and item['file'].strip()
                   and type(item.get('line')) is int and item['line'] > 0
                   and isinstance(item.get('problem'), str) and item['problem'].strip()
                   for item in findings):
                return findings, 'json'
        except (ValueError, KeyError, TypeError):
            pass
    findings = [dict(severity=sev.lower(), file=path, line=int(line), problem=problem)
                for sev, path, line, problem in re.findall(
                    r'^\s*-\s*\[(critical|high|medium|low)\]\s+(\S+):(\d+)\s+-\s+(.+)',
                    report, re.I | re.M)]
    return findings, 'lines'


def resume_worker(args, segment_base, message):
    # Use the saved worker session and retain the conductor's directory lock.
    prior = os.environ.get('GODFATHER_LOCK_HELD')
    os.environ['GODFATHER_LOCK_HELD'] = str(args.run_cwd)
    try:
        try:
            return a.nudge_main([segment_base.name, message], run_checks=False)
        except SystemExit as exc:
            return exc.code or 1
    finally:
        if prior is None:
            os.environ.pop('GODFATHER_LOCK_HELD', None)
        else:
            os.environ['GODFATHER_LOCK_HELD'] = prior


def complete_review(base, segment_base, review_name, rc, continue_review=True):
    state = a.read(str(base.parent / review_name) + '.route.json')
    active = state.get('active_name')
    review_run = a.read(str(base.parent / active) + '.run.json') if active else {}
    run = a.read(str(segment_base) + '.run.json')
    context = a.read(str(base) + '.review-state.json')
    options = context.get('options', {})
    round_number = options.get('review_round', 0)
    budget = options.get('review_rounds', 0)
    run.update(review_name=review_name, review_file=str(base) + '.review.md', review_exit_status=rc)
    if review_run.get('status') == 'needs_input':
        run.update(status='needs_input', review_pending=True)
        a.write(str(segment_base) + '.run.json', run)
        return rc
    report = tail(str(base.parent / active) + '.last.md', 100000) if active else ''
    if not report:
        report = 'Review failed or unavailable; inspect ' + review_name + '.models.log.\n'
    Path(str(base) + '.review.md').write_text(report + '\n')
    findings, method = parse_findings(report)
    history = context.setdefault('history', [])
    history.append(dict(round=round_number + 1, parse_method=method, findings=findings))
    a.write(str(base) + '.review-state.json', context)
    maximum = next((level for level in SEVERITIES if any(f['severity'] == level for f in findings)), 'none')
    detail = 'round {}: {} findings ({})'.format(round_number + 1, len(findings), maximum)
    safety.event(base, 'REVIEW', detail)
    print('REVIEW {} {}'.format(base.name, detail), flush=True)
    threshold = options.get('review_threshold', 'high')
    remaining = [item for item in findings if SEVERITIES.index(item['severity']) <= SEVERITIES.index(threshold)]
    status = ('review_failed' if rc else 'review_findings' if budget and remaining and round_number >= budget
              else 'reviewing' if budget and remaining else 'done')
    run.update(status=status, review_pending=False, ended_at=a.now(), exit_code=rc or int(status == 'review_findings'),
               review_findings=findings, review_parse_method=method, review_history=history, review_round=round_number,
               review_rounds=budget, review_threshold=threshold)
    a.write(str(segment_base) + '.run.json', run)
    if rc or not budget or not remaining or round_number >= budget:
        return run['exit_code']
    from types import SimpleNamespace
    args = SimpleNamespace(**options)
    args.run_cwd = Path(args.run_cwd)
    args.review_round = round_number + 1
    run.update(status='retrying', exit_code=None, ended_at=None)
    run.setdefault('launcher_env', {})['GODFATHER_REVIEW_ROUND'] = str(args.review_round)
    a.write(str(segment_base) + '.run.json', run)
    message = ('The independent review found these problems. Fix them or explain precisely why a finding is wrong. '
               'Preserve unrelated work and verify your changes.\n' +
               '\n'.join('- [{severity}] {file}:{line} - {problem}'.format(**item) for item in findings))
    rc = resume_worker(args, segment_base, message)
    current = a.read(str(segment_base) + '.run.json')
    if rc:
        current.update({key: value for key, value in run.items() if key.startswith('review_')})
        status = current.get('status')
        if status not in ('failed', 'cancelled', 'killed', 'timed_out', 'stopped', 'check_failed'):
            status = 'failed'
        current.update(status=status, exit_code=rc, ended_at=a.now(),
                       failure_class=current.get('failure_class') or 'resume',
                       review_resume_exit_status=rc, review_resume_error='worker resume failed after successful review')
        a.write(str(segment_base) + '.run.json', current)
        safety.event(base, 'ERROR', 'worker resume failed after successful review: exit=' + str(rc))
        return rc
    if current.get('status') == 'needs_input':
        return 0
    if args.check:
        rc = verify(args, segment_base)
        if rc or a.read(str(segment_base) + '.run.json').get('status') == 'needs_input':
            return rc
    if continue_review:
        return cross_review(args, segment_base, context['original'])
    return None


def main():
    os.umask(0o077)
    original = safety.original_environment()
    for key in ('PATH', 'BASH_ENV', 'ZDOTDIR'):
        if key in original:
            os.environ[key] = original[key]
        else:
            os.environ.pop(key, None)
    p = argparse.ArgumentParser(prog='godfather', description='Run the highest-ranked available model across all installed, authenticated CLIs.')
    sub = p.add_subparsers(dest='command', required=True)
    run = sub.add_parser('run', help='route a task; fall back on quota or authentication failure',
                         usage='CWD=<dir> godfather run <prompt-file> <name>\n       CWD=<dir> godfather run -p "<prompt>" <name>')
    run.add_argument('--wait-lock', action='store_true', help='with --no-isolate, wait for the CWD lock')
    dependencies = run.add_mutually_exclusive_group()
    dependencies.add_argument('--after', help='queue after comma-separated successful runs')
    dependencies.add_argument('--after-any', help='queue after comma-separated terminal runs, any status')
    run.add_argument('--dependency-timeout', type=float, default=300, help='seconds for missing dependency names to register (default: 300)')
    isolation = run.add_mutually_exclusive_group()
    isolation.add_argument('--isolate', dest='isolate', action='store_true',
                           help='run in a git worktree on godfather/<name>, or a scratch copy outside git (default)')
    isolation.add_argument('--no-isolate', dest='isolate', action='store_false',
                           help='run in CWD and take the CWD lock')
    isolation.set_defaults(isolate=True)
    run.add_argument('--self', action='store_true', help='allow modifying godfather installed scripts (also GODFATHER_SELF=1)')
    run.add_argument('--review-by', metavar='MODEL', help='review after success with a different model; auto uses a different CLI')
    run.add_argument('--review-fix', action='store_true', help='allow reviewer to fix only clear bugs (requires --review-by)')
    run.add_argument('--review-rounds', type=int, default=0, help='worker fix rounds after review findings (requires --review-by)')
    run.add_argument('--review-threshold', choices=('critical', 'high', 'medium', 'low'), default='high', help='minimum severity requiring worker fixes (default: high)')
    run.add_argument('--check', help='shell acceptance check, run after the agent finishes')
    run.add_argument('--max-fix-rounds', type=int, default=2, help='session nudges after check failures (default: 2)')
    run.add_argument('--task-type', default='edit', help='effort row for this task (default: edit)')
    run.add_argument('--container', action='store_true', help='run the CLI in a container; the repo is mounted read-write and nothing else is')
    ask = sub.add_parser('ask', help='ask the conductor for a decision from inside a run')
    ask.add_argument('question')
    ask.add_argument('--options', help='pipe-separated choices, e.g. "a|b|c"')
    ask.add_argument('--blocking', action='store_true', help='wait and print the answer to stdout')
    ask.add_argument('--timeout', type=float, default=1800, help='blocking timeout in seconds (default: 1800)')
    answer = sub.add_parser('answer', help='answer a question; resume the exact session if stopped')
    answer.add_argument('name')
    answer.add_argument('answer')
    answer.add_argument('--remember', action='store_true', help='learn this approved effect as a narrow rule')
    lessons = sub.add_parser('lessons', help='manage project reminders (does not change effect policy)')
    lessons.add_argument('--cwd', metavar='DIR', help='project directory (default: CWD or current directory)')
    lesson_commands = lessons.add_subparsers(dest='action', required=True)
    lesson_commands.add_parser('list', help='list newest first; repository lessons are read only')
    lesson_add = lesson_commands.add_parser('add', help='append safe non-empty lines as lessons')
    lesson_add.add_argument('text')
    lesson_rm = lesson_commands.add_parser('rm', help='remove a local lesson by its list number')
    lesson_rm.add_argument('number', type=int)
    lesson_commands.add_parser('edit', help='edit local lessons using VISUAL or EDITOR')
    run.add_argument('-p', '--prompt', help='inline task instead of a prompt file')
    run.add_argument('inputs', nargs='+', metavar='INPUT', help='prompt file and run name; with -p, just the run name')
    quota = sub.add_parser('quota', help='show ranked model eligibility, allowance and active cooldowns')
    quota.add_argument('--all', action='store_true', help='print persisted cooldowns (including expired entries)')
    quota.add_argument('--reset', metavar='MODEL', help='clear backend:model or all matching bare model names')
    quota.add_argument('--json', action='store_true', help='report ordered subscription accounts and pools')
    quota.add_argument('--live', action='store_true', help='refresh provider observations and bypass probe caches')
    usage = sub.add_parser('usage', help='show segment tokens, estimated USD and quota burn')
    periods = usage.add_mutually_exclusive_group()
    periods.add_argument('--today', action='store_true', help='since local midnight')
    periods.add_argument('--week', action='store_true', help='since local Monday midnight')
    usage.add_argument('--by', choices=('model', 'cli', 'run'), help='group totals')
    usage.add_argument('--json', action='store_true', help='structured totals and latest agy /usage snapshot')
    wait = sub.add_parser('wait', help='wait for all routed segments and pending fallback')
    wait.add_argument('name')
    wait.add_argument('--timeout', type=float)
    stop = sub.add_parser('stop', help='stop a named run and its process tree')
    stop.add_argument('name')
    sub.add_parser('daemon', help='drain the persisted dependency queue')
    policy = sub.add_parser('policy', help='show effective policy with rule sources; forget a learned rule')
    policy.add_argument('action', nargs='?', choices=('forget',))
    policy.add_argument('id', nargs='?')
    for command in ('doctor', 'improve'):
        report = sub.add_parser(command, help='summarize incidents' if command == 'doctor' else 'launch an isolated self-improvement branch')
        report.add_argument('--since', default='24h', help='incident window (default: 24h)')
        if command == 'doctor':
            report.add_argument('--live', action='store_true', help='probe with codex login status and agy -p /usage; default reads credential files only')
            report.add_argument('--fix', action='store_true', help='expire asks of terminal runs with no live owners')
        if command == 'improve':
            report.add_argument('--dry-run', action='store_true', help='print the improvement brief')
    sub.add_parser('mcp', help='serve MCP JSON-RPC over stdio')
    top = sub.add_parser('top', help='live dashboard; refreshes every 2 seconds')
    top.add_argument('--once', action='store_true', help='print one plain snapshot')
    plan = sub.add_parser('plan', help='run a task DAG, or show its stored status')
    plan_commands = plan.add_subparsers(dest='plan_command', required=True)
    plan_run = plan_commands.add_parser('run', help='run tasks from a plan file')
    plan_run.add_argument('plan_file')
    plan_run.add_argument('--max-fix-rounds', type=int, default=None, help='override acceptance-check fix rounds')
    plan_run.add_argument('--max-parallel', type=int, default=None, help='maximum concurrent tasks (default: plan max_parallel or 2)')
    plan_status = plan_commands.add_parser('status', help='show plan and task status')
    plan_status.add_argument('name', nargs='?')
    plan_status.add_argument('--json', action='store_true')
    gc = sub.add_parser('gc', help='list old owned resources; --yes removes safe candidates')
    gc.add_argument('--yes', action='store_true', help='apply cleanup (default: dry run)')
    gc.add_argument('--older-than', default='7d', help='minimum terminal age, e.g. 7d, 24h, 30m (default: 7d)')
    for name in ('status', 'watch', 'nudge'):
        sub.add_parser(name, help='alias for agents-' + name, add_help=False)
    from godfather_lifecycle import ensure_daemon
    ensure_daemon()
    if len(sys.argv) > 1 and sys.argv[1] in ('status', 'watch', 'nudge'):
        command = sys.argv[1]
        argv = sys.argv[2:]
        os.execv(str(Path(__file__).with_name('agents-' + command)), ['agents-' + command] + argv)
    args = p.parse_args()
    if args.command == 'run':
        args.isolate_explicit = '--isolate' in sys.argv[2:]
    try:
        if args.command == 'gc':
            return safety.gc_main(args)
        if args.command == 'lessons':
            return safety.lessons_main(args)
        if args.command == 'stop':
            from godfather_lifecycle import stop_run
            return stop_run(args.name)
        if args.command == 'wait':
            from godfather_lifecycle import wait_run
            status, timed_out = wait_run(args.name, args.timeout)
            print(args.name + ' ' + status + (' (timeout)' if timed_out else ''))
            return 0 if status == 'done' and not timed_out else 1
        if args.command == 'daemon':
            from godfather_lifecycle import daemon
            return daemon()
        if args.command == 'policy':
            from godfather_policy import policy_main
            return policy_main(args)
        if args.command in ('doctor', 'improve'):
            from godfather_lifecycle import doctor_report, improve
            if args.command == 'improve':
                return improve(args)
            if args.fix:
                from godfather_registry import expire_ended_asks
                print('Expired asks in %d ended runs.' % expire_ended_asks(a.runs_dir()))
            print(doctor_report(args.since), end='')
            a.doctor(live=args.live)
            return 0
        if args.command == 'ask':
            return safety.ask(args)
        if args.command == 'answer':
            return safety.answer(args)
        if args.command == 'quota':
            return quota_main(args)
        if args.command == 'usage':
            from godfather_usage import usage_main
            return usage_main(args)
        if args.command == 'mcp':
            import importlib.util
            runtime = Path(os.getenv('GODFATHER_MCP_PYTHON', str(Path(os.getenv('XDG_DATA_HOME', str(Path.home() / '.local/share'))) / 'godfather/mcp-venv/bin/python')))
            if importlib.util.find_spec('mcp') is None and runtime.is_file() and str(runtime.absolute()) != str(Path(sys.executable).absolute()):
                os.execv(str(runtime), [str(runtime), str(Path(__file__).resolve()), 'mcp'])
            from godfather_mcp import serve
            return serve()
        if args.command == 'top':
            from godfather_top import top_main
            return top_main(args)
        if args.command == 'plan':
            from godfather_plan import plan_main
            return plan_main(args)
        if args.command == 'run':
            if len(args.inputs) != (1 if args.prompt is not None else 2):
                run.error('provide <prompt-file> <name>, or -p "<prompt>" <name>')
            args.name = args.inputs[-1]
            args.prompt_file = args.inputs[0] if args.prompt is None else None
            if args.after or args.after_any:
                from godfather_lifecycle import enqueue
                return enqueue(args)
            if os.getenv('GODFATHER_DETACHED_RUN') != '1':
                from godfather_lifecycle import attached_run
                return attached_run(sys.argv[1:], args.name)
            os.environ.pop('GODFATHER_DETACHED_RUN', None)
            return route(args)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        p.error(str(exc))


if __name__ == '__main__':
    sys.exit(main())
