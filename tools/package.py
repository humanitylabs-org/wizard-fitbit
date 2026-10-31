"""Package: run `wam package-inline .`, then apply this app's runtime specifics (port, private data dir, pinned base
image) and compress the inline payload. Run from the repo root:  python3 tools/package.py   (verify: --check)"""
import base64
import json
import re
import subprocess
import sys
import zlib
from pathlib import Path

root = Path(__file__).resolve().parent.parent
APP = 'wizard-fitbit'
PAT = re.compile(r"json\.loads\(base64\.b64decode\('([A-Za-z0-9+/=]+)'\)\)")
ZPAT = re.compile(r"json\.loads\(zlib\.decompress\(base64\.b64decode\('([A-Za-z0-9+/=]+)'\)\)\)")

if '--check' in sys.argv:
    import yaml
    compose = yaml.safe_load((root / 'runtipi/docker-compose.yml').read_text())
    command = compose['services'][APP]['command'][-1]
    assert len(command.encode()) < 120000, 'inline runtime argument exceeds safe size'
    m = ZPAT.search(command)
    assert m, 'compressed inline payload missing'
    files = json.loads(zlib.decompress(base64.b64decode(m.group(1))))
    for name, text in files.items():
        assert (root / name).read_text() == text, f'stale packaged source: {name}'
    assert 'app/server.py' in files and 'web/app.js' in files
    compile(command, '<inline>', 'exec')
    print(f'PASS: {len(files)} embedded runtime files match source; argument {len(command.encode())} bytes')
    raise SystemExit(0)

subprocess.run(['wam', 'package-inline', str(root)], check=True, stdout=subprocess.DEVNULL)
p = root / 'runtipi' / 'docker-compose.yml'
t = p.read_text()
m = PAT.search(t)
if not m:
    raise SystemExit('package template changed: inline payload not found')
files = json.loads(base64.b64decode(m.group(1)))
blob = base64.b64encode(zlib.compress(json.dumps(files).encode(), 9)).decode()
t = t[:m.start()] + f"json.loads(zlib.decompress(base64.b64decode('{blob}')))" + t[m.end():]
swaps = [
    ('import base64,json,pathlib,runpy', 'import base64,json,pathlib,runpy,zlib'),
    ('    image: python:3.12-alpine\n',
     '    image: python:3.12-alpine@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4\n    pids_limit: 64\n'),
    ("      PORT: '8799'\n", "      PORT: '8830'\n      BIND: 0.0.0.0\n      PYTHONDONTWRITEBYTECODE: '1'\n"),
    ('      DATA_DIR: /data\n', '      DATA_DIR: /data/private\n'),
]
for old, new in swaps:
    if t.count(old) != 1:
        raise SystemExit(f'package template changed; expected one {old.strip()!r}, found {t.count(old)}')
    t = t.replace(old, new)
t = t.replace('internal_port: 8799', 'internal_port: 8830')
assert '/data/private' in t and '@sha256:' in t and '8799' not in t.replace(blob, ''), 'port/data swaps incomplete'
assert max(map(len, t.splitlines())) < 120000, 'inline payload too large'
p.write_text(t)
print('packaged', p.relative_to(root), f'({len(files)} files, {len(blob)} b64 bytes)')
