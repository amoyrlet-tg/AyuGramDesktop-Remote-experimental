#!/usr/bin/env bash
set -euo pipefail
umask 077

if [ "$(id -u)" != 0 ]; then
    echo 'The VPS installer currently requires a root SSH account.' >&2
    exit 1
fi
. /etc/os-release
case "$ID" in
    debian|ubuntu) ;;
    *) echo 'Automatic Docker setup supports Debian and Ubuntu VPS hosts.' >&2; exit 1 ;;
esac
if [ -e /opt/ayugram-remote/state/keys ]; then
    echo 'An existing server session was found. Refusing to replace it.' >&2
    exit 1
fi
if ! command -v docker >/dev/null; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends docker.io ca-certificates
fi
systemctl enable --now docker
if docker container inspect ayugram-remote >/dev/null 2>&1; then
    echo 'The ayugram-remote container already exists. Reuse the existing profile.' >&2
    exit 1
fi
cd /opt/ayugram-remote/source
python3 - <<'PY'
import json, re
from pathlib import Path
config = json.loads(Path('/opt/ayugram-remote/install.json').read_text())
assert isinstance(config['api_id'], int) and config['api_id'] > 0
assert re.fullmatch('[a-fA-F0-9]{32}', config['api_hash'])
assert isinstance(config['media_limit'], int) and 0 <= config['media_limit'] <= 1024**4
PY
API_ID=$(python3 -c 'import json; print(json.load(open("/opt/ayugram-remote/install.json"))["api_id"])')
API_HASH=$(python3 -c 'import json; print(json.load(open("/opt/ayugram-remote/install.json"))["api_hash"])')
MEDIA_LIMIT=$(python3 -c 'import json; print(json.load(open("/opt/ayugram-remote/install.json"))["media_limit"])')
docker build -f remote/Dockerfile --build-arg API_ID="$API_ID" \
    --build-arg API_HASH="$API_HASH" --build-arg BUILD_JOBS=2 \
    -t ayugram-remote:local .
install -d -m 700 -o 10000 -g 10000 /opt/ayugram-remote/state
if [ ! -f /opt/ayugram-remote/state/token ]; then
    python3 - <<'PY'
import os, secrets
path = '/opt/ayugram-remote/state/token'
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as stream:
    stream.write(secrets.token_urlsafe(32) + '\n')
os.chown(path, 10000, 10000)
PY
fi
docker run -d --name ayugram-remote --restart unless-stopped \
    --network host --read-only --cap-drop ALL --security-opt no-new-privileges \
    --pids-limit 512 --tmpfs /tmp:rw,noexec,nosuid,size=128m \
    --mount type=bind,src=/opt/ayugram-remote/state,dst=/data \
    ayugram-remote:local --media-limit "$MEDIA_LIMIT"
python3 - <<'PY'
import json, pathlib, socket, time
token = pathlib.Path('/opt/ayugram-remote/state/token').read_text().strip()
root = pathlib.Path('/opt/ayugram-remote/source/Telegram/SourceFiles/mtproto/scheme')
import hashlib
schema = hashlib.sha256((root/'api.tl').read_bytes()+b'\0'+(root/'mtproto.tl').read_bytes()).hexdigest()
for attempt in range(60):
    try:
        with socket.create_connection(('127.0.0.1',17555),timeout=2) as s:
            frame = dict(type='hello',version=1,token=token,client='installer',schema=schema,after='0',epoch='')
            s.sendall(json.dumps(frame).encode()+b'\n')
            response = json.loads(s.makefile('rb').readline(65536))
            if response.get('type') == 'hello' and response.get('schema') == schema:
                break
    except (OSError,ValueError):
        time.sleep(1)
else:
    raise SystemExit('Container did not become ready; inspect docker logs ayugram-remote.')
print('AyuGram Remote server is ready.')
PY
