"""settings.py 單元測試。"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import settings as settings_module


class TestLoadSettings:
    def test_missing_file_returns_empty_dict(self, tmp_path):
        result = settings_module.load_settings(tmp_path / "nope.json")
        assert result == {}

    def test_valid_json_loads_correctly(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"host": "1.2.3.4", "port": 22}), encoding="utf-8")
        result = settings_module.load_settings(path)
        assert result == {"host": "1.2.3.4", "port": 22}

    def test_corrupt_json_returns_empty_dict_without_raising(self, tmp_path, capsys):
        path = tmp_path / "settings.json"
        path.write_text("{not valid json!", encoding="utf-8")
        result = settings_module.load_settings(path)
        assert result == {}
        assert "讀取失敗" in capsys.readouterr().err

    def test_empty_json_object_returns_empty_dict(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text("{}", encoding="utf-8")
        assert settings_module.load_settings(path) == {}


class TestResolvePlaceholders:
    def _write_vessel_info(self, tmp_path, monkeypatch, content='{"vsl_name": "WH289", "ipc": "IPC-1"}'):
        path = tmp_path / "vessel_basic_info.json"
        path.write_text(content, encoding="utf-8")
        monkeypatch.setenv("VESSEL_INFO_PATH", str(path))
        return path

    def test_placeholders_replaced_from_vessel_info(self, tmp_path, monkeypatch):
        self._write_vessel_info(tmp_path, monkeypatch)
        result = settings_module.resolve_placeholders(
            {"log_remote_dir": "/fleet/deploy/{vsl_name}/{ipc}/sftp_logs"}
        )
        assert result["log_remote_dir"] == "/fleet/deploy/WH289/IPC-1/sftp_logs"

    def test_device_name_placeholder_replaced(self, tmp_path, monkeypatch):
        self._write_vessel_info(tmp_path, monkeypatch)
        result = settings_module.resolve_placeholders({"device_name": "{vsl_name}_{ipc}_SFTP_DOWNLOADER"})
        assert result["device_name"] == "WH289_IPC-1_SFTP_DOWNLOADER"

    def test_takeover_does_not_change_the_ipc_placeholder(self, tmp_path, monkeypatch):
        """接管中的機器,{ipc} 仍必須解析成原本的 IPC-x。

        這是「接管旗標用獨立欄位、不改寫 ipc」的全部理由所在。{ipc} 出現在 19 個
        settings 檔(device_name ×19、log_remote_dir ×9,以及
        device_monitor_report_upload_settings.json 的 remote_path ×1)。若接管時把 ipc 寫成
        "IPC-2-EMER",該船的報表會上傳到 device_monitor_reports/{vsl}/IPC-2-EMER/ ——
        岸端的歷史斷成兩個目錄,而且是在最需要分辨誰是誰的時候。

        settings.py 本身不需要為此改動;這條測試把該性質釘住,免得日後有人「順手簡化」
        成把 emer 塞回 ipc 欄位。
        """
        self._write_vessel_info(
            tmp_path, monkeypatch,
            content='{"vsl_name": "WH289", "ipc": "IPC-2", "failover": true,'
                    ' "failover_since": 1785000000}',
        )
        result = settings_module.resolve_placeholders({
            "remote_path": "/fleet/wanhai_nssms_deploy/device_monitor_reports/{vsl_name}/{ipc}",
            "device_name": "{vsl_name}_{ipc}_scheduler",
            "log_remote_dir": "/fleet/deploy/{vsl_name}/{ipc}/sftp_logs",
        })
        assert result["remote_path"] == (
            "/fleet/wanhai_nssms_deploy/device_monitor_reports/WH289/IPC-2")
        assert result["device_name"] == "WH289_IPC-2_scheduler"
        assert result["log_remote_dir"] == "/fleet/deploy/WH289/IPC-2/sftp_logs"
        for value in result.values():
            assert "EMER" not in value.upper()

    def test_non_string_values_left_untouched(self, tmp_path, monkeypatch):
        self._write_vessel_info(tmp_path, monkeypatch)
        result = settings_module.resolve_placeholders(
            {"port": 22, "resume": True, "host": "{vsl_name}.example.com"}
        )
        assert result["port"] == 22
        assert result["resume"] is True
        assert result["host"] == "WH289.example.com"

    def test_no_placeholders_does_not_require_vessel_info_file(self, tmp_path, monkeypatch):
        # 完全沒用到佔位符時，vessel 資訊檔可以不存在，行為不變（回歸保護）。
        monkeypatch.setenv("VESSEL_INFO_PATH", str(tmp_path / "nope.json"))
        data = {"host": "1.2.3.4", "log_remote_dir": "/data/logs"}
        assert settings_module.resolve_placeholders(data) == data

    def test_placeholder_with_missing_vessel_info_file_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VESSEL_INFO_PATH", str(tmp_path / "nope.json"))
        with pytest.raises(settings_module.PlaceholderError, match="找不到船舶資訊檔"):
            settings_module.resolve_placeholders({"log_remote_dir": "/data/{vsl_name}"})

    def test_unknown_placeholder_key_raises_with_field_name(self, tmp_path, monkeypatch):
        self._write_vessel_info(tmp_path, monkeypatch)
        with pytest.raises(settings_module.PlaceholderError, match="log_remote_dir.*vslname"):
            settings_module.resolve_placeholders({"log_remote_dir": "/data/{vslname}"})

    def test_corrupt_vessel_info_file_raises(self, tmp_path, monkeypatch):
        self._write_vessel_info(tmp_path, monkeypatch, content="{broken json")
        with pytest.raises(settings_module.PlaceholderError, match="讀取失敗"):
            settings_module.resolve_placeholders({"host": "{vsl_name}"})

    def test_load_settings_resolves_placeholders(self, tmp_path, monkeypatch):
        self._write_vessel_info(tmp_path, monkeypatch)
        path = tmp_path / "settings.json"
        path.write_text(
            json.dumps({"log_remote_dir": "/fleet/{vsl_name}/{ipc}/logs", "port": 22}), encoding="utf-8"
        )
        result = settings_module.load_settings(path)
        assert result == {"log_remote_dir": "/fleet/WH289/IPC-1/logs", "port": 22}

    def test_load_settings_propagates_placeholder_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VESSEL_INFO_PATH", str(tmp_path / "nope.json"))
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"host": "{vsl_name}"}), encoding="utf-8")
        with pytest.raises(settings_module.PlaceholderError):
            settings_module.load_settings(path)

    def test_list_values_resolved_per_element(self, tmp_path, monkeypatch):
        self._write_vessel_info(tmp_path, monkeypatch)
        result = settings_module.resolve_placeholders(
            {"remote_path": ["source/project1", "source/{vsl_name}/project/config"]}
        )
        assert result["remote_path"] == ["source/project1", "source/WH289/project/config"]

    def test_list_with_unknown_placeholder_raises(self, tmp_path, monkeypatch):
        self._write_vessel_info(tmp_path, monkeypatch)
        with pytest.raises(settings_module.PlaceholderError, match="remote_path.*nope"):
            settings_module.resolve_placeholders({"remote_path": ["a", "b/{nope}"]})

    def test_list_non_string_items_left_untouched(self, tmp_path, monkeypatch):
        self._write_vessel_info(tmp_path, monkeypatch)
        result = settings_module.resolve_placeholders({"mixed": ["{vsl_name}", 42, None]})
        assert result["mixed"] == ["WH289", 42, None]

    def test_vessel_info_values_coerced_to_string(self, tmp_path, monkeypatch):
        self._write_vessel_info(tmp_path, monkeypatch, content='{"ipc": 1}')
        result = settings_module.resolve_placeholders({"device_name": "IPC{ipc}"})
        assert result["device_name"] == "IPC1"


class TestSaveSettings:
    def test_writes_json_readable_by_load_settings(self, tmp_path):
        path = tmp_path / "exported.json"
        data = {"host": "10.0.0.1", "port": 22, "recursive": False, "device_name": "邊緣裝置-1"}
        result_path = settings_module.save_settings(path, data)
        assert result_path == path
        assert settings_module.load_settings(path) == data

    def test_overwrites_existing_file(self, tmp_path):
        path = tmp_path / "exported.json"
        path.write_text(json.dumps({"host": "old"}), encoding="utf-8")
        settings_module.save_settings(path, {"host": "new"})
        assert settings_module.load_settings(path) == {"host": "new"}

    def test_chinese_characters_saved_as_readable_text_not_escaped(self, tmp_path):
        # ensure_ascii=False：中文以原字元存檔，方便使用者直接用記事本檢視編輯。
        path = tmp_path / "exported.json"
        settings_module.save_settings(path, {"device_name": "測試裝置"})
        assert "測試裝置" in path.read_text(encoding="utf-8")


class TestEnsureSettingsFile:
    def test_creates_file_from_template_when_missing(self, tmp_path):
        path = tmp_path / "settings.json"
        result_path = settings_module.ensure_settings_file(path)
        assert result_path == path
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data == settings_module.SETTINGS_TEMPLATE

    def test_does_not_overwrite_existing_file(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"host": "already-here"}), encoding="utf-8")
        settings_module.ensure_settings_file(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data == {"host": "already-here"}

    def test_seed_values_override_template_defaults(self, tmp_path):
        path = tmp_path / "settings.json"
        settings_module.ensure_settings_file(path, seed={"host": "10.0.0.1", "port": 2222})
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["host"] == "10.0.0.1"
        assert data["port"] == 2222
        assert data["username"] == ""  # 未提供的欄位仍沿用範本預設值

    def test_seed_none_and_empty_string_values_are_ignored(self, tmp_path):
        path = tmp_path / "settings.json"
        settings_module.ensure_settings_file(path, seed={"host": "", "username": None, "port": 21})
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["host"] == ""  # 範本預設值本來就是空字串
        assert data["port"] == 21

    def test_seed_false_boolean_is_preserved_not_treated_as_empty(self, tmp_path):
        path = tmp_path / "settings.json"
        settings_module.ensure_settings_file(path, seed={"upload_log": False, "recursive": False})
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["upload_log"] is False
        assert data["recursive"] is False


class TestOpenInDefaultApp:
    def test_windows_uses_os_startfile(self):
        with patch.object(settings_module.sys, "platform", "win32"), \
             patch.object(settings_module.os, "startfile", create=True) as mock_startfile:
            settings_module.open_in_default_app("C:/settings.json")
            mock_startfile.assert_called_once_with("C:/settings.json")

    def test_macos_uses_open_command(self):
        with patch.object(settings_module.sys, "platform", "darwin"), \
             patch.object(settings_module, "subprocess") as mock_subprocess:
            settings_module.open_in_default_app("/tmp/settings.json")
            mock_subprocess.run.assert_called_once_with(["open", "/tmp/settings.json"])

    def test_linux_uses_xdg_open(self):
        with patch.object(settings_module.sys, "platform", "linux"), \
             patch.object(settings_module, "subprocess") as mock_subprocess:
            settings_module.open_in_default_app("/tmp/settings.json")
            mock_subprocess.run.assert_called_once_with(["xdg-open", "/tmp/settings.json"])


class TestLocalPathGuard:
    """本地端路徑欄位不得含 shell 語法。

    這一組守的是一個**靜默**的失敗:`~` 與 `$HOME` 都不是絕對路徑,settings.py 也從不
    展開它們,於是會被當成相對路徑、相對於 CWD(= share/sftp_transfer)解析成
    `share/sftp_transfer/$HOME/...` —— 真的建出名字叫 `$HOME` 的目錄並把檔案放進去,
    沒有任何錯誤訊息。所以寧可當場拒絕。
    """

    def _load(self, tmp_path, cfg):
        import json
        p = tmp_path / "c.json"
        p.write_text(json.dumps(cfg), encoding="utf-8")
        return settings_module.load_settings(p)

    @pytest.mark.parametrize("field, value", [
        ("local_path", "$HOME/Documents/x"),
        ("local_path", "~/x"),
        ("ignore_file", "~/ig.txt"),
        ("ignore_file", "$PROJECT/ig.txt"),
        ("log_dir", "$HOME/logs"),
        ("key_file", "$HOME/.ssh/id_rsa"),
    ])
    def test_rejects_shell_syntax(self, tmp_path, field, value):
        with pytest.raises(settings_module.ConfigPathError, match="shell 語法"):
            self._load(tmp_path, {field: value})

    def test_rejects_shell_syntax_inside_list(self, tmp_path):
        # local_path 可以是陣列(與 remote_path 逐一配對),每個元素都要檢查。
        with pytest.raises(settings_module.ConfigPathError):
            self._load(tmp_path, {"local_path": ["ok", "$HOME/bad"]})

    @pytest.mark.parametrize("cfg", [
        {"local_path": "."},
        {"local_path": "../scheduler"},
        {"ignore_file": "config/sftp_download_ignore.txt"},
        {"log_dir": "logs"},
        {"local_path": "/absolute/is/fine"},
    ])
    def test_accepts_relative_and_absolute(self, tmp_path, cfg):
        assert self._load(tmp_path, cfg) == cfg

    def test_remote_path_is_not_guarded(self, tmp_path):
        """remote_path 是 SFTP **伺服器**上的路徑,不能用本機的家目錄去解讀它。"""
        cfg = {"remote_path": "~/on-the-server"}
        assert self._load(tmp_path, cfg) == cfg

    def test_error_is_a_placeholder_error_subclass(self):
        """刻意繼承 PlaceholderError:main.py / gui.py / pack_upload.py 已有
        「設定檔的值不可用 → 印訊息並中止」的處理,不必逐一改就能一致。"""
        assert issubclass(settings_module.ConfigPathError,
                          settings_module.PlaceholderError)


class TestRunScriptCwdContract:
    """所有 script/run_*.sh 都必須 `cd "$BASE_DIR"`。

    這是相對路徑得以成立的前提:config/ 是集中管理、由 SFTP OTA 發佈到全船隊的
    (見 .sftp_upload_manifest.json;config/ 不在 sftp_download_ignore.txt 裡,而
    duplicate_mode=overwrite),所以那些檔案裡不能有機器專屬的絕對路徑 —— 只能寫
    相對於 share/sftp_transfer 的路徑。

    少一支 `cd` 就會讓那支腳本用錯的 CWD 解析 local_path/ignore_file/log_dir,
    而且是靜默放錯位置。新增腳本忘了 cd,這個測試會擋下來。
    """

    def test_every_run_script_cds_to_base_dir(self):
        import re
        script_dir = PROJECT_ROOT / "script"
        scripts = sorted(script_dir.glob("run_*.sh"))
        assert scripts, "找不到任何 script/run_*.sh"
        pattern = re.compile(r'^\s*cd\s+"\$BASE_DIR"\s*$', re.MULTILINE)
        missing = [p.name for p in scripts
                   if not pattern.search(p.read_text(encoding="utf-8"))]
        assert missing == [], (
            "這些 run_*.sh 沒有 `cd \"$BASE_DIR\"`，相對路徑會相對於呼叫者的 CWD "
            "而把檔案放到錯誤位置：" + ", ".join(missing)
        )


class TestNvmePlaceholder:
    """{nvme} 保留字佔位符：值由執行時探測產生，不是去船舶資訊檔查表。

    全部注入探測結果，不依賴本機磁碟狀態 —— 否則真實掛載點裡的檔案系統 UUID 會跑進
    斷言，換一顆盤或換一台機器就紅。
    """

    def test_expands_to_probed_mount_point(self):
        with patch.object(settings_module, "_probe_nvme_mount", return_value="/media/u/UUID"):
            result = settings_module.resolve_placeholders({"local_path": "{nvme}/sftp_data"})
        assert result["local_path"] == "/media/u/UUID/sftp_data"

    def test_unmounted_raises_with_device_and_remediation(self, monkeypatch):
        """探不到就中止。訊息是這筆失敗唯一的載體。

        PlaceholderError 發生在 main.py 建 logger **之前**，所以不會產生 SFTP log CSV、
        岸端 monitor 看不到；stderr 是唯一線索，因此訊息必須自己講完「哪顆盤、怎麼修」。
        """
        monkeypatch.setenv(settings_module.NVME_DEVICE_ENV, "/dev/does-not-exist")
        with patch.object(settings_module, "_probe_nvme_mount", return_value=None):
            with pytest.raises(settings_module.PlaceholderError) as exc:
                settings_module.resolve_placeholders({"local_path": "{nvme}/x"})
        message = str(exc.value)
        assert "/dev/does-not-exist" in message
        assert "udisksctl mount -b" in message
        assert "local_path" in message

    def test_no_nvme_placeholder_does_not_probe(self):
        """沒用到 {nvme} 就不該 fork findmnt（比照「沒佔位符不讀船舶資訊檔」）。"""
        with patch.object(settings_module, "_probe_nvme_mount") as probe:
            settings_module.resolve_placeholders({"local_path": ".", "port": 22})
        probe.assert_not_called()

    def test_probed_once_per_call(self):
        with patch.object(settings_module, "_probe_nvme_mount", return_value="/mnt/d") as probe:
            result = settings_module.resolve_placeholders({
                "local_path": "{nvme}/a",
                "log_dir": "{nvme}/logs",
                "ignore_file": "{nvme}/ig.txt",
            })
        assert probe.call_count == 1
        assert result["log_dir"] == "/mnt/d/logs"

    def test_reserved_word_wins_over_vessel_info_key(self, tmp_path, monkeypatch):
        """船舶資訊檔就算有 nvme 這個 key 也不能蓋掉保留字。

        身分檔是人手維護的宣告，探測是機器現況；現況優先，否則一個手誤的 key 會把
        全船隊導到一條不存在的路徑上。
        """
        path = tmp_path / "vessel_basic_info.json"
        path.write_text('{"vsl_name": "WH289", "nvme": "/wrong"}', encoding="utf-8")
        monkeypatch.setenv("VESSEL_INFO_PATH", str(path))
        with patch.object(settings_module, "_probe_nvme_mount", return_value="/mnt/right"):
            result = settings_module.resolve_placeholders({"local_path": "{nvme}/x"})
        assert result["local_path"] == "/mnt/right/x"

    def test_resolved_inside_list_elements(self):
        # local_path 可以是陣列（與 remote_path 逐一配對），每個元素都要展開。
        with patch.object(settings_module, "_probe_nvme_mount", return_value="/mnt/d"):
            result = settings_module.resolve_placeholders({"local_path": ["plain", "{nvme}/x"]})
        assert result["local_path"] == ["plain", "/mnt/d/x"]

    def test_mixed_with_vessel_info_placeholders(self, tmp_path, monkeypatch):
        path = tmp_path / "vessel_basic_info.json"
        path.write_text('{"vsl_name": "WH289"}', encoding="utf-8")
        monkeypatch.setenv("VESSEL_INFO_PATH", str(path))
        with patch.object(settings_module, "_probe_nvme_mount", return_value="/mnt/d"):
            result = settings_module.resolve_placeholders({"local_path": "{nvme}/{vsl_name}/x"})
        assert result["local_path"] == "/mnt/d/WH289/x"

    def test_expansion_passes_shell_guard(self, tmp_path):
        """展開結果是絕對路徑，通過 _check_local_paths（它刻意排在替換之後）。"""
        path = tmp_path / "c.json"
        path.write_text(json.dumps({"local_path": "{nvme}/data"}), encoding="utf-8")
        with patch.object(settings_module, "_probe_nvme_mount", return_value="/media/u/UUID"):
            result = settings_module.load_settings(path)
        assert result == {"local_path": "/media/u/UUID/data"}

    def test_unknown_key_error_mentions_reserved_words(self, tmp_path, monkeypatch):
        """打錯保留字（{nvem}）時，訊息要把保留字列出來，否則無從得知有這個字。"""
        path = tmp_path / "vessel_basic_info.json"
        path.write_text('{"vsl_name": "WH289"}', encoding="utf-8")
        monkeypatch.setenv("VESSEL_INFO_PATH", str(path))
        with pytest.raises(settings_module.PlaceholderError, match="nvme"):
            settings_module.resolve_placeholders({"local_path": "{nvem}/x"})


class TestProbeNvmeMount:
    """探測本身：findmnt 的呼叫方式，以及各種問不到都回 None（由呼叫方統一報錯）。"""

    def _completed(self, returncode=0, stdout=""):
        return MagicMock(returncode=returncode, stdout=stdout)

    def test_returns_first_mount_point(self, monkeypatch):
        # 同一顆裝置可能列出多個掛載點（bind mount），取第一個，與
        # scheduler/reboot_script/start_web_docker.sh 的 `| head -1` 一致。
        monkeypatch.setenv(settings_module.NVME_DEVICE_ENV, "/dev/nvme9n1")
        completed = self._completed(stdout="/media/u/UUID\n/mnt/bind\n")
        with patch.object(settings_module.subprocess, "run", return_value=completed) as run:
            assert settings_module._probe_nvme_mount() == "/media/u/UUID"
        assert run.call_args[0][0] == [
            "findmnt", "-n", "-o", "TARGET", "-S", "/dev/nvme9n1",
        ]

    def test_nonzero_returncode_is_none(self, monkeypatch):
        monkeypatch.delenv(settings_module.NVME_DEVICE_ENV, raising=False)
        with patch.object(settings_module.subprocess, "run",
                          return_value=self._completed(returncode=1)):
            assert settings_module._probe_nvme_mount() is None

    def test_blank_output_is_none(self, monkeypatch):
        monkeypatch.delenv(settings_module.NVME_DEVICE_ENV, raising=False)
        with patch.object(settings_module.subprocess, "run",
                          return_value=self._completed(stdout="\n   \n")):
            assert settings_module._probe_nvme_mount() is None

    def test_findmnt_missing_is_none(self, monkeypatch):
        # 非 Linux 或極簡環境沒有 findmnt。當成探測不到，不要讓 OSError 漏出去。
        monkeypatch.delenv(settings_module.NVME_DEVICE_ENV, raising=False)
        with patch.object(settings_module.subprocess, "run", side_effect=OSError("nope")):
            assert settings_module._probe_nvme_mount() is None

    def test_default_device_matches_reboot_launcher(self, monkeypatch):
        """預設裝置必須與 scheduler/reboot_launcher.sh 掛載的那顆一致。

        那支開機時跑 `udisksctl mount -b /dev/nvme0n1`；兩邊對不上的話，設定檔會去問
        一顆沒人掛的盤。
        """
        monkeypatch.delenv(settings_module.NVME_DEVICE_ENV, raising=False)
        assert settings_module.NVME_DEVICE == "/dev/nvme0n1"
        with patch.object(settings_module.subprocess, "run",
                          return_value=self._completed(stdout="/m\n")) as run:
            settings_module._probe_nvme_mount()
        assert run.call_args[0][0][-1] == "/dev/nvme0n1"

    def test_uses_py36_safe_subprocess_kwargs(self, monkeypatch):
        """船端 Bionic 是 py3.6：只能用 universal_newlines=，不能用 3.7 的 text 參數。

        test_offline_deploy.py 的靜態掃描已經守著字面寫法，這裡守的是**行為** ——
        真的有把解碼參數傳下去，否則 stdout 會是 bytes，splitlines 出來的元素比不過字串。
        """
        monkeypatch.delenv(settings_module.NVME_DEVICE_ENV, raising=False)
        with patch.object(settings_module.subprocess, "run",
                          return_value=self._completed(stdout="/m\n")) as run:
            settings_module._probe_nvme_mount()
        kwargs = run.call_args[1]
        assert kwargs["universal_newlines"] is True
        assert "text" not in kwargs
