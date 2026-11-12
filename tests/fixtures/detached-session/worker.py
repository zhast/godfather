"""Test-only private session with a reparented, TERM-resistant process group."""
import json
import os
from pathlib import Path
import signal
import sys
import time

marker = Path(sys.argv[1])
child = os.fork()
if child == 0:
    grandchild = os.fork()
    if grandchild:
        os._exit(0)
    os.setpgid(0, 0)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    marker.write_text(json.dumps(dict(pid=os.getpid(), sid=os.getsid(0), pgid=os.getpgrp())))
    while True:
        time.sleep(.1)
os.waitpid(child, 0)
while not marker.with_suffix('.release').exists():
    time.sleep(.02)
