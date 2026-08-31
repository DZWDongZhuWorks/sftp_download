"""downloader.py 單元測試：涵蓋 Happy Path、邊界條件與錯誤處理。"""

import hashlib
import logging
import os
import stat
from pathlib import Path
from unittest.mock import MagicMock, patch

import paramiko
import pytest

import downloader as dl
from conftest import FakeSFTPAttr


# ---------------------------------------------------------------------------
# format_size
# ---------------------------------------------------------------------------

class TestFormatSize:
    def test_zero_bytes_returns_b(self):
        assert dl.format_size(0) == "0.0B"

    def test_bytes_under_1024_returns_b(self):
        assert dl.format_size(512) == "512.0B"

    def test_exact_1024_rolls_over_to_kb(self):
        assert dl.format_size(1024) == "1.0KB"

    def test_megabyte_boundary(self):
        assert dl.format_size(1024 * 1024) == "1.0MB"

    def test_gigabyte_and_beyond_stays_gb(self):
        # 超過 GB 仍以 GB 為單位顯示（不會再往上換算 TB）。
        assert dl.format_size(1024 ** 4) == "1024.0GB"


# ---------------------------------------------------------------------------
# format_exception
# ---------------------------------------------------------------------------

class TestFormatException:
    def test_includes_type_and_repr_for_empty_message_exception(self):
        assert dl.format_exception(TimeoutError()) == "TimeoutError: TimeoutError()"

    def test_includes_type_and_message(self):
        # 不寫死 repr 的長相：3.6 印的是 OSError('connection reset',)（尾逗號），
        # 3.7+ 才是 OSError('connection reset')。要測的是「型別: repr」這個格式，
        # 不是各版本的 repr —— 船上的 venv 在 Bionic 是 3.6。
        error = OSError("connection reset")
        assert dl.format_exception(error) == "OSError: {}".format(repr(error))


# ---------------------------------------------------------------------------
# _retry_limit_reached
# ---------------------------------------------------------------------------

class TestRetryLimitReached:
    def test_none_means_unlimited(self, downloader_factory):
        d = downloader_factory(retry_count=None)
        assert d._retry_limit_reached(1) is False
        assert d._retry_limit_reached(10_000) is False

    def test_zero_means_unlimited(self, downloader_factory):
        d = downloader_factory(retry_count=0)
        assert d._retry_limit_reached(999) is False

    def test_negative_means_unlimited(self, downloader_factory):
        d = downloader_factory(retry_count=-5)
        assert d._retry_limit_reached(999) is False

    def test_positive_limit_not_reached_at_boundary(self, downloader_factory):
        d = downloader_factory(retry_count=3)
        assert d._retry_limit_reached(3) is False

    def test_positive_limit_reached_just_over_boundary(self, downloader_factory):
        d = downloader_factory(retry_count=3)
        assert d._retry_limit_reached(4) is True


# ---------------------------------------------------------------------------
# _connect
# ---------------------------------------------------------------------------

class TestConnect:
    def test_socket_timeout_is_tuned_for_slow_wan_links(self):
        assert dl.SOCKET_TIMEOUT == 120

    @patch("downloader.paramiko.SSHClient")
    def test_password_auth_connects_and_configures_timeouts(self, mock_ssh_client_cls, downloader_factory):
        mock_client = MagicMock()
        mock_ssh_client_cls.return_value = mock_client
        mock_sftp = MagicMock()
        mock_client.open_sftp.return_value = mock_sftp
        mock_transport = MagicMock()
        mock_client.get_transport.return_value = mock_transport

        d = downloader_factory(password="secret")
        d._connect()

        mock_client.connect.assert_called_once_with(
            hostname="host.example.com", port=22, username="user", timeout=15, password="secret"
        )
        mock_sftp.get_channel.return_value.settimeout.assert_called_once_with(dl.SOCKET_TIMEOUT)
        mock_transport.set_keepalive.assert_called_once_with(dl.KEEPALIVE_INTERVAL)
        assert d.client is mock_client
        assert d.sftp is mock_sftp

    @patch("downloader.paramiko.SSHClient")
    def test_closes_previous_connection_before_reconnecting(self, mock_ssh_client_cls, downloader_factory):
        old_sftp = MagicMock()
        old_client = MagicMock()
        new_client = MagicMock()
        new_sftp = MagicMock()
        new_client.open_sftp.return_value = new_sftp
        mock_ssh_client_cls.return_value = new_client

        d = downloader_factory(password="secret")
        d.sftp = old_sftp
        d.client = old_client
        d._connect()

        old_sftp.close.assert_called_once()
        old_client.close.assert_called_once()
        assert d.sftp is new_sftp
        assert d.client is new_client

    @patch("downloader.paramiko.SSHClient")
    def test_failed_new_connection_is_closed(self, mock_ssh_client_cls, downloader_factory):
        new_client = MagicMock()
        new_client.connect.side_effect = OSError("refused")
        mock_ssh_client_cls.return_value = new_client

        d = downloader_factory(password="secret")
        with pytest.raises(OSError):
            d._connect()

        new_client.close.assert_called_once()
        assert d.sftp is None
        assert d.client is None

    @patch("downloader.paramiko.SSHClient")
    def test_key_file_auth_used_instead_of_password(self, mock_ssh_client_cls, downloader_factory):
        mock_client = MagicMock()
        mock_ssh_client_cls.return_value = mock_client

        d = downloader_factory(key_file="/home/user/.ssh/id_rsa", password="should-be-ignored")
        d._connect()

        _, kwargs = mock_client.connect.call_args
        assert kwargs["key_filename"] == "/home/user/.ssh/id_rsa"
        assert "password" not in kwargs

    @patch("downloader.paramiko.SSHClient")
    def test_sets_auto_add_host_key_policy(self, mock_ssh_client_cls, downloader_factory):
        mock_client = MagicMock()
        mock_ssh_client_cls.return_value = mock_client

        d = downloader_factory(password="secret")
        d._connect()

        assert mock_client.set_missing_host_key_policy.call_args[0][0].__class__ is paramiko.AutoAddPolicy


# ---------------------------------------------------------------------------
# _connect_with_retry
# ---------------------------------------------------------------------------

class TestConnectWithRetry:
    def test_succeeds_on_first_try(self, downloader_factory):
        d = downloader_factory()
        d._connect = MagicMock()
        d._connect_with_retry()
        d._connect.assert_called_once()

    def test_authentication_exception_raises_immediately_without_retry(self, downloader_factory):
        d = downloader_factory(retry_count=5)
        d._connect = MagicMock(side_effect=paramiko.AuthenticationException("bad creds"))
        with pytest.raises(paramiko.AuthenticationException):
            d._connect_with_retry()
        d._connect.assert_called_once()

    def test_retries_after_transient_error_then_succeeds(self, downloader_factory, caplog):
        d = downloader_factory(retry_count=5, wait_for_network=False)
        d._connect = MagicMock(side_effect=[OSError("refused"), OSError("refused"), None])
        with caplog.at_level(logging.WARNING):
            d._connect_with_retry()
        assert d._connect.call_count == 3
        messages = [r.message for r in caplog.records if "[CONNECTION_RETRY]" in r.message]
        assert len(messages) == 2
        assert 'attempt=1' in messages[0]
        assert 'retry_limit=5' in messages[0]
        assert "error=" in messages[0] and "OSError" in messages[0] and "refused" in messages[0]

    def test_retries_after_sftp_protocol_error(self, downloader_factory):
        d = downloader_factory(retry_count=2, wait_for_network=False)
        d._connect = MagicMock(side_effect=[paramiko.SFTPError("Garbage packet received"), None])
        d._connect_with_retry()
        assert d._connect.call_count == 2

    def test_raises_after_exceeding_retry_limit(self, downloader_factory):
        d = downloader_factory(retry_count=2, wait_for_network=False)
        d._connect = MagicMock(side_effect=paramiko.SSHException("still down"))
        with pytest.raises(paramiko.SSHException):
            d._connect_with_retry()
        assert d._connect.call_count == 3  # 初次 + 2 次重試後才放棄

    def test_auto_reconnect_disabled_raises_immediately(self, downloader_factory):
        d = downloader_factory(auto_reconnect=False)
        d._connect = MagicMock(side_effect=OSError("refused"))
        with pytest.raises(OSError):
            d._connect_with_retry()
        d._connect.assert_called_once()

    def test_waits_for_network_between_retries_when_enabled(self, downloader_factory):
        d = downloader_factory(retry_count=3, wait_for_network=True)
        d._connect = MagicMock(side_effect=[OSError("refused"), None])
        d._wait_for_network = MagicMock()
        d._connect_with_retry()
        d._wait_for_network.assert_called_once()

    def test_does_not_wait_for_network_when_disabled(self, downloader_factory):
        d = downloader_factory(retry_count=3, wait_for_network=False)
        d._connect = MagicMock(side_effect=[OSError("refused"), None])
        d._wait_for_network = MagicMock()
        d._connect_with_retry()
        d._wait_for_network.assert_not_called()


# ---------------------------------------------------------------------------
# _wait_for_network
# ---------------------------------------------------------------------------

class TestWaitForNetwork:
    @patch("downloader.socket.create_connection")
    def test_returns_immediately_when_reachable(self, mock_create_conn, downloader_factory):
        mock_create_conn.return_value.__enter__ = MagicMock()
        mock_create_conn.return_value.__exit__ = MagicMock(return_value=False)
        d = downloader_factory()
        d._wait_for_network()
        mock_create_conn.assert_called_once()

    @patch("downloader.socket.create_connection")
    def test_retries_until_reachable(self, mock_create_conn, downloader_factory):
        ok_ctx = MagicMock()
        ok_ctx.__enter__ = MagicMock()
        ok_ctx.__exit__ = MagicMock(return_value=False)
        mock_create_conn.side_effect = [OSError("unreachable"), OSError("unreachable"), ok_ctx]
        d = downloader_factory(retry_delay=0)
        d._wait_for_network()
        assert mock_create_conn.call_count == 3


# ---------------------------------------------------------------------------
# _close
# ---------------------------------------------------------------------------

class TestClose:
    def test_close_with_none_client_and_sftp_does_not_raise(self, downloader_factory):
        d = downloader_factory()
        d.sftp = None
        d.client = None
        d._close()  # 不應拋出例外

    def test_close_swallows_exceptions_from_sftp_and_client(self, downloader_factory):
        d = downloader_factory()
        sftp = MagicMock()
        sftp.close.side_effect = Exception("already closed")
        client = MagicMock()
        client.close.side_effect = Exception("already closed")
        d.sftp = sftp
        d.client = client
        d._close()  # 不應向外拋出
        sftp.close.assert_called_once()
        client.close.assert_called_once()

    def test_close_clears_stale_connection_references(self, downloader_factory):
        d = downloader_factory()
        d.sftp = MagicMock()
        d.client = MagicMock()
        d._close()
        assert d.sftp is None
        assert d.client is None


# ---------------------------------------------------------------------------
# _list_remote_files / _walk_remote_dir
# ---------------------------------------------------------------------------

class TestListRemoteFiles:
    def test_single_remote_file_returns_one_entry(self, downloader_factory, fake_sftp_factory, tmp_path):
        d = downloader_factory(remote_path="/remote/report.csv")
        d.sftp = fake_sftp_factory(files={"/remote/report.csv": b"data"})
        files = d._list_remote_files("/remote/report.csv", tmp_path)
        assert [(remote, rel) for remote, rel, _ in files] == [("/remote/report.csv", "report.csv")]

    def test_recursive_directory_lists_all_nested_files(self, downloader_factory, fake_sftp_factory, tmp_path):
        d = downloader_factory(recursive=True)
        d.sftp = fake_sftp_factory(files={
            "/remote/a.txt": b"a",
            "/remote/sub/b.txt": b"b",
            "/remote/sub/deeper/c.txt": b"c",
        })
        files = d._list_remote_files("/remote", tmp_path)
        rels = sorted(rel for _, rel, _attr in files)
        assert rels == ["a.txt", "sub/b.txt", "sub/deeper/c.txt"]

    def test_recursive_creates_empty_subdirectories_locally(self, downloader_factory, fake_sftp_factory, tmp_path):
        d = downloader_factory(recursive=True)
        sftp = fake_sftp_factory(files={"/remote/a.txt": b"a"})
        # 手動補一個沒有任何檔案的空資料夾（FakeSFTPClient 靠檔案路徑推導資料夾，這裡直接擴充 listdir_attr 行為）
        original_listdir = sftp.listdir_attr

        def listdir_with_empty_dir(path):
            entries = original_listdir(path)
            if path.rstrip("/") == "/remote":
                entries.append(FakeSFTPAttr("empty_sub", True))
            return entries

        sftp.listdir_attr = listdir_with_empty_dir
        d.sftp = sftp
        files = d._list_remote_files("/remote", tmp_path)
        assert [rel for _, rel, _attr in files] == ["a.txt"]
        assert (tmp_path / "empty_sub").is_dir()

    def test_single_level_mode_skips_subdirectories(self, downloader_factory, fake_sftp_factory, tmp_path):
        d = downloader_factory(recursive=False)
        d.sftp = fake_sftp_factory(files={"/remote/a.txt": b"a", "/remote/sub/b.txt": b"b"})
        files = d._list_remote_files("/remote", tmp_path)
        assert [rel for _, rel, _attr in files] == ["a.txt"]

    def test_single_level_mode_logs_skipped_directory_count(self, downloader_factory, fake_sftp_factory, tmp_path, caplog):
        d = downloader_factory(recursive=False)
        d.sftp = fake_sftp_factory(files={"/remote/a.txt": b"a", "/remote/sub/b.txt": b"b"})
        with caplog.at_level(logging.INFO):
            d._list_remote_files("/remote", tmp_path)
        assert any("略過 1 個子資料夾" in r.message for r in caplog.records)

    def test_remote_path_not_found_raises_file_not_found(self, downloader_factory, fake_sftp_factory, tmp_path):
        d = downloader_factory()
        d.sftp = fake_sftp_factory(files={})
        with pytest.raises(FileNotFoundError):
            d._list_remote_files("/remote/missing", tmp_path)


# ---------------------------------------------------------------------------
# 下載忽略設定檔（_load_ignore_spec / _is_ignored / 列表過濾）
# ---------------------------------------------------------------------------

class TestIgnoreSpec:
    def _make_with_ignore(self, downloader_factory, tmp_path, rules, **overrides):
        ignore_path = tmp_path / "download_ignore.txt"
        ignore_path.write_text(rules, encoding="utf-8")
        d = downloader_factory(ignore_file=str(ignore_path), **overrides)
        d._ignore_spec = d._load_ignore_spec()
        return d

    def test_no_ignore_file_configured_returns_none(self, downloader_factory):
        d = downloader_factory()
        assert d._load_ignore_spec() is None

    def test_missing_ignore_file_means_no_ignore_and_logs_info(self, downloader_factory, tmp_path, caplog):
        d = downloader_factory(ignore_file=str(tmp_path / "not_exist.txt"))
        with caplog.at_level(logging.INFO):
            assert d._load_ignore_spec() is None
        assert any("忽略設定檔不存在" in r.message for r in caplog.records)

    def test_invalid_line_is_skipped_with_warning_but_other_rules_still_apply(self, downloader_factory, tmp_path, caplog):
        # "!" 單獨一行是不合法的 gitignore 規則，應跳過並警告；"*.tmp" 仍要生效。
        with caplog.at_level(logging.WARNING):
            d = self._make_with_ignore(downloader_factory, tmp_path, "!\n*.tmp\n")
        assert any("格式錯誤" in r.message for r in caplog.records)
        assert d._is_ignored("a.tmp")
        assert not d._is_ignored("a.txt")

    def test_utf8_bom_and_crlf_do_not_break_first_rule(self, downloader_factory, tmp_path):
        """Windows 記事本以 UTF-8 存檔常帶 BOM 且用 CRLF 換行；BOM 若沒去除會黏在
        第一行規則前面，導致第一條規則永遠比對不到（實際回報過的問題）。"""
        ignore_path = tmp_path / "download_ignore.txt"
        ignore_path.write_bytes("a.txt\r\nb.txt\r\n".encode("utf-8-sig"))
        d = downloader_factory(ignore_file=str(ignore_path))
        d._ignore_spec = d._load_ignore_spec()
        assert d._is_ignored("a.txt")  # 第一行規則（緊接在 BOM 後）也要生效
        assert d._is_ignored("b.txt")
        assert not d._is_ignored("c.txt")

    def test_comments_and_blank_lines_do_not_warn(self, downloader_factory, tmp_path, caplog):
        with caplog.at_level(logging.WARNING):
            self._make_with_ignore(downloader_factory, tmp_path, "# 註解\n\n*.tmp\n")
        assert not any("格式錯誤" in r.message for r in caplog.records)

    def test_negation_rule_re_includes_file(self, downloader_factory, tmp_path):
        d = self._make_with_ignore(downloader_factory, tmp_path, "*.tmp\n!keep.tmp\n")
        assert d._is_ignored("a.tmp")
        assert not d._is_ignored("keep.tmp")

    def test_recursive_listing_filters_ignored_files(self, downloader_factory, fake_sftp_factory, tmp_path):
        d = self._make_with_ignore(downloader_factory, tmp_path, "*.tmp\n", recursive=True)
        d.sftp = fake_sftp_factory(files={
            "/remote/a.txt": b"a",
            "/remote/b.tmp": b"b",
            "/remote/sub/c.tmp": b"c",
            "/remote/sub/d.txt": b"d",
        })
        files = d._list_remote_files("/remote", tmp_path)
        assert sorted(rel for _, rel, _attr in files) == ["a.txt", "sub/d.txt"]

    def test_recursive_listing_prunes_ignored_directory_entirely(self, downloader_factory, fake_sftp_factory, tmp_path, caplog):
        d = self._make_with_ignore(downloader_factory, tmp_path, "logs/\n", recursive=True)
        d.sftp = fake_sftp_factory(files={
            "/remote/a.txt": b"a",
            "/remote/logs/x.log": b"x",
            "/remote/logs/deep/y.log": b"y",
        })
        with caplog.at_level(logging.INFO):
            files = d._list_remote_files("/remote", tmp_path)
        assert [rel for _, rel, _attr in files] == ["a.txt"]
        # 整棵資料夾剪枝：本地端不建立被忽略的資料夾
        assert not (tmp_path / "logs").exists()
        assert any("略過資料夾" in r.message for r in caplog.records)

    def test_single_level_listing_filters_ignored_files(self, downloader_factory, fake_sftp_factory, tmp_path):
        d = self._make_with_ignore(downloader_factory, tmp_path, "*.tmp\n", recursive=False)
        d.sftp = fake_sftp_factory(files={"/remote/a.txt": b"a", "/remote/b.tmp": b"b"})
        files = d._list_remote_files("/remote", tmp_path)
        assert [rel for _, rel, _attr in files] == ["a.txt"]

    def test_single_remote_file_matching_rule_is_ignored(self, downloader_factory, fake_sftp_factory, tmp_path):
        d = self._make_with_ignore(downloader_factory, tmp_path, "report.csv\n")
        d.sftp = fake_sftp_factory(files={"/remote/report.csv": b"data"})
        assert d._list_remote_files("/remote/report.csv", tmp_path) == []

    def test_no_spec_loaded_nothing_is_ignored(self, downloader_factory):
        d = downloader_factory()
        assert not d._is_ignored("anything.txt")

    def test_run_loads_ignore_spec_and_skips_ignored_files(self, downloader_factory, fake_sftp_factory, tmp_path):
        ignore_path = tmp_path / "download_ignore.txt"
        ignore_path.write_text("*.tmp\n", encoding="utf-8")
        d = downloader_factory(wait_for_network=False, ignore_file=str(ignore_path))
        d._connect_with_retry = MagicMock(side_effect=lambda: setattr(
            d, "sftp",
            fake_sftp_factory(files={"/remote/a.txt": b"A", "/remote/b.tmp": b"B"},
                              mtimes={"/remote/a.txt": 1, "/remote/b.tmp": 2}),
        ))
        d._close = MagicMock()
        assert d.run() is True
        assert (Path(d.local_path) / "a.txt").exists()
        assert not (Path(d.local_path) / "b.tmp").exists()


# ---------------------------------------------------------------------------
# manifest load / save
# ---------------------------------------------------------------------------

class TestManifestPersistence:
    def test_load_missing_manifest_returns_empty_dict(self, downloader_factory, tmp_path):
        d = downloader_factory()
        assert d._load_manifest(tmp_path) == {}

    def test_save_then_load_round_trips(self, downloader_factory, tmp_path):
        d = downloader_factory()
        d._manifest = {"a.txt": {"size": 10, "mtime": 123}}
        d._save_manifest(tmp_path)
        loaded = d._load_manifest(tmp_path)
        assert loaded == {"a.txt": {"size": 10, "mtime": 123}}

    def test_load_corrupt_json_returns_empty_dict_and_warns(self, downloader_factory, tmp_path, caplog):
        d = downloader_factory()
        manifest_path = d._manifest_path(tmp_path)
        manifest_path.write_text("{not valid json", encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            result = d._load_manifest(tmp_path)
        assert result == {}
        message = next(r.message for r in caplog.records if "[MANIFEST_ERROR]" in r.message)
        assert "讀取失敗" in message
        assert 'reason="read_failed"' in message
        assert f'path="{manifest_path}"' in message
        assert 'action="ignore_manifest"' in message

    def test_load_non_object_root_reports_schema_error(self, downloader_factory, tmp_path, caplog):
        d = downloader_factory()
        manifest_path = d._manifest_path(tmp_path)
        manifest_path.write_text("[]", encoding="utf-8")

        with caplog.at_level(logging.WARNING):
            result = d._load_manifest(tmp_path)

        assert result == {}
        message = next(r.message for r in caplog.records if "[MANIFEST_ERROR]" in r.message)
        assert 'reason="invalid_root_type"' in message
        assert 'actual_type="list"' in message

    def test_invalid_entry_only_disables_tracking_for_that_file(
        self, downloader_factory, tmp_path, caplog
    ):
        d = downloader_factory()
        d._manifest = {"bad.bin": "not-an-object", "good.bin": {"size": 1}}

        with caplog.at_level(logging.WARNING):
            assert d._manifest_entry("bad.bin", tmp_path) is None

        assert d._manifest_entry("good.bin", tmp_path) == {"size": 1}
        message = next(r.message for r in caplog.records if "[MANIFEST_ERROR]" in r.message)
        assert 'reason="invalid_entry_type"' in message
        assert 'file="bad.bin"' in message
        assert 'action="ignore_entry"' in message

    def test_save_failure_is_caught_and_logged(self, downloader_factory, tmp_path, caplog):
        d = downloader_factory()
        d._manifest = {"a.txt": {"size": 1}}
        with patch("builtins.open", side_effect=OSError("disk full")):
            with caplog.at_level(logging.WARNING):
                d._save_manifest(tmp_path)  # 不應拋出例外
        assert any("寫入失敗" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# _next_duplicate_path
# ---------------------------------------------------------------------------

class TestNextDuplicatePath:
    def test_no_conflict_returns_plain_copy_name(self, downloader_factory, tmp_path):
        d = downloader_factory(duplicate_suffix="copy")
        target = tmp_path / "report.csv"
        result = d._next_duplicate_path(target)
        assert result == tmp_path / "report_copy.csv"

    def test_one_conflict_returns_numbered_suffix(self, downloader_factory, tmp_path):
        d = downloader_factory(duplicate_suffix="copy")
        target = tmp_path / "report.csv"
        (tmp_path / "report_copy.csv").write_bytes(b"x")
        result = d._next_duplicate_path(target)
        assert result == tmp_path / "report_copy1.csv"

    def test_multiple_conflicts_increment_correctly(self, downloader_factory, tmp_path):
        d = downloader_factory(duplicate_suffix="copy")
        target = tmp_path / "report.csv"
        (tmp_path / "report_copy.csv").write_bytes(b"x")
        (tmp_path / "report_copy1.csv").write_bytes(b"x")
        (tmp_path / "report_copy2.csv").write_bytes(b"x")
        result = d._next_duplicate_path(target)
        assert result == tmp_path / "report_copy3.csv"

    def test_custom_suffix_is_respected(self, downloader_factory, tmp_path):
        d = downloader_factory(duplicate_suffix="backup")
        target = tmp_path / "report.csv"
        result = d._next_duplicate_path(target)
        assert result == tmp_path / "report_backup.csv"


# ---------------------------------------------------------------------------
# _hash_local_file
# ---------------------------------------------------------------------------

class TestHashLocalFile:
    def test_computes_correct_sha256(self, downloader_factory, tmp_path):
        d = downloader_factory()
        content = b"hello world" * 1000
        f = tmp_path / "data.bin"
        f.write_bytes(content)
        result = d._hash_local_file(f)
        assert result.hexdigest() == hashlib.sha256(content).hexdigest()

    def test_empty_file_hashes_to_empty_digest(self, downloader_factory, tmp_path):
        d = downloader_factory()
        f = tmp_path / "empty.bin"
        f.write_bytes(b"")
        result = d._hash_local_file(f)
        assert result.hexdigest() == hashlib.sha256(b"").hexdigest()


# ---------------------------------------------------------------------------
# _download_one_file — the core state machine
# ---------------------------------------------------------------------------

class TestDownloadOneFileFreshDownload:
    def test_new_file_downloads_full_content(self, downloader_factory, fake_sftp_factory, tmp_path):
        d = downloader_factory()
        d.sftp = fake_sftp_factory(files={"/remote/a.txt": b"hello world"}, mtimes={"/remote/a.txt": 1000})
        result = d._download_one_file("/remote/a.txt", "a.txt", tmp_path)
        assert result == "downloaded"
        assert (tmp_path / "a.txt").read_bytes() == b"hello world"

    def test_nested_relative_path_creates_parent_directories(self, downloader_factory, fake_sftp_factory, tmp_path):
        d = downloader_factory()
        d.sftp = fake_sftp_factory(files={"/remote/sub/b.txt": b"nested"}, mtimes={"/remote/sub/b.txt": 1000})
        d._download_one_file("/remote/sub/b.txt", "sub/b.txt", tmp_path)
        assert (tmp_path / "sub" / "b.txt").read_bytes() == b"nested"


class TestDownloadPreservesModeAndMtime:
    def test_downloaded_file_mirrors_remote_mode_and_mtime(self, downloader_factory, fake_sftp_factory, tmp_path):
        d = downloader_factory()
        # FakeSFTPAttr 對一般檔案回 st_mode=S_IFREG|0o644、st_atime=st_mtime。
        d.sftp = fake_sftp_factory(files={"/remote/x.sh": b"#!/bin/sh\n"}, mtimes={"/remote/x.sh": 1234567})
        d._download_one_file("/remote/x.sh", "x.sh", tmp_path)
        st = os.stat(tmp_path / "x.sh")
        assert stat.S_IMODE(st.st_mode) == 0o644     # 權限鏡射自來源(而非本地 umask 預設)
        assert int(st.st_mtime) == 1234567           # mtime 保留


class TestDownloadOneFileResumeDisabled:
    def test_resume_disabled_overwrite_mode_replaces_in_place(self, downloader_factory, fake_sftp_factory, tmp_path):
        (tmp_path / "f.json").write_bytes(b"OLD")
        d = downloader_factory(resume=False, duplicate_mode="overwrite")
        d.sftp = fake_sftp_factory(files={"/remote/f.json": b"NEW-DATA"}, mtimes={"/remote/f.json": 1000})
        result = d._download_one_file("/remote/f.json", "f.json", tmp_path)
        assert result == "downloaded"
        assert (tmp_path / "f.json").read_bytes() == b"NEW-DATA"
        assert not (tmp_path / "f_copy.json").exists()

    def test_resume_disabled_duplicate_mode_creates_new_file(self, downloader_factory, fake_sftp_factory, tmp_path):
        (tmp_path / "f.json").write_bytes(b"OLD")
        d = downloader_factory(resume=False, duplicate_mode="duplicate")
        d.sftp = fake_sftp_factory(files={"/remote/f.json": b"NEW-DATA"}, mtimes={"/remote/f.json": 1000})
        result = d._download_one_file("/remote/f.json", "f.json", tmp_path)
        assert result == "downloaded"
        assert (tmp_path / "f.json").read_bytes() == b"OLD"
        assert (tmp_path / "f_copy.json").read_bytes() == b"NEW-DATA"


class TestDownloadOneFileSameSize:
    def test_no_manifest_entry_and_same_size_skips_and_bootstraps_manifest(self, downloader_factory, fake_sftp_factory, tmp_path):
        (tmp_path / "legacy.bin").write_bytes(b"SAMESIZE12")
        d = downloader_factory()
        d.sftp = fake_sftp_factory(files={"/remote/legacy.bin": b"SAMESIZE99"}, mtimes={"/remote/legacy.bin": 5000})
        result = d._download_one_file("/remote/legacy.bin", "legacy.bin", tmp_path)
        assert result == "skipped"
        assert (tmp_path / "legacy.bin").read_bytes() == b"SAMESIZE12"  # 內容未被覆蓋
        assert d._manifest["legacy.bin"] == {"size": 10, "mtime": 5000}

    def test_manifest_confirms_unchanged_skips(self, downloader_factory, fake_sftp_factory, tmp_path):
        d = downloader_factory(duplicate_mode="duplicate")
        d.sftp = fake_sftp_factory(files={"/remote/f.bin": b"STABLE"}, mtimes={"/remote/f.bin": 1000})
        d._download_one_file("/remote/f.bin", "f.bin", tmp_path)  # 建立紀錄
        result = d._download_one_file("/remote/f.bin", "f.bin", tmp_path)  # 第二次應略過
        assert result == "skipped"
        assert not (tmp_path / "f_copy.bin").exists()

    def test_same_size_but_manifest_mtime_mismatch_overwrite_mode_replaces(self, downloader_factory, fake_sftp_factory, tmp_path):
        """驗證原本 manifest 功能的核心目的：大小相同、內容其實已更新（用 mtime 判斷出來）。"""
        d = downloader_factory(duplicate_mode="overwrite")
        d.sftp = fake_sftp_factory(files={"/remote/f.bin": b"HELLO"}, mtimes={"/remote/f.bin": 1000})
        d._download_one_file("/remote/f.bin", "f.bin", tmp_path)
        d.sftp = fake_sftp_factory(files={"/remote/f.bin": b"WORLD"}, mtimes={"/remote/f.bin": 2000})  # 同大小、不同 mtime
        result = d._download_one_file("/remote/f.bin", "f.bin", tmp_path)
        assert result == "downloaded"
        assert (tmp_path / "f.bin").read_bytes() == b"WORLD"

    def test_same_size_but_manifest_mtime_mismatch_duplicate_mode_creates_new_file(self, downloader_factory, fake_sftp_factory, tmp_path):
        d = downloader_factory(duplicate_mode="duplicate")
        d.sftp = fake_sftp_factory(files={"/remote/f.bin": b"HELLO"}, mtimes={"/remote/f.bin": 1000})
        d._download_one_file("/remote/f.bin", "f.bin", tmp_path)
        d.sftp = fake_sftp_factory(files={"/remote/f.bin": b"WORLD"}, mtimes={"/remote/f.bin": 2000})
        result = d._download_one_file("/remote/f.bin", "f.bin", tmp_path)
        assert result == "downloaded"
        assert (tmp_path / "f.bin").read_bytes() == b"HELLO"  # 原檔不動
        assert (tmp_path / "f_copy.bin").read_bytes() == b"WORLD"


class TestDownloadOneFileLocalBigger:
    def test_local_bigger_overwrite_mode_replaces_in_place(self, downloader_factory, fake_sftp_factory, tmp_path):
        (tmp_path / "f.bin").write_bytes(b"THIS-LOCAL-FILE-IS-QUITE-LONG")
        d = downloader_factory(duplicate_mode="overwrite")
        d.sftp = fake_sftp_factory(files={"/remote/f.bin": b"SHORT"}, mtimes={"/remote/f.bin": 1000})
        result = d._download_one_file("/remote/f.bin", "f.bin", tmp_path)
        assert result == "downloaded"
        assert (tmp_path / "f.bin").read_bytes() == b"SHORT"
        assert not (tmp_path / "f_copy.bin").exists()

    def test_local_bigger_duplicate_mode_creates_new_file(self, downloader_factory, fake_sftp_factory, tmp_path):
        (tmp_path / "f.bin").write_bytes(b"THIS-LOCAL-FILE-IS-QUITE-LONG")
        d = downloader_factory(duplicate_mode="duplicate")
        d.sftp = fake_sftp_factory(files={"/remote/f.bin": b"SHORT"}, mtimes={"/remote/f.bin": 1000})
        result = d._download_one_file("/remote/f.bin", "f.bin", tmp_path)
        assert result == "downloaded"
        assert (tmp_path / "f.bin").read_bytes() == b"THIS-LOCAL-FILE-IS-QUITE-LONG"
        assert (tmp_path / "f_copy.bin").read_bytes() == b"SHORT"


class TestDownloadOneFileLocalSmallerDuplicateMode:
    def test_duplicate_mode_never_resumes_always_creates_new_file(self, downloader_factory, fake_sftp_factory, tmp_path):
        (tmp_path / "f.bin").write_bytes(b"SMALL")
        d = downloader_factory(duplicate_mode="duplicate")
        d.sftp = fake_sftp_factory(files={"/remote/f.bin": b"MUCH-BIGGER-CONTENT"}, mtimes={"/remote/f.bin": 1000})
        result = d._download_one_file("/remote/f.bin", "f.bin", tmp_path)
        assert result == "downloaded"
        assert (tmp_path / "f.bin").read_bytes() == b"SMALL"
        assert (tmp_path / "f_copy.bin").read_bytes() == b"MUCH-BIGGER-CONTENT"


class TestDownloadOneFileLocalSmallerOverwriteMode:
    def test_verified_same_version_resumes_via_append(
        self, downloader_factory, fake_sftp_factory, tmp_path, caplog
    ):
        full_content = b"AAAAABBBBBCCCCCDDDDDEEEEE"
        # 沒下載完的內容留在暫存檔，目的地此時還不存在（見 downloader.PART_SUFFIX）
        (tmp_path / ("f.bin" + dl.PART_SUFFIX)).write_bytes(full_content[:10])
        d = downloader_factory(duplicate_mode="overwrite")
        d._manifest = {
            "f.bin": {
                "size": len(full_content),
                "mtime": 1000,
                "local_sha256": hashlib.sha256(full_content[:10]).hexdigest(),
                "local_bytes": 10,
            }
        }
        d.sftp = fake_sftp_factory(files={"/remote/f.bin": full_content}, mtimes={"/remote/f.bin": 1000})
        with caplog.at_level(logging.INFO):
            result = d._download_one_file("/remote/f.bin", "f.bin", tmp_path)
        assert result == "downloaded"
        assert (tmp_path / "f.bin").read_bytes() == full_content
        assert not (tmp_path / "f_copy.bin").exists()
        assert not (tmp_path / ("f.bin" + dl.PART_SUFFIX)).exists(), "完成後暫存檔應已換名到目的地"
        message = next(r.message for r in caplog.records if "[RESUME_ACCEPTED]" in r.message)
        assert 'direction="download"' in message
        assert "resume_offset=10" in message
        assert f"remaining_bytes={len(full_content) - 10}" in message

    def test_hash_mismatch_tampered_local_file_falls_back_to_full_redownload(
        self, downloader_factory, fake_sftp_factory, tmp_path, caplog
    ):
        full_content = b"ORIGINAL-CONTENT-DATA"
        (tmp_path / ("f.bin" + dl.PART_SUFFIX)).write_bytes(b"TAMPERED12")  # 與紀錄檔中的雜湊對不上
        d = downloader_factory(duplicate_mode="overwrite")
        d._manifest = {
            "f.bin": {
                "size": len(full_content),
                "mtime": 1000,
                "local_sha256": hashlib.sha256(b"DIFFERENT-PREFIX").hexdigest(),
                "local_bytes": 10,
            }
        }
        d.sftp = fake_sftp_factory(files={"/remote/f.bin": full_content}, mtimes={"/remote/f.bin": 1000})
        with caplog.at_level(logging.WARNING):
            result = d._download_one_file("/remote/f.bin", "f.bin", tmp_path)
        assert result == "downloaded"
        assert (tmp_path / "f.bin").read_bytes() == full_content
        message = next(r.message for r in caplog.records if "[RESUME_REJECTED]" in r.message)
        assert 'reason="checkpoint_hash_mismatch"' in message
        assert "partial_bytes=10" in message
        assert "checkpoint_bytes=10" in message
        assert 'action="restart"' in message

    def test_checkpoint_offset_mismatch_reports_both_sizes(
        self, downloader_factory, fake_sftp_factory, tmp_path, caplog
    ):
        full_content = b"O" * 30
        part_size = 10
        checkpoint_bytes = 8
        (tmp_path / ("f.bin" + dl.PART_SUFFIX)).write_bytes(full_content[:part_size])
        d = downloader_factory(duplicate_mode="overwrite")
        d._manifest = {
            "f.bin": {
                "size": len(full_content),
                "mtime": 1000,
                "local_sha256": hashlib.sha256(full_content[:checkpoint_bytes]).hexdigest(),
                "local_bytes": checkpoint_bytes,
            }
        }
        d.sftp = fake_sftp_factory(files={"/remote/f.bin": full_content}, mtimes={"/remote/f.bin": 1000})

        with caplog.at_level(logging.WARNING):
            result = d._download_one_file("/remote/f.bin", "f.bin", tmp_path)

        assert result == "downloaded"
        assert (tmp_path / "f.bin").read_bytes() == full_content
        message = next(r.message for r in caplog.records if "[RESUME_REJECTED]" in r.message)
        assert 'reason="checkpoint_offset_mismatch"' in message
        assert f"partial_bytes={part_size}" in message
        assert f"checkpoint_bytes={checkpoint_bytes}" in message

    def test_remote_version_changed_falls_back_to_full_redownload(self, downloader_factory, fake_sftp_factory, tmp_path):
        """就算本地雜湊本身沒問題，只要遠端版本（size/mtime）跟紀錄不符，就不能信任接續。"""
        old_full = b"OLD-VERSION-CONTENT"
        (tmp_path / ("f.bin" + dl.PART_SUFFIX)).write_bytes(old_full[:5])
        d = downloader_factory(duplicate_mode="overwrite")
        d._manifest = {
            "f.bin": {
                "size": len(old_full),
                "mtime": 1000,  # 舊版本的 mtime
                "local_sha256": hashlib.sha256(old_full[:5]).hexdigest(),
                "local_bytes": 5,
            }
        }
        new_full = b"BRAND-NEW-VERSION-CONTENT"
        d.sftp = fake_sftp_factory(files={"/remote/f.bin": new_full}, mtimes={"/remote/f.bin": 9999})  # mtime 已變
        result = d._download_one_file("/remote/f.bin", "f.bin", tmp_path)
        assert result == "downloaded"
        assert (tmp_path / "f.bin").read_bytes() == new_full

    def test_no_checkpoint_at_all_conservatively_redownloads(self, downloader_factory, fake_sftp_factory, tmp_path):
        (tmp_path / "f.bin").write_bytes(b"SOME-OLD-STUFF")
        d = downloader_factory(duplicate_mode="overwrite")
        d._manifest = {}  # 完全沒有版本紀錄
        d.sftp = fake_sftp_factory(files={"/remote/f.bin": b"COMPLETELY-DIFFERENT-BIGGER-CONTENT"}, mtimes={"/remote/f.bin": 5000})
        result = d._download_one_file("/remote/f.bin", "f.bin", tmp_path)
        assert result == "downloaded"
        assert (tmp_path / "f.bin").read_bytes() == b"COMPLETELY-DIFFERENT-BIGGER-CONTENT"

    def test_resume_only_reads_remaining_bytes_not_already_downloaded_portion(self, downloader_factory, fake_sftp_factory, tmp_path):
        """效能保證：驗證接續下載時不會重新從遠端讀取已下載的部分（只讀本機雜湊）。"""
        full_content = b"A" * 6000 + b"B" * 4000
        (tmp_path / ("f.bin" + dl.PART_SUFFIX)).write_bytes(full_content[:6000])
        d = downloader_factory(duplicate_mode="overwrite")
        d._manifest = {
            "f.bin": {
                "size": len(full_content),
                "mtime": 1000,
                "local_sha256": hashlib.sha256(full_content[:6000]).hexdigest(),
                "local_bytes": 6000,
            }
        }
        sftp = fake_sftp_factory(files={"/remote/f.bin": full_content}, mtimes={"/remote/f.bin": 1000})
        read_sizes = []
        original_open = sftp.open

        def tracking_open(path, mode="rb"):
            fake_file = original_open(path, mode)
            original_read = fake_file.read

            def tracked_read(n=-1):
                chunk = original_read(n)
                read_sizes.append(len(chunk))
                return chunk

            fake_file.read = tracked_read
            return fake_file

        sftp.open = tracking_open
        d.sftp = sftp
        result = d._download_one_file("/remote/f.bin", "f.bin", tmp_path)
        assert result == "downloaded"
        assert (tmp_path / "f.bin").read_bytes() == full_content
        assert sum(read_sizes) == 4000, "只應該從遠端讀取剩餘的 4000 bytes，不應重新讀取已下載的 6000 bytes"


class TestDownloadOneFileCheckpointing:
    def test_checkpoint_persists_progress_during_transfer(self, downloader_factory, fake_sftp_factory, tmp_path):
        # chunk 大小是 32768，構造夠大的檔案讓進度跨越多個 10% 門檻
        content = b"X" * (dl.CHUNK_SIZE * 15)
        d = downloader_factory()
        d.sftp = fake_sftp_factory(files={"/remote/big.bin": content}, mtimes={"/remote/big.bin": 42})
        d._download_one_file("/remote/big.bin", "big.bin", tmp_path)
        manifest = d._load_manifest(tmp_path)
        assert manifest["big.bin"]["local_bytes"] == len(content)
        assert manifest["big.bin"]["local_sha256"] == hashlib.sha256(content).hexdigest()

    def test_interrupted_transfer_still_checkpoints_partial_progress(self, downloader_factory, fake_sftp_factory, tmp_path):
        """下載中途丟例外時，finally 仍要存下已寫入的進度，讓下次重試能安全接續。"""
        content = b"Y" * (dl.CHUNK_SIZE * 3)
        d = downloader_factory()
        sftp = fake_sftp_factory(files={"/remote/f.bin": content}, mtimes={"/remote/f.bin": 77})

        call_count = {"n": 0}
        original_open = sftp.open

        def flaky_open(path, mode="rb"):
            fake_file = original_open(path, mode)
            original_read = fake_file.read

            def flaky_read(n=-1):
                call_count["n"] += 1
                if call_count["n"] == 2:
                    raise OSError("simulated dropped connection")
                return original_read(n)

            fake_file.read = flaky_read
            return fake_file

        sftp.open = flaky_open
        d.sftp = sftp

        with pytest.raises(OSError):
            d._download_one_file("/remote/f.bin", "f.bin", tmp_path)

        manifest = d._load_manifest(tmp_path)
        assert manifest["f.bin"]["local_bytes"] == dl.CHUNK_SIZE  # 只成功寫入了第一個 chunk
        partial_on_disk = (tmp_path / ("f.bin" + dl.PART_SUFFIX)).read_bytes()
        assert len(partial_on_disk) == dl.CHUNK_SIZE
        # 半截內容只存在於暫存檔；目的地在下載完成前不該出現
        assert not (tmp_path / "f.bin").exists()

    def test_sigterm_cancellation_checkpoints_exact_progress_and_resumes(
        self, downloader_factory, fake_sftp_factory, tmp_path, caplog
    ):
        """SIGTERM 必須保存精確 offset；下一輪只讀剩餘 bytes，不退回 10% checkpoint。"""
        content = b"C" * (dl.CHUNK_SIZE * 4)
        target = tmp_path / "f.bin"
        target.write_bytes(b"old-complete-version")
        d = downloader_factory()
        first_sftp = fake_sftp_factory(files={"/remote/f.bin": content}, mtimes={"/remote/f.bin": 88})
        original_open = first_sftp.open
        reads = {"n": 0}

        def cancelling_open(path, mode="rb"):
            fake_file = original_open(path, mode)
            original_read = fake_file.read

            def cancelling_read(n=-1):
                reads["n"] += 1
                if reads["n"] == 3:
                    raise dl.TransferCancelled(15)
                return original_read(n)

            fake_file.read = cancelling_read
            return fake_file

        first_sftp.open = cancelling_open
        d.sftp = first_sftp
        with pytest.raises(dl.TransferCancelled):
            d._download_one_file("/remote/f.bin", "f.bin", tmp_path)

        part = tmp_path / ("f.bin" + dl.PART_SUFFIX)
        manifest = d._load_manifest(tmp_path)["f.bin"]
        assert manifest["size"] == len(content)
        assert manifest["mtime"] == 88
        assert manifest["local_bytes"] == dl.CHUNK_SIZE * 2
        assert manifest["local_bytes"] == part.stat().st_size
        assert manifest["local_sha256"] == hashlib.sha256(part.read_bytes()).hexdigest()
        assert target.read_bytes() == b"old-complete-version"
        assert any(
            "取消 checkpoint: f.bin offset={}/{}".format(dl.CHUNK_SIZE * 2, len(content)) in record.message
            for record in caplog.records
        )

        resumed = downloader_factory()
        resumed._manifest = resumed._load_manifest(tmp_path)
        second_sftp = fake_sftp_factory(files={"/remote/f.bin": content}, mtimes={"/remote/f.bin": 88})
        resumed_read_sizes = []
        second_open = second_sftp.open

        def tracking_open(path, mode="rb"):
            fake_file = second_open(path, mode)
            original_read = fake_file.read

            def tracking_read(n=-1):
                chunk = original_read(n)
                resumed_read_sizes.append(len(chunk))
                return chunk

            fake_file.read = tracking_read
            return fake_file

        second_sftp.open = tracking_open
        resumed.sftp = second_sftp
        assert resumed._download_one_file("/remote/f.bin", "f.bin", tmp_path) == "downloaded"
        assert target.read_bytes() == content
        assert sum(resumed_read_sizes) == len(content) - dl.CHUNK_SIZE * 2

    def test_sigterm_after_local_write_rehashes_the_actual_part(
        self, downloader_factory, fake_sftp_factory, tmp_path, monkeypatch
    ):
        """signal 落在 write 與 running counter 之間，manifest 仍須以磁碟實況為準。"""
        content = b"W" * (dl.CHUNK_SIZE * 3)
        d = downloader_factory()
        d.sftp = fake_sftp_factory(files={"/remote/race.bin": content}, mtimes={"/remote/race.bin": 99})
        part = tmp_path / ("race.bin" + dl.PART_SUFFIX)
        real_open = open
        writes = {"n": 0}

        class CancellingLocalFile:
            def __init__(self, wrapped):
                self.wrapped = wrapped

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return self.wrapped.__exit__(exc_type, exc, tb)

            def __getattr__(self, name):
                return getattr(self.wrapped, name)

            def write(self, chunk):
                result = self.wrapped.write(chunk)
                writes["n"] += 1
                if writes["n"] == 2:
                    raise dl.TransferCancelled(15)
                return result

        def race_open(path, mode="r", *args, **kwargs):
            wrapped = real_open(path, mode, *args, **kwargs)
            if Path(path) == part and mode in ("wb", "ab"):
                return CancellingLocalFile(wrapped)
            return wrapped

        monkeypatch.setattr("builtins.open", race_open)
        with pytest.raises(dl.TransferCancelled):
            d._download_one_file("/remote/race.bin", "race.bin", tmp_path)

        manifest = d._load_manifest(tmp_path)["race.bin"]
        assert part.stat().st_size == dl.CHUNK_SIZE * 2
        assert manifest["local_bytes"] == part.stat().st_size
        assert manifest["local_sha256"] == hashlib.sha256(part.read_bytes()).hexdigest()

    def test_progress_logged_and_increases_monotonically(self, downloader_factory, fake_sftp_factory, tmp_path, caplog):
        content = b"Z" * (dl.CHUNK_SIZE * 5)
        d = downloader_factory()
        d.sftp = fake_sftp_factory(files={"/remote/f.bin": content}, mtimes={"/remote/f.bin": 1})
        with caplog.at_level(logging.INFO):
            d._download_one_file("/remote/f.bin", "f.bin", tmp_path)
        pct_lines = [r.message for r in caplog.records if "進度" in r.message]
        # 進度訊息格式為「... 進度: NN%」或「... 進度: NN% (速率/s)」，取百分號前的數字。
        percents = [int(line.split("進度:")[-1].split("%")[0].strip()) for line in pct_lines]
        assert percents == sorted(percents)
        assert percents[-1] == 100


class TestDownloadNeverWritesDestinationInPlace:
    """目的地只能被「原子換名」替換，不可以就地改寫。

    這組是回歸測試,對應實際事故:開機時 update_booster 更新 scheduler,把正在執行中的
    reboot_launcher.sh 就地覆寫,bash 按 byte offset 續讀而讀到錯位內容,
    `line 547: syntax error near unexpected token '('` → 啟動器中斷 → 整台機器開機後
    一個 tmux session 都沒有。換名換的是 inode,執行中的行程抓著舊 inode 就不受影響。
    """

    def test_update_replaces_inode_so_old_readers_keep_the_old_content(
        self, downloader_factory, fake_sftp_factory, tmp_path
    ):
        script = tmp_path / "reboot_launcher.sh"
        script.write_bytes(b"#!/bin/bash\nold version\n")
        old_inode = script.stat().st_ino
        d = downloader_factory()
        d._manifest = {}
        new_body = b"#!/bin/bash\nnew version, quite a bit longer than the old one\n"
        d.sftp = fake_sftp_factory(
            files={"/remote/reboot_launcher.sh": new_body},
            mtimes={"/remote/reboot_launcher.sh": 4242},
        )
        # 模擬「這支腳本正在被 bash 執行」——執行中的行程持有的是舊 inode 的檔案描述子
        with open(script, "rb") as running:
            result = d._download_one_file(
                "/remote/reboot_launcher.sh", "reboot_launcher.sh", tmp_path
            )
            assert result == "downloaded"
            # 舊 fd 仍讀得到完整的舊內容,不會讀到新舊混雜的位元組
            assert running.read() == b"#!/bin/bash\nold version\n"

        assert script.read_bytes() == new_body          # 新版本已就位
        assert script.stat().st_ino != old_inode        # 換的是 inode,不是就地覆寫

    def test_interrupted_update_leaves_previous_version_intact(
        self, downloader_factory, fake_sftp_factory, tmp_path
    ):
        old_body = b"#!/bin/bash\nold but complete\n"
        script = tmp_path / "start_ecdis.sh"
        script.write_bytes(old_body)
        d = downloader_factory()
        d._manifest = {}
        sftp = fake_sftp_factory(
            files={"/remote/start_ecdis.sh": b"N" * (dl.CHUNK_SIZE * 3)},
            mtimes={"/remote/start_ecdis.sh": 55},
        )
        original_open = sftp.open

        def flaky_open(path, mode="rb"):
            fake_file = original_open(path, mode)
            original_read = fake_file.read

            def flaky_read(n=-1):
                chunk = original_read(n)
                raise OSError("simulated dropped connection")

            fake_file.read = flaky_read
            return fake_file

        sftp.open = flaky_open
        d.sftp = sftp

        with pytest.raises(OSError):
            d._download_one_file("/remote/start_ecdis.sh", "start_ecdis.sh", tmp_path)

        # 斷線當下目的地仍是上一版的完整內容,不會變成半截的壞腳本
        assert script.read_bytes() == old_body

    def test_successful_download_leaves_no_part_file_behind(
        self, downloader_factory, fake_sftp_factory, tmp_path
    ):
        d = downloader_factory()
        d.sftp = fake_sftp_factory(files={"/remote/x.sh": b"#!/bin/sh\n"}, mtimes={"/remote/x.sh": 7})
        d._download_one_file("/remote/x.sh", "x.sh", tmp_path)
        assert (tmp_path / "x.sh").read_bytes() == b"#!/bin/sh\n"
        assert not (tmp_path / ("x.sh" + dl.PART_SUFFIX)).exists()


# ---------------------------------------------------------------------------
# _resolve_remote_attr — 沿用走訪屬性、省下每檔一次 stat 來回
# ---------------------------------------------------------------------------

class TestResolveRemoteAttr:
    def test_listed_attribute_is_reused_without_calling_stat(self, downloader_factory, fake_sftp_factory):
        d = downloader_factory()
        sftp = fake_sftp_factory(files={"/remote/a.txt": b"AAA"}, mtimes={"/remote/a.txt": 9})
        sftp.stat = MagicMock(side_effect=AssertionError("不應該再 stat 一次"))
        d.sftp = sftp
        listed = FakeSFTPAttr("a.txt", is_dir=False, size=3, mtime=9)
        assert d._resolve_remote_attr("/remote/a.txt", listed) is listed

    def test_no_listed_attribute_falls_back_to_stat(self, downloader_factory, fake_sftp_factory):
        d = downloader_factory()
        d.sftp = fake_sftp_factory(files={"/remote/a.txt": b"AAA"}, mtimes={"/remote/a.txt": 9})
        attr = d._resolve_remote_attr("/remote/a.txt", None)
        assert (attr.st_size, attr.st_mtime) == (3, 9)

    def test_symlink_entry_falls_back_to_stat_to_follow_the_link(self, downloader_factory, fake_sftp_factory):
        # readdir 對 symlink 給的是連結自身的 lstat（size 是目標路徑字串長度），
        # 本工具的語意是跟著連結看實體，所以這種項目必須實打 stat。
        d = downloader_factory()
        d.sftp = fake_sftp_factory(files={"/remote/link": b"REAL-CONTENT"}, mtimes={"/remote/link": 5})
        listed = FakeSFTPAttr("link", is_dir=False, size=7, mtime=1)
        listed.st_mode = stat.S_IFLNK | 0o777
        attr = d._resolve_remote_attr("/remote/link", listed)
        assert (attr.st_size, attr.st_mtime) == (len(b"REAL-CONTENT"), 5)

    @pytest.mark.parametrize("missing", ["st_mode", "st_size", "st_mtime"])
    def test_incomplete_listed_attribute_falls_back_to_stat(
        self, downloader_factory, fake_sftp_factory, missing
    ):
        # SFTP 協定允許伺服器省略這些欄位；缺任何一個就不能拿來當判斷依據。
        d = downloader_factory()
        d.sftp = fake_sftp_factory(files={"/remote/a.txt": b"AAA"}, mtimes={"/remote/a.txt": 9})
        listed = FakeSFTPAttr("a.txt", is_dir=False, size=3, mtime=9)
        setattr(listed, missing, None)
        attr = d._resolve_remote_attr("/remote/a.txt", listed)
        assert attr is not listed
        assert (attr.st_size, attr.st_mtime) == (3, 9)

    def test_download_uses_listed_attribute_for_the_skip_decision(
        self, downloader_factory, fake_sftp_factory, tmp_path
    ):
        d = downloader_factory()
        sftp = fake_sftp_factory(files={"/remote/f.bin": b"12345"}, mtimes={"/remote/f.bin": 4})
        d.sftp = sftp
        listed = FakeSFTPAttr("f.bin", is_dir=False, size=5, mtime=4)
        assert d._download_one_file("/remote/f.bin", "f.bin", tmp_path, listed) == "downloaded"
        sftp.stat = MagicMock(side_effect=AssertionError("略過判斷不該再 stat"))
        assert d._download_one_file("/remote/f.bin", "f.bin", tmp_path, listed) == "skipped"


# ---------------------------------------------------------------------------
# _upload_log_file
# ---------------------------------------------------------------------------

class TestEnsureRemoteDir:
    def test_creates_all_missing_levels(self, downloader_factory, fake_sftp_factory):
        d = downloader_factory()
        d.sftp = fake_sftp_factory(files={})
        d._ensure_remote_dir("/fleet/WH289/IPC-1/sftp_logs")
        assert d.sftp.mkdir_calls == ["/fleet", "/fleet/WH289", "/fleet/WH289/IPC-1", "/fleet/WH289/IPC-1/sftp_logs"]

    def test_existing_levels_are_not_recreated(self, downloader_factory, fake_sftp_factory):
        d = downloader_factory()
        d.sftp = fake_sftp_factory(files={"/fleet/WH289/readme.txt": b"x"})
        d._ensure_remote_dir("/fleet/WH289/sftp_logs")
        assert d.sftp.mkdir_calls == ["/fleet/WH289/sftp_logs"]

    def test_fully_existing_path_creates_nothing(self, downloader_factory, fake_sftp_factory):
        d = downloader_factory()
        d.sftp = fake_sftp_factory(files={"/data/logs/old.csv": b"x"})
        d._ensure_remote_dir("/data/logs")
        assert d.sftp.mkdir_calls == []

    def test_upload_log_creates_remote_dir_before_put(self, downloader_factory, fake_sftp_factory, tmp_path):
        log_file = tmp_path / "run.csv"
        log_file.write_text("data", encoding="utf-8")
        d = downloader_factory(remote_log_dir="/fleet/WH289/IPC-1/sftp_logs", log_file=str(log_file))
        d.logger.addHandler(logging.NullHandler())
        d._connect_with_retry = MagicMock()
        sftp = fake_sftp_factory(files={})
        d.sftp = sftp
        d._upload_log_file()
        assert "/fleet/WH289/IPC-1/sftp_logs" in sftp.dirs
        assert "/fleet/WH289/IPC-1/sftp_logs/run.csv" in sftp.files
        assert d.sftp is None


class TestUploadLogFile:
    def test_successful_upload(self, downloader_factory, fake_sftp_factory, tmp_path, logger):
        log_file = tmp_path / "run.csv"
        log_file.write_text("timestamp,message\n", encoding="utf-8")
        d = downloader_factory(remote_log_dir="/data/logs", log_file=str(log_file))
        d.logger.addHandler(logging.NullHandler())
        d._connect_with_retry = MagicMock()
        sftp = fake_sftp_factory(files={})
        d.sftp = sftp
        d._upload_log_file()
        assert "/data/logs/run.csv" in sftp.files
        assert d.sftp is None

    def test_upload_failure_is_caught_and_does_not_propagate(self, downloader_factory, tmp_path):
        log_file = tmp_path / "run.csv"
        log_file.write_text("data", encoding="utf-8")
        d = downloader_factory(remote_log_dir="/data/logs", log_file=str(log_file))
        d.logger.addHandler(logging.NullHandler())
        d._connect_with_retry = MagicMock(side_effect=OSError("network down"))
        d._upload_log_file()  # 不應拋出例外

    def test_close_called_even_after_failure(self, downloader_factory, tmp_path):
        log_file = tmp_path / "run.csv"
        log_file.write_text("data", encoding="utf-8")
        d = downloader_factory(remote_log_dir="/data/logs", log_file=str(log_file))
        d.logger.addHandler(logging.NullHandler())
        d._connect_with_retry = MagicMock(side_effect=OSError("network down"))
        d._close = MagicMock()
        d._upload_log_file()
        d._close.assert_called_once()

    def test_existing_connection_is_reused_instead_of_handshaking_again(
        self, downloader_factory, fake_sftp_factory, tmp_path
    ):
        log_file = tmp_path / "run.csv"
        log_file.write_text("data", encoding="utf-8")
        d = downloader_factory(remote_log_dir="/data/logs", log_file=str(log_file))
        d.logger.addHandler(logging.NullHandler())
        d._connect_with_retry = MagicMock()
        d.sftp = fake_sftp_factory(files={})
        d._upload_log_file()
        d._connect_with_retry.assert_not_called()

    def test_without_a_connection_it_still_connects(
        self, downloader_factory, fake_sftp_factory, tmp_path
    ):
        log_file = tmp_path / "run.csv"
        log_file.write_text("data", encoding="utf-8")
        sftp = fake_sftp_factory(files={})
        d = downloader_factory(remote_log_dir="/data/logs", log_file=str(log_file))
        d.logger.addHandler(logging.NullHandler())
        d._connect_with_retry = MagicMock(side_effect=lambda: setattr(d, "sftp", sftp))
        d.sftp = None
        d._upload_log_file()
        d._connect_with_retry.assert_called_once()
        assert "/data/logs/run.csv" in sftp.files

    def test_dead_reused_connection_reconnects_once_and_still_uploads(
        self, downloader_factory, fake_sftp_factory, tmp_path
    ):
        # 沿用的連線可能在傳輸中途就無聲斷掉；此時必須退回「重新連線再傳」的舊行為。
        log_file = tmp_path / "run.csv"
        log_file.write_text("data", encoding="utf-8")
        dead = fake_sftp_factory(files={})
        dead.put = MagicMock(side_effect=OSError("Socket is closed"))
        fresh = fake_sftp_factory(files={})
        d = downloader_factory(remote_log_dir="/data/logs", log_file=str(log_file))
        d.logger.addHandler(logging.NullHandler())
        d._connect_with_retry = MagicMock(side_effect=lambda: setattr(d, "sftp", fresh))
        d.sftp = dead
        d._upload_log_file()
        d._connect_with_retry.assert_called_once()
        assert "/data/logs/run.csv" in fresh.files

    def test_failure_on_a_fresh_connection_is_not_retried_again(
        self, downloader_factory, fake_sftp_factory, tmp_path, caplog
    ):
        log_file = tmp_path / "run.csv"
        log_file.write_text("data", encoding="utf-8")
        sftp = fake_sftp_factory(files={})
        sftp.put = MagicMock(side_effect=OSError("Socket is closed"))
        d = downloader_factory(remote_log_dir="/data/logs", log_file=str(log_file))
        d.logger.addHandler(logging.NullHandler())
        d._connect_with_retry = MagicMock(side_effect=lambda: setattr(d, "sftp", sftp))
        d.sftp = None
        with caplog.at_level(logging.INFO):
            d._upload_log_file()
        d._connect_with_retry.assert_called_once()
        assert sftp.put.call_count == 1
        assert any("LOG_UPLOAD_ERROR" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# run() — full orchestration
# ---------------------------------------------------------------------------

class TestRun:
    def _prepare(self, downloader_factory, fake_sftp_factory, files=None, mtimes=None, **overrides):
        d = downloader_factory(wait_for_network=False, **overrides)
        d._connect_with_retry = MagicMock(side_effect=lambda: setattr(d, "sftp", fake_sftp_factory(files=files or {}, mtimes=mtimes or {})))
        d._close = MagicMock()
        return d

    def test_successful_run_downloads_all_files_and_returns_true(self, downloader_factory, fake_sftp_factory):
        d = self._prepare(
            downloader_factory, fake_sftp_factory,
            files={"/remote/a.txt": b"A", "/remote/b.txt": b"B"},
            mtimes={"/remote/a.txt": 1, "/remote/b.txt": 2},
        )
        result = d.run()
        assert result is True

    def test_second_run_skips_already_downloaded_unchanged_file(self, downloader_factory, fake_sftp_factory):
        files = {"/remote/a.txt": b"A"}
        mtimes = {"/remote/a.txt": 1}
        d = self._prepare(downloader_factory, fake_sftp_factory, files=files, mtimes=mtimes)
        assert d.run() is True  # 第一次：全新下載

        d2 = self._prepare(downloader_factory, fake_sftp_factory, files=files, mtimes=mtimes, local_path=d.local_path)
        assert d2.run() is True  # 第二次：內容未變，應該略過而非重新下載

    def test_wait_for_network_called_when_enabled(self, downloader_factory, fake_sftp_factory):
        d = downloader_factory(wait_for_network=True)
        d._wait_for_network = MagicMock()
        d._connect_with_retry = MagicMock(side_effect=lambda: setattr(d, "sftp", fake_sftp_factory(files={})))
        d._close = MagicMock()
        d.run()
        d._wait_for_network.assert_called_once()

    def test_authentication_exception_returns_false_without_retry(self, downloader_factory):
        d = downloader_factory(wait_for_network=False)
        d._connect_with_retry = MagicMock(side_effect=paramiko.AuthenticationException("bad creds"))
        d._close = MagicMock()
        result = d.run()
        assert result is False
        d._connect_with_retry.assert_called_once()

    def test_remote_path_not_found_skips_with_warning(self, downloader_factory, fake_sftp_factory, caplog):
        d = self._prepare(downloader_factory, fake_sftp_factory, files={})
        d.remote_path = "/remote/missing"
        with caplog.at_level(logging.WARNING):
            result = d.run()
        # 來源路徑不存在時記警告並略過該來源（見 commit 8251f01），不再視為整個任務失敗。
        assert result is True
        assert any("/remote/missing" in r.message for r in caplog.records)

    def test_remote_path_list_merges_all_sources_into_local_path(self, downloader_factory, fake_sftp_factory, tmp_path):
        d = self._prepare(
            downloader_factory, fake_sftp_factory,
            files={"/standard/proj/a.txt": b"A", "/unique/WH289/proj/config.json": b"{}"},
            mtimes={"/standard/proj/a.txt": 1, "/unique/WH289/proj/config.json": 2},
            remote_path=["/standard/proj", "/unique/WH289/proj"],
        )
        assert d.run() is True
        assert (tmp_path / "a.txt").read_bytes() == b"A"
        assert (tmp_path / "config.json").read_bytes() == b"{}"

    def test_paired_remote_local_lists_map_each_source_to_its_own_local(
        self, downloader_factory, fake_sftp_factory, tmp_path
    ):
        # local 為等長陣列 → 逐一配對 remote[i]→local[i]（多專案各自落在自己的目錄，
        # 如 STANDARD/share/alarm_controller → share/alarm_controller），不再攤平合併。
        d = self._prepare(
            downloader_factory, fake_sftp_factory,
            files={"/remote/alarm/x.py": b"alarm", "/remote/board/y.py": b"board"},
            mtimes={"/remote/alarm/x.py": 1, "/remote/board/y.py": 2},
            remote_path=["/remote/alarm", "/remote/board"],
            local_path=[str(tmp_path / "alarm_controller"), str(tmp_path / "board_controller")],
        )
        assert d.run() is True
        assert (tmp_path / "alarm_controller" / "x.py").read_bytes() == b"alarm"
        assert (tmp_path / "board_controller" / "y.py").read_bytes() == b"board"
        # 不會攤平到共同的 local 根目錄。
        assert not (tmp_path / "x.py").exists()

    def test_mismatched_pairing_returns_false(self, downloader_factory, fake_sftp_factory, tmp_path, caplog):
        d = self._prepare(
            downloader_factory, fake_sftp_factory, files={},
            remote_path=["/remote/alarm", "/remote/board"],
            local_path=[str(tmp_path / "only_one")],
        )
        with caplog.at_level(logging.ERROR):
            assert d.run() is False
        assert any("配對數量不符" in r.message for r in caplog.records)

    def test_trailing_slash_local_parent_fans_out_by_basename(
        self, downloader_factory, fake_sftp_factory, tmp_path
    ):
        # local 帶尾斜線 → 視為共同父目錄，各 remote 來源展開到 父目錄/來源basename。
        d = self._prepare(
            downloader_factory, fake_sftp_factory,
            files={"/remote/alarm_controller/x.py": b"alarm", "/remote/board_controller/y.py": b"board"},
            mtimes={"/remote/alarm_controller/x.py": 1, "/remote/board_controller/y.py": 2},
            remote_path=["/remote/alarm_controller", "/remote/board_controller"],
            local_path=str(tmp_path) + "/",
        )
        assert d.run() is True
        assert (tmp_path / "alarm_controller" / "x.py").read_bytes() == b"alarm"
        assert (tmp_path / "board_controller" / "y.py").read_bytes() == b"board"
        # 不會攤平到共同父目錄根。
        assert not (tmp_path / "x.py").exists()

    def test_remote_path_list_duplicate_rel_path_last_source_wins(self, downloader_factory, fake_sftp_factory, tmp_path, caplog):
        d = self._prepare(
            downloader_factory, fake_sftp_factory,
            files={"/standard/proj/config.json": b"standard", "/unique/proj/config.json": b"unique"},
            mtimes={"/standard/proj/config.json": 1, "/unique/proj/config.json": 2},
            remote_path=["/standard/proj", "/unique/proj"],
        )
        with caplog.at_level(logging.WARNING):
            assert d.run() is True
        assert (tmp_path / "config.json").read_bytes() == b"unique"
        assert any("以後面的來源為準" in r.message for r in caplog.records)

    def test_remote_path_list_missing_source_skips_with_warning_and_names_it(self, downloader_factory, fake_sftp_factory, tmp_path, caplog):
        d = self._prepare(
            downloader_factory, fake_sftp_factory,
            files={"/standard/proj/a.txt": b"A"},
            remote_path=["/standard/proj", "/unique/missing"],
        )
        with caplog.at_level(logging.WARNING):
            assert d.run() is True
        # 缺少的來源被略過並在 Log 指明，存在的來源照常下載（見 commit 8251f01）。
        assert any("/unique/missing" in r.message for r in caplog.records)
        assert (tmp_path / "a.txt").read_bytes() == b"A"

    def test_listing_error_retries_then_succeeds(self, downloader_factory, fake_sftp_factory):
        d = downloader_factory(wait_for_network=False, retry_count=3)
        attempts = {"n": 0}
        good_sftp = fake_sftp_factory(files={"/remote/a.txt": b"A"}, mtimes={"/remote/a.txt": 1})

        def connect_side_effect():
            attempts["n"] += 1
            d.sftp = good_sftp

        d._connect_with_retry = MagicMock(side_effect=connect_side_effect)
        d._close = MagicMock()

        original_list = d._list_remote_files
        call_state = {"first": True}

        def flaky_list(remote_root, local_root):
            if call_state["first"]:
                call_state["first"] = False
                raise OSError("connection reset")
            return original_list(remote_root, local_root)

        d._list_remote_files = flaky_list
        result = d.run()
        assert result is True

    def test_sftp_error_while_listing_retries_then_succeeds(self, downloader_factory, fake_sftp_factory):
        d = downloader_factory(wait_for_network=False, retry_count=2)
        good_sftp = fake_sftp_factory(files={"/remote/a.txt": b"A"}, mtimes={"/remote/a.txt": 1})
        d._connect_with_retry = MagicMock(side_effect=lambda: setattr(d, "sftp", good_sftp))
        d._close = MagicMock()
        original_list = d._list_remote_files
        state = {"failed_once": False}

        def flaky_list(remote_root, local_root):
            if not state["failed_once"]:
                state["failed_once"] = True
                raise paramiko.SFTPError("Garbage packet received")
            return original_list(remote_root, local_root)

        d._list_remote_files = MagicMock(side_effect=flaky_list)
        assert d.run() is True
        assert d._connect_with_retry.call_count == 2

    def test_listing_error_exceeds_retry_limit_returns_false(self, downloader_factory):
        d = downloader_factory(wait_for_network=False, retry_count=1)
        d._connect_with_retry = MagicMock()
        d._close = MagicMock()
        d._list_remote_files = MagicMock(side_effect=OSError("still broken"))
        result = d.run()
        assert result is False

    def test_permission_error_on_one_file_is_recorded_but_others_continue(self, downloader_factory, fake_sftp_factory):
        d = self._prepare(
            downloader_factory, fake_sftp_factory,
            files={"/remote/a.txt": b"A", "/remote/b.txt": b"B"},
            mtimes={"/remote/a.txt": 1, "/remote/b.txt": 2},
        )
        original_download = d._download_one_file

        def flaky_download(remote_file, rel_path, local_root, listed_attr=None):
            if rel_path == "a.txt":
                raise PermissionError("no write access")
            return original_download(remote_file, rel_path, local_root, listed_attr)

        d._download_one_file = flaky_download
        result = d.run()
        assert result is False  # 有檔案失敗，整體視為不完全成功

    def test_file_not_found_during_download_is_recorded_as_failure(self, downloader_factory, fake_sftp_factory):
        d = self._prepare(downloader_factory, fake_sftp_factory, files={"/remote/a.txt": b"A"}, mtimes={"/remote/a.txt": 1})
        d._download_one_file = MagicMock(side_effect=FileNotFoundError("gone"))
        result = d.run()
        assert result is False

    def test_connection_error_during_download_reconnects_and_succeeds(self, downloader_factory, fake_sftp_factory):
        d = self._prepare(downloader_factory, fake_sftp_factory, files={"/remote/a.txt": b"A"}, mtimes={"/remote/a.txt": 1}, retry_count=3)
        original_download = d._download_one_file
        state = {"failed_once": False}

        def flaky_download(remote_file, rel_path, local_root, listed_attr=None):
            if not state["failed_once"]:
                state["failed_once"] = True
                raise OSError("dropped")
            return original_download(remote_file, rel_path, local_root, listed_attr)

        d._download_one_file = flaky_download
        result = d.run()
        assert result is True
        assert d._connect_with_retry.call_count >= 2  # 初次連線 + 下載失敗後重連

    def test_sftp_error_during_download_reconnects_and_succeeds(self, downloader_factory, fake_sftp_factory):
        d = self._prepare(
            downloader_factory,
            fake_sftp_factory,
            files={"/remote/a.txt": b"A"},
            mtimes={"/remote/a.txt": 1},
            retry_count=2,
        )
        original_download = d._download_one_file
        state = {"failed_once": False}

        def flaky_download(remote_file, rel_path, local_root, listed_attr=None):
            if not state["failed_once"]:
                state["failed_once"] = True
                raise paramiko.SFTPError("Garbage packet received")
            return original_download(remote_file, rel_path, local_root, listed_attr)

        d._download_one_file = MagicMock(side_effect=flaky_download)
        assert d.run() is True
        assert d._connect_with_retry.call_count >= 2

    def test_connection_error_exceeds_retry_limit_marks_file_failed_but_continues(self, downloader_factory, fake_sftp_factory):
        d = self._prepare(
            downloader_factory, fake_sftp_factory,
            files={"/remote/a.txt": b"A", "/remote/b.txt": b"B"},
            mtimes={"/remote/a.txt": 1, "/remote/b.txt": 2},
            retry_count=1,
        )
        original_download = d._download_one_file

        def flaky_download(remote_file, rel_path, local_root, listed_attr=None):
            if rel_path == "a.txt":
                raise OSError("permanently broken")
            return original_download(remote_file, rel_path, local_root, listed_attr)

        d._download_one_file = flaky_download
        result = d.run()
        assert result is False
        # b.txt 仍應該成功下載
        assert (Path(d.local_path) / "b.txt").exists()

    def test_reconnect_failure_during_per_file_retry_marks_failed_and_continues(self, downloader_factory, fake_sftp_factory):
        d = self._prepare(
            downloader_factory, fake_sftp_factory,
            files={"/remote/a.txt": b"A", "/remote/b.txt": b"B"},
            mtimes={"/remote/a.txt": 1, "/remote/b.txt": 2},
            retry_count=3,
        )
        original_download = d._download_one_file
        original_connect = d._connect_with_retry
        state = {"a_failed": False}

        def flaky_download(remote_file, rel_path, local_root, listed_attr=None):
            if rel_path == "a.txt" and not state["a_failed"]:
                state["a_failed"] = True
                raise OSError("dropped")
            return original_download(remote_file, rel_path, local_root, listed_attr)

        def reconnect_side_effect():
            if state["a_failed"]:
                raise paramiko.SSHException("cannot reconnect")
            original_connect()

        d._download_one_file = flaky_download
        d._connect_with_retry = MagicMock(side_effect=reconnect_side_effect)
        result = d.run()
        assert result is False

    def test_unexpected_exception_aborts_task_and_returns_false(self, downloader_factory):
        d = downloader_factory(wait_for_network=False)
        d._connect_with_retry = MagicMock(side_effect=RuntimeError("totally unexpected"))
        d._close = MagicMock()
        result = d.run()
        assert result is False

    def test_close_always_called_even_on_exception(self, downloader_factory):
        d = downloader_factory(wait_for_network=False)
        d._connect_with_retry = MagicMock(side_effect=RuntimeError("boom"))
        d._close = MagicMock()
        d.run()
        d._close.assert_called_once()

    def test_upload_log_called_when_enabled(self, downloader_factory, fake_sftp_factory):
        d = self._prepare(
            downloader_factory, fake_sftp_factory,
            files={"/remote/a.txt": b"A"}, mtimes={"/remote/a.txt": 1},
            upload_log=True, remote_log_dir="/logs",
        )
        d._upload_log_file = MagicMock()
        d.run()
        d._upload_log_file.assert_called_once()

    def test_upload_log_not_called_when_disabled(self, downloader_factory, fake_sftp_factory):
        d = self._prepare(
            downloader_factory, fake_sftp_factory,
            files={"/remote/a.txt": b"A"}, mtimes={"/remote/a.txt": 1},
            upload_log=False,
        )
        d._upload_log_file = MagicMock()
        d.run()
        d._upload_log_file.assert_not_called()

    def test_upload_log_called_even_when_task_aborts(self, downloader_factory):
        # 任務中途中止（此處以連線階段拋出未預期例外模擬）時，log 仍必須上傳，
        # 否則最需要遠端紀錄的失敗情境反而沒有 log。此為 SFTPBase.run 以 finally 保證的行為。
        d = downloader_factory(wait_for_network=False, upload_log=True, remote_log_dir="/logs")
        d._connect_with_retry = MagicMock(side_effect=RuntimeError("boom"))
        d._close = MagicMock()
        d._upload_log_file = MagicMock()
        result = d.run()
        assert result is False
        d._upload_log_file.assert_called_once()

    def test_cancel_flushes_local_log_and_skips_remote_log_upload(self, downloader_factory):
        d = downloader_factory(upload_log=True, remote_log_dir="/logs")
        d._run = MagicMock(side_effect=dl.TransferCancelled(15))
        d._upload_log_file = MagicMock()
        handlers = [MagicMock(level=0), MagicMock(level=0)]
        d.logger.handlers = handlers

        with pytest.raises(dl.TransferCancelled):
            d.run()

        d._upload_log_file.assert_not_called()
        for handler in handlers:
            handler.flush.assert_called_once()

    def test_cancel_unwinds_run_and_closes_sftp_connection(self, downloader_factory, fake_sftp_factory):
        d = self._prepare(
            downloader_factory,
            fake_sftp_factory,
            files={"/remote/a.txt": b"A"},
            mtimes={"/remote/a.txt": 1},
            upload_log=True,
            remote_log_dir="/logs",
        )
        d._download_one_file = MagicMock(side_effect=dl.TransferCancelled(15))
        d._upload_log_file = MagicMock()

        with pytest.raises(dl.TransferCancelled):
            d.run()

        d._close.assert_called_once()
        d._upload_log_file.assert_not_called()

    def test_whole_run_stats_the_source_root_only_not_every_file(
        self, downloader_factory, fake_sftp_factory
    ):
        # 走訪用的 listdir_attr 已經帶回 size/mtime/mode，逐檔不該再各打一次 stat。
        # 高延遲鏈路上那一次來回正是「內容沒變動的檔案」最主要的成本。
        sftp = fake_sftp_factory(
            files={"/remote/a.txt": b"A", "/remote/sub/b.txt": b"B", "/remote/sub/c.txt": b"C"},
            mtimes={"/remote/a.txt": 1, "/remote/sub/b.txt": 2, "/remote/sub/c.txt": 3},
        )
        sftp.stat = MagicMock(side_effect=sftp.stat)
        d = downloader_factory(wait_for_network=False, recursive=True)
        d._connect_with_retry = MagicMock(side_effect=lambda: setattr(d, "sftp", sftp))
        d._close = MagicMock()

        assert d.run() is True
        assert sftp.stat.call_count == 1  # 只有 _list_remote_files 判斷來源是檔案或目錄的那次

        assert d.run() is True  # 第二次全部略過，同樣不該多出任何 stat
        assert sftp.stat.call_count == 2

    def test_log_upload_reuses_the_transfer_connection(
        self, downloader_factory, fake_sftp_factory, tmp_path
    ):
        # 改版前這裡會握手兩次（傳輸一次、log 上傳一次）；船上一次握手中位 5 秒，
        # 而排程是一個專案一個行程，省下的是「專案數 × 一次握手」。
        log_file = tmp_path / "run.csv"
        log_file.write_text("data", encoding="utf-8")
        sftp = fake_sftp_factory(files={"/remote/a.txt": b"A"}, mtimes={"/remote/a.txt": 1})
        d = downloader_factory(
            wait_for_network=False,
            local_path=str(tmp_path / "dest"),
            upload_log=True,
            remote_log_dir="/fleet/logs",
            log_file=str(log_file),
        )
        d.logger.addHandler(logging.NullHandler())
        d._connect_with_retry = MagicMock(side_effect=lambda: setattr(d, "sftp", sftp))

        assert d.run() is True
        d._connect_with_retry.assert_called_once()
        assert "/fleet/logs/run.csv" in sftp.files
        assert d.sftp is None  # 收尾關連線的責任在 run()

    def test_run_closes_the_connection_when_log_upload_is_disabled(
        self, downloader_factory, fake_sftp_factory, tmp_path
    ):
        sftp = fake_sftp_factory(files={"/remote/a.txt": b"A"}, mtimes={"/remote/a.txt": 1})
        d = downloader_factory(wait_for_network=False, local_path=str(tmp_path / "dest"))
        d._connect_with_retry = MagicMock(side_effect=lambda: setattr(d, "sftp", sftp))
        assert d.run() is True
        assert d.sftp is None

    def test_run_closes_the_connection_even_when_log_upload_raises(
        self, downloader_factory, fake_sftp_factory, tmp_path
    ):
        sftp = fake_sftp_factory(files={"/remote/a.txt": b"A"}, mtimes={"/remote/a.txt": 1})
        d = downloader_factory(
            wait_for_network=False,
            local_path=str(tmp_path / "dest"),
            upload_log=True,
            remote_log_dir="/fleet/logs",
        )
        d._connect_with_retry = MagicMock(side_effect=lambda: setattr(d, "sftp", sftp))
        d._upload_log_file = MagicMock(side_effect=RuntimeError("boom"))
        with pytest.raises(RuntimeError):
            d.run()
        assert d.sftp is None


# ---------------------------------------------------------------------------
# create_logger / _CSVFileHandler
# ---------------------------------------------------------------------------

class TestCreateLogger:
    def test_creates_csv_with_expected_header(self, tmp_path):
        logger, log_file = dl.create_logger(tmp_path, "edge-1")
        for h in logger.handlers:
            h.close()
        with open(log_file, encoding="utf-8-sig", newline="") as f:
            import csv as csv_module
            rows = list(csv_module.reader(f))
        assert rows[0] == ["timestamp", "device_name", "version_info", "level", "message"]

    def test_log_message_appears_in_csv_row(self, tmp_path):
        logger, log_file = dl.create_logger(tmp_path, "edge-1", "v1.0")
        logger.info("hello test")
        for h in logger.handlers:
            h.close()
        with open(log_file, encoding="utf-8-sig", newline="") as f:
            import csv as csv_module
            rows = list(csv_module.reader(f))
        assert rows[1] == [rows[1][0], "edge-1", "v1.0", "INFO", "hello test"]

    def test_device_name_with_unsafe_characters_sanitized_in_filename(self, tmp_path):
        logger, log_file = dl.create_logger(tmp_path, "edge/1:test")
        for h in logger.handlers:
            h.close()
        assert "/" not in log_file.name.replace(str(tmp_path), "")
        assert ":" not in log_file.name

    def test_version_info_omitted_from_text_format_when_empty(self, tmp_path, caplog):
        logger, log_file = dl.create_logger(tmp_path, "edge-1", "")
        with caplog.at_level(logging.INFO, logger=logger.name):
            pass
        # 直接檢查 handler 的 formatter 字串，確認沒有多餘的空括號
        text_handler = next(h for h in logger.handlers if isinstance(h, logging.StreamHandler) and not isinstance(h, dl._CSVFileHandler))
        assert "[]" not in text_handler.formatter._fmt
        for h in logger.handlers:
            h.close()

    def test_log_callback_invoked_with_formatted_message(self, tmp_path):
        received = []
        logger, log_file = dl.create_logger(tmp_path, "edge-1", log_callback=received.append)
        logger.info("callback test")
        for h in logger.handlers:
            h.close()
        assert any("callback test" in msg for msg in received)


class TestCSVFileHandlerErrorHandling:
    def test_emit_error_is_handled_without_crashing(self, tmp_path):
        log_file = tmp_path / "test.csv"
        handler = dl._CSVFileHandler(log_file, "device")
        handler.handleError = MagicMock()
        with patch.object(handler, "_writer") as mock_writer:
            mock_writer.writerow.side_effect = Exception("write failed")
            record = logging.LogRecord("test", logging.INFO, __file__, 1, "msg", None, None)
            handler.emit(record)  # 不應拋出例外
        handler.handleError.assert_called_once()
        handler.close()

    def test_close_swallows_exception_from_underlying_file_close(self, tmp_path):
        log_file = tmp_path / "test.csv"
        handler = dl._CSVFileHandler(log_file, "device")
        handler._file.close()  # 先手動關閉，讓 handler.close() 內部再次呼叫 close() 時真的出錯
        handler._file.close = MagicMock(side_effect=Exception("already closed"))
        handler.close()  # 不應向外拋出例外
