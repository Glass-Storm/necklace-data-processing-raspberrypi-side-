"""Tests for :mod:`ecosys_pi.token_store`: 0600 cache, atomicity, clear, corrupt."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from ecosys_pi import token_store as token_store_mod
from ecosys_pi.token_store import (
    APP_DIRNAME,
    CREDENTIALS_FILENAME,
    Credentials,
    TokenStore,
    config_home,
    credentials_path,
)

TOKEN = "single-use-issued-bearer-token-abc123"
DEVICE_ID = "device-7f3a"


def _mode(path: Path) -> int:
    """The permission bits of ``path`` (e.g. 0o600), ignoring file type bits."""
    return stat.S_IMODE(path.stat().st_mode)


def _store(tmp_path: Path) -> TokenStore:
    return TokenStore(tmp_path / APP_DIRNAME / CREDENTIALS_FILENAME)


# --- path resolution --------------------------------------------------------


def test_config_home_honours_xdg_config_home(tmp_path: Path) -> None:
    assert config_home({"XDG_CONFIG_HOME": str(tmp_path)}) == tmp_path


def test_config_home_blank_falls_back_to_dot_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/ecosys")))

    assert config_home({"XDG_CONFIG_HOME": "  "}) == Path("/home/ecosys/.config")
    assert config_home({}) == Path("/home/ecosys/.config")


def test_credentials_path_is_under_app_dir(tmp_path: Path) -> None:
    path = credentials_path({"XDG_CONFIG_HOME": str(tmp_path)})

    assert path == tmp_path / APP_DIRNAME / CREDENTIALS_FILENAME


def test_default_store_uses_xdg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    store = TokenStore.default()

    assert store.path == tmp_path / APP_DIRNAME / CREDENTIALS_FILENAME


# --- round-trip + permissions ----------------------------------------------


def test_load_missing_file_is_none(tmp_path: Path) -> None:
    assert _store(tmp_path).load() is None


def test_round_trip_and_mode_0600(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save(Credentials(token=TOKEN, device_id=DEVICE_ID))

    assert _mode(store.path) == 0o600, oct(_mode(store.path))

    loaded = store.load()
    assert loaded is not None
    assert loaded.token == TOKEN
    assert loaded.device_id == DEVICE_ID


def test_directory_mode_is_0700(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save(Credentials(token=TOKEN, device_id=DEVICE_ID))

    assert _mode(store.path.parent) == 0o700, oct(_mode(store.path.parent))


def test_existing_world_readable_dir_is_tightened(tmp_path: Path) -> None:
    directory = tmp_path / APP_DIRNAME
    directory.mkdir(mode=0o777)
    os.chmod(directory, 0o777)  # mkdir mode is masked by umask; force it.

    store = _store(tmp_path)
    store.save(Credentials(token=TOKEN, device_id=DEVICE_ID))

    assert _mode(directory) == 0o700


def test_save_overwrites_previous_credentials(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save(Credentials(token="old-token", device_id="old-device"))
    store.save(Credentials(token=TOKEN, device_id=DEVICE_ID))

    loaded = store.load()
    assert loaded == Credentials(token=TOKEN, device_id=DEVICE_ID)


def test_save_rejects_blank_fields(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(ValueError):
        store.save(Credentials(token="  ", device_id=DEVICE_ID))
    with pytest.raises(ValueError):
        store.save(Credentials(token=TOKEN, device_id=""))
    # Nothing was created.
    assert not store.path.exists()


def test_no_temporary_files_are_left_behind(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save(Credentials(token=TOKEN, device_id=DEVICE_ID))

    leftovers = [
        p.name for p in store.path.parent.iterdir() if p.name != CREDENTIALS_FILENAME
    ]
    assert leftovers == []


# --- atomicity: a crash mid-write must leave the OLD file intact -------------


def test_failed_write_leaves_old_file_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    store.save(Credentials(token="old-token", device_id="old-device"))
    assert store.path.read_text(encoding="utf-8")  # the old file exists

    real_write = os.write

    def boom(fd: int, data: bytes) -> int:
        raise OSError("simulated crash mid-write")

    monkeypatch.setattr(token_store_mod.os, "write", boom)
    with pytest.raises(OSError):
        store.save(Credentials(token=TOKEN, device_id=DEVICE_ID))
    monkeypatch.setattr(token_store_mod.os, "write", real_write)

    # Old content survives byte-for-byte, and the load still returns old creds.
    assert store.load() == Credentials(token="old-token", device_id="old-device")
    assert _mode(store.path) == 0o600


def test_failed_replace_leaves_old_file_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If os.replace itself never runs, the old file must be untouched."""
    store = _store(tmp_path)
    store.save(Credentials(token="old-token", device_id="old-device"))
    old_bytes = store.path.read_bytes()

    def boom_replace(src: object, dst: object) -> None:
        raise OSError("simulated crash before rename")

    monkeypatch.setattr(token_store_mod.os, "replace", boom_replace)
    with pytest.raises(OSError):
        store.save(Credentials(token=TOKEN, device_id=DEVICE_ID))

    assert store.path.read_bytes() == old_bytes
    assert store.load() == Credentials(token="old-token", device_id="old-device")
    # The temp file was cleaned up.
    assert [p.name for p in store.path.parent.iterdir()] == [CREDENTIALS_FILENAME]


def test_first_write_crash_leaves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)

    def boom(fd: int, data: bytes) -> int:
        raise OSError("simulated crash mid-write")

    monkeypatch.setattr(token_store_mod.os, "write", boom)
    with pytest.raises(OSError):
        store.save(Credentials(token=TOKEN, device_id=DEVICE_ID))

    # No credentials file, no temp leftovers; load treats it as absent.
    assert store.load() is None
    assert list(store.path.parent.iterdir()) == []


# --- corruption is treated as absent (no crash) -----------------------------


def test_corrupt_json_is_treated_as_absent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{not valid json", encoding="utf-8")

    assert store.load() is None


def test_non_object_json_is_treated_as_absent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(json.dumps(["token", "device"]), encoding="utf-8")

    assert store.load() is None


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"token": TOKEN},
        {"device_id": DEVICE_ID},
        {"token": "", "device_id": DEVICE_ID},
        {"token": TOKEN, "device_id": ""},
        {"token": 123, "device_id": DEVICE_ID},
        {"token": TOKEN, "device_id": None},
    ],
)
def test_missing_or_badly_typed_fields_are_absent(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    store = _store(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(json.dumps(payload), encoding="utf-8")

    assert store.load() is None


def test_binary_garbage_is_treated_as_absent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_bytes(b"\xff\xfe\x00\x01not utf-8")

    assert store.load() is None


# --- clear ------------------------------------------------------------------


def test_clear_removes_the_file(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save(Credentials(token=TOKEN, device_id=DEVICE_ID))
    assert store.path.exists()

    store.clear()

    assert not store.path.exists()
    assert store.load() is None


def test_clear_is_idempotent_when_absent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.clear()  # must not raise
    store.clear()


def test_repr_never_leaks_token() -> None:
    creds = Credentials(token=TOKEN, device_id=DEVICE_ID)

    text = repr(creds)

    assert TOKEN not in text
    assert "<redacted>" in text
    assert DEVICE_ID in text
