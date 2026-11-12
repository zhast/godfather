"""Append-only segment ledger and explicitly priced usage summaries."""
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import time

SNAPSHOT_MAX_AGE = 600
SNAPSHOT_TTL = SNAPSHOT_MAX_AGE


def state_dir():
    return Path(os.getenv('XDG_STATE_HOME', str(Path.home() / '.local/state'))).expanduser() / 'godfather'


def save_snapshot(text):
    from godfather_safety import atomic
    root = state_dir()
    root.mkdir(parents=True, exist_ok=True)
    atomic(root / 'agy-usage.json', dict(at=time.time(), text=text))


def agy_usage(binary):
    import godfather_router as router
    return router.agy_usage(binary)


def format_age(age_seconds, refreshed=False):
    age = max(0, int(age_seconds))
    if refreshed or age < 60:
        return 'live'
    if age < 3600:
        n = age // 60
        return '{} minute{} old'.format(n, '' if n == 1 else 's')
    if age < 86400:
        n = age // 3600
        return '{} hour{} old'.format(n, '' if n == 1 else 's')
    n = age // 86400
    return '{} day{} old'.format(n, '' if n == 1 else 's')


def load_snapshot(clock=None):
    from agent_oversight import cli_binary, epoch, read
    path = state_dir() / 'agy-usage.json'
    snapshot = read(path)
    if not snapshot or 'at' not in snapshot:
        return None
    now_ts = time.time() if clock is None else clock
    age = max(0.0, now_ts - epoch(snapshot['at']))
    refreshed = False
    if age > SNAPSHOT_MAX_AGE:
        old_at = snapshot.get('at')
        text = agy_usage(cli_binary('agy'))
        if text:
            fresh = read(path)
            if not fresh or fresh.get('at') == old_at:
                save_snapshot(text)
                fresh = read(path)
            snapshot = fresh
            refreshed = True
            now_ts = time.time() if clock is None else clock
            age = max(0.0, now_ts - epoch(snapshot['at']))
    stale = age > SNAPSHOT_MAX_AGE and not refreshed
    return dict(snapshot, at=snapshot['at'], age_seconds=int(round(age)), refreshed=refreshed, stale=stale)


def estimate(record, model):
    # CLI input counts include cached input. Do not silently charge unknown usage as zero.
    incoming, cached, outgoing = (record.get(k) for k in ('tokens_in', 'tokens_cached', 'tokens_out'))
    if incoming is None or outgoing is None:
        return None
    if not all(k in model for k in ('input_per_million', 'output_per_million')):
        return None
    if cached is None and model.get('cached_per_million', model['input_per_million']) != model['input_per_million']:
        return None
    cached = cached or 0
    if cached > incoming:
        return None
    return ((incoming - cached) * model['input_per_million'] +
            cached * model.get('cached_per_million', model['input_per_million']) +
            outgoing * model['output_per_million']) / 1_000_000


def append_segment(run, rc, fields):
    from godfather_router import load_models
    record = dict(name=run['name'], run=run.get('router_name') or run['name'],
                  run_id=run.get('router_id') or run['run_id'], segment_id=run['segment_id'], cli=run['cli'], model=run['model'],
                  started_at=run.get('segment_started_at', run['started_at']), ended_at=time.time(), exit_status=rc,
                  tokens_in=fields.get('tokens_in'), tokens_cached=fields.get('tokens_cached'),
                  tokens_out=fields.get('tokens_out'),
                  quota_fallback=os.getenv('GODFATHER_QUOTA_FALLBACK') == '1',
                  quota_exhausted=rc != 0 and (run.get('failure') or fields.get('failure') or {}).get('category') == 'quota')
    try:
        models = load_models()
    except (OSError, ValueError):
        models = []  # A price-table edit must not lose the completed segment.
    model = next((m for m in models if m['backend'] == record['cli'] and m['model'] == record['model']), {})
    record['estimated_usd'] = estimate(record, model)
    record['prices'] = {k: model[k] for k in ('input_per_million', 'cached_per_million', 'output_per_million') if k in model}
    root = state_dir()
    root.mkdir(parents=True, exist_ok=True)
    with (root / 'ledger.jsonl').open('a') as output:
        fcntl.flock(output, fcntl.LOCK_EX)
        output.write(json.dumps(record) + '\n')
    from godfather_registry import note_ledger
    note_ledger(record)


def summary(period=None, by=None):
    from agent_oversight import epoch, read
    now = dt.datetime.now().astimezone()
    cutoff = 0
    if period == 'today':
        cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    elif period == 'week':
        cutoff = (now.replace(hour=0, minute=0, second=0, microsecond=0) - dt.timedelta(days=now.weekday())).timestamp()
    elif period not in (None, 'all'):
        raise ValueError('period must be today, week, or all')
    groups = {}
    try:
        source = (state_dir() / 'ledger.jsonl').open()
    except FileNotFoundError:
        source = []
    try:
        for line in source:
            try:
                record = json.loads(line)
            except ValueError:
                continue  # Ignore an incomplete tail from a concurrent append.
            if epoch(record['started_at']) < cutoff:
                continue
            key = str(record.get(by, '?')) if by else 'total'
            total = groups.setdefault(key, dict(segments=0, tokens_in=None, tokens_cached=None, tokens_out=None,
                                                estimated_usd=None, unpriced_segments=0, quota_fallbacks=0, quota_exhaustions=0))
            total['segments'] += 1
            for field in ('tokens_in', 'tokens_cached', 'tokens_out', 'estimated_usd'):
                if record.get(field) is not None:
                    total[field] = (total[field] or 0) + record[field]
            total['unpriced_segments'] += record.get('estimated_usd') is None
            total['quota_fallbacks'] += bool(record.get('quota_fallback'))
            total['quota_exhaustions'] += bool(record.get('quota_exhausted'))
    finally:
        if hasattr(source, 'close'):
            source.close()
    snapshot = load_snapshot()
    return dict(period=period or 'all', by=by, groups=groups, agy_usage=snapshot,
                note='Known token totals only. USD covers priced segments only. Quota burn is observed exhaustions/fallbacks; agy snapshot shows remaining allowance.')


def usage_main(args):
    result = summary('today' if args.today else 'week' if args.week else None, args.by)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for key, row in result['groups'].items():
            tokens = '/'.join('?' if row[k] is None else str(row[k]) for k in ('tokens_in', 'tokens_cached', 'tokens_out'))
            cost = ' ${:.6f} estimated'.format(row['estimated_usd']) if row['estimated_usd'] is not None else ''
            print('{}: {} segments; tokens in/cached/out={}{}; quota exhausted={} fallbacks={} unpriced={}'.format(
                key, row['segments'], tokens, cost, row['quota_exhaustions'], row['quota_fallbacks'], row['unpriced_segments']))
        if not result['groups']:
            print('No usage recorded for this period.')
        if result['agy_usage']:
            snap = result['agy_usage']
            stamp = dt.datetime.fromtimestamp(snap['at']).astimezone().isoformat()
            age_text = format_age(snap.get('age_seconds', 0), snap.get('refreshed', False))
            if snap.get('stale'):
                print('Stale agy /usage snapshot at {} (stale, {}):\n{}'.format(stamp, age_text, snap['text']))
            else:
                print('Latest agy /usage snapshot at {} ({}):\n{}'.format(stamp, age_text, snap['text']))
        print(result['note'])
    return 0


# Subscription observations for quota charts. Only explicitly selected fields
# leave this layer: provider payloads can include identities and credentials.
def quota_iso(value):
    from godfather_errors import timestamp
    stamp = timestamp(value)
    try:
        return dt.datetime.fromtimestamp(stamp, dt.timezone.utc).isoformat() if stamp else None
    except (OverflowError, OSError, ValueError):
        return None


def quota_number(value):
    import math
    try:
        result = float(value)
        return result if not isinstance(value, bool) and math.isfinite(result) and result >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def quota_safe(value):
    text = str(value)
    text = re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b', '[redacted]', text)
    text = re.sub(r'''(?i)\b(?:bearer\s+\S+|(?:access[_ -]?token|api[_ -]?key|authorization|token)["']?\s*[:=]\s*["']?\S+|sk-[A-Za-z0-9_-]+)''', '[redacted]', text)
    return ' '.join(text.split())[:800]


def quota_window(name, used, reset=None, minutes=None):
    return dict(window=name, used_percent=quota_number(used), resets_at=quota_iso(reset),
                window_minutes=minutes)


def codex_observation(home):
    """Newest session-log or app-server observation for this effective home."""
    import agent_oversight as a
    from godfather_errors import timestamp
    latest = None
    for path in (Path(home) / 'sessions').glob('**/rollout-*.jsonl'):
        try:
            with path.open(errors='replace') as source:
                for line in source:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    payload = event.get('payload')
                    payload = payload if isinstance(payload, dict) else {}
                    limits = event.get('rate_limits') or payload.get('rate_limits')
                    if not isinstance(limits, dict) or not limits:
                        continue
                    at = timestamp(event.get('timestamp')) or timestamp(event.get('at')) or path.stat().st_mtime
                    if latest is None or at >= latest[0]:
                        latest = (at, limits, 'session-log')
        except OSError:
            continue
    for path in a.runs_dir().glob('*.run.json'):
        run = a.read(path)
        at = timestamp(run.get('rate_limits_at'))
        limits = run.get('rate_limits')
        if (run.get('cli') == 'codex' and a.backend_home('codex', run) == Path(home)
                and at and isinstance(limits, dict) and limits and (latest is None or at > latest[0])):
            latest = (at, limits, 'api')
    if latest is None:
        return dict(source='session-log', detail='allowance unknown', windows=[])
    at, limits, source = latest
    # App-server maps may be keyed by limit ID, unlike rollout token_count.
    if not any(k in limits for k in ('primary', 'secondary')):
        limits = limits.get('codex') or next((v for v in limits.values() if isinstance(v, dict) and 'primary' in v), {})
    windows = []
    for key in ('primary', 'secondary'):
        value = limits.get(key)
        if not isinstance(value, dict):
            continue
        minutes = value.get('window_minutes', value.get('windowDurationMins', value.get('windowMinutes')))
        name = 'weekly' if minutes == 10080 else 'five-hour' if minutes == 300 else key
        window = quota_window(name, value.get('used_percent', value.get('usedPercent')),
                              value.get('resets_at', value.get('resetsAt')), minutes)
        if window['used_percent'] is not None:
            windows.append(window)
    credits = limits.get('credits') or {}
    safe_credits = {key: credits[key] for key in ('has_credits', 'unlimited', 'hasCredits')
                    if key in credits and isinstance(credits[key], (bool, int, float))}
    balance = quota_number(credits.get('balance'))
    if balance is not None:
        safe_credits['balance'] = balance
    return dict(source=source, observed_at=quota_iso(at), plan=quota_safe(limits.get('plan_type', limits.get('planType'))) if limits.get('plan_type', limits.get('planType')) else None,
                windows=windows, credits=safe_credits or None,
                detail='; '.join('{} used={:g}%'.format(w['window'], w['used_percent']) for w in windows) or 'allowance unknown')


def agy_pool(model):
    if model.startswith(('claude-', 'gpt-')):
        return 'Claude and GPT models'
    if model.startswith('gemini-4-argon-'):
        return 'Gemini 4 Argon'
    return 'Gemini Models'


def agy_observation(pool, snapshot):
    windows = []
    for line in snapshot.get('text', '').splitlines():
        columns = line.split('\t')
        if len(columns) < 3 or columns[0] != pool:
            continue
        remaining = quota_number(columns[2].rstrip('%'))
        if remaining is None or remaining > 100:
            continue
        name = 'weekly' if 'weekly' in columns[1].lower() else 'five-hour' if 'five hour' in columns[1].lower() else columns[1]
        windows.append(quota_window(name, 100 - remaining, columns[3] if len(columns) > 3 else None))
    return dict(plan=None, windows=windows, source='usage-snapshot', observed_at=quota_iso(snapshot.get('at')),
                detail='; '.join('{} used={:g}%'.format(w['window'], w['used_percent']) for w in windows) or 'allowance unknown')


def grok_error_observation(home):
    """Read errors only from runs scoped to the account; never echo log text."""
    import agent_oversight as a
    import godfather_errors as errors
    latest = None
    for path in a.runs_dir().glob('*.run.json'):
        run = a.read(path)
        if run.get('cli') != 'grok' or a.backend_home('grok', run) != Path(home):
            continue
        failure = run.get('failure') or {}
        at = errors.timestamp(run.get('ended_at') or run.get('updated_at') or run.get('started_at')) or path.stat().st_mtime
        run_latest = None
        base = str(path)[:-len('.run.json')]
        for suffix in ('.log', '.stream.jsonl'):
            log = Path(base + suffix)
            try:
                with log.open(errors='replace') as source:
                    for line in source:
                        message = line.strip()
                        stamp = log.stat().st_mtime
                        if suffix == '.stream.jsonl':
                            try:
                                event = json.loads(message)
                            except ValueError:
                                continue
                            if not isinstance(event, dict) or event.get('type') not in ('error', 'result'):
                                continue
                            if event.get('type') == 'result' and not event.get('is_error') and not event.get('error'):
                                continue
                            stamp = errors.timestamp(event.get('timestamp') or event.get('at')) or stamp
                        else:
                            # Raw stderr logs can include ordinary output. Require
                            # a provider error marker before considering quota text.
                            if not re.search(r'(?i)\b(?:error|HTTP|402|429)\b', message):
                                continue
                            stamp = errors.timestamp(message.split(' ', 1)[0]) or stamp
                        if errors.REGEX['quota'].search(message) and (run_latest is None or stamp > run_latest):
                            run_latest = stamp
            except OSError:
                pass
        if run_latest is None and (failure.get('category') == 'quota' or run.get('failure_class') == 'quota' or run.get('status') == 'quota'):
            run_latest = at
        if run_latest is not None and (latest is None or run_latest > latest):
            latest = run_latest
    if latest is None:
        return None
    return dict(source='error-log', observed_at=quota_iso(latest), status='exhausted',
                windows=[], detail='latest run reports quota exhausted')


def cursor_observation(model, live, now):
    import godfather_cursor as cursor
    import agent_oversight as a
    data = cursor.usage(model.get('home'), force=live)
    saved = a.read(cursor.usage_cache_path(model.get('home')))
    windows = []
    used = quota_number((data.get('planUsage') or {}).get('totalPercentUsed'))
    if used is not None:
        windows.append(quota_window('billing-cycle', used, cursor.cycle_end(data, now)))
    grants = data.get('activeGrants') or []
    grants = [g for g in grants if not g.get('expiresAtMs') or (quota_number(g['expiresAtMs']) or 0) > now * 1000]
    money = None
    try:
        if grants:
            limit = sum(float(g['totalCents']) for g in grants)
            remaining = sum(float(g['remainingCents']) for g in grants)
            if quota_number(limit) is not None and quota_number(remaining) is not None:
                money = dict(used_cents=max(0, limit - remaining), limit_cents=limit, kind='grant')
    except (TypeError, ValueError, KeyError):
        pass
    spend = data.get('spendLimitUsage') or {}
    on_demand = {key: quota_number(spend[key]) for kind in ('overall', 'individual', 'pooled')
                 for key in (kind + 'Used', kind + 'Limit') if key in spend}
    return dict(source='api', observed_at=quota_iso(saved.get('at')), plan=quota_safe((data.get('planInfo') or {}).get('planName')) if (data.get('planInfo') or {}).get('planName') else None,
                windows=windows, money=money, on_demand=on_demand or None,
                resets_at=quota_iso(cursor.cycle_end(data, now)), window='billing-cycle', detail=cursor.describe(data)), data


def quota_report(models, persisted, live=False, now=None):
    """One ordered account/pool list and the same model eligibility for text."""
    import agent_oversight as a
    import godfather_router as router
    import godfather_supervisor as supervisor
    import godfather_cursor as cursor
    now = time.time() if now is None else now
    snapshot = a.read(state_dir() / 'agy-usage.json')
    if live and any(m['backend'] == 'agy' for m in models):
        router.agy_usage(a.cli_binary('agy'))
        snapshot = a.read(state_dir() / 'agy-usage.json')
    usage = snapshot.get('text', '')
    accounts, groups, text_rows = [], {}, []
    for m in models:
        backend, name = m['backend'], m['model']
        home = a.backend_home(backend, {backend + '_home': m.get('home')}) if backend != 'agy' else None
        pool = agy_pool(name) if backend == 'agy' else None
        short_home = str(home)
        if home:
            try:
                short_home = '~/' + str(home.relative_to(Path.home()))
            except ValueError:
                pass
        key = 'agy@' + pool if pool else backend + '@' + short_home
        if key not in groups:
            if backend == 'codex':
                obs = codex_observation(home)
            elif backend == 'agy':
                obs = agy_observation(pool, snapshot)
            elif backend == 'grok':
                probe = supervisor.quota_preflight('grok', a.cli_binary('grok'), home=home, now=now, force=live)
                windows = []
                if probe.get('remaining') is not None:
                    windows.append(quota_window('weekly', 100 - probe['remaining'], probe.get('resets_at')))
                obs = dict(source='tui', observed_at=quota_iso(probe.get('at')), plan=probe.get('plan'), windows=windows, detail=probe.get('detail', 'allowance unknown'))
                if not windows:
                    if probe.get('unavailable'):
                        obs = grok_error_observation(home) or obs
                    if obs.get('status') != 'exhausted':
                        obs['detail'] = 'allowance unknown; ' + obs['detail']
            else:
                obs, data = cursor_observation(m, live, now)
            windows = obs.get('windows', [])
            selected = next((w for w in windows if w['window'] == 'weekly'), windows[0] if windows else {})
            row = dict(order=len(accounts) + 1, key=quota_safe(key), backend=backend,
                       label=quota_safe(m.get('label') or {'codex': 'Codex', 'grok': 'Grok', 'agy': 'Antigravity', 'cursor': 'Cursor'}[backend] + ' · ' + (pool or home.name)),
                       models=[], plan=None, window=None, used_percent=None, resets_at=None,
                       status='unknown', eligible=False, source=obs['source'], observed_at=None, detail='', money=None)
            row.update({k: v for k, v in obs.items() if k not in ('windows', 'status')})
            row.update({k: v for k, v in selected.items() if k != 'window_minutes'})
            row['windows'] = windows
            row['status'] = obs.get('status', 'exhausted' if any(w['used_percent'] >= 100 for w in windows) else 'ok' if windows else 'unknown')
            if row['money'] and row['status'] == 'unknown':
                row['status'] = 'ok'
            row['cooldowns'] = []
            row['model_eligibility'] = {}
            group = groups[key] = dict(row=row, explicit_label=bool(m.get('label')), data=data if backend == 'cursor' else {}, low=False)
            accounts.append(row)
        group = groups[key]
        row = group['row']
        if m.get('label') and not group['explicit_label']:
            row['label'] = quota_safe(m['label'])
            group['explicit_label'] = True
        if name not in row['models']:
            row['models'].append(name)
        cooldown = persisted.get(router.model_key(m), {})
        active = cooldown if a.epoch(cooldown.get('until', 0)) > now else None
        allowance = row['detail']
        if backend == 'agy':
            limits_ok, allowance, _ = router.agy_limits(usage, name, m.get('min_pct', 0))
        elif backend == 'cursor':
            limits_ok = not m.get('included_only') or cursor.included(m, group['data'])
            quota_ok = limits_ok
            # Same catalog gate as routing and the existing text quota command.
            try:
                binary = a.cli_binary('cursor')
                if not binary:
                    raise ValueError('CLI missing')
                allowance += '; resolved=' + cursor.resolve(m, binary)
            except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
                limits_ok = False
                allowance += '; ' + quota_safe(exc)
        else:
            remaining = [100 - w['used_percent'] for w in row['windows']]
            limits_ok = row['status'] != 'exhausted' and all(v > 0 and v >= m.get('min_pct', 0) for v in remaining)
        eligible = limits_ok and active is None
        row['model_eligibility'][name] = row['model_eligibility'].get(name, False) or eligible
        row['eligible'] = row['eligible'] or eligible
        if not (quota_ok if backend == 'cursor' else limits_ok) and row['status'] == 'ok':
            group['low'] = True
        if active:
            note = 'cooldown until=' + str(active.get('until', ''))
            reason = quota_safe(a.compact(active.get('reason', ''), 120))
            if reason:
                note += ' (' + reason + ')'
            row['cooldowns'].append(dict(model=name, until=active.get('until'), detail=note))
            allowance += '; ' + note
        text_rows.append('{} {} eligible={}; {}'.format(backend, router.model_key(m).split(':', 1)[1], 'yes' if eligible else 'no', quota_safe(allowance)))
    for group in groups.values():
        row = group['row']
        if group['low'] and not row['eligible'] and row['status'] == 'ok':
            row['status'] = 'low'
        if row['cooldowns']:
            row['detail'] += '; ' + '; '.join(c['detail'] for c in row['cooldowns'])
            if not row['eligible']:
                row['status'] = 'cooldown'
        row['detail'] = quota_safe(row['detail'])
    return dict(generated_at=quota_iso(now), accounts=accounts), text_rows
