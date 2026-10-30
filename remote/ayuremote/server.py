import argparse
import asyncio
import contextlib
import fcntl
import hmac
import json
import os
import signal
import stat
import struct
import uuid
from collections import OrderedDict
from pathlib import Path

from .protocol import (BLOCKED_METHODS, GHOST_METHODS, MAX_ENGINE_FRAME, MAX_FRAME, READ_CACHE, VERSION,
                       ProtocolError, Schema, counter, encode, identifier, payload,
                       read, request_digest, rpc_error, sensitive)
from .store import Store


class NativeEngine:
    def __init__(self, binary, state, schema):
        self.binary, self.state, self.schema = binary, state, schema
        self.process = None
        self.pending = {}
        self.update = None
        self.reader_task = None
        self.ready = False

    async def start(self, checkpoint=None):
        self.process = await asyncio.create_subprocess_exec(
            self.binary, "--state", str(self.state), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=None, limit=MAX_ENGINE_FRAME)
        hello = await asyncio.wait_for(read(self.process.stdout), 30)
        if hello != {"type": "ready", "version": VERSION, "schema": self.schema.fingerprint}:
            await self.close()
            raise RuntimeError("native engine protocol/schema mismatch")
        self.ready = True
        await self.control({"type": "sync", "state": checkpoint or {}})
        self.reader_task = asyncio.create_task(self.receive())

    async def control(self, frame):
        self.process.stdin.write(encode(frame))
        await self.process.stdin.drain()

    async def receive(self):
        try:
            while True:
                frame = await read(self.process.stdout, MAX_ENGINE_FRAME)
                if frame["type"] == "update":
                    payload(frame.get("body"))
                    await self.update(frame)
                elif frame["type"] == "result":
                    payload(frame.get("body"))
                    future = self.pending.pop(frame.get("id"), None)
                    if future and not future.done():
                        future.set_result(frame)
                else:
                    raise ProtocolError("unexpected native engine frame")
        except (EOFError, ProtocolError, ConnectionError):
            pass
        finally:
            self.ready = False
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("native engine stopped"))
            self.pending.clear()

    async def rpc(self, body, dc):
        if not self.ready:
            raise ConnectionError("native engine unavailable")
        key = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[key] = future
        try:
            self.process.stdin.write(encode({"type": "rpc", "id": key, "body": body, "dc": dc}))
            await self.process.stdin.drain()
            return await asyncio.wait_for(asyncio.shield(future), 180)
        finally:
            self.pending.pop(key, None)
            if not future.done():
                future.cancel()
                if self.ready:
                    with contextlib.suppress(ConnectionError, asyncio.TimeoutError):
                        await asyncio.wait_for(self.control({"type": "cancel", "id": key}), 2)

    async def close(self):
        self.ready = False
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 10)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self.reader_task:
            self.reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reader_task


class Broker:
    def __init__(self, store, engine, token, schema, ghost=True):
        self.store, self.engine, self.token, self.schema = store, engine, token, schema
        self.ghost = ghost
        self.wake = asyncio.Event()
        self.inflight = {}
        self.clients = set()
        self.connections = set()
        self.completed = OrderedDict()
        self.slots = asyncio.Semaphore(64)
        self.engine.update = self.on_update

    async def on_update(self, frame):
        checkpoint = frame.get("checkpoint")
        if frame.get("notify", True):
            self.store.append_update(frame["body"], frame.get("messages", ()), frame.get("deleted", ()), checkpoint)
        elif checkpoint is not None:
            with self.store.db:
                self.store.db.execute("INSERT OR REPLACE INTO settings VALUES('sync',?)", (json.dumps(checkpoint),))
        if frame.get("gap"):
            with self.store.db:
                self.store.db.execute("INSERT OR REPLACE INTO settings VALUES('sync_gap','1')")
        if checkpoint is not None:
            await self.engine.control({"type": "commit", "state": checkpoint})
        self.wake.set()

    async def execute(self, client, frame):
        request_id = identifier(frame.get("id"))
        data = payload(frame.get("body"))
        dc = frame.get("dc", 0)
        if type(dc) is not int or dc < 0 or dc > 1_000_000:
            raise ProtocolError("invalid DC")
        after = frame.get("after", "")
        if after:
            identifier(after)
            if after == request_id:
                raise ProtocolError("request cannot depend on itself")
        elif not isinstance(after, str):
            raise ProtocolError("invalid dependency")
        method = self.schema.method(data)
        digest = request_digest(dc, data, after)
        key = (client, request_id)
        if key in self.inflight:
            old_digest, task = self.inflight[key]
            if digest != old_digest:
                raise ProtocolError("request id conflict")
            return await asyncio.shield(task)
        if len(self.inflight) >= 256:
            return self.error(request_id, "REMOTE_BUSY")
        task = asyncio.create_task(self.perform(client, request_id, frame, data, dc, after, method, digest))
        self.inflight[key] = (digest, task)
        try:
            response = await asyncio.shield(task)
            self.completed[key] = not payload(response["body"]).startswith(b"\x19\xca\x44\x21")
            while len(self.completed) > 2048:
                self.completed.popitem(last=False)
            return response
        finally:
            if task.done():
                self.inflight.pop(key, None)
            else:
                task.add_done_callback(lambda completed: self.inflight.pop(key, None))

    @staticmethod
    def error(request_id, message):
        return {"type": "result", "id": request_id, "body": rpc_error(400, message)}

    async def perform(self, client, request_id, frame, data, dc, after, method, digest):
        account = self.store.db.execute("SELECT value FROM settings WHERE key='account'").fetchone()
        if account and method in {"auth.sendCode", "auth.signIn", "auth.checkPassword"}:
            return self.error(request_id, "REMOTE_ACCOUNT_ALREADY_BOUND")
        if method in BLOCKED_METHODS or method.startswith("invoke"):
            return self.error(request_id, "REMOTE_METHOD_DISABLED")
        online = method == "account.updateStatus" and (len(data) != 8 or struct.unpack_from("<I", data, 4)[0] != 0x997275b5)
        if self.ghost and (method in GHOST_METHODS or online):
            return self.error(request_id, "REMOTE_GHOST_BLOCKED")
        if after:
            dependency = self.inflight.get((client, after))
            if dependency:
                previous = await asyncio.shield(dependency[1])
                if payload(previous["body"]).startswith(b"\x19\xca\x44\x21"):
                    return self.error(request_id, "REMOTE_DEPENDENCY_FAILED")
            elif self.completed.get((client, after)):
                pass
            else:
                return self.error(request_id, "REMOTE_DEPENDENCY_UNKNOWN")
        durable = method not in READ_CACHE and not sensitive(method)
        if durable:
            state, response = self.store.begin_request(client, request_id, digest)
            if state == "done":
                return response
            if state == "unknown":
                return self.error(request_id, "REMOTE_OUTCOME_UNKNOWN")
        cache_key = request_digest(dc, data, "")
        if not self.engine.ready:
            cached = self.store.cached(cache_key) if method in READ_CACHE else None
            return {"type": "result", "id": request_id, "body": cached} if cached else self.error(request_id, "REMOTE_ENGINE_UNAVAILABLE")
        async with self.slots:
            try:
                result = await self.engine.rpc(frame["body"], dc)
            except (ConnectionError, asyncio.TimeoutError):
                return self.error(request_id, "REMOTE_OUTCOME_UNKNOWN")
        if result.get("account"):
            if account and account[0] != result["account"]:
                self.engine.ready = False
                return self.error(request_id, "REMOTE_ACCOUNT_MISMATCH")
            with self.store.db:
                self.store.db.execute("INSERT OR REPLACE INTO settings VALUES('account',?)", (result["account"],))
        self.store.archive(result.get("messages", ()), result.get("deleted", ()))
        self.wake.set()
        response = {"type": "result", "id": request_id, "body": result["body"]}
        if durable:
            self.store.finish_request(client, request_id, response)
        self.store.cache(cache_key, method, result["body"])
        return response

    async def connection(self, reader, writer):
        connection_task = asyncio.current_task()
        self.connections.add(connection_task)
        client = None
        outbound = asyncio.Queue(maxsize=32)
        tasks = set()
        try:
            hello = await asyncio.wait_for(read(reader), 10)
            supplied = hello.get("token", "")
            if (hello["type"] != "hello" or type(supplied) is not str
                    or len(supplied) > 128 or not hmac.compare_digest(supplied.encode(), self.token.encode())
                    or hello.get("version") != VERSION or hello.get("schema") != self.schema.fingerprint):
                raise ProtocolError("handshake rejected")
            candidate = identifier(hello.get("client"))
            if candidate in self.clients:
                raise ProtocolError("profile already connected")
            if hello.get("epoch", "") not in ("", self.store.epoch):
                raise ProtocolError("server identity changed; use a new local profile")
            after = counter(hello.get("after", "0"))
            archive_after = counter(hello.get("archive_after", "0"))
            if after > self.store.latest_seq():
                raise ProtocolError("cursor ahead of server")
            client = candidate
            self.clients.add(client)
            writer.write(encode({"type": "hello", "version": VERSION, "epoch": self.store.epoch,
                                 "schema": self.schema.fingerprint, "seq": str(self.store.latest_seq())}))
            await writer.drain()

            async def send_loop():
                while True:
                    writer.write(encode(await outbound.get()))
                    await asyncio.wait_for(writer.drain(), 30)

            async def events_loop():
                cursor = after
                archive_cursor = archive_after
                while True:
                    self.wake.clear()
                    batch = self.store.updates(cursor, 32)
                    for event in batch:
                        await outbound.put(event)
                        cursor = int(event["seq"])
                    archive_batch = self.store.history_events(archive_cursor)
                    for event in archive_batch:
                        await outbound.put(event)
                        archive_cursor = int(event["seq"])
                    if not batch and not archive_batch:
                        try:
                            await asyncio.wait_for(self.wake.wait(), 2)
                        except asyncio.TimeoutError:
                            pass

            rpc_tasks = set()

            async def respond(frame):
                try:
                    await outbound.put(await self.execute(client, frame))
                except (ValueError, ProtocolError):
                    writer.close()

            async def command_loop():
                while True:
                    frame = await asyncio.wait_for(read(reader), 300)
                    if frame["type"] == "rpc":
                        if len(rpc_tasks) >= 64:
                            raise ProtocolError("too many requests")
                        task = asyncio.create_task(respond(frame))
                        rpc_tasks.add(task)
                        task.add_done_callback(rpc_tasks.discard)
                    elif frame["type"] == "ping":
                        await outbound.put({"type": "pong"})
                    elif frame["type"] == "history":
                        peer = frame.get("peer")
                        mid = frame.get("message_id")
                        if not isinstance(peer, str) or len(peer) > 64 or type(mid) is not int:
                            raise ProtocolError("invalid history key")
                        await outbound.put({"type": "history", "id": identifier(frame.get("id")),
                                            **self.store.history(peer, mid)})
                    elif frame["type"] == "settings":
                        value = frame.get("media_limit_bytes")
                        if type(value) is not int or not 0 <= value <= 1024**4:
                            raise ProtocolError("invalid cache limit")
                        self.store.media_limit = value
                        self.store.evict()
                        with self.store.db:
                            self.store.db.execute("INSERT OR REPLACE INTO settings VALUES('media_limit',?)", (str(value),))
                        await outbound.put({"type": "settings", "media_limit_bytes": value})
                    else:
                        raise ProtocolError("unsupported command")

            tasks = {asyncio.create_task(send_loop()), asyncio.create_task(events_loop()), asyncio.create_task(command_loop())}
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (EOFError, ProtocolError, ConnectionError, asyncio.TimeoutError, ValueError):
            pass
        finally:
            if "rpc_tasks" in locals():
                for task in rpc_tasks:
                    task.cancel()
                await asyncio.gather(*rpc_tasks, return_exceptions=True)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if client:
                self.clients.discard(client)
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
            self.connections.discard(connection_task)


async def serve(args):
    os.umask(0o077)
    state = Path(args.state).resolve()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = (state / "server.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    token_file = state / "token"
    mode = token_file.stat().st_mode
    if not stat.S_ISREG(mode) or mode & 0o077 or token_file.is_symlink():
        raise RuntimeError("token file must be a regular file with mode 0600")
    token = token_file.read_text().strip()
    if len(token) < 43:
        raise RuntimeError("token is too short")
    schema = Schema(args.schema)
    store = Store(state, args.media_limit)
    saved = store.db.execute("SELECT value FROM settings WHERE key='media_limit'").fetchone()
    if saved:
        store.media_limit = int(saved[0])
        store.evict()
    engine = NativeEngine(args.engine, state, schema)
    broker = Broker(store, engine, token, schema, not args.allow_presence)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    try:
        checkpoint = store.db.execute("SELECT value FROM settings WHERE key='sync'").fetchone()
        await engine.start(json.loads(checkpoint[0]) if checkpoint else None)
        server = await asyncio.start_server(broker.connection, "127.0.0.1", args.port, limit=MAX_FRAME)
        async with server:
            signal_task = asyncio.create_task(stop.wait())
            engine_task = asyncio.create_task(engine.process.wait())
            await asyncio.wait({signal_task, engine_task, engine.reader_task}, return_when=asyncio.FIRST_COMPLETED)
            signal_task.cancel()
            engine_task.cancel()
            await asyncio.gather(signal_task, engine_task, return_exceptions=True)
    finally:
        connections = list(broker.connections)
        for task in connections:
            task.cancel()
        await asyncio.gather(*connections, return_exceptions=True)
        requests = [task for digest, task in broker.inflight.values()]
        for task in requests:
            task.cancel()
        await asyncio.gather(*requests, return_exceptions=True)
        await engine.close()
        store.close()
        lock.close()


def main():
    parser = argparse.ArgumentParser(description="AyuGram Remote experimental RPC broker")
    parser.add_argument("--state", required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--engine", required=True)
    parser.add_argument("--port", type=int, default=17555)
    parser.add_argument("--media-limit", type=int, default=3 * 1024**3)
    parser.add_argument("--allow-presence", action="store_true")
    args = parser.parse_args()
    try:
        asyncio.run(serve(args))
    except (OSError, RuntimeError, ValueError):
        raise SystemExit("AyuGram Remote could not start. Check engine, state permissions, and schema.")


if __name__ == "__main__":
    main()
