"""SFTP 上傳核心邏輯（local → remote）：與 `downloader.SFTPDownloader` 對稱的反向傳輸。

以 `local_path` 為來源、`remote_path` 為目的地，鏡射下載端的能力：遞迴走訪本地目錄、忽略規則、
以 size/mtime 判斷跳過未變更檔案、byte 級斷點續傳與版本紀錄（manifest）。連線/重試/網路偵測/關閉
等方向無關邏輯全部繼承自 `SFTPBase`。
"""

import hashlib
import os
import stat
import time
from pathlib import Path, PurePosixPath

import paramiko

from downloader import (
    CHUNK_SIZE,
    MANIFEST_FILENAME,
    PART_SUFFIX,
    SFTPBase,
    TransferCancelled,
    checkpoint_offset,
    diagnostic_message,
    format_exception,
    format_size,
)

UPLOAD_MANIFEST_FILENAME = ".sftp_upload_manifest.json"

# 版本紀錄檔存放在「本地來源目錄」內，走訪來源時必須排除，否則會把自己的 manifest 一起上傳。
# 同時排除下載端的 manifest，避免同一目錄雙向使用時把對方的紀錄檔也上傳出去。
_MANIFEST_NAMES = {UPLOAD_MANIFEST_FILENAME, MANIFEST_FILENAME}


def _is_transfer_temp(name):
    """下載端未完成的暫存檔（見 downloader.PART_SUFFIX）——絕不上傳。

    與 _MANIFEST_NAMES 同一個道理:這是本工具自己的中間產物,不是使用者資料。而且多個
    上傳任務的來源目錄同時也是下載目的地(例如 share/scheduler 既由 scheduler_download
    下載、又由 scheduler_upload 上傳回 fleet 的 STANDARD 目錄),一旦下載中斷留下半截
    暫存檔又被上傳出去,污染的是整支船隊的來源。這裡寫死、不依賴各船的 ignore 設定,
    才不會因為某艘船的設定檔沒跟上而破功。
    """
    return name.endswith(PART_SUFFIX)


class SFTPUploader(SFTPBase):
    """SFTP 上傳（local → remote）：遞迴走訪本地目錄、斷點續傳、忽略規則與版本紀錄。"""

    manifest_filename = UPLOAD_MANIFEST_FILENAME

    def _remote_exists(self, remote_path):
        try:
            self.sftp.stat(remote_path)
            return True
        except FileNotFoundError:
            return False

    def _handle_symlink(self, local_path, rel_path):
        """走訪遇到 symlink 時的掛鉤；回傳 True 代表已自行處理、不再往下走訪。

        預設回傳 False，也就是沿用「跟著連結看實體」的既有行為：SFTP 遠端沒有
        可靠的 symlink 語意，上傳仍需把連結解析成實際的檔案或資料夾。只有把內容
        封裝成 tar 時（見 pack_upload.py）才會覆寫成保留連結本身。
        """
        return False

    def _list_local_files(self, source, remote_root):
        """回傳 [(本地絕對路徑, rel_path)]。rel_path 一律以 / 分隔，作為遠端相對路徑與 manifest 的鍵。"""
        files = []
        if source.is_file():
            filename = source.name
            if _is_transfer_temp(filename):
                self.logger.info(f"略過未完成的下載暫存檔: {filename}")
            elif self._is_ignored(filename):
                self.logger.info(f"依忽略設定檔略過: {filename}")
            else:
                files.append((source, filename))
        elif self.recursive:
            self._walk_local_dir(source, "", files, remote_root)
        else:
            skipped_dirs = []
            for name in sorted(os.listdir(source)):
                full = source / name
                if full.is_dir():
                    skipped_dirs.append(name)
                elif name in _MANIFEST_NAMES or _is_transfer_temp(name):
                    continue
                elif self._is_ignored(name):
                    self.logger.info(f"依忽略設定檔略過: {name}")
                elif full.is_symlink() and self._handle_symlink(full, name):
                    continue
                else:
                    files.append((full, name))
            if skipped_dirs:
                self.logger.info(
                    f"僅上傳單層（未啟用多層），略過 {len(skipped_dirs)} 個子資料夾: {', '.join(skipped_dirs)}"
                )
        return files

    def _walk_local_dir(self, local_dir, rel_dir, files, remote_root):
        # 即使子資料夾底下沒有任何檔案，也要在遠端建立對應的空資料夾，與下載端「鏡射空資料夾」的行為對稱。
        remote_dir = remote_root.rstrip("/") + ("/" + rel_dir if rel_dir else "")
        self._ensure_remote_dir(remote_dir)
        for name in sorted(os.listdir(local_dir)):
            full = local_dir / name
            rel_path = f"{rel_dir}/{name}" if rel_dir else name
            # 只有根目錄層才會有 manifest 檔；rel_dir 為空字串代表目前正在走訪根目錄。
            if not rel_dir and name in _MANIFEST_NAMES:
                continue
            # 暫存檔則可能出現在任何一層(下載是逐檔進行的),每層都要擋。
            if not full.is_dir() and _is_transfer_temp(name):
                continue
            if full.is_dir():
                # 被忽略的資料夾整棵略過、不往下走訪，遠端也不會建立對應資料夾（與 git 行為一致）。
                if self._is_ignored(rel_path + "/"):
                    self.logger.info(f"依忽略設定檔略過資料夾: {rel_path}/")
                    continue
                # 目錄連結先問掛鉤；沒人接手就照舊跟進去，等同於把連結解析成實體內容。
                if full.is_symlink() and self._handle_symlink(full, rel_path):
                    continue
                self._walk_local_dir(full, rel_path, files, remote_root)
            elif self._is_ignored(rel_path):
                self.logger.info(f"依忽略設定檔略過: {rel_path}")
            elif full.is_symlink() and self._handle_symlink(full, rel_path):
                continue
            else:
                files.append((full, rel_path))

    def _next_remote_duplicate_path(self, remote_file):
        p = PurePosixPath(remote_file)

        def make(suffix):
            return str(p.with_name(f"{p.stem}_{suffix}{p.suffix}"))

        candidate = make(self.duplicate_suffix)
        n = 1
        while self._remote_exists(candidate):
            candidate = make(f"{self.duplicate_suffix}{n}")
            n += 1
        return candidate

    def _upload_one_file(self, local_file, rel_path, remote_root, local_root):
        remote_file = remote_root.rstrip("/") + "/" + rel_path
        self._ensure_remote_dir(str(PurePosixPath(remote_file).parent))
        local_stat = local_file.stat()
        local_size = local_stat.st_size
        local_mtime = int(local_stat.st_mtime)

        target_remote = remote_file
        uploaded_bytes = 0  # 遠端已存在的位元組數（續傳起點）
        mode = "wb"
        running_hash = hashlib.sha256()  # 邊上傳邊累加，最後（或中斷當下）存進版本紀錄檔

        try:
            remote_stat = self.sftp.stat(remote_file)
            remote_disk_size = remote_stat.st_size
            remote_exists = True
        except FileNotFoundError:
            remote_disk_size = 0
            remote_exists = False

        if remote_exists:
            if not self.resume:
                # 斷點續傳未啟用：不判斷是否未變更、也不接續，一律整份重新上傳；
                # 但存到哪個遠端檔名仍然要依 duplicate_mode 決定。
                if self.duplicate_mode == "overwrite":
                    self.logger.info(f"重新上傳並覆蓋遠端檔案: {rel_path}")
                else:
                    target_remote = self._next_remote_duplicate_path(remote_file)
                    self.logger.info(f"重新上傳，另存為: {PurePosixPath(target_remote).name}")
            else:
                known = self._manifest_entry(rel_path, local_root)

                if remote_disk_size == local_size:
                    # 大小相同：用版本紀錄（若有）判斷是否真的未變更；沒有紀錄則姑且視為未變更略過。
                    if known is None or (known.get("size") == local_size and known.get("mtime") == local_mtime):
                        self.logger.info(diagnostic_message(
                            "FILE_DECISION",
                            f"略過（已完整上傳）: {rel_path}",
                            direction="upload",
                            reason="same_size_without_manifest" if known is None else "manifest_version_match",
                            file=rel_path,
                            local_size=local_size,
                            local_mtime=local_mtime,
                            remote_size=remote_disk_size,
                            verification="size_only" if known is None else "size_and_mtime",
                            action="skip",
                        ))
                        self._manifest[rel_path] = {"size": local_size, "mtime": local_mtime}
                        self._manifest_dirty = True  # 收尾一次寫回，見 _flush_manifest
                        return "skipped"
                    if self.duplicate_mode == "overwrite":
                        self.logger.info(diagnostic_message(
                            "FILE_DECISION",
                            f"偵測到本地檔案已更新，覆蓋遠端檔案: {rel_path}",
                            direction="upload",
                            reason="manifest_version_changed",
                            file=rel_path,
                            local_size=local_size,
                            local_mtime=local_mtime,
                            remote_size=remote_disk_size,
                            checkpoint_size=known.get("size"),
                            checkpoint_mtime=known.get("mtime"),
                            action="overwrite",
                        ))
                    else:
                        target_remote = self._next_remote_duplicate_path(remote_file)
                        self.logger.info(f"偵測到本地檔案已更新，另存為: {PurePosixPath(target_remote).name}")
                elif remote_disk_size > local_size:
                    if self.duplicate_mode == "overwrite":
                        self.logger.warning(f"遠端檔案大於本地檔案，重新上傳: {rel_path}")
                    else:
                        target_remote = self._next_remote_duplicate_path(remote_file)
                        self.logger.warning(f"遠端檔案大於本地檔案，另存為: {PurePosixPath(target_remote).name}")
                elif self.duplicate_mode == "duplicate":
                    # 「另存新檔」模式一律整份重新上傳、不接續遠端舊檔案，斷點續傳形同停用，不需要驗證內容。
                    target_remote = self._next_remote_duplicate_path(remote_file)
                    self.logger.info(f"重新上傳，另存為: {PurePosixPath(target_remote).name}")
                else:
                    # 遠端檔案比本地小：逐項驗證 checkpoint，並留下穩定 reason code。
                    # 只讀本機前綴與 manifest，不回讀遠端內容 —— 能接續的位置一律是
                    # checkpoint_bytes（唯一有雜湊可驗證的 offset）；遠端目前長度只用來判斷
                    # 該直接 append（相等）、先把遠端切回檢查點（更長）、還是整份重傳（更短）。
                    reject_reason = None
                    actual_hash = None
                    truncate_error = None
                    discarded = 0
                    checkpoint_bytes = checkpoint_offset(known)
                    if known is None:
                        reject_reason = "checkpoint_missing"
                    elif known.get("size") != local_size:
                        reject_reason = "local_size_changed"
                    elif known.get("mtime") != local_mtime:
                        reject_reason = "local_mtime_changed"
                    elif checkpoint_bytes is None:
                        reject_reason = "checkpoint_offset_missing"
                    elif checkpoint_bytes > remote_disk_size:
                        # 遠端比檢查點短：檢查點聲稱驗證過的那一段內容已經不在遠端上（被截斷或
                        # 換過檔案），沒有東西可以比對 → 整份重新上傳。
                        reject_reason = "checkpoint_offset_mismatch"
                    elif not known.get("local_sha256"):
                        reject_reason = "checkpoint_hash_missing"
                    else:
                        prefix_hash = self._hash_local_prefix(local_file, checkpoint_bytes)
                        actual_hash = prefix_hash.hexdigest()
                        if actual_hash != known["local_sha256"]:
                            reject_reason = "checkpoint_hash_mismatch"
                        else:
                            # 遠端比檢查點長 → 多出來的尾巴是上一趟被硬砍（SIGKILL／斷電）時
                            # 已經寫進遠端、卻來不及記進 manifest 的部分。它「很可能」就是同一
                            # 份內容，但沒有任何雜湊能證明（要證明就得把遠端那段回讀下來，在
                            # 20 KB/s 的船岸鏈路上比重傳還貴），所以不賭：把遠端切回已驗證的
                            # checkpoint_bytes 再接續。丟掉的量有上限（CHECKPOINT_INTERVAL_BYTES）。
                            #
                            # 舊版在這裡要求「遠端大小與檢查點精確相等」，於是硬砍後留下的正常
                            # 狀態被判成不可信，每趟都從 byte 0 覆蓋重傳 —— 慢鏈路上的大檔因此
                            # 永遠上傳不完（實測 1.2 GB 的包裹每小時被砍掉重練一次）。
                            discarded = remote_disk_size - checkpoint_bytes
                            if discarded > 0:
                                try:
                                    self.sftp.truncate(target_remote, checkpoint_bytes)
                                except (OSError, paramiko.SSHException) as e:
                                    # 伺服器不支援 SETSTAT/size 之類的情況：退回整份覆蓋，
                                    # 語意與舊版相同，只是保留原因供診斷。
                                    reject_reason = "remote_truncate_failed"
                                    truncate_error = format_exception(e)
                            if reject_reason is None:
                                running_hash = prefix_hash  # 直接沿用，後續新上傳的內容繼續累加上去
                                self.logger.info(diagnostic_message(
                                    "RESUME_ACCEPTED",
                                    f"檢查點驗證相符，接續上傳: {rel_path}",
                                    direction="upload",
                                    file=rel_path,
                                    local_size=local_size,
                                    local_mtime=local_mtime,
                                    remote_size=remote_disk_size,
                                    resume_offset=checkpoint_bytes,
                                    remaining_bytes=local_size - checkpoint_bytes,
                                    discarded_bytes=discarded,
                                    action="truncate_and_append" if discarded else "append",
                                ))
                                uploaded_bytes = checkpoint_bytes
                                mode = "ab"

                    if mode == "wb":
                        # 走到這裡 duplicate_mode 必定是 overwrite；安全檢查未通過就從頭覆蓋。
                        self.logger.warning(diagnostic_message(
                            "RESUME_REJECTED",
                            f"遠端部分檔案無法安全接續，覆蓋遠端檔案: {rel_path}",
                            direction="upload",
                            reason=reject_reason or "unknown",
                            file=rel_path,
                            local_size=local_size,
                            local_mtime=local_mtime,
                            remote_size=remote_disk_size,
                            checkpoint_size=known.get("size") if known else None,
                            checkpoint_mtime=known.get("mtime") if known else None,
                            checkpoint_bytes=known.get("local_bytes") if known else None,
                            checkpoint_hash_present=bool(known and known.get("local_sha256")),
                            expected_hash_prefix=str(known.get("local_sha256") or "")[:12] if known else None,
                            actual_hash_prefix=actual_hash[:12] if actual_hash else None,
                            action="overwrite",
                            **({"error": truncate_error} if truncate_error else {}),
                        ))

        self.logger.info(f"開始上傳: {rel_path} ({format_size(local_size)})")
        last_pct_logged = -1
        transferred = uploaded_bytes
        start_time = time.time()
        # 上次落盤檢查點的位元組數與時間（節奏與理由見 CHECKPOINT_INTERVAL_*）。
        last_checkpoint_bytes = transferred
        last_checkpoint_time = start_time
        # 記住上次印進度的時間與位元組數，用差值算「這段期間的即時速率」，比整體平均更能反映當下網速。
        last_log_time = start_time
        last_log_bytes = transferred
        cancelled = None
        transfer_error = None
        try:
            with open(local_file, "rb") as local_f:
                local_f.seek(uploaded_bytes)
                with self.sftp.open(target_remote, mode) as remote_f:
                    while True:
                        chunk = local_f.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        remote_f.write(chunk)
                        running_hash.update(chunk)
                        transferred += len(chunk)
                        if local_size > 0:
                            pct = int(transferred / local_size * 100)
                            if pct > last_pct_logged:
                                now = time.time()
                                elapsed = now - last_log_time
                                # elapsed 可能為 0（連續 chunk 太快），此時略過速率不印，避免除以零。
                                if elapsed > 0:
                                    speed = (transferred - last_log_bytes) / elapsed
                                    self.logger.info(f"  {rel_path} 進度: {pct}% ({format_size(speed)}/s)")
                                else:
                                    self.logger.info(f"  {rel_path} 進度: {pct}%")
                                last_log_time = now
                                last_log_bytes = transferred
                                last_pct_logged = pct
                        # 存檢查點的節奏見 CHECKPOINT_INTERVAL_*：位元組或秒數任一到達就落盤，
                        # 不跟著百分比走。flush() 之後遠端已收下這些位元組（未啟用 pipeline 的
                        # SFTP 寫入是逐一等回應的），行程即使被 SIGKILL，manifest 記的 offset
                        # 仍然對得上遠端實際長度。
                        if self.resume:
                            now = time.time()
                            if self._checkpoint_due(transferred, last_checkpoint_bytes, last_checkpoint_time, now):
                                remote_f.flush()
                                self._manifest[rel_path] = {
                                    "size": local_size,
                                    "mtime": local_mtime,
                                    "local_sha256": running_hash.hexdigest(),
                                    "local_bytes": transferred,
                                }
                                self._save_manifest(local_root)
                                last_checkpoint_bytes = transferred
                                last_checkpoint_time = now
        except TransferCancelled as e:
            cancelled = e
            raise
        except (paramiko.SSHException, OSError, EOFError) as e:
            transfer_error = e
            raise
        finally:
            # 不論成功、失敗或中途被中斷，都存下目前實際上傳到的位置與雜湊，讓下次重試時能正確判斷
            # 「這是同一版本尚未上傳完的部分」，而不是每次中斷後都只能整份重來。
            if self.resume:
                self._manifest[rel_path] = {
                    "size": local_size,
                    "mtime": local_mtime,
                    "local_sha256": running_hash.hexdigest(),
                    "local_bytes": transferred,
                }
                saved = self._save_manifest(local_root)
                if cancelled:
                    self.logger.warning(diagnostic_message(
                        "CHECKPOINT_SAVED",
                        f"取消 checkpoint: {rel_path} offset={transferred}/{local_size}",
                        direction="upload",
                        reason="cancelled",
                        signal=cancelled.signum,
                        file=rel_path,
                        offset=transferred,
                        total_size=local_size,
                        local_mtime=local_mtime,
                        sha256_prefix=running_hash.hexdigest()[:12],
                        manifest_saved=saved,
                    ))
                elif transfer_error:
                    self.logger.warning(diagnostic_message(
                        "CHECKPOINT_SAVED",
                        f"上傳錯誤後已保存 checkpoint: {rel_path}",
                        direction="upload",
                        reason="transfer_error",
                        file=rel_path,
                        offset=transferred,
                        total_size=local_size,
                        local_mtime=local_mtime,
                        sha256_prefix=running_hash.hexdigest()[:12],
                        manifest_saved=saved,
                        error=format_exception(transfer_error),
                    ))

        total_elapsed = time.time() - start_time
        uploaded_this_run = transferred - uploaded_bytes  # 本次實際上傳的位元組（不含斷點續傳前遠端已存在的部分）
        done_name = PurePosixPath(target_remote).name if target_remote != remote_file else rel_path
        if total_elapsed > 0 and uploaded_this_run > 0:
            avg_speed = uploaded_this_run / total_elapsed
            self.logger.info(f"完成上傳: {done_name}（平均 {format_size(avg_speed)}/s）")
        else:
            self.logger.info(f"完成上傳: {done_name}")
        # 保留本地權限與 mtime 到遠端(SFTP 預設不搬;否則 .sh 等會掉 +x)。
        # 失敗只警告不中斷 —— 內容已上傳完成,不該因權限/時間視為失敗。
        try:
            self.sftp.chmod(target_remote, stat.S_IMODE(local_stat.st_mode))
            self.sftp.utime(target_remote, (local_stat.st_atime, local_stat.st_mtime))
        except (OSError, IOError, AttributeError, TypeError, ValueError) as e:
            self.logger.warning(f"設定遠端 {done_name} 權限/mtime 失敗(不影響上傳內容): {e}")
        return "uploaded"

    def _build_jobs(self):
        """把 local_path / remote_path 正規化成一組 (job_sources, remote_root) 工作。

        local_path 為來源、remote_path 為目的地，對稱於下載端的三種形狀：
          remote 陣列        → 與 local 來源「逐一配對」local[i]→remote[i]（長度須相同）。
          remote 單一帶尾斜線 → 視為「共同父目錄」，各 local 來源展開到 父目錄/來源basename
                               （多專案各自上傳到自己的目的地，如 share/alarm_controller
                                → STANDARD/share/alarm_controller）。
          remote 單一無尾斜線 → 所有 local 來源「合併」上傳到同一個 remote（同 rel_path 後者覆蓋）。
        回傳 None 代表配對數量不符（已記錄錯誤）。"""
        local_paths = self.local_path if isinstance(self.local_path, list) else [self.local_path]
        remote_paths = self.remote_path if isinstance(self.remote_path, list) else [self.remote_path]
        if len(remote_paths) > 1:
            if len(remote_paths) != len(local_paths):
                self.logger.error(
                    f"上傳路徑配對數量不符：local {len(local_paths)} 個、remote {len(remote_paths)} 個"
                )
                return None
            return [([local_paths[i]], remote_paths[i]) for i in range(len(local_paths))]
        remote_root = remote_paths[0]
        if isinstance(remote_root, str) and remote_root.endswith("/") and remote_root.rstrip("/"):
            base = remote_root.rstrip("/")
            return [([lp], base + "/" + Path(lp).name) for lp in local_paths]
        return [(local_paths, remote_root)]

    def _run(self):
        self.logger.info("=== SFTP 上傳任務開始 ===")
        jobs = self._build_jobs()
        if jobs is None:
            return False

        # 連線前先驗證來源存在性：唯一來源不存在維持原行為視為失敗；多來源時略過不存在者。
        all_sources = [lp for job_sources, _ in jobs for lp in job_sources]
        for lp in all_sources:
            if not Path(lp).exists():
                if len(all_sources) == 1:
                    self.logger.error(f"來源路徑不存在: {lp}")
                    return False
                self.logger.warning(f"來源路徑不存在，略過此來源: {lp}")
        if not any(Path(lp).exists() for lp in all_sources):
            self.logger.error("所有上傳來源路徑皆不存在，任務中止")
            return False

        self._ignore_spec = self._load_ignore_spec()
        multi_job = len(jobs) > 1  # 配對或依 basename 展開時皆為多組獨立工作

        uploaded, skipped, failed = 0, 0, []
        current_local_root = None  # 中止時要把哪一份 manifest 寫回（見收尾的 finally）
        try:
            if self.wait_for_network:
                self._wait_for_network()
            # 這條連線刻意留給 run() 關閉：中間隔著收尾的 log 行與 log 上傳，讓後者能沿用
            # 同一條連線、少一次 SSH 握手。所有離開路徑都在 run() 的 finally 被關掉。
            self._connect_with_retry()

            for job_sources, remote_root in jobs:
                seen_rel = {}  # rel_path -> 來源字串，偵測同一 remote 目的地下跨來源的同名覆蓋
                for lp in job_sources:
                    source = Path(lp)
                    if not source.exists():
                        continue  # 不存在的來源已於上方記錄，直接略過
                    # 單一檔案上傳時 manifest 放在其所在目錄；目錄上傳時放在該目錄本身。
                    # 各來源各自維護自己目錄內的版本紀錄檔（rel_path 相對於各自來源根）。
                    local_root = source if source.is_dir() else source.parent
                    self._manifest = self._load_manifest(local_root) if self.resume else {}
                    self._manifest_dirty = False
                    current_local_root = local_root

                    file_list = None
                    list_attempts = 0
                    while file_list is None:
                        try:
                            file_list = self._list_local_files(source, remote_root)
                        except (paramiko.SSHException, OSError, EOFError) as e:
                            file_list = None
                            list_attempts += 1
                            self.logger.warning(diagnostic_message(
                                "LIST_RETRY",
                                f"建立遠端目錄或列出本地檔案清單發生錯誤（第 {list_attempts} 次）: {format_exception(e)}",
                                direction="upload",
                                phase="prepare_remote_and_list_local",
                                source=source,
                                remote_root=remote_root,
                                attempt=list_attempts,
                                retry_limit=(self.retry_count if self.retry_count is not None and self.retry_count > 0 else "unlimited"),
                                error=format_exception(e),
                                action="reconnect",
                            ))
                            if not self.auto_reconnect or self._retry_limit_reached(list_attempts):
                                self.logger.error("已達重試上限，任務中止")
                                return False
                            self._connect_with_retry()

                    if multi_job:
                        self.logger.info(f"{source} → {remote_root}，發現 {len(file_list)} 個檔案")
                    elif len(job_sources) > 1:
                        self.logger.info(f"來源 {source} 發現 {len(file_list)} 個檔案")
                    else:
                        self.logger.info(f"共發現 {len(file_list)} 個檔案")

                    for local_file, rel_path in file_list:
                        if rel_path in seen_rel and seen_rel[rel_path] != str(source):
                            self.logger.warning(f"多個來源都含有 {rel_path}，遠端以後面的來源為準: {source}")
                        seen_rel[rel_path] = str(source)
                        attempts = 0
                        while True:
                            try:
                                result = self._upload_one_file(local_file, rel_path, remote_root, local_root)
                                if result == "skipped":
                                    skipped += 1
                                else:
                                    uploaded += 1
                                break
                            except PermissionError as e:
                                self.logger.error(diagnostic_message(
                                    "TRANSFER_ERROR",
                                    f"上傳失敗（權限不足）: {rel_path}: {e}",
                                    direction="upload",
                                    reason="permission_denied",
                                    file=rel_path,
                                    local_file=local_file,
                                    remote_root=remote_root,
                                    error=format_exception(e),
                                    action="fail_file",
                                ))
                                failed.append(rel_path)
                                break
                            except FileNotFoundError as e:
                                self.logger.error(diagnostic_message(
                                    "TRANSFER_ERROR",
                                    f"本地檔案不存在: {rel_path}: {e}",
                                    direction="upload",
                                    reason="local_file_missing",
                                    file=rel_path,
                                    local_file=local_file,
                                    error=format_exception(e),
                                    action="fail_file",
                                ))
                                failed.append(rel_path)
                                break
                            except (paramiko.SSHException, OSError, EOFError) as e:
                                attempts += 1
                                self.logger.warning(diagnostic_message(
                                    "TRANSFER_RETRY",
                                    f"上傳 {rel_path} 發生錯誤（第 {attempts} 次）: {format_exception(e)}",
                                    direction="upload",
                                    file=rel_path,
                                    local_file=local_file,
                                    remote_root=remote_root,
                                    attempt=attempts,
                                    retry_limit=(self.retry_count if self.retry_count is not None and self.retry_count > 0 else "unlimited"),
                                    error=format_exception(e),
                                    action="reconnect",
                                ))
                                if not self.auto_reconnect or self._retry_limit_reached(attempts):
                                    reason = "auto_reconnect_disabled" if not self.auto_reconnect else "retry_limit_reached"
                                    self.logger.error(diagnostic_message(
                                        "TRANSFER_ERROR",
                                        f"檔案 {rel_path} 上傳失敗，放棄重試",
                                        direction="upload",
                                        reason=reason,
                                        file=rel_path,
                                        attempts=attempts,
                                        error=format_exception(e),
                                        action="fail_file",
                                    ))
                                    failed.append(rel_path)
                                    break
                                try:
                                    self._connect_with_retry()
                                except Exception:
                                    failed.append(rel_path)
                                    break

                    # 這個來源跑完：把累積的「略過」項目一次寫回。
                    self._flush_manifest(local_root)
        except paramiko.AuthenticationException:
            self.logger.error("=== 任務中止：帳號或密碼錯誤 ===")
            return False
        except Exception as e:
            detail = format_exception(e)
            self.logger.error(diagnostic_message(
                "RUN_ABORTED",
                "任務發生未處理錯誤",
                direction="upload",
                error=detail,
                action="abort",
            ) + f" === 任務中止：{detail} ===")
            return False
        finally:
            # 中止（含 SIGTERM 取消）時，尚未落盤的略過項目也寫回，避免下一趟白跑一次比對。
            # 正常路徑上面已經寫過，這裡因 _manifest_dirty 為 False 而不會重複寫。
            if current_local_root is not None:
                self._flush_manifest(current_local_root)

        if multi_job:
            self.logger.info(
                f"=== 上傳任務結束（{len(jobs)} 組）：成功 {uploaded}，略過 {skipped}，失敗 {len(failed)} ==="
            )
        else:
            self.logger.info(f"=== 上傳任務結束：成功 {uploaded}，略過 {skipped}，失敗 {len(failed)} ===")
        if failed:
            self.logger.info("失敗清單：" + ", ".join(failed))

        return len(failed) == 0
