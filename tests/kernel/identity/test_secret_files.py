"""Atomic writes under secrets/ (PLAN Phase 5b-2b): the final mode is set before the first
byte is written, the rename is the only moment the target changes, and a crash leaves
the old file, never a half-written or too-readable one."""

import os
import stat
from pathlib import Path

import pytest

from atlas.kernel.identity import secret_files
from atlas.kernel.identity.secret_files import read_text, write_atomic, write_if_different


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def leftovers(directory: Path, keep: str) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.name != keep)


@pytest.mark.parametrize("final", [0o600, 0o640])
def test_a_new_file_has_its_final_mode_and_content(tmp_path: Path, final: int) -> None:
    target = tmp_path / "idp.env"
    write_atomic(target, "A=1\n", mode=final)
    assert target.read_text() == "A=1\n"
    assert mode(target) == final
    assert leftovers(tmp_path, "idp.env") == []  # no temporary file


def test_the_mode_is_right_whatever_the_umask(tmp_path: Path) -> None:
    old = os.umask(0)
    try:
        write_atomic(tmp_path / "a", "x", mode=0o640)
    finally:
        os.umask(old)
    assert mode(tmp_path / "a") == 0o640


def test_the_mode_is_set_before_any_byte_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, int, int]] = []
    real_fchmod, real_fsync = os.fchmod, os.fsync

    def fchmod(fd: int, new_mode: int) -> None:
        info = os.fstat(fd)
        # At creation: owner only, whatever the final mode, and still empty.
        events.append(("before-chmod", stat.S_IMODE(info.st_mode), info.st_size))
        real_fchmod(fd, new_mode)

    def fsync(fd: int) -> None:
        info = os.fstat(fd)
        events.append(("after-write", stat.S_IMODE(info.st_mode), info.st_size))
        real_fsync(fd)

    monkeypatch.setattr(os, "fchmod", fchmod)
    monkeypatch.setattr(os, "fsync", fsync)
    write_atomic(tmp_path / "idp.env", "SECRET=1\n", mode=0o640)
    assert events[0] == ("before-chmod", 0o600, 0)
    assert events[1] == ("after-write", 0o640, len("SECRET=1\n"))


def test_the_new_content_is_in_place_only_at_the_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "idp.env"
    target.write_text("OLD=1\n")
    seen: dict[str, object] = {}
    real_replace = os.replace

    def replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        seen["target_before"] = Path(dst).read_text()
        seen["temp_content"] = Path(src).read_text()
        seen["temp_mode"] = mode(Path(src))
        seen["temp_dir"] = Path(src).parent
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    write_atomic(target, "NEW=2\n", mode=0o640)
    assert seen["target_before"] == "OLD=1\n"
    assert seen["temp_content"] == "NEW=2\n"
    assert seen["temp_mode"] == 0o640  # final before the rename: never readable by more
    assert seen["temp_dir"] == tmp_path  # same directory, so the rename is atomic
    assert target.read_text() == "NEW=2\n"


@pytest.mark.parametrize("crash", ["write", "rename"])
def test_a_crash_leaves_the_old_file_untouched_and_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crash: str
) -> None:
    target = tmp_path / "idp.env"
    target.write_text("OLD=1\n")
    target.chmod(0o640)

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError(5, "I/O error")

    monkeypatch.setattr(os, "fsync" if crash == "write" else "replace", boom)
    with pytest.raises(OSError):
        write_atomic(target, "NEW=2\n", mode=0o600)
    assert target.read_text() == "OLD=1\n"
    assert mode(target) == 0o640
    assert leftovers(tmp_path, "idp.env") == []


def test_replacing_a_file_with_a_looser_mode_never_exposes_it(tmp_path: Path) -> None:
    target = tmp_path / "immich.env"
    write_atomic(target, "A=1\n", mode=0o600)
    write_atomic(target, "A=2\n", mode=0o600)
    assert mode(target) == 0o600 and target.read_text() == "A=2\n"


def test_a_symlink_in_the_way_is_replaced_not_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.write_text("untouched")
    target = tmp_path / "idp.env"
    target.symlink_to(outside)
    write_atomic(target, "NEW\n", mode=0o640)
    assert outside.read_text() == "untouched"
    assert not target.is_symlink() and target.read_text() == "NEW\n"


def test_an_unwritable_directory_is_an_error_not_a_silent_skip(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root can write anywhere")
    directory = tmp_path / "ro"
    directory.mkdir(mode=0o500)
    with pytest.raises(PermissionError):
        write_atomic(directory / "idp.env", "x", mode=0o600)


# --- write_if_different ------------------------------------------------------------


def test_write_if_different_says_what_it_did(tmp_path: Path) -> None:
    target = tmp_path / "groups.json"
    assert write_if_different(target, "a\n", mode=0o640) == "created"
    before = target.stat().st_mtime_ns
    assert write_if_different(target, "a\n", mode=0o640) == "kept"
    assert target.stat().st_mtime_ns == before  # not rewritten
    assert write_if_different(target, "b\n", mode=0o640) == "updated"
    assert target.read_text() == "b\n"


def test_a_file_with_the_wrong_mode_is_rewritten(tmp_path: Path) -> None:
    target = tmp_path / "idp.env"
    target.write_text("A=1\n")
    target.chmod(0o644)  # someone loosened it
    assert write_if_different(target, "A=1\n", mode=0o640) == "updated"
    assert mode(target) == 0o640


def test_read_text_is_none_for_anything_but_a_readable_file(tmp_path: Path) -> None:
    assert read_text(tmp_path / "missing") is None
    assert read_text(tmp_path) is None  # a directory
    binary = tmp_path / "binary"
    binary.write_bytes(b"\xff\xfe\x00")
    assert read_text(binary) is None
    (tmp_path / "ok").write_text("x")
    assert read_text(tmp_path / "ok") == "x"


def test_the_module_reads_no_environment() -> None:
    assert "environ" not in Path(secret_files.__file__).read_text()
