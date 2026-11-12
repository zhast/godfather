"""Extract only a terminal Cursor result, preserving errors for router fallback."""
import json
from pathlib import Path
import sys


def extract(base, stderr_path=None, rc=0):
    events = []
    try:
        for line in Path(base + '.stream.jsonl').read_text(errors='replace').splitlines():
            try:
                event = json.loads(line)
                if isinstance(event, dict):
                    events.append(event)
            except ValueError:
                pass
    except OSError:
        pass
    session = next((e.get('session_id') or e.get('chat_id') or e.get('chatId') for e in events if e.get('session_id') or e.get('chat_id') or e.get('chatId')), None)
    if session:
        with Path(base + '.models.log').open('a') as log:
            log.write('session=' + str(session) + '\n')
    result = next((e for e in reversed(events) if e.get('type') == 'result'), {})
    errors = [e for e in events if e.get('type') == 'error' or e.get('is_error')]
    stderr = Path(stderr_path).read_text(errors='replace') if stderr_path else ''
    failed = rc != 0 or errors or result.get('subtype') != 'success' or result.get('is_error') or not isinstance(result.get('result'), str)
    if failed:
        # Native print failures often have only stderr and exit 1, no result.
        # Preserve that evidence; a synthetic Error line must never outrank it.
        evidence = errors + ([stderr] if stderr.strip() else [])
        if result and result.get('subtype') != 'success' and result not in evidence:
            evidence.append(result)
        Path(base + '.json').write_text(json.dumps(dict(is_error=True, errors=evidence)) + '\n')
        from godfather_errors import classify
        return 10 if any(classify('cursor', error)['category'] == 'quota' for error in evidence) else 1
    Path(base + '.json').write_text(json.dumps(result) + '\n')
    Path(base + '.last.md').write_text(result['result'] + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(extract(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None, int(sys.argv[3]) if len(sys.argv) > 3 else 0))
