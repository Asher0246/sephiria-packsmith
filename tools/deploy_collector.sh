#!/bin/sh
# Run with sudo after copying build_collector.py alongside this file.
set -eu
SOURCE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
TARGET=/opt/packsmith-collector
if ! id packsmith-collector >/dev/null 2>&1; then
    useradd --system --home /var/lib/packsmith-collector --shell /usr/sbin/nologin packsmith-collector
fi
install -d -m 755 "$TARGET"
install -d -o packsmith-collector -g packsmith-collector -m 700 /var/lib/packsmith-collector
python3 -m venv "$TARGET/venv"
"$TARGET/venv/bin/pip" install --index-url https://pypi.org/simple gunicorn==26.2.0
install -m 644 "$SOURCE/build_collector.py" "$TARGET/build_collector.py"
cat > /etc/systemd/system/packsmith-collector.service <<'UNIT'
[Unit]
Description=Packsmith consented build intake
After=network.target

[Service]
User=packsmith-collector
Group=packsmith-collector
WorkingDirectory=/opt/packsmith-collector
Environment=PACKSMITH_COLLECTOR_DB=/var/lib/packsmith-collector/builds.sqlite3
ExecStart=/opt/packsmith-collector/venv/bin/gunicorn --bind 127.0.0.1:4081 --workers 1 --threads 4 --timeout 20 --limit-request-line 2048 --limit-request-fields 30 build_collector:application
Restart=on-failure
UMask=0077
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/packsmith-collector
PrivateTmp=true
MemoryMax=256M
TasksMax=32

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now packsmith-collector
systemctl restart packsmith-collector
python3 - <<'PY'
from pathlib import Path
import shutil
import subprocess
import time
path = Path('/etc/caddy/Caddyfile')
original = path.read_text()
if '127.0.0.1:4081' not in original:
    anchor = 'asher0627.site {'
    if original.count(anchor) != 1:
        raise SystemExit('Expected site block not found; Caddy untouched')
    updated = original.replace(anchor, anchor + '''
    handle /packsmith/* {
        request_body {
            max_size 2MB
        }
        reverse_proxy 127.0.0.1:4081
    }
''', 1)
    candidate = path.with_name('Caddyfile.packsmith-candidate')
    candidate.write_text(updated)
    subprocess.run(['caddy', 'validate', '--config', str(candidate), '--adapter', 'caddyfile'], check=True)
    backup = path.with_name('Caddyfile.before-packsmith-' + str(int(time.time())))
    shutil.copy2(path, backup)
    path.write_text(updated)
    try:
        subprocess.run(['systemctl', 'reload', 'caddy'], check=True)
    except Exception:
        shutil.copy2(backup, path)
        subprocess.run(['systemctl', 'reload', 'caddy'], check=True)
        raise
PY
systemctl --no-pager is-active packsmith-collector caddy
