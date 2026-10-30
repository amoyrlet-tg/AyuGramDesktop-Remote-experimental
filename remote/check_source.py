#!/usr/bin/env python3
import hashlib
import re
from pathlib import Path

root = Path(__file__).resolve().parent.parent
source = root / "Telegram/SourceFiles"
schema = source / "mtproto/scheme"
expected = hashlib.sha256((schema / "api.tl").read_bytes() + b"\0" + (schema / "mtproto.tl").read_bytes()).hexdigest()
header = source / "ayu/remote/schema_fingerprint.h"
assert expected in header.read_text(), "schema fingerprint is stale"
constructors = set(re.findall(r"^([\w.]+)#[\da-f]+", (schema / "api.tl").read_text(), re.M))
constructors |= set(re.findall(r"^([\w.]+)#[\da-f]+", (schema / "mtproto.tl").read_text(), re.M))
constructors = {x.replace('.', '_') for x in constructors}
for file in (source / "ayu/remote").glob("*.cpp"):
    for name in re.findall(r"\bmtpc_(\w+)", file.read_text()):
        assert name in constructors, f"unknown constructor {name} in {file.name}"
for file in [source / "mtproto/mtp_instance.cpp", source / "mtproto/session.cpp"]:
    assert not re.search(r"Core::App\(|Lang::|Local::", file.read_text()), f"GUI dependency in {file.name}"
print("Schema fingerprint, referenced TL constructors, and runtime boundary checks passed.")
