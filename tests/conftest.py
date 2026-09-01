"""pytest 共用 fixtures：假 SFTP client（不碰真實網路）、靜音 logger、封鎖 time.sleep。"""

import logging
import stat as stat_module
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import downloader as downloader_module  # noqa: E402
import uploader as uploader_module  # noqa: E402


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    """所有測試都封鎖 time.sleep，避免重試/等待邏輯的測試真的卡住等待。"""
    monkeypatch.setattr(downloader_module.time, "sleep", lambda seconds: None)


@pytest.fixture
def logger():
    """一般 logging.Logger，交給 pytest 內建的 caplog fixture 擷取訊息斷言用。"""
    lg = logging.getLogger("sftp_transfer_test")
    lg.setLevel(logging.DEBUG)
    lg.propagate = True
    lg.handlers.clear()
    return lg


class FakeSFTPAttr:
    """模擬 paramiko.SFTPAttributes。"""

    def __init__(self, filename, is_dir, size=0, mtime=0, atime=None, mode=None):
        self.filename = filename
        # 含權限位元(不只格式位元),讓 stat.S_IMODE 取得到真實權限(對應保留權限功能)。
        # mode=None 用預設值(檔 0644 / 目錄 0755);權限對齊的測試靠 FakeSFTPClient 的
        # modes 指定個別檔案的權限。
        if mode is None:
            mode = 0o755 if is_dir else 0o644
        self.st_mode = (stat_module.S_IFDIR if is_dir else stat_module.S_IFREG) | mode
        self.st_size = size
        self.st_mtime = mtime
        self.st_atime = atime if atime is not None else mtime


class FakeSFTPFile:
    """模擬 paramiko 開啟遠端檔案回傳的檔案物件，支援讀取（下載）與寫入（上傳）。

    寫入模式（"w"/"a"）會即時把緩衝內容寫回 client.files，讓上傳過程中的斷點續傳測試
    可以觀察到「已上傳到一半」的遠端狀態。"""

    def __init__(self, client, path, mode="rb"):
        self.client = client
        self.path = path
        self.mode = mode
        if "a" in mode:
            self.buffer = bytearray(client.files.get(path, b""))
            self.pos = len(self.buffer)
        elif "w" in mode:
            self.buffer = bytearray()
            client.files[path] = b""  # 開檔即截斷，對應覆蓋上傳
            self.pos = 0
        else:
            self.buffer = bytearray(client.files.get(path, b""))
            self.pos = 0

    def seek(self, pos):
        self.pos = pos

    def read(self, n=-1):
        data = bytes(self.buffer)
        chunk = data[self.pos:] if n is None or n < 0 else data[self.pos:self.pos + n]
        self.pos += len(chunk)
        return chunk

    def write(self, data):
        self.buffer[self.pos:self.pos + len(data)] = data
        self.pos += len(data)
        self._flush_to_client()

    def flush(self):
        self._flush_to_client()

    def _flush_to_client(self):
        if "w" in self.mode or "a" in self.mode:
            self.client.files[self.path] = bytes(self.buffer)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self._flush_to_client()
        return False


class FakeSFTPClient:
    """輕量假 SFTP client：用一個 {遠端路徑: bytes} 的字典模擬檔案樹，資料夾由路徑前綴自動推導。
    用於不需要真正連線的邏輯測試（列表、下載決策、版本比對等）。"""

    def __init__(self, files=None, mtimes=None, modes=None):
        self.files = dict(files or {})
        self.mtimes = dict(mtimes or {})
        self.modes = dict(modes or {})   # {遠端路徑: 權限位元},未列出的檔用 FakeSFTPAttr 預設
        self.put_calls = []
        self.dirs = set()
        self.mkdir_calls = []
        self.chmod_calls = []   # 記錄 (path, mode),供保留權限相關測試檢查
        self.truncate_calls = []  # 記錄 (path, size),供續傳切回檢查點的測試檢查
        self.utime_calls = []   # 記錄 (path, (atime, mtime))

    def stat(self, path):
        path = path.rstrip("/")
        if path in self.files:
            return FakeSFTPAttr(path, is_dir=False, size=len(self.files[path]),
                                mtime=self.mtimes.get(path, 0), mode=self.modes.get(path))
        prefix = path + "/"
        if path == "" or path in self.dirs or any(p.startswith(prefix) for p in self.files):
            return FakeSFTPAttr(path, is_dir=True)
        raise FileNotFoundError(f"No such file: {path}")

    def mkdir(self, path):
        self.dirs.add(path.rstrip("/"))
        self.mkdir_calls.append(path)

    def listdir_attr(self, path):
        prefix = path.rstrip("/") + "/"
        seen_dirs = set()
        results = []
        for full_path, data in self.files.items():
            if not full_path.startswith(prefix):
                continue
            rest = full_path[len(prefix):]
            if "/" in rest:
                dirname = rest.split("/")[0]
                if dirname not in seen_dirs:
                    seen_dirs.add(dirname)
                    results.append(FakeSFTPAttr(dirname, is_dir=True))
            else:
                results.append(FakeSFTPAttr(rest, is_dir=False, size=len(data),
                                            mtime=self.mtimes.get(full_path, 0),
                                            mode=self.modes.get(full_path)))
        return results

    def open(self, path, mode="rb"):
        return FakeSFTPFile(self, path.rstrip("/"), mode)

    def put(self, local_path, remote_path):
        with open(local_path, "rb") as f:
            data = f.read()
        self.files[remote_path] = data
        self.put_calls.append((local_path, remote_path))

    def truncate(self, path, size):
        """對應 paramiko.SFTPClient.truncate：把遠端檔案裁到指定長度（續傳切回檢查點用）。"""
        path = path.rstrip("/")
        if path not in self.files:
            raise FileNotFoundError(f"No such file: {path}")
        self.truncate_calls.append((path, size))
        data = self.files[path]
        self.files[path] = data[:size] + b"\0" * max(0, size - len(data))

    def chmod(self, path, mode):
        path = path.rstrip("/")
        self.chmod_calls.append((path, mode))
        self.modes[path] = mode         # 之後的 stat 要看得到新權限(權限對齊的收斂測試)

    def utime(self, path, times):
        self.utime_calls.append((path.rstrip("/"), times))


@pytest.fixture
def fake_sftp_factory():
    """回傳一個可建立 FakeSFTPClient 的工廠函式，讓測試自訂檔案樹內容。"""
    def _make(files=None, mtimes=None, modes=None):
        return FakeSFTPClient(files=files, mtimes=mtimes, modes=modes)
    return _make


def make_downloader(tmp_path, logger, **overrides):
    """建立一個不會真的連線的 SFTPDownloader，供測試直接操作內部方法。"""
    kwargs = dict(
        host="host.example.com",
        port=22,
        username="user",
        remote_path="/remote",
        local_path=str(tmp_path),
        logger=logger,
    )
    kwargs.update(overrides)
    return downloader_module.SFTPDownloader(**kwargs)


@pytest.fixture
def downloader_factory(tmp_path, logger):
    def _make(**overrides):
        return make_downloader(tmp_path, logger, **overrides)
    return _make


def make_uploader(tmp_path, logger, **overrides):
    """建立一個不會真的連線的 SFTPUploader，供測試直接操作內部方法。
    預設以 tmp_path 為本地來源、/remote 為遠端目的地。"""
    kwargs = dict(
        host="host.example.com",
        port=22,
        username="user",
        remote_path="/remote",
        local_path=str(tmp_path),
        logger=logger,
    )
    kwargs.update(overrides)
    return uploader_module.SFTPUploader(**kwargs)


@pytest.fixture
def uploader_factory(tmp_path, logger):
    def _make(**overrides):
        return make_uploader(tmp_path, logger, **overrides)
    return _make
