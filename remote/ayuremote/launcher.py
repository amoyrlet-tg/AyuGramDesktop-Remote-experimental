import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import uuid
from pathlib import Path


def ssh_args(host, port, key=None):
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.:-]{0,252}", host) or not 1 <= int(port) <= 65535:
        raise ValueError("invalid SSH host or port")
    result = ["ssh", "-p", str(port), "-o", "StrictHostKeyChecking=ask",
              "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=15",
              "-o", "ServerAliveCountMax=3", "-o", "ConnectTimeout=15",
              "-o", "ForwardAgent=no", "-o", "ForwardX11=no"]
    if key:
        path = Path(key).expanduser().resolve(strict=True)
        result += ["-i", str(path), "-o", "IdentitiesOnly=yes"]
    result += ["--", "root@" + host]
    return result


def archive_source(root, output):
    omitted = {".git", "out", "__pycache__", "state", "profiles", ".venv", "tdata", "node_modules"}
    def keep(info):
        if any(part in omitted for part in Path(info.name).parts):
            return None
        if info.name.endswith((".session", "/profile.json", "/token")):
            return None
        return info
    with tarfile.open(output, "w:gz") as archive:
        archive.add(root, arcname="source", filter=keep)


def atomic_json(path, value):
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix=".profile-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def provision(root, profile, host, port, key, api_id, api_hash, media_limit):
    if type(api_id) is not int or api_id <= 0 or not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
        raise ValueError("valid api_id and api_hash are required")
    if type(media_limit) is not int or not 0 <= media_limit <= 1024**4:
        raise ValueError("invalid media limit")
    profile = Path(profile).expanduser().resolve()
    if (profile / "profile.json").exists() or (profile / "tdata").exists():
        raise ValueError("choose a new, empty remote profile")
    profile.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(profile, 0o700)
    command = ssh_args(host, port, key)
    stage = "/opt/ayugram-remote/upload-" + uuid.uuid4().hex
    subprocess.run(command + ["test ! -e /opt/ayugram-remote/state/keys && install -d -m 700 " + stage], check=True)
    with tempfile.TemporaryDirectory(prefix="ayugram-upload-") as temporary:
        archive = Path(temporary) / "source.tar.gz"
        archive_source(root, archive)
        with archive.open("rb") as stream:
            subprocess.run(command + ["umask 077; tar -xzf - -C " + stage], stdin=stream, check=True)
    previous = "/opt/ayugram-remote/source.previous-" + uuid.uuid4().hex
    promote = ("set -eu; if test -e /opt/ayugram-remote/source; then "
               "test -f /opt/ayugram-remote/source/remote/ayuremote/__init__.py; "
               "mv /opt/ayugram-remote/source " + previous + "; fi; "
               "mv " + stage + "/source /opt/ayugram-remote/source; rmdir " + stage)
    subprocess.run(command + [promote], check=True)
    install = dict(api_id=api_id, api_hash=api_hash, media_limit=media_limit)
    subprocess.run(command + ["umask 077; cat > /opt/ayugram-remote/install.json"],
                   input=json.dumps(install).encode(), check=True)
    subprocess.run(command + ["bash /opt/ayugram-remote/source/remote/install-server.sh"], check=True)
    token = subprocess.run(command + ["cat /opt/ayugram-remote/state/token"],
                           check=True, stdout=subprocess.PIPE).stdout.decode().strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", token):
        raise ValueError("invalid pairing token")
    config = dict(host=host, port=int(port), key=str(Path(key).expanduser().resolve()) if key else "",
                  client=uuid.uuid4().hex, token=token, epoch="", socket="")
    atomic_json(profile / "profile.json", config)
    return profile


def pair_existing(profile, host, port, key):
    profile = Path(profile).expanduser().resolve()
    if (profile / "profile.json").exists() or (profile / "tdata").exists():
        raise ValueError("choose a new profile")
    command = ssh_args(host, port, key)
    token = subprocess.run(command + ["cat /opt/ayugram-remote/state/token"], check=True,
                           stdout=subprocess.PIPE).stdout.decode().strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", token):
        raise ValueError("invalid pairing token")
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(profile, 0o700)
    atomic_json(profile / "profile.json", dict(host=host, port=int(port), key=key or "",
                client=uuid.uuid4().hex, token=token, epoch="", socket=""))
    return profile


def update_settings(profile, media_limit):
    if type(media_limit) is not int or not 0 <= media_limit <= 1024**4:
        raise ValueError("invalid media cache limit")
    config = json.loads((Path(profile).expanduser() / "profile.json").read_text())
    command = ssh_args(config["host"], config["port"], config.get("key"))
    remote = ("docker exec ayugram-remote python3 -m ayuremote.admin "
              "--state /data --schema /opt/ayugram/schema "
              "--media-limit " + str(media_limit))
    subprocess.run(command + [remote], check=True)
    return "Лимит кэша на сервере обновлён."


def run_client(profile, binary, offline=False):
    os.umask(0o077)
    profile = Path(profile).expanduser().resolve(strict=True)
    binary = Path(binary).expanduser().resolve(strict=True)
    if not os.access(binary, os.X_OK):
        raise ValueError("client binary is not executable")
    if not shutil.which("bwrap"):
        raise RuntimeError("install bubblewrap on Arch Linux; remote mode requires network isolation")
    lock = (profile / "launcher.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    config = json.loads((profile / "profile.json").read_text())
    runtime = tempfile.TemporaryDirectory(prefix="ayu-remote-", dir=os.environ.get("XDG_RUNTIME_DIR"))
    socket_path = Path(runtime.name) / "rpc.sock"
    config["socket"] = str(socket_path)
    atomic_json(profile / "profile.json", config)
    ssh = None
    client = None
    try:
        if not offline:
            command = ssh_args(config["host"], config["port"], config.get("key"))
            split = command.index("--")
            command[split:split] = ["-N", "-L", f"{socket_path}:127.0.0.1:17555", "-o", "StreamLocalBindUnlink=no"]
            ssh = subprocess.Popen(command)
            for attempt in range(300):
                if ssh.poll() is not None:
                    raise RuntimeError("SSH tunnel failed")
                if socket_path.exists():
                    os.chmod(socket_path, 0o600)
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError("SSH tunnel timeout")
        environment = os.environ.copy()
        environment["AYUGRAM_REMOTE_CONFIG"] = str(profile / "profile.json")
        arguments = ["bwrap", "--unshare-net", "--die-with-parent", "--bind", "/", "/",
                     "--", str(binary), "-many", "-workdir", str(profile)]
        client = subprocess.Popen(arguments, env=environment, cwd=profile)
        retry_at, delay = 0.0, 1.0
        while client.poll() is None:
            if not offline and ssh.poll() is not None and time.monotonic() >= retry_at:
                socket_path.unlink(missing_ok=True)
                ssh = subprocess.Popen(command)
                retry_at = time.monotonic() + delay
                delay = min(30.0, delay * 2)
            time.sleep(0.25)
        return client.returncode
    finally:
        if client and client.poll() is None:
            client.terminate()
            client.wait()
        if ssh and ssh.poll() is None:
            ssh.terminate()
            try:
                ssh.wait(timeout=10)
            except subprocess.TimeoutExpired:
                ssh.kill()
                ssh.wait()
        runtime.cleanup()
        lock.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True)
    parser.add_argument("--binary", required=True)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    raise SystemExit(run_client(args.profile, args.binary, args.offline))


if __name__ == "__main__":
    main()
