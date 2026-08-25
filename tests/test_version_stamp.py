"""version_stamp.py 單元測試：宣告檢查、manifest 範圍、上傳/下載的差異。

版本標記掛在 run_cli() 這個收口而不是某支 run script —— 發布與更新有 run_*.sh、
run_all_uploads.py、run_selected_transfers.py、手動 main.py --cli 等多條路，只在單一
腳本裡處理，換條路走就靜默失去版本資訊。

manifest 刻意直接沿用 pack_upload.build_archive_plan()（＝ SFTPUploader 的選檔邏輯 +
同一份 ignore_file），所以「manifest 內容」與「真正會上傳的檔案」不可能走鐘；
這裡的測試因此重在「範圍」與「失敗時不擋住傳輸」。
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import version_stamp as V


def make_project(root, declaration=None, ignore_lines=None):
    """做一個「自帶 VERSION.json」的假專案，回傳 (root, transfer_settings)。"""
    root = Path(root)
    (root / "sub").mkdir(parents=True, exist_ok=True)
    (root / "code.py").write_text("print('hi')\n", encoding="utf-8")
    (root / "sub" / "conf.json").write_text('{"k": 1}\n', encoding="utf-8")
    (root / "big.whl").write_text("x" * 64, encoding="utf-8")
    (root / "skipme.tmp").write_text("tmp\n", encoding="utf-8")
    if declaration is not None:
        (root / V.DECLARATION_NAME).write_text(json.dumps(declaration, ensure_ascii=False),
                                               encoding="utf-8")
    ignore_file = None
    if ignore_lines is not None:
        ignore_file = root.parent / "ignore.txt"
        ignore_file.write_text("\n".join(ignore_lines) + "\n", encoding="utf-8")
    settings = {
        "mode": "upload",
        "local_path": str(root),
        "remote_path": "/remote/proj",
        "recursive": True,
        "ignore_file": str(ignore_file) if ignore_file else None,
    }
    return root, settings


BASE_DECL = {"version": "1.2.3", "date": "2026-01-01", "notes": "n"}


class TestDeclaration:
    def test_missing_declaration_means_feature_off(self, tmp_path):
        root, settings = make_project(tmp_path / "proj")       # 不寫 VERSION.json
        assert V.load_declaration(root) is None
        assert V.apply("upload", str(root), "keepme", settings) == "keepme"
        assert not (root / V.STAMP_NAME).exists()

    def test_version_must_be_non_empty(self, tmp_path):
        assert V.validate_declaration({"version": "  "})[0] is None
        assert V.validate_declaration({})[0] is None
        assert V.validate_declaration({"version": 7})[0] is None

    def test_version_must_not_contain_whitespace(self, tmp_path):
        # 版號會被寫進 log CSV 的 version_info 欄與 shell 變數，含空白只會讓下游難解
        assert V.validate_declaration({"version": "1 2"})[0] is None

    def test_stamp_exclude_must_be_string_list(self, tmp_path):
        assert V.validate_declaration({"version": "1", "stamp_exclude": "wheels/"})[0] is None
        assert V.validate_declaration({"version": "1", "stamp_exclude": [1]})[0] is None
        assert V.validate_declaration({"version": "1", "stamp_exclude": ["wheels/"]}) \
            == ("1", None, None, ["wheels/"])

    def test_valid_declaration_round_trip(self, tmp_path):
        assert V.validate_declaration(BASE_DECL) == ("1.2.3", "2026-01-01", "n", [])


class TestProjectRoot:
    def test_list_local_path_is_refused(self, tmp_path):
        # 多來源代表這次傳輸涵蓋多個專案，「哪一個的版號」沒有答案，寧可不做也不猜
        assert V.project_root([str(tmp_path)]) is None

    def test_missing_dir_is_refused(self, tmp_path):
        assert V.project_root(str(tmp_path / "nope")) is None

    def test_existing_dir(self, tmp_path):
        assert V.project_root(str(tmp_path)) == tmp_path.resolve()


class TestStamp:
    def test_upload_writes_stamp_and_fills_version(self, tmp_path):
        root, settings = make_project(tmp_path / "proj", BASE_DECL)
        got = V.apply("upload", str(root), "", settings)
        info = json.loads((root / V.STAMP_NAME).read_text(encoding="utf-8"))
        assert info["version"] == "1.2.3"
        assert info["notes"] == "n"
        assert got.startswith("1.2.3+")
        assert set(info["files"]) == {"code.py", "sub/conf.json", "big.whl", "skipme.tmp",
                                      V.DECLARATION_NAME}
        assert info["tree_sha256"] == V.tree_sha256(info["files"])
        assert info["file_count"] == len(info["files"])

    def test_download_reads_without_writing(self, tmp_path):
        root, settings = make_project(tmp_path / "proj", BASE_DECL)
        V.apply("upload", str(root), "", settings)                 # 先有一份標記
        stamp_before = (root / V.STAMP_NAME).read_text(encoding="utf-8")
        (root / "code.py").write_text("changed\n", encoding="utf-8")
        got = V.apply("download", str(root), "", settings)          # 下載只讀不寫
        assert got.startswith("1.2.3+")                             # 記的是「下載前」的版本
        assert (root / V.STAMP_NAME).read_text(encoding="utf-8") == stamp_before

    def test_stamp_file_never_lists_itself(self, tmp_path):
        # 否則自己的雜湊寫進去的瞬間就過期，船上永遠 mismatch
        root, settings = make_project(tmp_path / "proj", BASE_DECL)
        V.apply("upload", str(root), "", settings)
        info = json.loads((root / V.STAMP_NAME).read_text(encoding="utf-8"))
        assert V.STAMP_NAME not in info["files"]

    def test_ignore_file_shrinks_manifest(self, tmp_path):
        # manifest ＝ 真正會上傳的檔案，所以 upload ignore 一生效，manifest 就跟著縮
        root, settings = make_project(tmp_path / "proj", BASE_DECL, ignore_lines=["*.tmp", "sub/"])
        V.apply("upload", str(root), "", settings)
        info = json.loads((root / V.STAMP_NAME).read_text(encoding="utf-8"))
        assert set(info["files"]) == {"code.py", "big.whl", V.DECLARATION_NAME}

    def test_stamp_exclude_shrinks_manifest(self, tmp_path):
        # 會上船但不屬於「程式碼身分」的東西（安裝期產物、由別的元件覆寫的檔）
        decl = dict(BASE_DECL, stamp_exclude=["*.whl", "sub/"])
        root, settings = make_project(tmp_path / "proj", decl)
        V.apply("upload", str(root), "", settings)
        info = json.loads((root / V.STAMP_NAME).read_text(encoding="utf-8"))
        assert set(info["files"]) == {"code.py", "skipme.tmp", V.DECLARATION_NAME}

    def test_explicit_version_info_is_not_overridden(self, tmp_path):
        root, settings = make_project(tmp_path / "proj", BASE_DECL)
        assert V.apply("upload", str(root), "手動指定", settings) == "手動指定"
        assert (root / V.STAMP_NAME).exists()          # 標記仍然要產生

    def test_invalid_declaration_does_not_block_transfer(self, tmp_path):
        # 版本資訊是觀測用的，宣告寫壞不該讓傳輸停擺（船上會看到 files:UNSTAMPED）
        root, settings = make_project(tmp_path / "proj", {"version": ""})
        assert V.apply("upload", str(root), "", settings) == "unknown"
        assert not (root / V.STAMP_NAME).exists()

    def test_broken_settings_does_not_block_transfer(self, tmp_path):
        root, settings = make_project(tmp_path / "proj", BASE_DECL)
        settings["remote_path"] = None                  # pack_upload 會拒絕
        assert V.apply("upload", str(root), "", settings) == "1.2.3-unstamped"
        assert not (root / V.STAMP_NAME).exists()

    def test_forgot_to_bump_warns(self, tmp_path, capsys):
        root, settings = make_project(tmp_path / "proj", BASE_DECL)
        V.apply("upload", str(root), "", settings)
        (root / "code.py").write_text("changed\n", encoding="utf-8")
        capsys.readouterr()
        V.apply("upload", str(root), "", settings)      # 版號沒動、內容變了
        assert "忘了在" in capsys.readouterr().err

    def test_same_content_does_not_warn(self, tmp_path, capsys):
        root, settings = make_project(tmp_path / "proj", BASE_DECL)
        V.apply("upload", str(root), "", settings)
        capsys.readouterr()
        V.apply("upload", str(root), "", settings)
        assert "忘了在" not in capsys.readouterr().err


class TestVersionString:
    """radar 的 little_utils/version_info.py 有一份同格式的實作，兩邊必須一致。"""

    def test_short_version_shapes(self):
        assert V.short_version({"version": "9.7", "commit": "abc1234"}) == "9.7+abc1234"
        assert V.short_version({"version": "9.7", "commit": "abc1234", "dirty": True}) \
            == "9.7+abc1234-dirty"
        assert V.short_version({"version": "9.7"}) == "9.7+nogit"
        assert V.short_version(None) == "unknown"

    def test_current_version_falls_back_to_declaration(self, tmp_path):
        root, _ = make_project(tmp_path / "proj", BASE_DECL)
        assert V.current_version(root) == "1.2.3-unstamped"

    def test_current_version_unknown_without_declaration(self, tmp_path):
        root, _ = make_project(tmp_path / "proj")
        assert V.current_version(root) == "unknown"


def test_manifest_hashes_actual_content(tmp_path):
    root, settings = make_project(tmp_path / "proj", BASE_DECL)
    V.apply("upload", str(root), "", settings)
    info = json.loads((root / V.STAMP_NAME).read_text(encoding="utf-8"))
    assert info["files"]["code.py"] == V.sha256_file(root / "code.py")
