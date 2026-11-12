"""Standard-library live terminal dashboard."""
import json
import os
import shlex
import subprocess
import sys
import time

import agent_oversight as a
from godfather_mcp import background, cancel_run, events, named_base


def row_text(run):
    tokens = '/'.join('?' if run.get(key) is None else str(run[key]) for key in ('tokens_in', 'tokens_cached', 'tokens_out'))
    from godfather_store import TERMINAL
    pending = None if run['status'] in TERMINAL else next((q for q in run.get('questions', []) if q.get('answer') is None), None)
    status = 'NEEDS_INPUT' if pending else run['status']
    action = pending['question'] if pending else run.get('last_action', '')
    return '{}  {}  {}  {}  {}  idle={}s  {}  {}'.format(
        run['name'], a.display_model(run), run['cli'], status, run['elapsed'],
        run['seconds_since_progress'], tokens, a.compact(action, 120)), bool(pending)


def recent_events():
    return events()['events'][-6:]


def plain(once=False):
    try:
        while True:
            rows = a.snapshot(a.runs_dir())
            print('\nGodfather {} | NAME MODEL CLI STATUS ELAPSED IDLE TOKENS(in/cached/out) LAST ACTION'.format(a.now()), flush=True)
            for run in rows:
                text, pending = row_text(run)
                print(('! ' if pending else '  ') + text, flush=True)
            for event in recent_events():
                print('{} {} {}'.format(event['kind'], event['name'], a.compact(event.get('detail', ''), 140)), flush=True)
            if once:
                return 0
            time.sleep(2)
    except KeyboardInterrupt:
        return 0


def interactive(screen):
    import curses
    curses.curs_set(0)
    screen.timeout(100)
    selected, refreshed, rows, recent = 0, 0, [], []
    notice = ''
    def put(y, text, style=0):
        height, width = screen.getmaxyx()
        if y < height:
            try:
                screen.addnstr(y, 0, text, max(0, width - 1), style)
            except curses.error:
                pass
    def prompt(text):
        screen.timeout(-1)
        height, width = screen.getmaxyx()
        screen.move(max(0, height - 1), 0)
        screen.clrtoeol()
        put(height - 1, text)
        curses.echo()
        curses.curs_set(1)
        try:
            return screen.getstr(height - 1, min(len(text), max(0, width - 2)), 4096).decode(errors='replace')
        finally:
            curses.noecho()
            curses.curs_set(0)
            screen.timeout(100)
    while True:
        if time.monotonic() - refreshed >= 2:
            rows, recent = a.snapshot(a.runs_dir()), recent_events()
            selected = min(selected, max(0, len(rows) - 1))
            refreshed = time.monotonic()
        screen.erase()
        put(0, 'Godfather | q quit  ↑/↓ select  a answer  n nudge  k kill  l log', curses.A_BOLD)
        put(1, 'NAME MODEL CLI STATUS ELAPSED IDLE TOKENS(in/cached/out) LAST ACTION')
        height, width = screen.getmaxyx()
        capacity = max(1, height - 11)
        offset = max(0, selected - capacity + 1)
        for index, run in enumerate(rows[offset:offset + capacity], offset):
            text, pending = row_text(run)
            style = (curses.A_REVERSE if index == selected else 0) | (curses.A_BOLD if pending else 0)
            put(index - offset + 2, ('! ' if pending else '  ') + text, style)
        event_y = min(height - 8, capacity + 3)
        put(event_y, 'Recent events', curses.A_BOLD)
        for i, event in enumerate(recent):
            put(event_y + i + 1, '{} {} {}'.format(event['kind'], event['name'], a.compact(event.get('detail', ''), width)))
        put(height - 1, notice)
        screen.refresh()
        key = screen.getch()
        if key == ord('q'):
            return 0
        if key in (curses.KEY_DOWN, ord('j')):
            selected = min(selected + 1, max(0, len(rows) - 1))
        elif key in (curses.KEY_UP,):
            selected = max(0, selected - 1)
        elif rows and key in (ord('a'), ord('n'), ord('k'), ord('l')):
            run = rows[selected]
            try:
                if run['status'] == 'unregistered':
                    raise ValueError('Unregistered process: control is disabled.')
                base = named_base(run['name'])
                if key in (ord('a'), ord('n')):
                    if key == ord('a') and not any(q.get('answer') is None for q in run.get('questions', [])):
                        raise ValueError('No pending question for selected run.')
                    text = prompt('Answer: ' if key == ord('a') else 'Nudge: ')
                    if text:
                        command = 'answer' if key == ord('a') else 'nudge'
                        background([command, run['name'], text], base)
                        notice = command + ' submitted; see ' + str(base) + '.mcp.log'
                elif key == ord('k'):
                    if prompt('Kill {}? Type yes: '.format(run['name'])) == 'yes':
                        notice = json.dumps(cancel_run(run.get('router_name') or run['name']))
                else:
                    curses.def_prog_mode()
                    curses.endwin()
                    try:
                        subprocess.run(shlex.split(os.getenv('PAGER', 'less')) + [str(base) + '.log'])
                    finally:
                        curses.reset_prog_mode()
                        screen.timeout(100)
                        screen.clear()
                refreshed = 0
            except (ValueError, OSError) as exc:
                notice = str(exc)


def top_main(args):
    if args.once or not (sys.stdin.isatty() and sys.stdout.isatty()):
        return plain(args.once)
    import curses
    try:
        return curses.wrapper(interactive)
    except (curses.error, KeyboardInterrupt):
        return 0
