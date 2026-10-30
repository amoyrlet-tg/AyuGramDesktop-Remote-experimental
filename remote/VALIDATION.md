# Validation — 2026-10-30

Upstream: AyuGram/AyuGramDesktop `0f2a31bb420de98f331990432265b131b8d836b5`.
Status: experimental source implementation; no native binaries supplied.

## Executed

| Check | Result |
| --- | --- |
| `python3 -m unittest discover -s remote/tests -q` | 38 tests passed. |
| `python3 remote/check_source.py` | Passed: schema fingerprint, named TL constructors, removal of direct GUI singleton calls from shared instance/session implementation. |
| `python3 -m compileall -q remote` | Passed; syntax only, does not import PySide6 or exercise the GUI. |
| `bash -n remote/install-server.sh` | Passed; shell syntax only. |
| `git diff --check` | Passed. |
| Recursive Git submodule checkout | All recorded submodules present at their recorded revisions. |
| Upstream TL generator | Ran successfully into a temporary folder; generated types were inspected to correct boxed TL serialization. This is not a C++ compile check. |

Tests exercise real SQLite/filesystem behavior and loopback TCP connections with
an explicit test engine fixture. They cover frame/ID validation, cursor precision,
authentication rejection, update replay, archive edits/deletions, checkpoint
atomicity, non-channel deletion isolation, cache eviction/zero quota, orphan cleanup,
deduplication, unknown outcomes after a crash, dependency ordering/conflicts,
Ghost blocking, private profile creation, native cancellation signaling, source
archive exclusions and administrative cache changes. The fixture does not log in
to Telegram or validate its API behavior.

## Not executed

- CMake configure, C++ compile/link, launch of the native engine or modified desktop.
- PySide6 window interaction, Bubblewrap/Wayland behavior and network capture.
- Docker image build, installation on a real VPS, SSH setup and reconnect.
- Telegram phone/code/2FA flow, DC migration, rate limits, uploads and downloads.
- Comparison with the original AyuGram history UI, genuine offline startup,
  long-running synchronization, storage/performance and crash testing.

The repository's [AGENTS.md](../AGENTS.md) explicitly instructs: “Avoid building
the project.” The available environment also lacked the prepared native build
dependencies. Therefore the new C++ targets are supplied as unverified source,
and a successful build or a complete working application is not claimed.

## Remaining acceptance scenarios

1. Build Debug `Telegram` and `AyuGramRemoteEngine`; fix any compile/link/runtime
   integration failures and record compiler, Qt, platform and container digest.
2. Use a separate test account; confirm phone/code/2FA and restart without a second
   Telegram login. Inspect local state to confirm it contains no Telegram auth key.
3. Send/edit/delete while the GUI is closed; reconnect, verify text and media history.
   Implement channel-difference and offline-media gaps before claiming coverage.
4. Capture network traffic: native engine connects to Telegram, GUI's network
   namespace has no egress, desktop SSH connects only to its configured VPS.
5. Kill broker/engine during requests and checkpoint transactions; verify recovery
   and that uncertain sends are not silently duplicated.
6. Fill and lower the media quota; check actual files, independent text retention,
   and disk-full behavior. Verify UI offline retrieval after a full process restart.
7. Exercise all requested AyuGram functions; calls/web apps/other direct-network
   features and account switching currently remain outside implemented support.
