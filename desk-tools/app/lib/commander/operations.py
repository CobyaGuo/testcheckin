# -*- coding: utf-8 -*-
"""文件操作：复制 / 移动 / 删除（回收站）/ 重命名 / 新建，全部本地执行。

大任务在后台线程执行，通过信号回报进度；UI 弹进度框可取消。
删除走 Windows 回收站（SHFileOperation），可恢复。
"""
import os
import shutil
import stat as stat_mod
import string
import threading

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal


def delete_to_recycle_bin(paths):
    """送入回收站（仅 Windows）；失败返回 False。"""
    if not paths:
        return True
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [
                ("hwnd", wintypes.HWND),
                ("wFunc", wintypes.UINT),
                ("pFrom", wintypes.LPCWSTR),
                ("pTo", wintypes.LPCWSTR),
                ("fFlags", wintypes.WORD),
                ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", wintypes.LPVOID),
                ("lpszProgressTitle", wintypes.LPCWSTR),
            ]

        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        op = SHFILEOPSTRUCTW()
        op.hwnd = None
        op.wFunc = 3  # FO_DELETE
        op.pFrom = "\0".join(p.replace("/", "\\") for p in paths) + "\0"
        op.pTo = None
        op.fFlags = 0x0064  # FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT
        res = shell32.SHFileOperationW(ctypes.byref(op))
        return res == 0 and not op.fAnyOperationsAborted
    except Exception:
        return False


def _force_rmtree(path):
    """处理只读属性后递归删除（用于移动残留 / 失败兜底）。"""

    def onerr(func, p, exc):
        try:
            os.chmod(p, stat_mod.S_IWRITE)
            func(p)
        except OSError:
            raise

    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, onerror=onerr)
    else:
        os.chmod(path, stat_mod.S_IWRITE)
        os.remove(path)


def _same_volume(a, b):
    if os.name == "nt":
        try:
            da, db = os.path.splitdrive(os.path.abspath(a))[0].upper(), \
                os.path.splitdrive(os.path.abspath(b))[0].upper()
            return da == db
        except Exception:
            return False
    return os.stat(a).st_dev == os.stat(b).st_dev


def _unique_dst(dst):
    """目标重名时自动加 (2) (3) …后缀。"""
    if not os.path.lexists(dst):
        return dst
    base, ext = os.path.splitext(dst)
    n = 2
    while os.path.lexists("%s (%d)%s" % (base, n, ext)):
        n += 1
    return "%s (%d)%s" % (base, n, ext)


class OpSignals(QObject):
    started = Signal(int)                 # 总字节数
    progress = Signal(int, int)           # 已完成字节, 总字节
    file_done = Signal(str)               # 单个文件完成
    moved = Signal(str, str)              # 移动/改名完成：旧路径, 新路径（标签路径同步钩子）
    conflict = Signal(str, str, object)   # src, dst, 回调(bool: 覆盖?)
    error = Signal(str, str, object)      # src, 错误文本, 回调(bool: 跳过?)
    status = Signal(str)                  # 阶段提示（如"正在统计文件大小"）
    finished = Signal(bool)               # 是否有错误


class OpTask(QRunnable):
    """后台执行 copy/move。

    mode: "copy" | "move"
    冲突策略: "ask"(信号回调) / "overwrite" / "skip" / "rename"
    """

    def __init__(self, srcs, dst_dir, mode, policy="ask", overwrite_all=False):
        super().__init__()
        self.srcs = srcs
        self.dst_dir = dst_dir
        self.mode = mode
        self.policy = policy
        self.overwrite_all = overwrite_all
        self.sig = OpSignals()
        self.cancelled = threading.Event()
        self._sem = threading.Semaphore(0)
        self._answer = None

    # ---- 由 UI 线程调用的答复入口 ---------------------------------------
    def answer(self, value):
        self._answer = value
        self._sem.release()

    def _ask(self, src, dst):
        """UI 线程提问后阻塞等待；返回 UI 给的布尔值。"""
        self._answer = None
        self.sig.conflict.emit(src, dst, self._ask_reply)
        self._sem.acquire()
        return self._answer

    def _ask_reply(self, value):
        self.answer(bool(value))

    # ---- 主流程 ----------------------------------------------------------
    def run(self):
        total = 0
        done = 0
        had_error = False
        items = []
        if len(self.srcs) > 1:
            self.sig.status.emit("正在统计文件大小（%d 项）…" % len(self.srcs))
        for s in self.srcs:
            if self.cancelled.is_set():
                break
            try:
                size = 0
                if os.path.isdir(s):
                    for root, _dirs, files in os.walk(s):
                        for f in files:
                            try:
                                size += os.path.getsize(os.path.join(root, f))
                            except OSError:
                                pass
                else:
                    size = os.path.getsize(s)
            except OSError:
                size = 0
            items.append((s, size))
            total += size

        self.sig.started.emit(total)
        for s, size in items:
            if self.cancelled.is_set():
                break
            while True:
                try:
                    done = self._transfer_one(s, done, total)
                    break
                except self.SkipFile:
                    had_error = True
                    break
                except Exception as exc:
                    had_error = True
                    if not self._handle_error(s, str(exc)):
                        break
        self.sig.finished.emit(had_error)

    class _Skip(Exception):
        pass

    SkipFile = _Skip

    def _handle_error(self, src, msg):
        """错误弹窗：返回 True 重试 / False 跳过。"""
        self._answer = None
        self.sig.error.emit(src, msg, self._error_reply)
        self._sem.acquire()
        return bool(self._answer) and not self.cancelled.is_set()

    def _error_reply(self, value):
        self.answer(bool(value))

    def _transfer_one(self, src, done, total):
        name = os.path.basename(src.rstrip("\\/"))
        dst = os.path.join(self.dst_dir, name)
        if os.path.abspath(src) == os.path.abspath(dst):
            return done
        if os.path.lexists(dst):
            if self.policy == "ask":
                if not self._ask(src, dst):
                    self.sig.file_done.emit(src)
                    return done
            elif self.policy == "skip":
                self.sig.file_done.emit(src)
                return done
            elif self.policy == "rename":
                dst = _unique_dst(dst)
        if os.path.isdir(src) and not os.path.islink(src):
            done = self._copy_tree(src, dst, done, total, delete_after=(self.mode == "move"))
            if self.mode == "move" and os.path.isdir(dst) and not os.path.exists(src):
                self.sig.moved.emit(src, dst)
            return done
        # 单文件
        tmp = dst + ".part"
        if os.path.isdir(dst):
            dst = _unique_dst(os.path.join(dst, name))
            tmp = dst + ".part"
        self._copy_file(src, tmp, done, total)
        if os.path.lexists(dst):
            _force_rmtree(dst)
        os.replace(tmp, dst)
        if self.mode == "move":
            os.remove(src)
            self.sig.moved.emit(src, dst)
        return done + os.path.getsize(dst) if os.path.isfile(dst) else done

    def _copy_tree(self, src, dst, done, total, delete_after=False):
        os.makedirs(dst, exist_ok=True)
        for root, dirs, files in os.walk(src):
            if self.cancelled.is_set():
                return done
            rel = os.path.relpath(root, src)
            troot = os.path.join(dst, rel) if rel != "." else dst
            for d in dirs:
                os.makedirs(os.path.join(troot, d), exist_ok=True)
            for f in files:
                if self.cancelled.is_set():
                    return done
                s = os.path.join(root, f)
                t = os.path.join(troot, f)
                try:
                    tmp = t + ".part"
                    done = self._copy_file(s, tmp, done, total)
                    if os.path.lexists(t):
                        os.chmod(t, stat_mod.S_IWRITE)
                        os.remove(t)
                    os.replace(tmp, t)
                    if delete_after:
                        os.remove(s)
                except Exception as exc:
                    try:
                        if os.path.lexists(tmp):
                            os.remove(tmp)
                    except OSError:
                        pass
                    self._handle_error(s, str(exc))
            if delete_after:
                try:
                    for d in dirs:
                        _force_rmtree(os.path.join(root, d))
                except OSError:
                    pass
        if delete_after:
            try:
                os.rmdir(src)
            except OSError:
                pass
        return done

    def _copy_file(self, src, dst, done, total):
        CHUNK = 1024 * 512
        with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
            while True:
                if self.cancelled.is_set():
                    fdst.close()
                    try:
                        os.remove(dst)
                    except OSError:
                        pass
                    raise self.SkipFile()
                buf = fsrc.read(CHUNK)
                if not buf:
                    break
                fdst.write(buf)
                done += len(buf)
                self.sig.progress.emit(done, total)
        return done


class RenameTask(QRunnable):
    """后台批量重命名：大量文件时避免 UI 线程逐个 os.rename 卡死。"""

    def __init__(self, jobs):
        super().__init__()
        self.jobs = list(jobs)      # [(src, dst, display_name), ...]
        self.cancelled = threading.Event()
        self.sig = OpSignals()

    def run(self):
        had_error = False
        total = len(self.jobs)
        try:
            self.sig.started.emit(total)
            for i, (src, dst, name) in enumerate(self.jobs, 1):
                if self.cancelled.is_set():
                    break
                try:
                    os.rename(src, dst)
                    self.sig.file_done.emit(name)
                    self.sig.moved.emit(src, dst)
                except OSError as exc:
                    had_error = True
                    self.sig.error.emit(name, str(exc), lambda _reply: None)
                self.sig.progress.emit(i, total)
        except Exception as exc:
            had_error = True
            self.sig.error.emit("", str(exc), lambda _reply: None)
        finally:
            self.sig.finished.emit(had_error)


class DeleteTask(QRunnable):
    """后台删除到回收站：SHFileOperationW 可能长时间阻塞，绝不能在 UI 线程调用。"""

    def __init__(self, paths):
        super().__init__()
        self.paths = list(paths)
        self.cancelled = threading.Event()
        self.sig = OpSignals()

    def run(self):
        had_error = False
        total = len(self.paths)
        try:
            self.sig.started.emit(total)
            if self.cancelled.is_set():
                self.sig.finished.emit(False)
                return
            ok = delete_to_recycle_bin(self.paths)
            if not ok:
                had_error = True
                src = self.paths[0] if self.paths else ""
                self.sig.error.emit(src, "回收站操作失败（非 Windows 或权限不足）",
                                    lambda _reply: None)
            else:
                for i, p in enumerate(self.paths, 1):
                    self.sig.progress.emit(i, total)
                    self.sig.file_done.emit(p)
        except Exception as exc:
            had_error = True
            self.sig.error.emit(self.paths[0] if self.paths else "", str(exc),
                                lambda _reply: None)
        finally:
            self.sig.finished.emit(had_error)


def purge_paths(paths):
    """永久删除（不进回收站，不可恢复）。返回 (成功数, 错误列表)。

    危险路径守卫：拒绝删除盘符根/用户主目录，避免 Shift+Delete 误操作
    把整个工作目录抹掉。单个条目失败不影响其余项。
    """
    ok_count, errors = 0, []
    protected = {os.path.normcase(os.path.abspath(p))
                 for p in (os.path.expanduser("~"), os.path.abspath(os.sep))}
    if os.name == "nt":
        for letter in string.ascii_uppercase:
            protected.add(os.path.normcase(letter + ":\\"))
    for p in paths:
        try:
            ap = os.path.normcase(os.path.abspath(p))
            if ap in protected:
                errors.append((p, "受保护的系统目录，拒绝永久删除"))
                continue
            if not os.path.lexists(p):
                errors.append((p, "路径不存在"))
                continue
            _force_rmtree(p)
            ok_count += 1
        except Exception as exc:
            errors.append((p, str(exc)))
    return ok_count, errors


class PurgeTask(QRunnable):
    """后台永久删除：大目录 shutil.rmtree 可能耗时，绝不能占 UI 线程。"""

    def __init__(self, paths):
        super().__init__()
        self.paths = list(paths)
        self.cancelled = threading.Event()
        self.sig = OpSignals()

    def run(self):
        had_error = False
        total = len(self.paths)
        try:
            self.sig.started.emit(total)
            if self.cancelled.is_set():
                self.sig.finished.emit(False)
                return
            done, errors = purge_paths(self.paths)
            for src, msg in errors:
                had_error = True
                self.sig.error.emit(src, msg, lambda _reply: None)
            for i, p in enumerate(self.paths[:done], 1):
                self.sig.progress.emit(i, total)
                self.sig.file_done.emit(p)
        except Exception as exc:
            had_error = True
            self.sig.error.emit(self.paths[0] if self.paths else "", str(exc),
                                lambda _reply: None)
        finally:
            self.sig.finished.emit(had_error)


class OpQueue(QObject):
    """操作队列：跟踪全部后台任务，供操作队列栏显示（FC 的 Operations queue bar）。"""
    changed = Signal()

    def __init__(self):
        super().__init__()
        self.items = []   # {desc, task, status, percent, cur}

    def add(self, task, desc):
        it = {"desc": desc, "task": task, "status": "运行中", "percent": 0, "cur": ""}
        self.items.append(it)
        if len(self.items) > 50:
            self.items.pop(0)
        task.sig.progress.connect(
            lambda d, tot, it=it: self._upd(it, int(d * 100 / tot) if tot else 100))
        task.sig.file_done.connect(lambda p, it=it: self._setcur(it, p))
        task.sig.finished.connect(lambda e, it=it: self._fin(it, e))
        self.changed.emit()

    def _upd(self, it, percent):
        it["percent"] = percent
        self.changed.emit()

    def _setcur(self, it, cur):
        it["cur"] = cur
        self.changed.emit()

    def _fin(self, it, err):
        it["status"] = "完成（有错误）" if err else "完成"
        it["percent"] = 100
        self.changed.emit()


class OpRunner:
    """UI 侧封装：创建任务、连接进度弹窗、提交线程池。"""

    def __init__(self, widget, queue=None):
        self.widget = widget
        self.queue = queue

    def run_copy(self, srcs, dst_dir, mode="copy", moved_cb=None):
        task = OpTask(srcs, dst_dir, mode)
        if moved_cb is not None:
            task.sig.moved.connect(moved_cb)
        if self.queue is not None:
            self.queue.add(task, "%s %d 项 → %s"
                           % ("复制" if mode == "copy" else "移动", len(srcs), dst_dir))
        self._run_with_dialog(task, "复制文件" if mode == "copy" else "移动文件")
        return task

    def run_delete(self, paths):
        task = DeleteTask(paths)
        if self.queue is not None:
            self.queue.add(task, "删除到回收站 %d 项" % len(paths))
        self._run_with_dialog(task, "删除到回收站")
        return task

    def run_purge(self, paths):
        """永久删除（Shift+Delete）：不可恢复，UI 侧须先二次确认。"""
        task = PurgeTask(paths)
        if self.queue is not None:
            self.queue.add(task, "永久删除 %d 项" % len(paths))
        self._run_with_dialog(task, "永久删除")
        return task

    def run_zip(self, srcs, zip_path):
        task = ZipTask("compress", srcs, zip_path)
        if self.queue is not None:
            self.queue.add(task, "压缩 %d 项 → %s" % (len(srcs), zip_path))
        self._run_with_dialog(task, "压缩 ZIP")
        return task

    def run_unzip(self, zip_path, dst_dir):
        task = ZipTask("extract", [zip_path], dst_dir)
        if self.queue is not None:
            self.queue.add(task, "解压 %s → %s" % (zip_path, dst_dir))
        self._run_with_dialog(task, "解压 ZIP")
        return task

    def _run_with_dialog(self, task, title):
        """非模态进度对话框：后台执行期间可继续操作面板，队列栏同步显示。"""
        from .progress import ProgressDialog
        dlg = ProgressDialog(self.widget, task, title=title)
        dlg.setModal(False)
        task.sig.started.connect(dlg.set_total)
        task.sig.progress.connect(dlg.set_progress)
        task.sig.file_done.connect(dlg.add_log)
        task.sig.error.connect(dlg.on_error)
        task.sig.status.connect(dlg.set_status)
        task.sig.conflict.connect(dlg.on_conflict)
        task.sig.finished.connect(dlg.on_finished)
        task.sig.finished.connect(dlg.close)
        dlg.show()
        QThreadPool.globalInstance().start(task)


class ZipTask(QRunnable):
    """后台 ZIP 压缩（compress）或解压（extract），信号接口与 OpTask 一致。"""

    def __init__(self, mode, srcs, target):
        """compress: srcs 为待压缩路径列表, target 为目标 zip 路径。
        extract: srcs 为 [zip 路径], target 为解压目的目录。"""
        super().__init__()
        self.mode = mode
        self.srcs = srcs
        self.target = target
        self.sig = OpSignals()
        self.cancelled = threading.Event()

    def run(self):
        import zipfile
        had_error = False
        try:
            if self.mode == "compress":
                items = []
                for s in self.srcs:
                    if os.path.isdir(s):
                        for root, _d, files in os.walk(s):
                            rel = os.path.relpath(root, os.path.dirname(s))
                            for f in files:
                                p = os.path.join(root, f)
                                try:
                                    items.append((p, os.path.join(rel, f)))
                                except OSError:
                                    pass
                    else:
                        items.append((s, os.path.basename(s)))
                total = sum((os.path.getsize(p) for p, _ in items
                             if os.path.isfile(p)), 0)
                self.sig.started.emit(total)
                done = 0
                with zipfile.ZipFile(self.target, "w", zipfile.ZIP_DEFLATED) as zf:
                    for p, arc in items:
                        if self.cancelled.is_set():
                            break
                        try:
                            zf.write(p, arc)
                            done += os.path.getsize(p) if os.path.isfile(p) else 0
                            self.sig.progress.emit(done, total)
                            self.sig.file_done.emit(p)
                        except OSError as exc:
                            had_error = True
                            self.sig.error.emit(p, str(exc), lambda cb: cb(False))
            else:
                with zipfile.ZipFile(self.srcs[0]) as zf:
                    infos = zf.infolist()
                    total = sum(i.file_size for i in infos)
                    self.sig.started.emit(total)
                    done = 0
                    for info in infos:
                        if self.cancelled.is_set():
                            break
                        try:
                            dst = os.path.join(self.target, info.filename)
                            os.makedirs(os.path.dirname(dst) or self.target, exist_ok=True)
                            with zf.open(info) as fsrc, open(dst, "wb") as fdst:
                                shutil.copyfileobj(fsrc, fdst)
                            done += info.file_size
                            self.sig.progress.emit(done, total)
                            self.sig.file_done.emit(info.filename)
                        except Exception as exc:
                            had_error = True
                            self.sig.error.emit(info.filename, str(exc), lambda cb: cb(False))
        except Exception as exc:
            had_error = True
            self.sig.error.emit(self.target, str(exc), lambda cb: cb(False))
        self.sig.finished.emit(had_error)


class Md5Task(QRunnable):
    """后台计算一组文件的 MD5，逐个回报。"""

    class Sig(QObject):
        one = Signal(str, str)      # path, md5 或 "错误: …"
        finished = Signal()

    def __init__(self, paths):
        super().__init__()
        self.paths = paths
        self.sig = self.Sig()
        self.cancelled = threading.Event()

    def run(self):
        import hashlib
        for p in self.paths:
            if self.cancelled.is_set():
                break
            try:
                h = hashlib.md5()
                with open(p, "rb") as f:
                    for chunk in iter(lambda: f.read(1024 * 1024), b""):
                        h.update(chunk)
                self.sig.one.emit(p, h.hexdigest())
            except OSError as exc:
                self.sig.one.emit(p, "错误：%s" % exc)
        self.sig.finished.emit()
