from __future__ import annotations

from pathlib import Path
import socket

import pytest


@pytest.fixture(autouse=True)
def reject_unrecorded_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard this Python test process and its threads; preserve local IPC."""
    def reject(*_args, **_kwargs):
        # pytest.fail raises outside Exception so read-failure adapters cannot
        # turn an unintended external call into a passing negative test.
        pytest.fail(
            'offline_network_forbidden: use an explicit recorded/fake transport '
            'in this pytest case',
            pytrace=False,
        )

    for name in ('getaddrinfo', 'gethostbyname', 'gethostbyname_ex', 'gethostbyaddr'):
        monkeypatch.setattr(socket, name, reject)

    for name in ('connect', 'connect_ex', 'sendto', 'sendmsg'):
        original = getattr(socket.socket, name, None)
        if original is None:
            continue

        def guarded(channel, *args, _original=original, **kwargs):
            if channel.family in (socket.AF_INET, socket.AF_INET6):
                reject()
            return _original(channel, *args, **kwargs)

        monkeypatch.setattr(socket.socket, name, guarded)


@pytest.fixture(autouse=True)
def isolate_production_runtime_controls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "OA_FINANCE_PREAUDIT_STOP_FILE",
        str(tmp_path / "control" / "stop-preaudit.flag"),
    )
    monkeypatch.setenv(
        "OA_FROZEN_WRITER_LOCK_FILE",
        str(tmp_path / "control" / "frozen-writer.lock"),
    )
    monkeypatch.setenv(
        "OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE",
        str(tmp_path / "control" / "rolling-controller.lock"),
    )
