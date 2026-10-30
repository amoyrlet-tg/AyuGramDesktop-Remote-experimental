import asyncio
import base64
import json
import os
import struct
import sys
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ayuremote.protocol import (MAX_FRAME, ProtocolError, Schema, counter, encode,
                               identifier, payload, read, rpc_error)
from ayuremote.server import Broker, NativeEngine
from ayuremote.admin import configure
from ayuremote.store import Store
from ayuremote.launcher import archive_source, atomic_json, ssh_args

SCHEMA = Schema(Path(__file__).resolve().parents[2] / "Telegram/SourceFiles/mtproto/scheme")
TRUE = base64.b64encode(struct.pack("<I", 0x997275b5)).decode()
IDS = {name: key for key, name in SCHEMA.methods.items()}


def request(name="help.getConfig", request_id="r1", extra=b"", **fields):
    return {"type": "rpc", "id": request_id, "dc": 0, "after": "",
            "body": base64.b64encode(struct.pack("<I", IDS[name]) + extra).decode(), **fields}


class FakeEngine:
    def __init__(self):
        self.calls = 0
        self.ready = True
        self.delay = 0
        self.fail = False
        self.commits = []

    async def rpc(self, body, dc):
        self.calls += 1
        await asyncio.sleep(self.delay)
        if self.fail:
            raise ConnectionError("fixture engine disconnected")
        return {"type": "result", "body": TRUE}

    async def control(self, frame):
        self.commits.append(frame)


class ProtocolTests(unittest.TestCase):
    def test_bad_base64_and_alignment(self):
        for value in (None, "???", "YQ==", 3):
            with self.assertRaises(ProtocolError):
                payload(value)

    def test_counter_precision_and_validation(self):
        self.assertEqual(counter("9007199254740993"), 9007199254740993)
        for value in (True, -1, "-1", "01", "1.5", "1000000000000000000"):
            with self.assertRaises(ProtocolError):
                counter(value)

    def test_id_validation(self):
        for value in ("a\nb", "../token", "a b", "", "x" * 97):
            with self.assertRaises(ProtocolError):
                identifier(value)

    def test_schema_pinned_and_unknown_constructors(self):
        self.assertEqual(len(SCHEMA.fingerprint), 64)
        with self.assertRaises(ProtocolError):
            SCHEMA.method(b"\0" * 4)

    def test_error_is_boxed_tl(self):
        data = payload(rpc_error(400, "REMOTE_OFFLINE"))
        self.assertEqual(struct.unpack_from("<Ii", data), (0x2144ca19, 400))
        self.assertEqual(data[8], len("REMOTE_OFFLINE"))

    def test_oversize_outgoing_frame(self):
        with self.assertRaises(ProtocolError):
            encode({"type": "rpc", "body": "a" * MAX_FRAME})


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name, 64)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_replay_after_restart(self):
        first = self.store.append_update(TRUE)
        self.store.append_update(TRUE)
        epoch = self.store.epoch
        self.store.close()
        self.store = Store(self.tmp.name, 64)
        self.assertEqual(self.store.epoch, epoch)
        self.assertEqual([x["seq"] for x in self.store.updates(first)], ["2"])

    def test_transaction_does_not_ack_failed_archive(self):
        with self.assertRaises(KeyError):
            self.store.append_update(TRUE, [{"id": 1}], checkpoint={"pts": 5})
        self.assertEqual(self.store.latest_seq(), 0)
        self.assertIsNone(self.store.db.execute("SELECT value FROM settings WHERE key='sync'").fetchone())

    def test_checkpoint_and_event_are_persistent_together(self):
        self.store.append_update(TRUE, checkpoint={"pts": 5})
        self.assertEqual(self.store.latest_seq(), 1)
        self.assertEqual(json.loads(self.store.db.execute("SELECT value FROM settings WHERE key='sync'").fetchone()[0]), {"pts": 5})

    def test_archive_preserves_edits_and_deletions(self):
        original = {"peer": "user:22", "id": 7, "text": "before"}
        changed = {**original, "text": "after"}
        self.store.archive([original, changed, changed])
        self.store.archive(deleted=[{"peer": "nonchannel", "ids": [7]}])
        self.store.archive([{**original, "peer": "channel:22"}])
        result = self.store.history("user:22", 7)
        self.assertEqual([v["text"] for v in result["versions"]], ["before", "after"])
        self.assertTrue(result["deleted"])
        self.assertFalse(self.store.history("channel:22", 7)["deleted"])

    def test_delete_before_original_is_retained(self):
        self.store.archive(deleted=[{"peer": "nonchannel", "ids": [8]}])
        self.store.archive([{"peer": "user:22", "id": 8, "text": "late history"}])
        self.assertTrue(self.store.history("user:22", 8)["deleted"])

    def test_original_ayu_history_replay_is_ordered(self):
        before = {"peer": "user:22", "id": 1, "text": "first", "edit_date": 0}
        after = {**before, "text": "edited", "edit_date": 10}
        self.store.archive([before, before, after, after])
        self.store.archive(deleted=[{"peer": "nonchannel", "ids": [1]}])
        frames = self.store.history_events(0)
        self.assertEqual([x["kind"] for x in frames], ["edited", "deleted"])
        self.assertEqual([x["message"]["text"] for x in frames], ["first", "edited"])
        self.assertEqual([x["seq"] for x in self.store.history_events(1)], ["2"])

    def test_metadata_changes_do_not_create_fake_edits(self):
        message = {"peer": "channel:22", "id": 1, "text": "same"}
        self.store.archive([message, {**message, "tl": "a later view count"}])
        self.assertEqual(self.store.history_events(0), [])

    def test_cache_limit_and_lru(self):
        data = base64.b64encode(b"x" * 32).decode()
        self.store.cache("a", "upload.getFile", data)
        self.store.cache("b", "upload.getFile", data)
        self.assertEqual(self.store.cached("a"), data)
        self.store.cache("c", "upload.getFile", data)
        self.assertIsNone(self.store.cached("b"))
        self.assertEqual(sum(x.stat().st_size for x in self.store.files.iterdir()), 64)
        self.store.media_limit = 0
        self.store.evict()
        self.assertEqual(list(self.store.files.iterdir()), [])

    def test_auth_and_errors_never_cached(self):
        self.store.cache("secret", "auth.signIn", TRUE)
        self.store.cache("error", "help.getConfig", rpc_error(400, "FAILED"))
        self.assertIsNone(self.store.cached("secret"))
        self.assertIsNone(self.store.cached("error"))

    def test_oversize_file_rejected_without_erasing_history(self):
        self.store.append_update(TRUE)
        self.store.cache("big", "upload.getFile", base64.b64encode(b"x" * 100).decode())
        self.assertIsNone(self.store.cached("big"))
        self.assertEqual(self.store.latest_seq(), 1)

    def test_orphan_file_recovery(self):
        (self.store.files / "tmp-incomplete").write_bytes(b"sensitive media")
        self.store.reconcile()
        self.assertFalse((self.store.files / "tmp-incomplete").exists())

    def test_request_collision_and_crash_deduplication(self):
        self.assertEqual(self.store.begin_request("c", "r", "digest")[0], "new")
        self.store.close()
        self.store = Store(self.tmp.name, 64)
        self.assertEqual(self.store.begin_request("c", "r", "digest")[0], "unknown")
        with self.assertRaises(ValueError):
            self.store.begin_request("c", "r", "changed")

    def test_file_permissions(self):
        self.assertEqual(os.stat(self.tmp.name).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(Path(self.tmp.name) / "archive.sqlite3").st_mode & 0o777, 0o600)


class BrokerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name, 64)
        self.engine = FakeEngine()
        self.broker = Broker(self.store, self.engine, "test-token" * 6, SCHEMA)

    async def asyncTearDown(self):
        tasks = [x[1] for x in self.broker.inflight.values()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.store.close()
        self.tmp.cleanup()

    async def test_mutation_is_not_sent_twice(self):
        frame = request("messages.sendMessage")
        a = await self.broker.execute("c", frame)
        b = await self.broker.execute("c", frame)
        self.assertEqual(a, b)
        self.assertEqual(self.engine.calls, 1)

    async def test_reconnect_joins_inflight_request(self):
        self.engine.delay = .02
        frame = request("messages.sendMessage")
        a, b = await asyncio.gather(self.broker.execute("c", frame), self.broker.execute("c", frame))
        self.assertEqual(a, b)
        self.assertEqual(self.engine.calls, 1)

    async def test_unacknowledged_mutation_is_not_retried(self):
        self.engine.fail = True
        frame = request("messages.sendMessage")
        await self.broker.execute("c", frame)
        self.engine.fail = False
        result = await self.broker.execute("c", frame)
        self.assertIn(b"REMOTE_OUTCOME_UNKNOWN", payload(result["body"]))
        self.assertEqual(self.engine.calls, 1)

    async def test_ghost_blocks_presence_and_wrappers(self):
        for method in ("messages.readHistory", "messages.setTyping", "stories.readStories", "auth.exportAuthorization"):
            await self.broker.execute("c", request(method, method.replace(".", "_")))
        online = request("account.updateStatus", "online", struct.pack("<I", 0xbc799737))
        await self.broker.execute("c", online)
        await self.broker.execute("c", request("invokeWithoutUpdates", "wrap"))
        self.assertEqual(self.engine.calls, 0)
        offline = request("account.updateStatus", "offline", struct.pack("<I", 0x997275b5))
        await self.broker.execute("c", offline)
        self.assertEqual(self.engine.calls, 1)

    async def test_offline_read_uses_cache(self):
        await self.broker.execute("c", request())
        self.engine.ready = False
        result = await self.broker.execute("c", request(request_id="r2"))
        self.assertEqual(result["body"], TRUE)
        self.assertEqual(self.engine.calls, 1)

    async def test_auth_material_not_written_to_database(self):
        frame = request("auth.signIn")
        await self.broker.execute("c", frame)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM requests").fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM cache").fetchone()[0], 0)

    async def test_bound_account_cannot_mix_another_login(self):
        with self.store.db:
            self.store.db.execute("INSERT INTO settings VALUES('account','123')")
        result = await self.broker.execute("c", request("auth.signIn"))
        self.assertIn(b"REMOTE_ACCOUNT_ALREADY_BOUND", payload(result["body"]))
        self.assertEqual(self.engine.calls, 0)

    async def test_media_is_not_duplicated_in_unbounded_request_ledger(self):
        await self.broker.execute("c", request("upload.getCdnFile"))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM requests").fetchone()[0], 0)

    async def test_inflight_request_id_cannot_change_contents(self):
        self.engine.delay = .02
        first = asyncio.create_task(self.broker.execute("c", request("messages.sendMessage", "same")))
        await asyncio.sleep(.001)
        with self.assertRaises(ProtocolError):
            await self.broker.execute("c", request("messages.editMessage", "same"))
        await first

    async def test_dependency_orders_requests(self):
        first = request("messages.sendMessage", "one")
        second = request("messages.sendMessage", "two", after="one")
        await self.broker.execute("c", first)
        response = await self.broker.execute("c", second)
        self.assertEqual(response["body"], TRUE)
        self.assertEqual(self.engine.calls, 2)

    async def test_self_dependency_is_rejected_without_dispatch(self):
        with self.assertRaises(ProtocolError):
            await self.broker.execute("c", request(request_id="one", after="one"))
        self.assertEqual(self.engine.calls, 0)

    async def test_admin_changes_limit_and_evicts_without_losing_history(self):
        self.store.append_update(TRUE)
        self.store.cache("media", "upload.getFile", TRUE)
        (Path(self.tmp.name) / "token").write_text(self.broker.token)
        server = await asyncio.start_server(self.broker.connection, "127.0.0.1", 0, limit=MAX_FRAME)
        async with server:
            result = await configure(self.tmp.name, SCHEMA, 0, server.sockets[0].getsockname()[1])
            self.assertEqual(result["media_limit_bytes"], 0)
            self.assertIsNone(self.store.cached("media"))
            self.assertEqual(self.store.latest_seq(), 1)
            self.assertEqual(self.store.db.execute("SELECT value FROM settings WHERE key='media_limit'").fetchone()[0], "0")
            await asyncio.sleep(.01)

    async def test_sync_ack_happens_after_transaction(self):
        await self.broker.on_update({"body": TRUE, "checkpoint": {"pts": 20}, "notify": False})
        self.assertEqual(self.engine.commits, [{"type": "commit", "state": {"pts": 20}}])
        self.assertEqual(self.store.latest_seq(), 0)

    async def test_real_socket_handshake_replay_and_rpc(self):
        self.store.append_update(TRUE)
        server = await asyncio.start_server(self.broker.connection, "127.0.0.1", 0, limit=MAX_FRAME)
        async with server:
            reader, writer = await asyncio.open_connection(*server.sockets[0].getsockname(), limit=MAX_FRAME)
            writer.write(encode({"type": "hello", "version": 1, "token": self.broker.token,
                                 "client": "integration", "schema": SCHEMA.fingerprint, "after": "0", "epoch": ""}))
            await writer.drain()
            hello = await asyncio.wait_for(read(reader), 2)
            self.assertEqual(hello["epoch"], self.store.epoch)
            update = await asyncio.wait_for(read(reader), 2)
            self.assertEqual(update["seq"], "1")
            writer.write(encode(request()))
            await writer.drain()
            result = await asyncio.wait_for(read(reader), 2)
            self.assertEqual(result["body"], TRUE)
            writer.close()
            await writer.wait_closed()
            await asyncio.sleep(.01)

    async def test_wrong_token_never_reaches_engine(self):
        server = await asyncio.start_server(self.broker.connection, "127.0.0.1", 0, limit=MAX_FRAME)
        async with server:
            reader, writer = await asyncio.open_connection(*server.sockets[0].getsockname())
            writer.write(encode({"type": "hello", "token": "wrong"}))
            await writer.drain()
            self.assertEqual(await asyncio.wait_for(reader.read(), 2), b"")
            self.assertEqual(self.engine.calls, 0)
            writer.close()
            await writer.wait_closed()


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_wait_cancels_native_request(self):
        frames = []
        sent = asyncio.Event()

        class Input:
            def write(self, data):
                frames.append(json.loads(data))
                sent.set()

            async def drain(self):
                pass

        engine = NativeEngine("unused", "unused", SCHEMA)
        engine.process = SimpleNamespace(stdin=Input())
        engine.ready = True
        task = asyncio.create_task(engine.rpc(TRUE, 0))
        await sent.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual([frame["type"] for frame in frames], ["rpc", "cancel"])
        self.assertEqual(frames[0]["id"], frames[1]["id"])
        self.assertEqual(engine.pending, {})


class LauncherTests(unittest.TestCase):
    def test_ssh_arguments_reject_shell_injection(self):
        for host in ("-oProxyCommand=evil", "localhost;id", "$(id)", "user@host", "host\n-oX"):
            with self.assertRaises(ValueError):
                ssh_args(host, 22)
        command = ssh_args("2001:db8::1", 2222)
        self.assertIn("StrictHostKeyChecking=ask", command)
        self.assertNotIn("StrictHostKeyChecking=no", command)

    def test_profile_write_is_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            atomic_json(path, {"token": "fixture"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text()), {"token": "fixture"})

    def test_source_archive_excludes_sessions(self):
        import tarfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repository"
            (root / "tdata").mkdir(parents=True)
            (root / "tdata/secret").write_text("private")
            (root / "source.cpp").write_text("code")
            (root / "profile.json").write_text("private")
            archive = Path(directory) / "source.tar.gz"
            archive_source(root, archive)
            with tarfile.open(archive) as result:
                names = result.getnames()
            self.assertIn("source/source.cpp", names)
            self.assertFalse(any("secret" in name or "profile.json" in name for name in names))


if __name__ == "__main__":
    unittest.main()
