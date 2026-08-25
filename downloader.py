"""SFTP 傳輸核心邏輯：連線、斷線重連、斷點續傳、Log 紀錄與回傳。

`SFTPBase` 收攏方向無關的共用邏輯（連線/重試/網路偵測/關閉/ignore/manifest/遠端建目錄/Log 上傳），
`SFTPDownloader`（下載，remote→local）與 `uploader.SFTPUploader`（上傳，local→remote）皆繼承之。
"""

import csv
import hashlib
import json
import logging
import os
import re
import socket
import stat
import time
from datetime import datetime
from pathlib import Path, PurePosixPath

import paramiko

from gitignore import GitIgnoreSpec

CHUNK_SIZE = 32768
SOCKET_TIMEOUT = 120
KEEPALIVE_INTERVAL = 15
MANIFEST_FILENAME = ".sftp_download_manifest.json"
# 下載一律先寫進「目的檔名 + 這個後綴」的暫存檔，完成後才 os.replace 換名到目的地。
# 換名換的是 inode，於是：
#   1. 目的地在任何時刻都只會是「上一版完整檔案」或「這一版完整檔案」，不會出現半截檔；
#   2. 正在執行中的 .sh 抓著舊 inode 不放，即使自己被更新也能安全跑完（bash 是邊讀邊
#      執行、按 byte offset 續讀的，就地覆寫會讓它讀到錯位的內容而語法錯誤 —— 實際發生
#      過:開機時 update_booster 更新 reboot_launcher.sh，把正在跑的自己改掉而中斷開機）。
# 暫存檔與目的檔同目錄,確保同一個檔案系統、rename 才具原子性。
PART_SUFFIX = ".part"
SFTP_RETRY_EXCEPTIONS = (
    paramiko.SSHException,
    paramiko.SFTPError,
    OSError,
    EOFError,
)

_FILENAME_UNSAFE = re.compile(r'[<>:"/\\|?*]')


class TransferCancelled(BaseException):
    """外部要求傳輸乾淨收尾（例如 systemd 的 SIGTERM）。

    刻意繼承 BaseException，而不是 Exception：下載器內部有多層「一般錯誤要記錄後
    繼續／回傳 False」的廣泛 except Exception。取消必須穿過那些攔截點，讓檔案 context
    manager 與 manifest 的 finally 先收尾，再由 CLI 以 128 + signal 回報。
    """

    def __init__(self, signum):
        super().__init__("transfer cancelled by signal {}".format(signum))
        self.signum = signum


def format_size(num_bytes):
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f}{unit}"
        size /= 1024


def format_exception(error):
    """保留例外類型與 repr；即使 socket.timeout 沒有訊息，Log 仍可辨識原因。"""
    return f"{type(error).__name__}: {error!r}"


class _CSVFileHandler(logging.Handler):
    """把 Log 寫成 CSV，方便日後把上百台裝置的 Log 彙整成同一份表格用 Excel 檢視。"""

    def __init__(self, filename, device_name, version_info=""):
        super().__init__()
        self._device_name = device_name
        self._version_info = version_info
        # utf-8-sig：讓 Excel 開啟時能正確辨識 UTF-8 中文，不會顯示成亂碼。
        self._file = open(filename, "w", newline="", encoding="utf-8-sig")
        self._writer = csv.writer(self._file)
        self._writer.writerow(["timestamp", "device_name", "version_info", "level", "message"])
        self._file.flush()

    def emit(self, record):
        try:
            timestamp = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S")
            self._writer.writerow(
                [timestamp, self._device_name, self._version_info, record.levelname, record.getMessage()]
            )
            self._file.flush()
        except Exception:
            self.handleError(record)

    def close(self):
        try:
            self._file.close()
        except Exception:
            pass
        super().close()


def create_logger(log_dir, device_name, version_info="", log_callback=None, mode="download"):
    """device_name 用於標示這份 Log 屬於哪一台設備/使用者（多台 edge device 共用同一 SFTP 帳號時仍可分辨）。
    version_info 為選填的上傳版號資訊，會一併記錄在 Log 中，不影響任何傳輸邏輯。
    mode 決定檔名前綴：download → D_、upload → U_，以便一眼分辨傳輸方向。"""
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    safe_device_name = _FILENAME_UNSAFE.sub("_", device_name).strip() or "unknown"
    prefix = "U_" if mode == "upload" else "D_"
    log_file = log_dir / f"{prefix}{safe_device_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

    logger = logging.getLogger(f"sftp_transfer.{id(log_file)}")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    version_tag = f"[{version_info}]" if version_info else ""
    fmt = logging.Formatter(
        f"%(asctime)s [%(levelname)s] [{device_name}]{version_tag} %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    csv_handler = _CSVFileHandler(log_file, device_name, version_info)
    logger.addHandler(csv_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)

    if log_callback:
        class CallbackHandler(logging.Handler):
            def emit(self, record):
                log_callback(self.format(record))

        callback_handler = CallbackHandler()
        callback_handler.setFormatter(fmt)
        logger.addHandler(callback_handler)

    return logger, log_file


class SFTPBase:
    """下載與上傳共用的基底：連線、斷線重連、網路偵測、ignore 規則、版本紀錄檔（manifest）、
    本地端內容雜湊、遠端建目錄與 Log 上傳等方向無關的邏輯。

    子類別以 `manifest_filename` 類別屬性指定各自的版本紀錄檔名，避免同一目錄同時被下載與上傳
    使用時互相覆蓋。"""

    manifest_filename = MANIFEST_FILENAME

    def __init__(
        self,
        host,
        port,
        username,
        remote_path,
        local_path,
        password=None,
        key_file=None,
        auto_reconnect=True,
        resume=True,
        wait_for_network=True,
        recursive=True,
        ignore_file=None,
        retry_count=None,
        retry_delay=10,
        upload_log=False,
        remote_log_dir=None,
        duplicate_mode="overwrite",
        duplicate_suffix="copy",
        logger=None,
        log_file=None,
    ):
        self.host = host
        self.port = port
        self.username = username
        # 下載時為來源、上傳時為目的地。下載可傳入單一字串或路徑陣列（陣列時各來源合併到同一個
        # local_path，適合把「標準路徑 + 各船專屬路徑」合併成一個完整專案）；上傳僅使用單一目的地路徑。
        self.remote_path = remote_path
        # 下載時為儲存目的地、上傳時為來源。
        self.local_path = local_path
        self.password = password
        self.key_file = key_file
        self.auto_reconnect = auto_reconnect
        self.resume = resume
        self.wait_for_network = wait_for_network
        self.recursive = recursive  # True：處理所有子資料夾（多層）；False：只處理該路徑下的檔案（單層）
        self.ignore_file = ignore_file  # 忽略設定檔路徑（格式同 .gitignore），None 或檔案不存在代表無需忽略
        self.retry_count = retry_count  # None 或 <= 0 代表無限次重試
        self.retry_delay = retry_delay
        self.upload_log = upload_log
        self.remote_log_dir = remote_log_dir
        self.duplicate_mode = duplicate_mode or "overwrite"  # "duplicate"（另存新檔）或 "overwrite"（直接覆蓋，預設）
        self.duplicate_suffix = duplicate_suffix or "copy"
        self.logger = logger
        self.log_file = log_file

        self.client = None
        self.sftp = None
        self._manifest = {}
        self._ignore_spec = None

    def _retry_limit_reached(self, attempts):
        if self.retry_count is None or self.retry_count <= 0:
            return False
        return attempts > self.retry_count

    def _connect(self):
        self.logger.info(f"正在連線至 {self.host}:{self.port} ...")
        # 每次建立新連線前先清掉舊的 SFTP channel / SSH transport，避免斷線
        # 重連時殘留半開連線，累積占用本機與伺服器端資源。
        self._close()
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        connect_kwargs = dict(hostname=self.host, port=self.port, username=self.username, timeout=15)
        if self.key_file:
            connect_kwargs["key_filename"] = self.key_file
        else:
            connect_kwargs["password"] = self.password
        try:
            client.connect(**connect_kwargs)
            sftp = client.open_sftp()
            # 若無此逾時設定，連線在傳輸中途「無聲斷線」（如網路線拔掉、Wi-Fi 斷線）時，
            # 讀寫呼叫會永遠卡住不會丟出例外，導致斷線重連機制永遠不會被觸發。
            sftp.get_channel().settimeout(SOCKET_TIMEOUT)
            client.get_transport().set_keepalive(KEEPALIVE_INTERVAL)
        except Exception:
            # 連線或 SFTP subsystem 初始化到一半失敗時，client 尚未掛到
            # self.client，需在此主動關閉，否則 _close() 無法清掉。
            try:
                client.close()
            except Exception:
                pass
            raise
        self.client = client
        self.sftp = sftp
        self.logger.info("連線成功")

    def _connect_with_retry(self):
        attempts = 0
        while True:
            try:
                self._connect()
                return
            except paramiko.AuthenticationException:
                self.logger.error("連線失敗：帳號或密碼錯誤")
                raise
            except SFTP_RETRY_EXCEPTIONS as e:
                attempts += 1
                self.logger.warning(f"連線失敗（第 {attempts} 次）：{format_exception(e)}")
                if not self.auto_reconnect or self._retry_limit_reached(attempts):
                    self.logger.error("已達重試上限，放棄連線")
                    raise
                if self.wait_for_network:
                    self._wait_for_network()
                time.sleep(self.retry_delay)

    def _wait_for_network(self):
        self.logger.info("正在偵測網路連線狀態...")
        while True:
            try:
                with socket.create_connection((self.host, self.port), timeout=5):
                    self.logger.info("網路連線已恢復")
                    return
            except OSError:
                self.logger.warning(f"無法連線至 {self.host}:{self.port}，{self.retry_delay} 秒後重試...")
                time.sleep(self.retry_delay)

    def _close(self):
        try:
            if self.sftp:
                self.sftp.close()
        except Exception:
            pass
        finally:
            self.sftp = None
        try:
            if self.client:
                self.client.close()
        except Exception:
            pass
        finally:
            self.client = None

    def _load_ignore_spec(self):
        """讀取「忽略設定檔」（格式同 .gitignore）。未設定或檔案不存在代表無需忽略；
        格式錯誤的規則逐行略過並記錄警告，其餘正確的規則仍照常生效。"""
        if not self.ignore_file:
            return None
        path = Path(self.ignore_file)
        if not path.exists():
            self.logger.info(f"忽略設定檔不存在，不忽略任何檔案: {path}")
            return None
        try:
            # utf-8-sig：Windows 記事本以 UTF-8 存檔時常會加上 BOM，若不去除，
            # BOM 會黏在第一行規則前面，導致第一條規則永遠比對不到。
            lines = path.read_text(encoding="utf-8-sig").splitlines()
        except (OSError, UnicodeDecodeError) as e:
            self.logger.warning(f"忽略設定檔讀取失敗，不忽略任何檔案: {e}")
            return None
        valid_lines = []
        for lineno, line in enumerate(lines, 1):
            try:
                GitIgnoreSpec.from_lines([line])
                valid_lines.append(line)
            except ValueError:
                self.logger.warning(f"忽略設定檔第 {lineno} 行格式錯誤，已略過此規則: {line!r}")
        self.logger.info(f"已載入忽略設定檔: {path}")
        return GitIgnoreSpec.from_lines(valid_lines)

    def _is_ignored(self, rel_path):
        """rel_path 為相對於傳輸根目錄的路徑；資料夾請加上結尾的 /（gitignore 的資料夾規則才會匹配）。"""
        return self._ignore_spec is not None and self._ignore_spec.match_file(rel_path)

    def _manifest_path(self, local_root):
        return local_root / self.manifest_filename

    def _load_manifest(self, local_root):
        path = self._manifest_path(local_root)
        if not path.exists():
            return {}
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            self.logger.warning(f"版本紀錄檔讀取失敗，將視為未追蹤過任何檔案: {e}")
            return {}

    def _save_manifest(self, local_root):
        try:
            with open(self._manifest_path(local_root), "w", encoding="utf-8") as f:
                json.dump(self._manifest, f, ensure_ascii=False, indent=2)
        except OSError as e:
            self.logger.warning(f"版本紀錄檔寫入失敗: {e}")

    def _hash_local_file(self, local_file):
        """計算本地端檔案目前內容的 SHA-256（只讀本機磁碟，不牽涉網路），
        回傳 hashlib 雜湊物件，方便驗證後可直接沿用繼續累加後續新傳輸的內容。"""
        local_hash = hashlib.sha256()
        with open(local_file, "rb") as local_f:
            while True:
                chunk = local_f.read(CHUNK_SIZE)
                if not chunk:
                    break
                local_hash.update(chunk)
        return local_hash

    def _ensure_remote_dir(self, remote_dir):
        """從根目錄逐層確認/建立遠端目錄（等同 mkdir -p），已存在的層級略過。

        佔位符展開後的每船/每機目錄（如 /fleet/.../WH289/IPC-1/sftp_logs）
        伺服器上通常尚未存在，直接 put 會失敗。"""
        parts = [p for p in remote_dir.split("/") if p]
        current = "/" if remote_dir.startswith("/") else ""
        for part in parts:
            current = current.rstrip("/") + "/" + part if current else part
            try:
                self.sftp.stat(current)
            except FileNotFoundError:
                self.sftp.mkdir(current)

    def _upload_log_file(self):
        try:
            self.logger.info("正在上傳 Log 檔至 SFTP...")
            for handler in self.logger.handlers:
                handler.flush()
            self._connect_with_retry()
            self._ensure_remote_dir(self.remote_log_dir)
            remote_name = self.remote_log_dir.rstrip("/") + "/" + Path(self.log_file).name
            self.sftp.put(str(self.log_file), remote_name)
            self.logger.info(f"Log 上傳完成: {remote_name}")
        except Exception as e:
            self.logger.error(f"Log 上傳失敗: {format_exception(e)}")
        finally:
            self._close()

    def run(self):
        """統一進入點：呼叫子類別的 _run() 執行實際傳輸。

        傳輸成功或一般失敗（帳密錯誤、達重試上限、未預期例外）時，最後都會在 upload_log
        開啟時把 log 上傳回 remote。SIGTERM 取消則只 flush 本地 log、略過遠端 upload，避免
        收尾又進入可能無限等待的網路路徑。log 上傳本身的錯誤已在 _upload_log_file 內部吞掉，
        不影響回傳值。"""
        cancelled = False
        try:
            return self._run()
        except TransferCancelled:
            cancelled = True
            self.logger.warning("=== 收到終止要求：已保存本地續傳進度，停止本次傳輸 ===")
            # CSV handler 每行本來就 flush；這裡再做一次，明確保證 SIGTERM 返回前本地
            # 記錄已落盤。遠端 log upload 需要重新連線，取消時不能再掉入無限重試。
            for handler in self.logger.handlers:
                try:
                    handler.flush()
                except Exception:
                    pass
            raise
        finally:
            if self.upload_log and not cancelled:
                self._upload_log_file()


class SFTPDownloader(SFTPBase):
    """SFTP 下載（remote → local）：遞迴走訪遠端目錄、斷點續傳、忽略規則與版本紀錄。"""

    manifest_filename = MANIFEST_FILENAME

    def _list_remote_files(self, remote_root, local_root):
        files = []
        root_stat = self.sftp.stat(remote_root)
        if not stat.S_ISDIR(root_stat.st_mode):
            filename = os.path.basename(remote_root.rstrip("/"))
            if self._is_ignored(filename):
                self.logger.info(f"依忽略設定檔略過: {filename}")
            else:
                files.append((remote_root, filename))
        elif self.recursive:
            self._walk_remote_dir(remote_root, "", files, local_root)
        else:
            skipped_dirs = []
            for entry in self.sftp.listdir_attr(remote_root):
                if stat.S_ISDIR(entry.st_mode):
                    skipped_dirs.append(entry.filename)
                elif self._is_ignored(entry.filename):
                    self.logger.info(f"依忽略設定檔略過: {entry.filename}")
                else:
                    remote_path = remote_root.rstrip("/") + "/" + entry.filename
                    files.append((remote_path, entry.filename))
            if skipped_dirs:
                self.logger.info(f"僅下載單層（未啟用多層），略過 {len(skipped_dirs)} 個子資料夾: {', '.join(skipped_dirs)}")
        return files

    def _walk_remote_dir(self, remote_dir, rel_dir, files, local_root):
        # 即使子資料夾底下沒有任何檔案，也要在本地端建立對應的空資料夾，
        # 否則單純比對「有沒有檔案」永遠不會觸發 mkdir，空資料夾就不會被下載下來。
        local_dir = local_root / Path(*rel_dir.split("/")) if rel_dir else local_root
        local_dir.mkdir(parents=True, exist_ok=True)
        for entry in self.sftp.listdir_attr(remote_dir):
            remote_path = remote_dir.rstrip("/") + "/" + entry.filename
            rel_path = f"{rel_dir}/{entry.filename}" if rel_dir else entry.filename
            if stat.S_ISDIR(entry.st_mode):
                # 被忽略的資料夾整棵略過、不往下走訪，本地端也不會建立對應資料夾（與 git 行為一致）。
                if self._is_ignored(rel_path + "/"):
                    self.logger.info(f"依忽略設定檔略過資料夾: {rel_path}/")
                    continue
                self._walk_remote_dir(remote_path, rel_path, files, local_root)
            elif self._is_ignored(rel_path):
                self.logger.info(f"依忽略設定檔略過: {rel_path}")
            else:
                files.append((remote_path, rel_path))

    def _next_duplicate_path(self, local_file):
        candidate = local_file.with_name(f"{local_file.stem}_{self.duplicate_suffix}{local_file.suffix}")
        n = 1
        while candidate.exists():
            candidate = local_file.with_name(f"{local_file.stem}_{self.duplicate_suffix}{n}{local_file.suffix}")
            n += 1
        return candidate

    def _download_one_file(self, remote_file, rel_path, local_root):
        local_file = local_root / Path(*rel_path.split("/"))
        local_file.parent.mkdir(parents=True, exist_ok=True)
        remote_stat = self.sftp.stat(remote_file)
        remote_size = remote_stat.st_size
        remote_mtime = int(remote_stat.st_mtime)

        target_file = local_file
        local_size = 0
        mode = "wb"
        running_hash = hashlib.sha256()  # 邊下載邊累加，最後（或中斷當下）存進版本紀錄檔
        known = self._manifest.get(rel_path)

        if local_file.exists():
            if not self.resume:
                # 斷點續傳未啟用：不判斷是否未變更、也不接續，一律整份重新下載；
                # 但存到哪個檔名仍然要依 duplicate_mode 決定，這一步跟斷點續傳是否啟用無關。
                if self.duplicate_mode == "overwrite":
                    self.logger.info(f"重新下載並覆蓋舊檔案: {rel_path}")
                else:
                    target_file = self._next_duplicate_path(local_file)
                    self.logger.info(f"重新下載，另存為: {target_file.name}")
            else:
                disk_size = local_file.stat().st_size

                if disk_size == remote_size:
                    # 大小相同：用版本紀錄（若有）判斷是否真的未變更；沒有紀錄則姑且視為未變更略過。
                    # 這裡不逐一雜湊比對整個檔案內容，避免每次執行都要重新讀取所有已下載完成的檔案。
                    if known is None or (known.get("size") == remote_size and known.get("mtime") == remote_mtime):
                        self.logger.info(f"略過（已完整下載）: {rel_path}")
                        self._manifest[rel_path] = {"size": remote_size, "mtime": remote_mtime}
                        self._save_manifest(local_root)
                        return "skipped"
                    if self.duplicate_mode == "overwrite":
                        self.logger.info(f"偵測到來源檔案已更新，覆蓋舊檔案: {rel_path}")
                    else:
                        target_file = self._next_duplicate_path(local_file)
                        self.logger.info(f"偵測到來源檔案已更新，另存為: {target_file.name}")
                elif disk_size > remote_size:
                    if self.duplicate_mode == "overwrite":
                        self.logger.warning(f"本地檔案大於遠端檔案，重新下載: {rel_path}")
                    else:
                        target_file = self._next_duplicate_path(local_file)
                        self.logger.warning(f"本地檔案大於遠端檔案，另存為: {target_file.name}")
                elif self.duplicate_mode == "duplicate":
                    # 「另存新檔」模式一律整份重新下載、不接續舊檔案，斷點續傳形同停用，不需要驗證內容。
                    target_file = self._next_duplicate_path(local_file)
                    self.logger.info(f"重新下載，另存為: {target_file.name}")
                else:
                    # 走到這裡 duplicate_mode 必定是 "overwrite"："duplicate" 模式在上面
                    # 的 elif 分支就已經攔截、一律整份重新下載成新檔案，不會執行到這裡。
                    # 目的地永遠只會是「某一版的完整檔案」（沒下載完的內容都留在 .part 暫存檔，
                    # 見 PART_SUFFIX），所以本地比遠端小只代表來源長大了 → 整份重新下載。
                    self.logger.info(f"偵測到來源檔案已更新，覆蓋舊檔案: {rel_path}")

        # 實際寫入的是暫存檔，成功後才原子換名到 target_file。斷點續傳接續的對象因此也是
        # 暫存檔而不是目的地；duplicate 模式另存新檔、本來就不接續，所以只有「目的地就是
        # 原檔名」時才嘗試接續。
        part_file = target_file.with_name(target_file.name + PART_SUFFIX)
        if self.resume and target_file == local_file and part_file.exists():
            part_size = part_file.stat().st_size
            # 遠端版本要與紀錄一致，且紀錄的長度/雜湊要對得上暫存檔的現況，才敢接著往下寫。
            # 用「本地端雜湊」確認這段尚未下載完的內容有沒有被外部更動過（例如被人手動修改）。
            # 這裡刻意只讀本機磁碟跟紀錄檔裡存的雜湊比對，不會為了驗證而重新從遠端讀取已下載
            # 的內容，避免已下載比例越高、驗證反而越花時間、越像卡住的問題。
            resumable = (
                known is not None
                and known.get("size") == remote_size
                and known.get("mtime") == remote_mtime
                and known.get("local_bytes") == part_size
                and known.get("local_sha256")
                and part_size < remote_size
            )
            if resumable:
                disk_hash = self._hash_local_file(part_file)
                if disk_hash.hexdigest() == known["local_sha256"]:
                    self.logger.info(f"本地端內容雜湊比對相符，接續下載: {rel_path}")
                    local_size = part_size
                    running_hash = disk_hash  # 直接沿用，後續新下載的內容繼續累加上去
                    mode = "ab"
            if mode == "wb":
                # 暫存檔對不上紀錄（來源已換版、內容被動過或根本沒有檢查點）→ 不可信，
                # 整份重新下載；"wb" 開檔即截斷，不必另外刪除。
                self.logger.info(f"既有暫存檔無法接續，整份重新下載: {rel_path}")

        self.logger.info(f"開始下載: {rel_path} ({format_size(remote_size)})")
        last_pct_logged = -1
        last_checkpoint_pct = -1
        transferred = local_size
        start_time = time.time()
        # 記住上次印進度的時間與位元組數，用差值算「這段期間的即時速率」，比整體平均更能反映當下網速。
        last_log_time = start_time
        last_log_bytes = transferred
        cancelled = False
        try:
            with self.sftp.open(remote_file, "rb") as remote_f:
                remote_f.seek(local_size)
                with open(part_file, mode) as local_f:
                    while True:
                        chunk = remote_f.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        local_f.write(chunk)
                        running_hash.update(chunk)
                        transferred += len(chunk)
                        if remote_size > 0:
                            pct = int(transferred / remote_size * 100)
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
                            # 每跨過 10% 進度就存一次檢查點，而不是每個 chunk 都寫檔，
                            # 避免大檔案下載時頻繁寫入版本紀錄檔造成不必要的效能負擔。
                            if self.resume and pct >= last_checkpoint_pct + 10:
                                local_f.flush()
                                self._manifest[rel_path] = {
                                    "size": remote_size,
                                    "mtime": remote_mtime,
                                    "local_sha256": running_hash.hexdigest(),
                                    "local_bytes": transferred,
                                }
                                self._save_manifest(local_root)
                                last_checkpoint_pct = pct
        except TransferCancelled:
            cancelled = True
            # Python signal 可能恰好落在 local_f.write() 已完成、running_hash / transferred
            # 尚未更新的兩個 bytecode 之間。此時只相信記憶體 counter 會少記一個 chunk。
            # with 已先關閉並 flush 本地檔案，所以取消時從磁碟重算一次，讓 manifest 與
            # 實際 .part 精確一致；正常傳輸沒有這筆額外成本。
            if self.resume and part_file.exists():
                transferred = part_file.stat().st_size
                running_hash = self._hash_local_file(part_file)
            raise
        finally:
            # 不論成功、失敗或中途被中斷，都存下目前實際寫到的位置與雜湊，讓下次重試時
            # 能正確判斷「這是同一版本尚未下載完的部分」，而不是每次中斷後都只能整份重來。
            if self.resume:
                self._manifest[rel_path] = {
                    "size": remote_size,
                    "mtime": remote_mtime,
                    "local_sha256": running_hash.hexdigest(),
                    "local_bytes": transferred,
                }
                self._save_manifest(local_root)
                if cancelled:
                    self.logger.warning(
                        f"取消 checkpoint: {rel_path} offset={transferred}/{remote_size}, "
                        f"mtime={remote_mtime}, sha256={running_hash.hexdigest()}"
                    )

        total_elapsed = time.time() - start_time
        downloaded_bytes = transferred - local_size  # 本次實際下載的位元組（不含斷點續傳前已存在的部分）
        done_name = target_file.name if target_file != local_file else rel_path
        if total_elapsed > 0 and downloaded_bytes > 0:
            avg_speed = downloaded_bytes / total_elapsed
            self.logger.info(f"完成下載: {done_name}（平均 {format_size(avg_speed)}/s）")
        else:
            self.logger.info(f"完成下載: {done_name}")
        # 保留來源權限與 mtime:SFTP/paramiko 預設不會搬,需以 remote_stat 自行鏡射
        # （否則 .sh 等會掉 +x）。趁還是暫存檔時就套用,換名之後目的地第一眼就是對的權限,
        # 不會有「檔案已經在了但還沒 +x」的空窗。
        # 失敗只警告不中斷 —— 內容已下載完成,不該因權限/時間視為失敗。
        try:
            os.chmod(part_file, stat.S_IMODE(remote_stat.st_mode))
            atime = getattr(remote_stat, "st_atime", None)
            os.utime(part_file, (atime if atime is not None else remote_stat.st_mtime,
                                 remote_stat.st_mtime))
        except (OSError, AttributeError, TypeError, ValueError) as e:
            self.logger.warning(f"設定 {done_name} 權限/mtime 失敗(不影響下載內容): {e}")
        # 原子換名:同一個檔案系統上的 rename,對讀者而言目的地只會是「換名前的舊版完整檔案」
        # 或「換名後的新版完整檔案」,不存在中間狀態,也不會就地改寫舊檔的 inode。
        os.replace(part_file, target_file)
        return "downloaded"

    def _build_jobs(self):
        """把 remote_path / local_path 正規化成一組 (job_sources, local_root) 工作。

        remote_path 為來源、local_path 為目的地，三種形狀：
          local 陣列        → 與 remote 來源「逐一配對」remote[i]→local[i]（長度須相同）。
          local 單一帶尾斜線 → 視為「共同父目錄」，各 remote 來源展開到 父目錄/來源basename
                               （多專案各自落在自己的目錄，如 STANDARD/share/alarm_controller
                                → share/alarm_controller）。
          local 單一無尾斜線 → 所有 remote 來源「合併」到同一個 local（STANDARD + 各船 UNIQUE
                               疊加成完整專案，相同相對路徑以後面的來源為準）。
        回傳 None 代表配對數量不符（已記錄錯誤）。"""
        remote_paths = self.remote_path if isinstance(self.remote_path, list) else [self.remote_path]
        local = self.local_path
        if isinstance(local, list):
            if len(local) != len(remote_paths):
                self.logger.error(
                    f"下載路徑配對數量不符：remote {len(remote_paths)} 個、local {len(local)} 個"
                )
                return None
            return [([remote_paths[i]], Path(local[i])) for i in range(len(remote_paths))]
        if isinstance(local, str) and local.endswith("/") and local.rstrip("/"):
            parent = Path(local)
            return [([r], parent / PurePosixPath(r.rstrip("/")).name) for r in remote_paths]
        return [(remote_paths, Path(local))]

    def _run(self):
        self.logger.info("=== SFTP 下載任務開始 ===")
        jobs = self._build_jobs()
        if jobs is None:
            return False
        self._ignore_spec = self._load_ignore_spec()
        multi_job = len(jobs) > 1  # 配對或依 basename 展開時皆為多組獨立工作

        downloaded, skipped, failed = 0, 0, []
        try:
            if self.wait_for_network:
                self._wait_for_network()
            self._connect_with_retry()

            for job_sources, local_root in jobs:
                local_root.mkdir(parents=True, exist_ok=True)
                # 配對模式各目的地各自維護版本紀錄檔；合併模式共用單一 local 的紀錄檔。
                self._manifest = self._load_manifest(local_root) if self.resume else {}

                file_list = None
                list_attempts = 0
                while file_list is None:
                    current_root = None
                    try:
                        file_list = []
                        for current_root in job_sources:
                            try:
                                file_list.extend(self._list_remote_files(current_root, local_root))
                            except FileNotFoundError:
                                # 單一來源路徑不存在（常見於各船專屬路徑並非每船都有）時，只記警告並略過此來源，
                                # 其餘存在的來源照常下載。FileNotFoundError 為 OSError 子類，需在此個別攔截，
                                # 才不會被外層的網路錯誤分支當成連線問題而觸發重連。
                                self.logger.warning(f"遠端路徑不存在，略過此來源: {current_root}")
                    except SFTP_RETRY_EXCEPTIONS as e:
                        file_list = None
                        list_attempts += 1
                        self.logger.warning(
                            f"列出遠端檔案清單發生錯誤（第 {list_attempts} 次）: {format_exception(e)}"
                        )
                        if not self.auto_reconnect or self._retry_limit_reached(list_attempts):
                            self.logger.error("已達重試上限，任務中止")
                            return False
                        self._connect_with_retry()

                # 同一 job 內多來源合併時，若不同來源含有相同的相對路徑，後面的來源會覆蓋前面的
                # （版本紀錄也以後者為準），僅保留最後一筆並記錄警告。
                deduped = {}
                for remote_file, rel_path in file_list:
                    if rel_path in deduped and deduped[rel_path] != remote_file:
                        self.logger.warning(f"多個來源路徑都含有 {rel_path}，以後面的來源為準: {remote_file}")
                    deduped[rel_path] = remote_file
                file_list = [(remote_file, rel_path) for rel_path, remote_file in deduped.items()]

                if multi_job:
                    self.logger.info(f"{job_sources[0]} → {local_root}，發現 {len(file_list)} 個檔案")
                elif len(job_sources) > 1:
                    self.logger.info(f"共 {len(job_sources)} 個來源路徑，合併後發現 {len(file_list)} 個檔案")
                else:
                    self.logger.info(f"共發現 {len(file_list)} 個檔案")

                for remote_file, rel_path in file_list:
                    attempts = 0
                    while True:
                        try:
                            result = self._download_one_file(remote_file, rel_path, local_root)
                            if result == "skipped":
                                skipped += 1
                            else:
                                downloaded += 1
                            break
                        except PermissionError as e:
                            self.logger.error(f"寫入失敗（權限不足）: {rel_path}: {e}")
                            failed.append(rel_path)
                            break
                        except FileNotFoundError as e:
                            self.logger.error(f"檔案不存在: {rel_path}: {e}")
                            failed.append(rel_path)
                            break
                        except SFTP_RETRY_EXCEPTIONS as e:
                            attempts += 1
                            self.logger.warning(
                                f"下載 {rel_path} 發生錯誤（第 {attempts} 次）: {format_exception(e)}"
                            )
                            if not self.auto_reconnect or self._retry_limit_reached(attempts):
                                self.logger.error(f"檔案 {rel_path} 下載失敗，放棄重試")
                                failed.append(rel_path)
                                break
                            try:
                                self._connect_with_retry()
                            except Exception:
                                failed.append(rel_path)
                                break
        except paramiko.AuthenticationException:
            self.logger.error("=== 任務中止：帳號或密碼錯誤 ===")
            return False
        except Exception as e:
            self.logger.error(f"=== 任務中止：{format_exception(e)} ===")
            return False
        finally:
            self._close()

        if multi_job:
            self.logger.info(
                f"=== 下載任務結束（{len(jobs)} 組）：成功 {downloaded}，略過 {skipped}，失敗 {len(failed)} ==="
            )
        else:
            self.logger.info(f"=== 下載任務結束：成功 {downloaded}，略過 {skipped}，失敗 {len(failed)} ===")
        if failed:
            self.logger.info("失敗清單：" + ", ".join(failed))

        return len(failed) == 0
