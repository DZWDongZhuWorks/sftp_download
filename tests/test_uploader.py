"""uploader.py 單元測試：本地走訪、上傳決策、斷點續傳、忽略規則與整體流程。

沿用 conftest.py 的 FakeSFTPClient（支援串流寫入）與 uploader_factory；不碰真實網路。
"""

import hashlib
import logging
import os
import stat
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uploader as up  # noqa: E402


def _write(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _mtime(path: Path):
    return int(path.stat().st_mtime)


class TestListLocalFiles:
    def test_recursive_walk_collects_all_files_with_relative_paths(self, uploader_factory, fake_sftp_factory, tmp_path):
        _write(tmp_path / "a.txt", b"a")
        _write(tmp_path / "sub" / "b.txt", b"b")
        _write(tmp_path / "sub" / "deep" / "c.txt", b"c")
        d = uploader_factory(recursive=True)
        d.sftp = fake_sftp_factory(files={})

        files = d._list_local_files(tmp_path, "/remote")

        rels = sorted(rel for _, rel in files)
        assert rels == ["a.txt", "sub/b.txt", "sub/deep/c.txt"]

    def test_recursive_walk_creates_empty_remote_dirs(self, uploader_factory, fake_sftp_factory, tmp_path):
        (tmp_path / "emptydir").mkdir()
        _write(tmp_path / "a.txt", b"a")
        d = uploader_factory(recursive=True)
        d.sftp = fake_sftp_factory(files={})

        d._list_local_files(tmp_path, "/remote")

        # 即使 emptydir 底下沒有檔案，也要在遠端建立對應資料夾（鏡射下載端行為）。
        assert "/remote/emptydir" in d.sftp.dirs

    def test_single_layer_skips_subdirectories(self, uploader_factory, fake_sftp_factory, tmp_path):
        _write(tmp_path / "a.txt", b"a")
        _write(tmp_path / "sub" / "b.txt", b"b")
        d = uploader_factory(recursive=False)
        d.sftp = fake_sftp_factory(files={})

        files = d._list_local_files(tmp_path, "/remote")

        assert sorted(rel for _, rel in files) == ["a.txt"]

    def test_single_file_source(self, uploader_factory, fake_sftp_factory, tmp_path):
        target = tmp_path / "only.txt"
        _write(target, b"x")
        d = uploader_factory()
        d.sftp = fake_sftp_factory(files={})

        files = d._list_local_files(target, "/remote")

        assert [rel for _, rel in files] == ["only.txt"]

    def test_manifest_file_is_excluded_from_upload(self, uploader_factory, fake_sftp_factory, tmp_path):
        _write(tmp_path / "a.txt", b"a")
        _write(tmp_path / up.UPLOAD_MANIFEST_FILENAME, b"{}")
        _write(tmp_path / up.MANIFEST_FILENAME, b"{}")
        d = uploader_factory(recursive=True)
        d.sftp = fake_sftp_factory(files={})

        files = d._list_local_files(tmp_path, "/remote")

        assert sorted(rel for _, rel in files) == ["a.txt"]

    def test_download_part_files_are_never_uploaded(self, uploader_factory, fake_sftp_factory, tmp_path):
        """下載中斷留下的暫存檔不能被上傳出去。

        share/scheduler 這類目錄既是 scheduler_download 的目的地、又是 scheduler_upload
        的來源（上傳回 fleet 的 STANDARD），半截的 reboot_launcher.sh.part 一旦被推上去，
        污染的是整支船隊的來源。這條不依賴各船的 ignore 設定，寫死在程式裡。
        """
        _write(tmp_path / "reboot_launcher.sh", b"complete")
        _write(tmp_path / ("reboot_launcher.sh" + up.PART_SUFFIX), b"half")
        _write(tmp_path / "sub" / ("deep.sh" + up.PART_SUFFIX), b"half")
        _write(tmp_path / "sub" / "deep.sh", b"complete")
        d = uploader_factory(recursive=True)
        d.sftp = fake_sftp_factory(files={})

        files = d._list_local_files(tmp_path, "/remote")

        assert sorted(rel for _, rel in files) == ["reboot_launcher.sh", "sub/deep.sh"]

    def test_single_file_source_pointing_at_a_part_file_uploads_nothing(
        self, uploader_factory, fake_sftp_factory, tmp_path
    ):
        target = tmp_path / ("x.sh" + up.PART_SUFFIX)
        _write(target, b"half")
        d = uploader_factory()
        d.sftp = fake_sftp_factory(files={})

        assert d._list_local_files(target, "/remote") == []

    def test_ignore_rules_skip_matching_files(self, uploader_factory, fake_sftp_factory, tmp_path):
        ignore = tmp_path / "up_ignore.txt"
        ignore.write_text("*.log\n", encoding="utf-8")
        _write(tmp_path / "keep.txt", b"k")
        _write(tmp_path / "skip.log", b"s")
        d = uploader_factory(recursive=True, ignore_file=str(ignore))
        d._ignore_spec = d._load_ignore_spec()
        d.sftp = fake_sftp_factory(files={})

        files = d._list_local_files(tmp_path, "/remote")

        assert sorted(rel for _, rel in files) == ["keep.txt", "up_ignore.txt"]

    def test_symlinks_are_still_followed_by_default(self, uploader_factory, fake_sftp_factory, tmp_path):
        """_handle_symlink 預設不接手，SFTP 上傳仍把連結解析成實體檔案與資料夾。

        pack_upload 會覆寫這個掛鉤以保留連結；這裡釘住「上傳端不受影響」的契約。
        """
        _write(tmp_path / "realdir" / "a.txt", b"a")
        (tmp_path / "filelink").symlink_to("realdir/a.txt")
        (tmp_path / "dirlink").symlink_to("realdir")
        d = uploader_factory(recursive=True)
        d.sftp = fake_sftp_factory(files={})

        files = d._list_local_files(tmp_path, "/remote")

        assert sorted(rel for _, rel in files) == ["dirlink/a.txt", "filelink", "realdir/a.txt"]

    def test_symlink_hook_can_take_over_the_walk(self, uploader_factory, fake_sftp_factory, tmp_path):
        _write(tmp_path / "realdir" / "a.txt", b"a")
        (tmp_path / "dirlink").symlink_to("realdir")
        d = uploader_factory(recursive=True)
        d.sftp = fake_sftp_factory(files={})
        seen = []
        d._handle_symlink = lambda local_path, rel_path: (seen.append(rel_path), True)[1]

        files = d._list_local_files(tmp_path, "/remote")

        assert seen == ["dirlink"]
        assert sorted(rel for _, rel in files) == ["realdir/a.txt"]


class TestNextRemoteDuplicatePath:
    def test_first_duplicate_uses_suffix(self, uploader_factory, fake_sftp_factory):
        d = uploader_factory(duplicate_suffix="copy")
        d.sftp = fake_sftp_factory(files={"/remote/a.txt": b"x"})
        assert d._next_remote_duplicate_path("/remote/a.txt") == "/remote/a_copy.txt"

    def test_increments_when_duplicate_already_exists(self, uploader_factory, fake_sftp_factory):
        d = uploader_factory(duplicate_suffix="copy")
        d.sftp = fake_sftp_factory(files={"/remote/a.txt": b"x", "/remote/a_copy.txt": b"y"})
        assert d._next_remote_duplicate_path("/remote/a.txt") == "/remote/a_copy1.txt"


class TestUploadPreservesModeAndMtime:
    def test_upload_mirrors_local_mode_and_mtime_to_remote(self, uploader_factory, fake_sftp_factory, tmp_path):
        local = tmp_path / "x.sh"
        _write(local, b"#!/bin/sh\n")
        os.chmod(local, 0o755)
        os.utime(local, (1111, 2222))
        d = uploader_factory()
        d.sftp = fake_sftp_factory(files={})
        d._upload_one_file(local, "x.sh", "/remote", tmp_path)
        # 上傳後應把本地權限與 mtime 鏡射到遠端(SFTP 預設不搬)。
        assert ("/remote/x.sh", 0o755) in d.sftp.chmod_calls
        assert any(p == "/remote/x.sh" and int(times[1]) == 2222 for p, times in d.sftp.utime_calls)


class TestUploadOneFileFresh:
    def test_fresh_upload_writes_remote_and_records_manifest(self, uploader_factory, fake_sftp_factory, tmp_path):
        content = b"hello world"
        local = tmp_path / "a.txt"
        _write(local, content)
        d = uploader_factory()
        d.sftp = fake_sftp_factory(files={})

        result = d._upload_one_file(local, "a.txt", "/remote", tmp_path)

        assert result == "uploaded"
        assert d.sftp.files["/remote/a.txt"] == content
        assert d._manifest["a.txt"]["local_bytes"] == len(content)
        assert d._manifest["a.txt"]["local_sha256"] == hashlib.sha256(content).hexdigest()

    def test_fresh_upload_creates_remote_parent_dirs(self, uploader_factory, fake_sftp_factory, tmp_path):
        local = tmp_path / "b.txt"
        _write(local, b"data")
        d = uploader_factory()
        d.sftp = fake_sftp_factory(files={})

        d._upload_one_file(local, "sub/deep/b.txt", "/remote", tmp_path)

        assert d.sftp.files["/remote/sub/deep/b.txt"] == b"data"
        assert "/remote/sub/deep" in d.sftp.dirs


class TestUploadOneFileSkip:
    def test_skips_when_remote_matches_and_manifest_unchanged(self, uploader_factory, fake_sftp_factory, tmp_path):
        content = b"unchanged content"
        local = tmp_path / "a.txt"
        _write(local, content)
        d = uploader_factory()
        d.sftp = fake_sftp_factory(files={})

        first = d._upload_one_file(local, "a.txt", "/remote", tmp_path)
        second = d._upload_one_file(local, "a.txt", "/remote", tmp_path)

        assert first == "uploaded"
        assert second == "skipped"


class TestUploadOneFileUpdated:
    def test_overwrites_when_local_updated(self, uploader_factory, fake_sftp_factory, tmp_path):
        local = tmp_path / "a.txt"
        _write(local, b"NEWCONTENT")  # 與遠端同長度、內容不同
        d = uploader_factory(duplicate_mode="overwrite")
        d.sftp = fake_sftp_factory(files={"/remote/a.txt": b"OLDCONTENT"})
        # 版本紀錄記的是舊 mtime，與目前本地 mtime 不符 → 視為已更新，覆蓋上傳。
        d._manifest = {"a.txt": {"size": len(b"NEWCONTENT"), "mtime": 1}}

        result = d._upload_one_file(local, "a.txt", "/remote", tmp_path)

        assert result == "uploaded"
        assert d.sftp.files["/remote/a.txt"] == b"NEWCONTENT"

    def test_duplicate_mode_saves_new_remote_file(self, uploader_factory, fake_sftp_factory, tmp_path):
        local = tmp_path / "a.txt"
        _write(local, b"a much longer new content")
        d = uploader_factory(duplicate_mode="duplicate", duplicate_suffix="copy")
        d.sftp = fake_sftp_factory(files={"/remote/a.txt": b"short"})

        result = d._upload_one_file(local, "a.txt", "/remote", tmp_path)

        assert result == "uploaded"
        assert d.sftp.files["/remote/a.txt"] == b"short"  # 舊檔不動
        assert d.sftp.files["/remote/a_copy.txt"] == b"a much longer new content"


class TestUploadOneFileResume:
    def test_resumes_from_partial_remote_when_prefix_verified(
        self, uploader_factory, fake_sftp_factory, tmp_path, caplog
    ):
        content = b"Z" * (up.CHUNK_SIZE * 3)
        partial = up.CHUNK_SIZE  # 已上傳 1/3
        local = tmp_path / "big.bin"
        _write(local, content)
        d = uploader_factory()
        d.sftp = fake_sftp_factory(files={"/remote/big.bin": content[:partial]})
        d._manifest = {
            "big.bin": {
                "size": len(content),
                "mtime": _mtime(local),
                "local_sha256": hashlib.sha256(content[:partial]).hexdigest(),
                "local_bytes": partial,
            }
        }

        with caplog.at_level(logging.INFO):
            result = d._upload_one_file(local, "big.bin", "/remote", tmp_path)

        assert result == "uploaded"
        assert d.sftp.files["/remote/big.bin"] == content
        message = next(r.message for r in caplog.records if "[RESUME_ACCEPTED]" in r.message)
        assert 'direction="upload"' in message
        assert f"resume_offset={partial}" in message
        assert f"remaining_bytes={len(content) - partial}" in message

    def test_reupload_from_scratch_when_prefix_hash_mismatches(
        self, uploader_factory, fake_sftp_factory, tmp_path, caplog
    ):
        content = b"Q" * (up.CHUNK_SIZE * 2)
        partial = up.CHUNK_SIZE
        local = tmp_path / "big.bin"
        _write(local, content)
        d = uploader_factory()
        d.sftp = fake_sftp_factory(files={"/remote/big.bin": content[:partial]})
        # 紀錄的雜湊與實際本地前綴不符 → 不可續傳，整份重新上傳並覆蓋。
        d._manifest = {
            "big.bin": {
                "size": len(content),
                "mtime": _mtime(local),
                "local_sha256": "deadbeef",
                "local_bytes": partial,
            }
        }

        with caplog.at_level(logging.WARNING):
            result = d._upload_one_file(local, "big.bin", "/remote", tmp_path)

        assert result == "uploaded"
        assert d.sftp.files["/remote/big.bin"] == content
        message = next(r.message for r in caplog.records if "[RESUME_REJECTED]" in r.message)
        assert 'reason="checkpoint_hash_mismatch"' in message
        assert f"remote_size={partial}" in message
        assert f"checkpoint_bytes={partial}" in message
        assert 'expected_hash_prefix="deadbeef"' in message
        assert 'actual_hash_prefix=' in message
        assert 'action="overwrite"' in message

    def test_offset_mismatch_reports_both_remote_and_checkpoint_sizes(
        self, uploader_factory, fake_sftp_factory, tmp_path, caplog
    ):
        content = b"R" * (up.CHUNK_SIZE * 3)
        remote_size = up.CHUNK_SIZE
        checkpoint_bytes = up.CHUNK_SIZE * 2
        local = tmp_path / "big.bin"
        _write(local, content)
        d = uploader_factory()
        d.sftp = fake_sftp_factory(files={"/remote/big.bin": content[:remote_size]})
        d._manifest = {
            "big.bin": {
                "size": len(content),
                "mtime": _mtime(local),
                "local_sha256": hashlib.sha256(content[:checkpoint_bytes]).hexdigest(),
                "local_bytes": checkpoint_bytes,
            }
        }

        with caplog.at_level(logging.WARNING):
            result = d._upload_one_file(local, "big.bin", "/remote", tmp_path)

        assert result == "uploaded"
        assert d.sftp.files["/remote/big.bin"] == content
        message = next(r.message for r in caplog.records if "[RESUME_REJECTED]" in r.message)
        assert 'reason="checkpoint_offset_mismatch"' in message
        assert f"remote_size={remote_size}" in message
        assert f"checkpoint_bytes={checkpoint_bytes}" in message

    def test_missing_checkpoint_hash_has_its_own_reason(
        self, uploader_factory, fake_sftp_factory, tmp_path, caplog
    ):
        content = b"H" * (up.CHUNK_SIZE * 2)
        partial = up.CHUNK_SIZE
        local = tmp_path / "big.bin"
        _write(local, content)
        d = uploader_factory()
        d.sftp = fake_sftp_factory(files={"/remote/big.bin": content[:partial]})
        d._manifest = {
            "big.bin": {
                "size": len(content),
                "mtime": _mtime(local),
                "local_bytes": partial,
            }
        }

        with caplog.at_level(logging.WARNING):
            d._upload_one_file(local, "big.bin", "/remote", tmp_path)

        message = next(r.message for r in caplog.records if "[RESUME_REJECTED]" in r.message)
        assert 'reason="checkpoint_hash_missing"' in message
        assert "checkpoint_hash_present=false" in message


class TestUploadOneFileCheckpointing:
    def test_checkpoint_persists_progress_during_transfer(self, uploader_factory, fake_sftp_factory, tmp_path):
        content = b"X" * (up.CHUNK_SIZE * 15)
        local = tmp_path / "big.bin"
        _write(local, content)
        d = uploader_factory()
        d.sftp = fake_sftp_factory(files={})

        d._upload_one_file(local, "big.bin", "/remote", tmp_path)

        manifest = d._load_manifest(tmp_path)
        assert manifest["big.bin"]["local_bytes"] == len(content)
        assert manifest["big.bin"]["local_sha256"] == hashlib.sha256(content).hexdigest()

    def test_transfer_error_logs_saved_checkpoint_context(
        self, uploader_factory, fake_sftp_factory, tmp_path, caplog
    ):
        content = b"E" * (up.CHUNK_SIZE * 3)
        local = tmp_path / "big.bin"
        _write(local, content)
        d = uploader_factory()
        sftp = fake_sftp_factory(files={})
        original_open = sftp.open
        writes = {"n": 0}

        def flaky_open(path, mode="rb"):
            fake_file = original_open(path, mode)
            original_write = fake_file.write

            def flaky_write(chunk):
                writes["n"] += 1
                if writes["n"] == 2:
                    raise OSError("simulated dropped connection")
                return original_write(chunk)

            fake_file.write = flaky_write
            return fake_file

        sftp.open = flaky_open
        d.sftp = sftp

        with caplog.at_level(logging.WARNING), pytest.raises(OSError):
            d._upload_one_file(local, "big.bin", "/remote", tmp_path)

        manifest = d._load_manifest(tmp_path)["big.bin"]
        assert manifest["local_bytes"] == up.CHUNK_SIZE
        message = next(r.message for r in caplog.records if "[CHECKPOINT_SAVED]" in r.message)
        assert 'direction="upload"' in message
        assert 'reason="transfer_error"' in message
        assert f"offset={up.CHUNK_SIZE}" in message
        assert "manifest_saved=true" in message

    def test_sigterm_logs_signal_and_checkpoint_context(
        self, uploader_factory, fake_sftp_factory, tmp_path, caplog
    ):
        content = b"C" * (up.CHUNK_SIZE * 3)
        local = tmp_path / "big.bin"
        _write(local, content)
        d = uploader_factory()
        sftp = fake_sftp_factory(files={})
        original_open = sftp.open
        writes = {"n": 0}

        def cancelling_open(path, mode="rb"):
            fake_file = original_open(path, mode)
            original_write = fake_file.write

            def cancelling_write(chunk):
                writes["n"] += 1
                if writes["n"] == 2:
                    raise up.TransferCancelled(15)
                return original_write(chunk)

            fake_file.write = cancelling_write
            return fake_file

        sftp.open = cancelling_open
        d.sftp = sftp

        with caplog.at_level(logging.WARNING), pytest.raises(up.TransferCancelled):
            d._upload_one_file(local, "big.bin", "/remote", tmp_path)

        manifest = d._load_manifest(tmp_path)["big.bin"]
        assert manifest["local_bytes"] == up.CHUNK_SIZE
        message = next(r.message for r in caplog.records if "[CHECKPOINT_SAVED]" in r.message)
        assert 'reason="cancelled"' in message
        assert "signal=15" in message
        assert f"offset={up.CHUNK_SIZE}" in message
        assert "manifest_saved=true" in message


class TestRun:
    def _prepare(self, uploader_factory, fake_sftp_factory, files=None, **overrides):
        d = uploader_factory(wait_for_network=False, **overrides)
        fake = fake_sftp_factory(files=files or {})
        d._connect_with_retry = MagicMock(side_effect=lambda: setattr(d, "sftp", fake))
        d._close = MagicMock()
        return d, fake

    def test_run_uploads_all_files(self, uploader_factory, fake_sftp_factory, tmp_path):
        _write(tmp_path / "a.txt", b"aaa")
        _write(tmp_path / "sub" / "b.txt", b"bbb")
        d, fake = self._prepare(uploader_factory, fake_sftp_factory)

        ok = d.run()

        assert ok is True
        assert fake.files["/remote/a.txt"] == b"aaa"
        assert fake.files["/remote/sub/b.txt"] == b"bbb"

    def test_run_returns_false_when_source_missing(self, uploader_factory, fake_sftp_factory, tmp_path):
        d, fake = self._prepare(uploader_factory, fake_sftp_factory, local_path=str(tmp_path / "nope"))
        assert d.run() is False

    def test_all_skipped_run_writes_the_manifest_once_not_once_per_file(
        self, uploader_factory, fake_sftp_factory, tmp_path
    ):
        for i in range(12):
            _write(tmp_path / ("f%02d.bin" % i), bytes([i]) * (i + 1))
        fake = fake_sftp_factory(files={})
        d = uploader_factory(wait_for_network=False)
        d._connect_with_retry = MagicMock(side_effect=lambda: setattr(d, "sftp", fake))
        d._close = MagicMock()
        assert d.run() is True  # 第一次：全新上傳

        d._save_manifest = MagicMock(side_effect=d._save_manifest)
        assert d.run() is True  # 第二次：12 個檔全部略過
        assert d._save_manifest.call_count == 1
        assert set(d._load_manifest(tmp_path)) == {"f%02d.bin" % i for i in range(12)}

    def test_skip_marks_dirty_without_touching_disk(self, uploader_factory, fake_sftp_factory, tmp_path):
        _write(tmp_path / "a.txt", b"aaa")
        d = uploader_factory()
        d.sftp = fake_sftp_factory(files={"/remote/a.txt": b"aaa"})
        assert d._upload_one_file(tmp_path / "a.txt", "a.txt", "/remote", tmp_path) == "skipped"
        assert d._manifest_dirty is True
        assert not d._manifest_path(tmp_path).exists()

    def test_log_upload_reuses_the_transfer_connection(self, uploader_factory, fake_sftp_factory, tmp_path):
        # 與下載端同一個機制：_run() 不關連線，留給 run() 收尾，log 上傳因此少一次握手。
        _write(tmp_path / "a.txt", b"aaa")
        log_file = tmp_path.parent / "run.csv"
        log_file.write_text("data", encoding="utf-8")
        fake = fake_sftp_factory(files={})
        d = uploader_factory(
            wait_for_network=False,
            upload_log=True,
            remote_log_dir="/fleet/logs",
            log_file=str(log_file),
        )
        d.logger.addHandler(logging.NullHandler())
        d._connect_with_retry = MagicMock(side_effect=lambda: setattr(d, "sftp", fake))

        assert d.run() is True
        d._connect_with_retry.assert_called_once()
        assert "/fleet/logs/run.csv" in fake.files
        assert d.sftp is None  # 收尾關連線的責任在 run()

    def test_run_closes_the_connection_when_log_upload_is_disabled(
        self, uploader_factory, fake_sftp_factory, tmp_path
    ):
        _write(tmp_path / "a.txt", b"aaa")
        fake = fake_sftp_factory(files={})
        d = uploader_factory(wait_for_network=False)
        d._connect_with_retry = MagicMock(side_effect=lambda: setattr(d, "sftp", fake))
        assert d.run() is True
        assert d.sftp is None

    def test_run_retries_upload_on_connection_error(self, uploader_factory, fake_sftp_factory, tmp_path):
        _write(tmp_path / "a.txt", b"data")
        d, fake = self._prepare(uploader_factory, fake_sftp_factory)
        d._upload_one_file = MagicMock(side_effect=[OSError("dropped"), "uploaded"])

        ok = d.run()

        assert ok is True
        assert d._upload_one_file.call_count == 2

    def test_run_mismatched_pairing_returns_false(self, uploader_factory, fake_sftp_factory, tmp_path, caplog):
        # remote 為陣列但與 local（單一）數量不符 → 配對失敗，任務中止。
        _write(tmp_path / "a.txt", b"aaa")
        d, fake = self._prepare(uploader_factory, fake_sftp_factory, remote_path=["/first", "/second"])

        with caplog.at_level(logging.ERROR):
            ok = d.run()

        assert ok is False
        assert any("配對數量不符" in r.message for r in caplog.records)


class TestRunPaired:
    """local_path 與 remote_path 皆為等長陣列時：逐一配對 local[i]→remote[i]
    （多專案各自上傳到自己的目的地，如 share/alarm_controller → STANDARD/share/alarm_controller）。"""

    def _prepare(self, uploader_factory, fake_sftp_factory, **overrides):
        d = uploader_factory(wait_for_network=False, **overrides)
        fake = fake_sftp_factory(files={})
        d._connect_with_retry = MagicMock(side_effect=lambda: setattr(d, "sftp", fake))
        d._close = MagicMock()
        return d, fake

    def test_paired_lists_map_each_source_to_its_own_remote(self, uploader_factory, fake_sftp_factory, tmp_path):
        _write(tmp_path / "alarm" / "x.py", b"alarm")
        _write(tmp_path / "board" / "y.py", b"board")
        d, fake = self._prepare(
            uploader_factory, fake_sftp_factory,
            local_path=[str(tmp_path / "alarm"), str(tmp_path / "board")],
            remote_path=["/remote/alarm_controller", "/remote/board_controller"],
        )

        ok = d.run()

        assert ok is True
        # 各專案落在自己配對的遠端目的地，不會互相攤平碰撞。
        assert fake.files["/remote/alarm_controller/x.py"] == b"alarm"
        assert fake.files["/remote/board_controller/y.py"] == b"board"
        # 同名檔案在不同配對下互不干擾。
        assert "/remote/board_controller/x.py" not in fake.files


class TestRunFanout:
    """remote_path 為「帶尾斜線的單一父目錄」+ local_path 為陣列時：
    各 local 來源依 basename 展開到 remote父目錄/basename（多專案各自上傳到自己的目錄）。"""

    def _prepare(self, uploader_factory, fake_sftp_factory, **overrides):
        d = uploader_factory(wait_for_network=False, **overrides)
        fake = fake_sftp_factory(files={})
        d._connect_with_retry = MagicMock(side_effect=lambda: setattr(d, "sftp", fake))
        d._close = MagicMock()
        return d, fake

    def test_trailing_slash_remote_parent_fans_out_by_basename(self, uploader_factory, fake_sftp_factory, tmp_path):
        _write(tmp_path / "alarm_controller" / "x.py", b"alarm")
        _write(tmp_path / "board_controller" / "y.py", b"board")
        d, fake = self._prepare(
            uploader_factory, fake_sftp_factory,
            local_path=[str(tmp_path / "alarm_controller"), str(tmp_path / "board_controller")],
            remote_path="/remote/share/",  # 尾斜線 → 展開到 /remote/share/<basename>
        )

        ok = d.run()

        assert ok is True
        assert fake.files["/remote/share/alarm_controller/x.py"] == b"alarm"
        assert fake.files["/remote/share/board_controller/y.py"] == b"board"
        # 不會攤平進 /remote/share 根目錄造成碰撞。
        assert "/remote/share/x.py" not in fake.files

    def test_no_trailing_slash_remote_still_merges(self, uploader_factory, fake_sftp_factory, tmp_path):
        # 對照組：remote 不帶尾斜線 → 維持合併（攤平進同一 remote 根）。
        _write(tmp_path / "alarm_controller" / "x.py", b"alarm")
        d, fake = self._prepare(
            uploader_factory, fake_sftp_factory,
            local_path=[str(tmp_path / "alarm_controller")],
            remote_path="/remote/share",  # 無尾斜線 → 合併
        )

        ok = d.run()

        assert ok is True
        assert fake.files["/remote/share/x.py"] == b"alarm"


class TestRunMultiSource:
    """local_path 為陣列時：多個本地來源合併上傳到單一 remote_root，
    與下載端「多來源合併到單一 local」對稱。"""

    def _prepare(self, uploader_factory, fake_sftp_factory, **overrides):
        d = uploader_factory(wait_for_network=False, **overrides)
        fake = fake_sftp_factory(files={})
        d._connect_with_retry = MagicMock(side_effect=lambda: setattr(d, "sftp", fake))
        d._close = MagicMock()
        return d, fake

    def test_multi_local_sources_merge_into_single_remote(self, uploader_factory, fake_sftp_factory, tmp_path):
        _write(tmp_path / "src1" / "a.txt", b"aaa")
        _write(tmp_path / "src2" / "b.txt", b"bbb")
        d, fake = self._prepare(
            uploader_factory, fake_sftp_factory,
            local_path=[str(tmp_path / "src1"), str(tmp_path / "src2")],
        )

        ok = d.run()

        assert ok is True
        # 兩個來源的檔案都合併進同一個 /remote 下（rel_path 相對於各自來源根）。
        assert fake.files["/remote/a.txt"] == b"aaa"
        assert fake.files["/remote/b.txt"] == b"bbb"

    def test_same_relpath_later_source_overwrites_with_warning(
        self, uploader_factory, fake_sftp_factory, tmp_path, caplog
    ):
        _write(tmp_path / "src1" / "x.txt", b"first")
        _write(tmp_path / "src2" / "x.txt", b"second")
        d, fake = self._prepare(
            uploader_factory, fake_sftp_factory,
            local_path=[str(tmp_path / "src1"), str(tmp_path / "src2")],
        )

        with caplog.at_level(logging.WARNING):
            ok = d.run()

        assert ok is True
        # 相同 rel_path：後面的來源(src2)覆蓋前面的(src1)。
        assert fake.files["/remote/x.txt"] == b"second"
        assert any("以後面的來源為準" in r.message for r in caplog.records)

    def test_missing_source_among_many_is_skipped(self, uploader_factory, fake_sftp_factory, tmp_path):
        _write(tmp_path / "src1" / "a.txt", b"aaa")
        d, fake = self._prepare(
            uploader_factory, fake_sftp_factory,
            local_path=[str(tmp_path / "src1"), str(tmp_path / "nope")],
        )

        ok = d.run()

        # 不存在的來源略過、不影響其餘來源，整體仍算成功。
        assert ok is True
        assert fake.files["/remote/a.txt"] == b"aaa"
