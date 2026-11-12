"""Watcher synchronization for subprocess tests, including slow startup coverage."""
import os
import selectors
import subprocess
import sys
import time

import godfather_store as store


def launch(command, env, startup_delay=0):
    if startup_delay:
        command = [sys.executable, '-c',
                   'import os,sys,time; time.sleep(float(sys.argv[1])); os.execv(sys.argv[2],sys.argv[2:])',
                   str(startup_delay)] + command
    return subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def wait_for(process, predicate, description, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        if process.poll() is not None:
            output, error = process.communicate(timeout=5)
            raise AssertionError('watcher exited before %s: stdout=%r stderr=%r' % (description, output, error))
        time.sleep(.02)
    raise AssertionError('watcher did not reach %s within %ss' % (description, timeout))


def leased(root, process):
    def find():
        conn = store.connect(root)
        try:
            row = conn.execute('SELECT subscriber FROM watch_leases WHERE pid=?', (process.pid,)).fetchone()
            return row['subscriber'] if row else None
        finally:
            conn.close()
    return wait_for(process, find, 'its persisted watch lease')


def consumed(root, process, subscriber, seq):
    wait_for(process, lambda: store.open_cursor(root, subscriber) >= seq, 'event cursor %s' % seq)


def read_until(process, needle, timeout=10):
    output = b''
    deadline = time.monotonic() + timeout
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while time.monotonic() < deadline:
            if selector.select(.1):
                chunk = os.read(process.stdout.fileno(), 65536)
                output += chunk
                if needle in output:
                    return output
                if not chunk:
                    break
    raise AssertionError('watcher did not emit %r: stdout=%r exit=%r' % (needle, output, process.poll()))
