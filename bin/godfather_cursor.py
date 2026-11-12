"""Cursor catalog and best-effort Dashboard usage; no credential values in diagnostics."""
import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.request


def home(value=None):
    return Path(value or os.getenv('CURSOR_HOME') or os.getenv('CURSOR_CONFIG_DIR') or str(Path.home() / '.cursor')).expanduser().absolute()


def environment(value=None):
    root = str(home(value))
    return dict(os.environ, CURSOR_CONFIG_DIR=root, CURSOR_DATA_DIR=root)


def cached(kind, ttl, fetch, value=None):
    import hashlib
    root = Path(os.getenv('AGENT_RUNS_DIR', str(Path.home() / '.agent-runs')))
    root.mkdir(parents=True, exist_ok=True)
    identity = str(home(value)) + ':' + os.getenv('CURSOR_AGENT_BIN', 'cursor-agent')
    path = root / ('cursor-' + kind + '-' + hashlib.sha256(identity.encode()).hexdigest()[:16] + '.json')
    try:
        data = json.loads(path.read_text())
        if time.time() - data['at'] < ttl:
            return data['value']
    except (OSError, ValueError, KeyError, TypeError):
        pass
    result = fetch()
    from godfather_safety import atomic
    atomic(path, dict(at=time.time(), value=result))
    return result


def catalog(binary, value=None):
    def fetch():
        result = subprocess.run([binary, '--list-models'], env=environment(value), capture_output=True, text=True, timeout=20)
        if result.returncode:
            raise ValueError('Cursor model catalog unavailable')
        rows = {}
        for line in result.stdout.splitlines():
            line = re.sub(r'\x1b\[[0-9;]*m', '', line).strip()
            match = re.match(r'(?:[-*•]\s*)?([A-Za-z0-9][A-Za-z0-9_.:/-]*)(?=\s+-\s+|\s*\(|$)', line)
            if match:
                rows[match[1]] = line
        return rows
    return cached('models', 3600, fetch, value)


def resolve(model, binary):
    rows = catalog(binary, model.get('home'))
    name = model['model']
    resolved = name + '-fast' if model.get('fast', True) and name + '-fast' in rows else name
    if name not in rows or resolved not in rows:
        raise ValueError('Cursor model absent from catalog: ' + resolved)
    if not model.get('allow_no_zdr', False) and any('NO ZDR' in rows[n].upper() for n in (name, resolved)):
        raise ValueError('Cursor NO ZDR model requires allow_no_zdr = true: ' + resolved)
    return resolved


def token(value=None):
    # Keychain is the default account only. Explicit homes must never borrow it.
    if value is None and not os.getenv('CURSOR_HOME') and not os.getenv('CURSOR_CONFIG_DIR'):
        try:
            result = subprocess.run(['security', 'find-generic-password', '-s', 'cursor-access-token', '-a', 'cursor-user', '-w'], capture_output=True, text=True, timeout=5)
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        return json.loads((home(value) / 'auth.json').read_text()).get('accessToken')
    except (OSError, ValueError, AttributeError):
        return None


def usage_cache_path(value=None):
    import hashlib
    root = Path(os.getenv('AGENT_RUNS_DIR', str(Path.home() / '.agent-runs')))
    identity = str(home(value)) + ':' + os.getenv('CURSOR_AGENT_BIN', 'cursor-agent')
    return root / ('cursor-usage-' + hashlib.sha256(identity.encode()).hexdigest()[:16] + '.json')


def usage(value=None, force=False):
    path = usage_cache_path(value)
    saved = {}
    try:
        saved = json.loads(path.read_text())
        if not isinstance(saved, dict) or not isinstance(saved.get('value'), dict):
            saved = {}
    except (OSError, ValueError):
        pass
    previous = saved.get('value', {})
    try:
        if not force and 0 <= time.time() - float(saved['at']) < 300:
            return previous
    except (KeyError, ValueError, TypeError):
        pass

    def fetch():
        bearer = token(value)
        if not bearer:
            return {}, False
        data, successes = {}, 0
        methods = ('GetPlanInfo', 'GetCurrentPeriodUsage', 'GetUsageLimitStatusAndActiveGrants', 'GetHardLimit')
        for method in methods:
            try:
                request = urllib.request.Request('https://api2.cursor.sh/aiserver.v1.DashboardService/' + method, data=b'{}', headers={'Authorization': 'Bearer ' + bearer, 'Content-Type': 'application/json', 'Connect-Protocol-Version': '1', 'x-cursor-client-type': 'cli'})
                with urllib.request.urlopen(request, timeout=5) as response:
                    payload = json.load(response)
                if isinstance(payload, dict):
                    data.update(payload)
                    successes += 1
            except Exception:
                pass  # Never include an exception that might contain request credentials.
        return data, successes == len(methods)
    try:
        data, complete = fetch()
    except (OSError, ValueError, TypeError):
        return previous
    if not data:
        return previous  # Failed reads must not erase data or renew its TTL.
    result = data if complete else dict(previous, **data)
    # A partial refresh retains older fields, so its observation time must not
    # claim that those fields were refreshed. An expired snapshot stays expired.
    observed_at = time.time() if complete or not previous else saved.get('at')
    try:
        from godfather_safety import atomic
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic(path, dict(at=observed_at, value=result))
    except OSError:
        pass
    return result


def included(model, data):
    try:
        if float(data.get('planUsage', {}).get('totalPercentUsed', -1)) >= 100:
            return False
        grants = data.get('activeGrants') or []
        grants = [g for g in grants if not g.get('expiresAtMs') or float(g['expiresAtMs']) > time.time() * 1000]
        if grants and sum(float(g['remainingCents']) for g in grants) <= model.get('included_floor_cents', 500):
            return False
    except (ValueError, TypeError, KeyError):
        pass
    return True


def cycle_end(data, now=None):
    from godfather_errors import timestamp
    now = time.time() if now is None else now
    end = timestamp(data.get('billingCycleEnd'))
    start = timestamp(data.get('billingCycleStart'))
    if end is None or end == start or end <= now:
        end = timestamp((data.get('planInfo') or {}).get('billingCycleEnd'))
    return end


def describe(data):
    if not data:
        return 'usage unknown'
    plan = data.get('planInfo') or {}
    parts = ['plan=' + str(plan.get('planName', 'unknown'))]
    end = cycle_end(data)
    if end:
        from godfather_errors import timestamp
        import datetime
        stamp = timestamp(end)
        parts.append('cycle end=' + (datetime.datetime.fromtimestamp(stamp, datetime.timezone.utc).isoformat() if stamp else str(end)))
    pct = data.get('planUsage', {}).get('totalPercentUsed')
    if pct is not None:
        parts.append('plan used=' + str(pct) + '%')
    grants = data.get('activeGrants') or []
    try:
        grants = [g for g in grants if not g.get('expiresAtMs') or float(g['expiresAtMs']) > time.time()*1000]
        if grants:
            parts.append('grants=${:.2f}/${:.2f}'.format(sum(float(g['remainingCents']) for g in grants)/100, sum(float(g['totalCents']) for g in grants)/100))
        spend = data.get('spendLimitUsage') or {}
        for kind in ('overall', 'individual', 'pooled'):
            if kind + 'Used' in spend or kind + 'Limit' in spend:
                parts.append('on-demand ' + kind + '=${:.2f}/${:.2f}'.format(float(spend.get(kind+'Used', 0))/100, float(spend.get(kind+'Limit', 0))/100))
    except (TypeError, ValueError, KeyError):
        pass
    if pct is None and not grants:
        parts.append('included usage not reported')
    return '; '.join(parts)
