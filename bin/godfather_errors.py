"""Provider failure taxonomy. Structured error evidence outranks message heuristics."""
import datetime as dt
import json
import math
import re
import time

CATEGORIES = ('auth', 'quota', 'capacity', 'context_window', 'model_not_found', 'policy',
              'permission_denied', 'bad_request', 'network', 'broken_stream', 'internal', 'other')
RETRYABLE = {'auth', 'quota', 'capacity', 'network', 'broken_stream', 'internal'}
# Preserve the recovery policy's public vocabulary.
PUBLIC = {'permission_denied': 'tool_denied', 'network': 'crash', 'internal': 'crash'}
CODES = {
    'auth': ('unauthorized', 'authenticationfailed', 'invalidapikey', 'unauthenticated', 'authenticationerror'),
    'quota': ('usagelimitexceeded', 'ratelimitexceeded', 'ratelimit', 'ratelimiterror', 'resourceexhausted', 'insufficientquota', 'sessionbudgetexceeded', 'spendlimitexceeded', 'usagelimit', 'spendinglimitexceeded'),
    'capacity': ('serveroverloaded', 'overloadederror', 'overloaded', 'flexunavailable', 'policycapacityunavailable'),
    'context_window': ('contextwindowexceeded', 'contextlengthexceeded'),
    'model_not_found': ('modelnotfound',),
    'policy': ('policyviolation', 'contentpolicyviolation', 'safety', 'contentfilter', 'refusal', 'cyberpolicy', 'misalignmentpolicyviolation', 'toomanydenials'),
    'permission_denied': ('sandboxerror', 'permissiondenied', 'tooldenied', 'approvaldenied'),
    'bad_request': ('badrequest', 'invalidrequest', 'invalidargument'),
    'network': ('httpconnectionfailed', 'responsestreamconnectionfailed', 'responsestreamdisconnected',
                'responsetoomanyfailedattempts', 'connectionerror', 'timeout', 'etimedout', 'econnreset'),
    'broken_stream': ('brokenstream',), 'internal': ('internal', 'internalerror', 'servererror', 'internalservererror', 'threadrollbackfailed'),
}
PATTERNS = (
    ('permission_denied', r'permission denied|sandbox.{0,60}(?:denied|not permitted)|auto[- ]denied|(?:tool|command|approval)[^\n]{0,120}\bdenied\b'),
    ('capacity', r'\bat capacity\b|\boverload(?:ed)?\b|overloaded_error|server_overloaded'),
    ('context_window', r'context (?:window|length).{0,40}(?:exceed|limit|full)|maximum context length'),
    ('model_not_found', r'model.{0,50}(?:not found|does not exist)|model_not_found'),
    ('policy', r'content.?policy|policy (?:refusal|violation)|safety refusal'),
    ('auth', r'not logged (?:in|into)|unauthenticated|unauthorized|authentication (?:failed|required)|invalid.api.key|please (?:log|sign) in|\b401\b|\b403\b'),
    ('quota', r'\b402\b|\blimit reached\b|spend[ _-]*limit|rate[ _-]*limit|\b429\b|resource[ _-]*exhausted|insufficient_quota|(?:quota|usage.limit|credit.limit|five.hour.limit|5.hour.limit|weekly.limit).{0,80}(?:exceed|exhaust|reach|deplet)|(?:exhaust|exceed).{0,50}quota|(?:hit|reached|exceeded).{0,30}(?:usage|credit|five.hour|5.hour|weekly).{0,15}limit|out of (?:extra )?usage|\bquota\b.{0,100}(?:insufficient|unavailable)|(?:usage|credit|spending|request)[ _-]*limit.{0,80}(?:exceed|reach)|(?:insufficient|exhausted|depleted)\s+credits|(?:usage|credit|spending) (?:balance|allowance).{0,80}(?:exhaust|deplet)'),
    ('broken_stream', r'stream was interrupted|broken stream'),
    ('network', r'reconnecting|timed? out|timeout|connection (?:failed|reset|refused)|stream (?:drop|disconnect)'),
    ('bad_request', r'bad request|invalid request'),
    ('internal', r'internal server error|\b50[0234]\b'),
)


REGEX = {category: re.compile(pattern, re.I) for category, pattern in PATTERNS}

AGY_STARTUP = re.compile(r'failed to get cs path|Failed to resolve GeminiDir|failed to get git info for workspace|Failed to refresh cache in background', re.I)


def diagnostic_noise(cli, message):
    """Shared disposition for Agy discovery diagnostics that can recover silently."""
    if cli == 'agy':
        if AGY_STARTUP.search(str(message)):
            return 'startup'
        if re.search(r'you are not logged into Antigravity', str(message), re.I):
            return 'silent_auth'
    return None


def normalize(value):
    return re.sub(r'[^a-z0-9]', '', str(value).lower())


def timestamp(value):
    try:
        number = float(value)
        if math.isfinite(number) and number > 0:
            return number / 1000 if number > 1e12 else number
    except (ValueError, TypeError, OverflowError):
        try:
            return dt.datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
        except (ValueError, OverflowError, OSError):
            pass
    return None


def reset_at(value, now=None):
    """Only exhausted windows block; all blocking windows must reset."""
    now = time.time() if now is None else now
    found = []
    unknown = []
    def walk(obj):
        if not isinstance(obj, dict):
            return
        # Non-exhausted windows must not extend a quota wait.
        used = obj.get('usedPercent', obj.get('used_percent'))
        if used is not None:
            try:
                if float(used) < 100:
                    return
            except (TypeError, ValueError):
                return
            if not any(timestamp(obj.get(key)) for key in ('resets_at', 'resetsAt', 'resetAt')):
                unknown.append(True)
        for key, item in obj.items():
            if key in ('resets_at', 'resetsAt', 'resetAt'):
                stamp = timestamp(item)
                if stamp and stamp > now:
                    found.append(stamp)
            elif isinstance(item, dict):
                walk(item)
            elif isinstance(item, list):
                for child in item:
                    walk(child)
    walk(value)
    if unknown:
        return None
    if found:
        return max(found)
    if isinstance(value, str):
        # Shared existing parser also handles local clocks, dates and durations.
        from godfather_router import reset_time
        return reset_time(value, now, default=False)
    return None


def classify(cli, event_or_text):
    raw = event_or_text
    if isinstance(raw, str):
        try:
            obj = json.loads(raw)
        except ValueError:
            obj = None
    else:
        obj = raw
    codes, statuses, messages = [], [], []
    generic = {'error', 'errormessage', 'failed', 'failure', 'turnfailed', 'streamerror', 'done', 'eventmsg', 'errornotification', 'result'}
    def walk(node):
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            if key == 'codexErrorInfo':
                codes.extend(value.keys() if isinstance(value, dict) else [value])
                if isinstance(value, dict):
                    for detail in value.values():
                        walk(detail)
            elif key in ('code', 'status', 'type', 'errorType') and isinstance(value, (str, int)):
                if str(value).isdigit() and 400 <= int(value) <= 599:
                    statuses.append(int(value))
                elif normalize(value) not in generic:
                    codes.append(value)
            elif key in ('httpStatusCode', 'statusCode', 'status_code', 'http_status', 'http_status_code'):
                try:
                    status = int(value)
                    if 400 <= status <= 599:
                        statuses.append(status)
                except (TypeError, ValueError):
                    pass
            if key in ('message', 'content', 'reason', 'result') and isinstance(value, str):
                messages.append(value)
                # Grok embeds a provider JSON error after this literal wrapper.
                embedded = re.fullmatch(r'(?:Internal|API|Provider) error:\s*(\{.*\})', value, re.I | re.S)
                if embedded:
                    try:
                        walk(json.loads(embedded[1]))
                    except ValueError:
                        pass
            if key in ('error', 'errors', 'params', 'payload', 'data', 'cause'):
                if isinstance(value, dict):
                    walk(value)
                elif isinstance(value, list):
                    for child in value:
                        walk(child)
                elif isinstance(value, str):
                    messages.append(value)
    walk(obj)
    category = None
    # Semantic codes outrank HTTP statuses (sandbox errors can contain 403).
    for candidate, names in CODES.items():
        if any(normalize(code) in names for code in codes):
            category = candidate
            break
    if (category is None or category == 'network') and statuses:
        status = statuses[-1]
        category = ('auth' if status in (401, 403) else 'quota' if status in (402, 429) else
                    'capacity' if status in (503, 529) else 'internal' if status >= 500 else 'bad_request')
    # Unknown explicit error codes remain structured other; generic event envelopes are not codes.
    structured = category is not None or bool(codes)
    if category is None and structured:
        category = 'other'
    text = '\n'.join(messages) if isinstance(obj, dict) else str(raw or '')
    if category is None:
        category = next((cat for cat, pattern in PATTERNS if re.search(pattern, text, re.I)), 'other')
    # A text tool denial cannot become auth from an incidental HTTP-looking number.
    if category == 'auth' and re.search(PATTERNS[0][1], text, re.I):
        category = 'permission_denied'
    transient = cli == 'codex' and (not isinstance(obj, dict) or obj.get('type') != 'turn.failed') and (
        isinstance(obj, dict) and obj.get('willRetry') is True or
        bool(re.search(r'Reconnecting\.\.\.\s*\d+/\d+', text, re.I)))
    return dict(category=category, source='structured' if structured else 'text',
                retryable=category in RETRYABLE, evidence=raw, resets_at=reset_at(obj) or reset_at(text),
                transient=transient)


def public_class(failure):
    category = failure['category']
    return PUBLIC.get(category, category)
