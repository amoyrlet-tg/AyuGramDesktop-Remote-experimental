import argparse
import asyncio
import json
import uuid
from pathlib import Path

from .protocol import MAX_FRAME, VERSION, ProtocolError, Schema, encode, read


async def configure(state, schema, media_limit, port=17555):
    if type(media_limit) is not int or not 0 <= media_limit <= 1024**4:
        raise ValueError("invalid media cache limit")
    token = (Path(state) / "token").read_text().strip()
    reader, writer = await asyncio.open_connection("127.0.0.1", port, limit=MAX_FRAME)
    try:
        writer.write(encode({"type": "hello", "version": VERSION, "schema": schema.fingerprint,
                             "token": token, "client": "admin-" + uuid.uuid4().hex,
                             "epoch": "", "after": "0", "archive_after": "0"}))
        await writer.drain()
        hello = await asyncio.wait_for(read(reader), 10)
        if hello.get("type") != "hello" or hello.get("schema") != schema.fingerprint:
            raise ProtocolError("server handshake failed")
        writer.write(encode({"type": "settings", "media_limit_bytes": media_limit}))
        await writer.drain()
        async def result():
            while True:
                frame = await read(reader)
                if frame["type"] == "settings":
                    return frame
        return await asyncio.wait_for(result(), 30)
    finally:
        writer.close()
        await writer.wait_closed()


def main():
    parser = argparse.ArgumentParser(description="Change the local AyuGram Remote server cache limit")
    parser.add_argument("--state", required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--media-limit", required=True, type=int)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(configure(args.state, Schema(args.schema), args.media_limit))))


if __name__ == "__main__":
    main()
