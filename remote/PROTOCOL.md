# AyuGram Remote protocol v1

This protocol is experimental and tied to the exact API and MTProto TL schemas
in this checkout. It is a relay for the original AyuGram MTProto implementation,
not a TDLib wrapper. The native engine must be built; there is no mock fallback.

## Boundaries

The modified desktop connects to a filesystem Unix socket forwarded by OpenSSH
to the Python broker on VPS loopback port 17555. The broker owns a native engine
subprocess. The engine owns Telegram connections and authorization keys. The
desktop retains the original UI and session data model but no MTProto keys.
Every connection is account-wide: the bearer token is not a restricted read token.

Transport is UTF-8 JSONL. Each message is an object with a string `type`. A network
frame is at most 24 MiB including its newline. A `body` is strict base64 encoding
of at most 16 MiB of 4-byte-aligned boxed TL. Native engine output can reach 96 MiB
because a response can also contain normalized archive records. Request IDs match
`[A-Za-z0-9_-]{1,96}`; counters are decimal strings to avoid JSON/Qt precision loss.

## Desktop/broker messages

- `hello`: version, schema SHA-256, token, client ID, epoch, `after`, `archive_after`.
  Schema hash is `SHA256(api.tl + NUL + mtproto.tl)`. An empty epoch pairs a fresh
  profile; later connections require the stored epoch. Only one connection with
  a given client ID is allowed. Different paired profiles are independent clients
  of the same single Telegram account.
- Server `hello`: version, schema, epoch and latest update sequence.
- `rpc`: ID, body, shifted DC integer and optional preceding request ID in `after`.
- `result`: same ID, body. Errors are boxed Telegram `rpc_error` values, including
  local `REMOTE_*` errors. The native engine performs native flood/migration retries;
  the broker waits at most 180 seconds and cancels its native request after timeout.
- `update`: monotonically increasing string sequence and raw boxed TL updates.
- `archive`: independent monotonically increasing sequence, kind (`edited` or
  `deleted`), normalized message and creation time. Desktop imports these into
  the original AyuGram database, then advances its archive cursor. Import is
  idempotent for matching account/dialog/message/edit date/text.
- `ping`/`pong`: keepalive.
- `history`: request ID, peer (`user:ID`, `chat:ID`, `channel:ID`), integer message ID.
  Response contains known versions and deletion status. This endpoint is available
  for tooling; desktop history integration uses archive messages.
- `settings`: integer `media_limit_bytes` from 0 to 1 TiB. Applies LRU eviction,
  persists the setting, and returns the effective value. The setup wizard calls
  `ayuremote.admin` inside the server container through SSH.

The broker allows 64 concurrent requests per connection and 256 total pending
requests, with 64 concurrent native calls. Outbound queue size is 32 frames.
The desktop has a 256-request bound and reconnects periodically. These are count
limits, not a measured global memory bound; stress testing remains outstanding.

## Durability and errors

Updates, archive changes and a proposed global synchronization checkpoint are
committed in one SQLite transaction before the broker acknowledges the checkpoint
to the engine. Empty differences persist the checkpoint without generating UI
update traffic. `differenceTooLong` records a gap and stops advancing that cycle.
Channel-specific difference recovery is not implemented.

Mutating, non-sensitive requests are recorded before dispatch. The key is
`(client, request ID)` with a digest of DC/body/dependency. Repeating that key either
returns the committed result, waits for the existing request, or reports
`REMOTE_OUTCOME_UNKNOWN`. A conflicting digest is rejected. A timeout/cancellation
does not prove that Telegram did not execute a mutation. Unknown mutations are
not automatically resubmitted. This is not an exactly-once delivery guarantee.
The UI does not currently preserve every request ID across a process restart.

Read-cache allowlist excludes authorization and password methods. Media responses
are kept only in the bounded file cache, not duplicated in the mutation ledger.
Authorization responses are not persisted there. Persistent archives still contain
private message text and file metadata. No at-rest encryption layer is added.

The client writes allowed successful read responses atomically. Offline lookup is
by exact request body plus shifted DC. It is not an indexed offline Telegram API.
The original AyuGram model cannot reconstruct every query from these cached frames.
Raw event files are retained, but a full startup replay into an offline model has
not been implemented. The original local media cache remains available.

## Native engine messages

The child first emits `ready` with version/schema. Broker sends `sync` with the
last checkpoint or an empty state. `rpc` requests use broker-generated IDs and
return `result` with TL plus optional normalized `messages`, `deleted`, and account
ID metadata. Push notifications use `update`; difference results add `checkpoint`.
The broker sends `commit` only after persistence. `cancel` removes a pending native
request; it cannot undo a previously executed Telegram operation.

The engine runs without desktop application singletons. Host-specific settings,
language and diagnostics are injected through `ayu/remote/mtp_runtime.h` with
separate desktop and daemon implementations. Engine keys use atomic `QSaveFile`;
they are never serialized to the relay protocol. Schema upgrades require rebuilding
both binaries and updating the fingerprint after reviewing compatibility.
