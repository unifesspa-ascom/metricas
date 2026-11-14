"""Run Python contracts with socket access denied, including Python children.

No dependency installation or collector CLI invocation. Run with python -B.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
BLOCK = '''import socket
def denied(*args, **kwargs):
    raise AssertionError("offline suite: network forbidden")
socket.socket.connect = denied
socket.socket.connect_ex = denied
socket.create_connection = denied
socket.getaddrinfo = denied
socket.socket.sendto = denied
'''


def main():
    with tempfile.TemporaryDirectory(prefix='account-offline-') as directory:
        Path(directory, 'sitecustomize.py').write_text(BLOCK, encoding='utf-8')
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1',
                   PYTHONPATH=directory + os.pathsep + str(ROOT / 'scripts'))
        # Tests must never inherit actual credentials.
        for key in ('FB_PAGE_ACCESS_TOKEN', 'FB_PAGE_ID'):
            env.pop(key, None)
        return subprocess.run([sys.executable, '-B', '-m', 'unittest', 'discover',
                               '-s', 'tests', '-p', 'test_*.py'], cwd=ROOT, env=env).returncode


if __name__ == '__main__':
    sys.exit(main())
