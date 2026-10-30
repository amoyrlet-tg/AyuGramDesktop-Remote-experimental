# AyuGram Remote

Start with [README-REMOTE-RU.md](../README-REMOTE-RU.md) for setup, implementation
status and known missing functionality. This directory contains the SSH launcher,
PySide6 setup window, Docker installer, Python broker and executable Python tests.

Native code is in `Telegram/SourceFiles/ayu/remote`; its build target is declared
in `Telegram/cmake/td_remote.cmake`. The original desktop target remains `Telegram`.

- [Protocol and persistence](PROTOCOL.md)
- [Actual validation results](VALIDATION.md)
- [Upstream submodule revisions](UPSTREAM-SUBMODULES.txt)
