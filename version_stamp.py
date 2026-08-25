# -*- coding: utf-8 -*-
"""版本標記：待傳輸的專案根目錄有 VERSION.json 時，上傳前自動產生 VERSION.stamp.json。

為什麼這件事在傳輸工具裡
------------------------
船端的更新是「整目錄 overwrite 鏡像」，而且不是原子操作：部分下載會留下「有些檔新、
有些檔舊」的混合樹，光看版號字串完全看不出來。要判斷船上那份程式碼到底完不完整，
只能靠「發布時每個檔的 sha256」——而那必須在**發布端**算（船上沒有 .git，也沒有基準）。

發布/更新有很多條路（run_*.sh、run_all_uploads.py、run_selected_transfers.py、手動
main.py --cli），`main.py` 的 `run_cli()` 是它們共同的收口，所以掛在這裡；掛在任何一支
腳本上，換條路走就靜默失去版本資訊。

兩個檔，兩種角色（都放在**待傳輸專案**的根目錄，不是本工具的目錄）
-----------------------------------------------------------------
    VERSION.json        「宣告」：對外版號，人工編輯、納入該專案的 git。
                        version（必填）/ date / notes / stamp_exclude。
    VERSION.stamp.json  「事實」：本模組產生，該專案應把它 gitignore；隨鏡像上船。
                        = 宣告內容 + git commit/branch/dirty + files{path: sha256}。

要讓一個新專案獲得這項功能，只需在它的根目錄放一個 VERSION.json。不需要在專案裡放任何
腳本 —— 這正是本模組存在的理由（前一版要求每個專案自帶 tools/stamp_version.py，於是
每個專案都得複製兩支 py，還各自維護一份與 upload ignore 重複的排除清單）。

manifest 的範圍
---------------
manifest **就是這次真正會上傳的檔案清單**：直接沿用 pack_upload.build_archive_plan()，
也就是 SFTPUploader 的選檔邏輯 + 同一份 ignore_file。所以不存在「第二份排除清單要跟
upload ignore 同步」的問題。

之上再扣掉專案自己宣告的 stamp_exclude（gitignore 語法）。它是給「會上船、但不屬於
程式碼身分」的東西用的，兩類典型：
  * 巨大的安裝期產物（radar 的 wheels/、YOLOv7_MODEL/ 合計 780 MB）——
    列進 manifest 會讓船上每次開機驗證都要重讀好幾百 MB。
  * 內容不由該專案決定的檔（radar 有 3 個檔每次 import 都被 share/ 覆寫）——
    列進 manifest 只會永遠 mismatch。

相容性：本檔在船端也會被 import（下載時要讀版本字串），Bionic 船端的 venv 只有
python3.6（見 tests/test_offline_deploy.py 的 ShipInterpreterCompatTests）。
"""

import hashlib
import json
import os
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from gitignore import GitIgnoreSpec

DECLARATION_NAME = "VERSION.json"
STAMP_NAME = "VERSION.stamp.json"

_HASH_CHUNK = 1 << 20
# manifest 大到這個程度就提醒:船上每次開機都要重算一次,幾百 MB 的 manifest 是設定失誤,
# 通常代表該專案忘了把安裝期產物列進 stamp_exclude。只警告不阻擋 —— 也許人家就是要。
_MANIFEST_SIZE_WARN = 100 * 1000 * 1000


def sha256_file(path):
    digest = hashlib.sha256()
    with open(str(path), "rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_sha256(files):
    """整棵樹的單值摘要：排序後 "path<TAB>sha256" 行的 sha256。"""
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(("%s\t%s\n" % (path, files[path])).encode("utf-8"))
    return digest.hexdigest()


def project_root(local_path):
    """待傳輸專案的根目錄；local_path 不是單一路徑（多來源設定）時回 None。

    多來源代表這次傳輸涵蓋多個專案，「哪一個的版號」沒有答案，寧可不做也不猜。
    """
    if not local_path or not isinstance(local_path, str):
        return None
    try:
        root = Path(local_path).resolve()
    except OSError:
        return None
    return root if root.is_dir() else None


def load_declaration(root):
    """讀 <root>/VERSION.json。沒有這個檔就回 None（代表這個專案沒有要用版本標記）。"""
    if root is None:
        return None
    path = Path(root) / DECLARATION_NAME
    try:
        declared = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return declared if isinstance(declared, dict) else None


def validate_declaration(declared, logger=None):
    """檢查宣告內容。回傳 (version, date, notes, stamp_exclude)；version 為 None 代表不合格。"""
    def complain(message):
        if logger is not None:
            logger.warning("%s 不合格：%s", DECLARATION_NAME, message)
        else:
            print("%s 不合格：%s" % (DECLARATION_NAME, message), file=sys.stderr)

    version = declared.get("version")
    if not isinstance(version, str) or not version.strip():
        complain("version 必須是非空字串")
        return None, None, None, []
    version = version.strip()
    # 版號會被寫進 log CSV 的 version_info 欄、也會被 shell 取用,含空白只會讓下游難解。
    if any(char.isspace() for char in version):
        complain("version 不可含空白字元（收到 '%s'）" % version)
        return None, None, None, []
    exclude = declared.get("stamp_exclude") or []
    if not isinstance(exclude, list) or any(not isinstance(x, str) for x in exclude):
        complain("stamp_exclude 必須是字串陣列（gitignore 語法）")
        return None, None, None, []
    return version, declared.get("date"), declared.get("notes"), exclude


def _git(root, *args):
    """在專案根目錄執行 git，回傳 stdout；失敗回 None（沒有 git / 不是 repo 都不該爆掉）。"""
    try:
        # capture_output= / text= 是 3.7+；見檔首「相容性」。
        proc = subprocess.run(("git",) + args, cwd=str(root),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.rstrip("\n")


def git_metadata(root, exclude_spec):
    """回傳 (commit, branch, commit_time, dirty)。

    dirty 只看「會進 manifest 的檔案」——被 stamp_exclude 排除的檔（例如那 3 個每次
    import 都被 share/ 覆寫的檔）本來就不算這個專案的內容，算進去的話每次發布都是 dirty，
    這個旗標就沒有意義了。
    """
    commit = _git(root, "rev-parse", "--short", "HEAD")
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    commit_time = _git(root, "log", "-1", "--format=%cI")
    # core.quotePath=false：否則含中文的檔名會被轉義成 "\345\256\211…"，判定與顯示都失準。
    status = _git(root, "-c", "core.quotePath=false", "status", "--porcelain")
    dirty = []
    for line in (status or "").splitlines():
        code, rel = (line[:2].strip() or "?"), line[3:].strip().strip('"')
        if " -> " in rel:               # rename 的格式是 "old -> new"，取新路徑判定
            rel = rel.split(" -> ", 1)[1]
        if rel and not _excluded(exclude_spec, rel):
            dirty.append((code, rel))
    return commit, branch, commit_time, dirty


def _compile_exclude(patterns):
    """把 stamp_exclude 編成 gitignore 規則；空清單或全部無效時回 None。"""
    valid = []
    for line in patterns or []:
        try:
            GitIgnoreSpec.from_lines([line])
            valid.append(line)
        except ValueError:
            print("%s 的 stamp_exclude 規則格式錯誤，略過：%s" % (DECLARATION_NAME, line),
                  file=sys.stderr)
    return GitIgnoreSpec.from_lines(valid) if valid else None


def _excluded(spec, rel_path):
    return spec is not None and spec.match_file(rel_path)


def _relative(path, root):
    """把絕對路徑轉成相對專案根目錄的 posix 路徑；不在根目錄下則回 None。"""
    try:
        rel = Path(path).resolve().relative_to(root)
    except (ValueError, OSError):
        return None
    return rel.as_posix()


def build_manifest(root, transfer_settings, exclude_spec, logger=None):
    """算出 {相對路徑: sha256}。回傳 (files, skipped, total_bytes)。

    檔案清單直接來自 pack_upload.build_archive_plan()（= SFTPUploader 的選檔邏輯 +
    同一份 ignore_file），所以 manifest 與「真正會上傳的東西」不可能走鐘。
    """
    # 延遲匯入:下載路徑用不到(只讀版本字串),不必為它拉進 pack_upload 與 dataclasses。
    import pack_upload

    stamp_path = Path(root) / STAMP_NAME
    plan = pack_upload.build_archive_plan(transfer_settings,
                                          excluded_paths=(stamp_path,),
                                          logger=logger)
    files = {}
    skipped = 0
    total = 0
    for entry in plan.files:
        rel = _relative(entry.source, root)
        if rel is None:                 # 多來源設定裡不屬於本專案的檔
            continue
        if _excluded(exclude_spec, rel):
            skipped += 1
            continue
        files[rel] = sha256_file(entry.source)
        try:
            total += os.path.getsize(str(entry.source))
        except OSError:
            pass
    return files, skipped, total


def short_version(info):
    """單行版本字串：9.7+35e1464 / 9.7+35e1464-dirty / 9.7-unstamped。

    radar 的 little_utils/version_info.py 有一份同格式的實作（船上要自己印橫幅）。
    改格式時兩邊要一起改，否則岸端會看到兩種形狀的版號。
    """
    if not info:
        return "unknown"
    version = info.get("version") or "unknown"
    # 有標記但取不到 commit(發布端不是 git repo)仍算「已標記」——sha256 manifest 還是在的。
    # 「未標記」是另一回事,由 current_version() 處理。
    commit = info.get("commit") or "nogit"
    return "%s+%s%s" % (version, commit, "-dirty" if info.get("dirty") else "")


def read_stamp(root):
    """讀已存在的 VERSION.stamp.json（沒有就回 None）。"""
    if root is None:
        return None
    try:
        info = json.loads((Path(root) / STAMP_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) and info.get("version") else None


def current_version(root):
    """這個專案「現在」的版本字串：有標記用標記，否則退回宣告（標成 -unstamped）。"""
    info = read_stamp(root)
    if info:
        return short_version(info)
    declared = load_declaration(root)
    if declared and isinstance(declared.get("version"), str) and declared["version"].strip():
        return "%s-unstamped" % declared["version"].strip()
    return "unknown"


def write_stamp(root, transfer_settings, logger=None):
    """產生並寫入 VERSION.stamp.json。回傳版本字串（失敗回 None）。"""
    def say(message):
        if logger is not None:
            logger.info(message)
        else:
            print(message)

    def warn(message):
        if logger is not None:
            logger.warning(message)
        else:
            print(message, file=sys.stderr)

    declared = load_declaration(root)
    if declared is None:
        return None
    version, date, notes, exclude = validate_declaration(declared, logger)
    if version is None:
        return None

    exclude_spec = _compile_exclude(exclude)
    previous = read_stamp(root)
    commit, branch, commit_time, dirty = git_metadata(root, exclude_spec)
    try:
        files, skipped, total = build_manifest(root, transfer_settings, exclude_spec, logger)
    except Exception as error:          # noqa: BLE001 —— 版本標記不該讓發布停擺
        warn("算不出 manifest，這次上傳將不帶版本標記：%s" % error)
        return None

    info = {
        "version": version,
        "date": date,
        "notes": notes,
        "commit": commit,
        "branch": branch,
        "commit_time": commit_time,
        "dirty": bool(dirty),
        "build_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "built_by": "%s@%s" % (os.environ.get("USER") or os.environ.get("LOGNAME") or "unknown",
                               _hostname()),
        "file_count": len(files),
        "tree_sha256": tree_sha256(files),
        "files": dict(sorted(files.items())),
    }
    path = Path(root) / STAMP_NAME
    try:
        path.write_text(json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as error:
        warn("寫不進 %s：%s" % (path, error))
        return None

    say("版本標記已寫入 %s：%s（%d 個檔，%.1f MB%s）"
        % (path.name, short_version(info), len(files), total / 1e6,
           "，另有 %d 個檔依 stamp_exclude 排除" % skipped if skipped else ""))
    if total > _MANIFEST_SIZE_WARN:
        warn("manifest 有 %.0f MB —— 船上每次開機都要重算一次。"
             "安裝期產物（wheels、模型檔）建議列進 %s 的 stamp_exclude。"
             % (total / 1e6, DECLARATION_NAME))
    if commit is None:
        warn("取不到 git 資訊（不是 git repo？）—— 標記裡不會有 commit，"
             "船上只看得到宣告的版號。")
    if dirty:
        warn("工作區有 %d 個未提交的修改，已記為 dirty: true。" % len(dirty))
        for code, rel in dirty[:10]:
            warn("        %2s %s" % (code, rel))
        if len(dirty) > 10:
            warn("        …（其餘 %d 個略）" % (len(dirty) - 10))
    # 版號沒動但內容變了 = 大概是忘了編輯 VERSION.json。不中止(緊急發布不該被擋),
    # 但一定要吵:兩批不同的程式碼掛同一個版號,岸端就分不出船上跑的是哪一批。
    if (previous and previous.get("version") == version
            and previous.get("tree_sha256") != info["tree_sha256"]):
        warn("版號仍是 %s，但檔案內容已與上次標記不同 —— 忘了在 %s 升版？"
             % (version, DECLARATION_NAME))
    return short_version(info)


def _hostname():
    try:
        return socket.gethostname()
    except OSError:
        return "unknown"


def apply(mode, local_path, version_info, transfer_settings, logger=None):
    """上傳前產生版本標記；並在呼叫端未指定 version_info 時填入版本字串。

    上傳 = 發布端動作，必須「上傳前」產生標記：船上沒有 .git，算不出自己是哪個 commit。
    下載時只讀不寫，取到的是「下載前」的版本 —— 那正是要記進 log 的（下載後的版本由各
    專案的啟動腳本自己記，例如 scheduler 的 start_radar.sh）。
    任何失敗都只警告：版本資訊是觀測用的，不該讓傳輸本身停擺；標記缺漏也不會被吃掉，
    船上驗證時會顯示成 files:UNSTAMPED / files:MISMATCH。
    """
    root = project_root(local_path)
    if root is None or load_declaration(root) is None:
        return version_info             # 這個專案沒有要用版本標記，完全照舊
    if mode == "upload":
        write_stamp(root, transfer_settings, logger)
    if version_info:
        return version_info             # 呼叫端明確指定過，不覆蓋
    return current_version(root)
