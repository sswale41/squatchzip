#!/usr/bin/env python3
"""
SquatchZip — Modern archive manager for openSUSE Tumbleweed / KDE Plasma 6
Backend : p7zip (7z)
Requires: python3-PyQt6   (sudo zypper install python3-PyQt6)
          p7zip            (sudo zypper install p7zip p7zip-full)
"""

import sys
import os
import re
import glob
import shutil
import signal
import subprocess
import tempfile
import time
from collections import namedtuple
from pathlib import Path
from datetime import datetime

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QStatusBar, QLabel,
    QLineEdit, QPushButton, QFileDialog, QComboBox, QSlider,
    QCheckBox, QGroupBox, QFormLayout, QMessageBox,
    QListWidget, QSpinBox,
    QTextEdit, QAbstractItemView, QHeaderView, QFrame,
    QDialog, QDialogButtonBox, QInputDialog, QMenu,
    QSplitter, QStackedWidget, QTreeView, QTabWidget, QTabBar, QProgressBar,
)
from PyQt6.QtCore import (
    Qt, QThread, pyqtSignal, QSize, QSettings, QDir, QTimer,
    QAbstractItemModel, QModelIndex,
)
from PyQt6.QtGui import QAction, QIcon, QFont, QDragEnterEvent, QDropEvent, QCursor, QFileSystemModel, QPixmap

# ─────────────────────────────────────────────────────
VERSION  = "2.3.0"
APP_NAME = "SquatchZip"
ORG_NAME = "SquatchLabs"

FORMATS = {
    "7-Zip (.7z)":          "7z",
    "ZIP (.zip)":           "zip",
    "TAR+GZ (.tar.gz)":     "tar.gz",
    "TAR+BZ2 (.tar.bz2)":   "tar.bz2",
    "TAR+XZ (.tar.xz)":     "tar.xz",
    "TAR (.tar)":           "tar",
    "GZIP (.gz)":           "gz",
    "BZIP2 (.bz2)":         "bz2",
}

COMPRESS_LABELS = {
    0: "Store",  1: "Fastest", 3: "Fast",
    5: "Normal", 7: "Maximum", 9: "Ultra",
}

ARCHIVE_FILTER = (
    "Archives (*.7z *.zip *.tar *.gz *.bz2 *.xz "
    "*.tar.gz *.tar.bz2 *.tar.xz *.tgz *.rar *.iso *.cab *.wim);;"
    "All files (*)"
)

# Regex matching 7-zip's startup banner lines (filtered from progress log)
_BANNER_RE = re.compile(r'^7-Zip\b|^\s*\d+-bit\s+locale=')
# Finds percentage tokens in raw 7z output (backspace-overwrite style, e.g. "  23%")
_PCT_SCAN_RE = re.compile(rb'(?<!\d)(\d{1,3})%')

ICON_EXT_MAP = {
    "py":   "text-x-python",        "sh":   "text-x-script",
    "txt":  "text-plain",           "md":   "text-x-markdown",
    "pdf":  "application-pdf",      "jpg":  "image-jpeg",
    "jpeg": "image-jpeg",           "png":  "image-png",
    "gif":  "image-gif",            "svg":  "image-svg+xml",
    "mp3":  "audio-mpeg",           "flac": "audio-x-flac",
    "mp4":  "video-mp4",            "mkv":  "video-x-matroska",
    "zip":  "application-zip",      "7z":   "application-x-7z-compressed",
    "deb":  "application-x-deb",    "rpm":  "application-x-rpm",
    "html": "text-html",            "css":  "text-css",
    "js":   "application-javascript","json": "application-json",
    "xml":  "text-xml",             "conf": "text-x-generic",
    "cfg":  "text-x-generic",
}


# ═════════════════════════════════════════════════════
#  Worker Thread
# ═════════════════════════════════════════════════════

# 7z reports a missing/incorrect password through its normal output, not a
# distinct exit code, so callers have to sniff for it.
#
# _LIST: broad.  A false positive only re-prompts for a password, which is
# harmless, and "encrypted" is how 7z labels a header-encrypted archive.
# _TEST:  narrow.  A corrupt *encrypted* archive also mentions "encrypted" in
# its error output, and calling that a password problem would send the user
# looking for the wrong thing.
_PW_MARKERS_LIST = ("encrypted", "enter password", "wrong password")
_PW_MARKERS_TEST = ("enter password", "wrong password", "can not open encrypted")


class OperationCancelled(Exception):
    """Raised inside a worker when the user cancels the running 7z process."""


# gzip, bzip2 and xz each wrap a *single* stream, so 7-Zip cannot build a
# .tar.gz in one pass.  Given several inputs it fails outright with E_INVALIDARG;
# given exactly one it silently writes a plain gzip under a .tar.gz name, which
# no tar can unpack.  These formats need an explicit tar stage on the way in and
# a second extract pass on the way out.
_TAR_COMPOUND_SUFFIXES = (
    ".tar.gz", ".tar.bz2", ".tar.xz",
    ".tgz", ".tbz2", ".tbz", ".txz",
)


def _tar_compound_suffix(path):
    """The matched suffix if path is a tar-then-compress archive, else None."""
    low = path.lower()
    # Longest first, so ".tar.gz" wins over a bare ".gz" check elsewhere.
    for suf in sorted(_TAR_COMPOUND_SUFFIXES, key=len, reverse=True):
        if low.endswith(suf):
            return suf
    return None


def _entry_is_dir(entry):
    """True if a "7z l -slt" entry is a directory.

    7-Zip describes this differently per format: .7z and .zip use
    "Attributes = D ...", while tar uses "Folder = +" and "Mode = drwx...".
    Checking only Attributes counted every directory in a tar as a file.
    """
    if "D" in (entry.get("Attributes") or ""):
        return True
    if (entry.get("Folder") or "").strip() == "+":
        return True
    if (entry.get("Mode") or "").startswith("d"):
        return True
    return (entry.get("Path") or "").endswith("/")


def _archive_part_paths(path):
    """Every on-disk file belonging to an archive: the base name plus any
    multi-volume parts (name.7z.001…, name.part1.rar…).  A split archive has
    no file at the base name at all, so callers must not assume it exists."""
    paths = [path] if os.path.exists(path) else []
    paths += sorted(glob.glob(f"{path}.[0-9][0-9][0-9]"))
    stem = path[:-4] if path.lower().endswith(".rar") else path
    paths += sorted(glob.glob(f"{stem}.part*.rar"))
    return paths


class ArchiveWorker(QThread):
    progress     = pyqtSignal(str)
    progress_pct = pyqtSignal(int)   # 0-100, emitted during create/extract
    op_finished  = pyqtSignal(bool, str)   # NOT 'finished' — QThread owns that name
    listing      = pyqtSignal(list)

    def __init__(self, operation, **kwargs):
        super().__init__()
        self.operation = operation
        self.kwargs    = kwargs
        self._proc      = None    # the 7z process currently running, if any
        self._cancelled = False
        self._created_archive = None   # set by _create when it makes a new file
        self._stage_dir       = None   # scratch dir for the .tar.gz tar stage

    def run(self):
        try:
            getattr(self, f"_{self.operation}")()
        except OperationCancelled:
            self._cancel_cleanup()
            self.op_finished.emit(False, "Cancelled.")
        except Exception as exc:
            self.op_finished.emit(False, str(exc))

    def cancel(self):
        """Stop the running 7z process.  Called from the GUI thread."""
        self._cancelled = True
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            # start_new_session=True put 7z in its own process group, so this
            # also reaps any helper it spawned.
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except OSError:
            pass

    def _cancel_cleanup(self):
        """Delete a half-written archive after a cancelled create.  Only ever
        touches a file this worker created — never one it was appending to."""
        if self._stage_dir:
            shutil.rmtree(self._stage_dir, ignore_errors=True)
            self._stage_dir = None
        if not self._created_archive:
            return
        for p in _archive_part_paths(self._created_archive):
            try:
                os.remove(p)
            except OSError:
                pass

    # ── internal helpers ──────────────────────────────

    def _run(self, cmd, cwd=None, track_pct=False):
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            # DEVNULL, never the inherited tty: 7z prompts "Enter password:" on
            # stdin whenever it needs one we did not supply, and would otherwise
            # block forever behind a modal dialog.  At EOF it exits non-zero.
            stdin=subprocess.DEVNULL,
            bufsize=0, cwd=cwd,
            start_new_session=True,  # isolate from terminal SIGINT/SIGHUP
        )
        self._proc = proc
        lines = []
        buf = b""
        last_pct = 0

        for chunk in iter(lambda: proc.stdout.read(256), b""):
            # Scan raw chunk for percentage tokens before any splitting.
            # 7z uses backspace sequences (\x08) to overwrite progress in-place,
            # so percentages are NOT newline-terminated — they must be found in the
            # raw byte stream. Emit only monotonically non-decreasing values so the
            # "100%  : 0% : moving temp archive" phase never regresses the bar.
            if track_pct and last_pct < 100:
                for m in _PCT_SCAN_RE.finditer(chunk):
                    pct = min(100, int(m.group(1)))
                    if pct >= last_pct:
                        last_pct = pct
                        self.progress_pct.emit(pct)
            buf += chunk
            parts = re.split(rb'[\r\n]', buf)
            buf = parts[-1]
            for part in parts[:-1]:
                # Strip backspace/control chars before treating as a log line.
                line = re.sub(rb'[\x00-\x08\x0b-\x1f\x7f]', b'', part) \
                           .decode("utf-8", errors="replace").strip()
                if not line or _BANNER_RE.match(line):
                    continue
                # Suppress progress-blob lines (handled by the bar)
                if track_pct and re.match(r'^\s*\d+%', line):
                    continue
                self.progress.emit(line)
                lines.append(line)

        if buf:
            line = re.sub(rb'[\x00-\x08\x0b-\x1f\x7f]', b'', buf) \
                       .decode("utf-8", errors="replace").strip()
            if line and not _BANNER_RE.match(line):
                if not (track_pct and re.match(r'^\s*\d+%', line)):
                    self.progress.emit(line)
                    lines.append(line)

        proc.wait()
        self._proc = None
        if self._cancelled:
            raise OperationCancelled()
        return proc.returncode, lines

    # ── operations ────────────────────────────────────

    def _list(self):
        archive  = self.kwargs["archive"]
        password = self.kwargs.get("password", "")
        cmd = ["7z", "l", "-slt", archive]
        if password:
            cmd += [f"-p{password}"]
        rc, lines = self._run(cmd)
        if rc not in (0, 1):
            if any(m in l.lower() for l in lines for m in _PW_MARKERS_LIST):
                self.op_finished.emit(False, "__ENCRYPTED__")
            else:
                self.op_finished.emit(False, "Failed to list archive contents.")
            return
        # "7z l -slt" opens with a header block describing the archive itself
        # ("Path = <the archive>", Type, Method, Solid…) before the first
        # ---------- separator.  Treating that as an entry added a phantom row
        # to the tree, inflated the file count by one, and — because its Path is
        # the archive's own absolute path — fed a bogus root name into
        # _should_create_subfolder(), so that heuristic almost always said
        # "wrap" and Extract double-nested.
        files, current = [], {}
        in_entries = False
        for line in lines:
            if line.startswith("----------"):
                if not in_entries:
                    in_entries = True      # header block ends here; discard it
                elif current:
                    files.append(current)
                current = {}
            elif in_entries and "=" in line:
                k, _, v = line.partition("=")
                key = k.strip()
                if key == "Path" and "Path" in current:
                    # New entry with no ---------- separator — save and reset
                    files.append(current)
                    current = {}
                current[key] = v.strip()
        if in_entries and current:
            files.append(current)
        self.listing.emit(files)
        self.op_finished.emit(True, f"Loaded {len(files)} entries.")

    def _create(self):
        archive     = str(Path(self.kwargs["archive"]).resolve())  # must be abs before cwd changes
        sources     = self.kwargs["sources"]
        level       = self.kwargs.get("level", 5)
        password    = self.kwargs.get("password", "")
        split_size  = self.kwargs.get("split_size", 0)
        split_unit  = self.kwargs.get("split_unit", "MB").lower()[0]  # k, m, or g
        delete_src  = self.kwargs.get("delete_src", False)
        solid       = self.kwargs.get("solid", True)
        excl_hidden = self.kwargs.get("excl_hidden", False)

        # Resolve sources to absolute paths, then derive a working directory so
        # 7z stores clean relative paths (e.g. "photo.jpg") instead of the full
        # absolute path structure (e.g. "home/user/Pictures/photo.jpg").
        sources_abs = [str(Path(s).resolve()) for s in sources]
        try:
            work_cwd = os.path.commonpath([str(Path(s).parent) for s in sources_abs])
            if work_cwd == "/":
                raise ValueError("root — no meaningful common parent")
            rel_sources = [os.path.relpath(s, work_cwd) for s in sources_abs]
        except (ValueError, TypeError):
            work_cwd    = None
            rel_sources = sources_abs

        # A .tar.gz / .tar.bz2 / .tar.xz is built in two steps: tar the sources
        # into a scratch dir, then compress that single file.  Everything below
        # therefore targets the inner .tar, and _finish_tar_compound() runs the
        # compression stage afterwards.
        final_archive = archive
        tar_suffix    = _tar_compound_suffix(archive)
        self._stage_dir = None
        if tar_suffix:
            if not _archive_part_paths(final_archive):
                self._created_archive = final_archive
            self._stage_dir = tempfile.mkdtemp(prefix="squatchzip_tar_")
            archive = os.path.join(
                self._stage_dir, Path(final_archive).name[:-len(tar_suffix)] + ".tar"
            )
            if password:
                # None of gzip, bzip2 or xz can encrypt.  Silently dropping the
                # password would hand back an archive the user thinks is locked.
                self.progress.emit(
                    "⚠ Password ignored — gzip/bzip2/xz cannot encrypt. "
                    "Use .7z or .zip if you need an encrypted archive."
                )
                password = ""

        # -bsp1 breaks pipeline formats (gz/bz2/xz use a two-stage internal stream)
        _arc_lower = archive.lower()
        _bsp = [] if any(_arc_lower.endswith(e) for e in ('.gz', '.bz2', '.xz')) else ['-bsp1']
        _is_7z = _arc_lower.endswith(".7z")

        # Remember a brand-new archive so a cancel can clean up the partial file.
        # Appending to an existing archive must never mark it for deletion.
        if not tar_suffix and not _archive_part_paths(archive):
            self._created_archive = archive

        cmd = ["7z", "a"] + _bsp
        if tar_suffix:
            # The tar stage only bundles; -mx belongs on the compression stage.
            cmd += ["-ttar"]
        else:
            cmd += [f"-mx={level}"]
        # -ms (solid mode) is a 7z-only switch.  zip/tar/gz reject it outright
        # with E_INVALIDARG and rc=2, so it must never be sent for them — and
        # 'solid' is a persisted setting, so an unchecked box used to break
        # every non-7z create and every "add to archive" until it was re-checked.
        if _is_7z:
            if not solid:
                cmd += ["-ms=off"]
            else:
                # LZMA2 uses ~11× dictionary per encoder thread (~700 MB/thread at -mx=9).
                # 16 threads × 700 MB = 11 GB encoder state alone — earlyoom kills it.
                # Cap at 4 threads: 4 × 700 MB + 2 GB solid block ≈ 5 GB total. Thread
                # count doesn't affect compression ratio, only speed.
                cmd += ["-ms=2g", "-mmt=4"]
        if password:
            cmd += [f"-p{password}"]
            if _is_7z:                   # _arc_lower, so "Backup.7Z" counts too
                cmd += ["-mhe=on"]       # encrypt header too
        if split_size > 0 and not tar_suffix:
            cmd += [f"-v{split_size}{split_unit}"]
        if excl_hidden:
            cmd += ["-xr!.*"]            # exclude dot-files/folders
        cmd += [archive] + rel_sources

        symlinks_excluded = False
        rc, lines = self._run(cmd, cwd=work_cwd, track_pct=bool(_bsp))
        if rc != 0:
            # Detect NTFS junction-point / symlink-loop errors (errno=40, ELOOP).
            # When archiving from a Windows partition, junction points are mounted
            # as Linux symlinks; 7z recursive traversal hits the 40-hop limit.
            # Auto-retry with -xr!@ to exclude all symlinks from the archive.
            _ELOOP_MARKERS = ("too many levels of symbolic links",
                              "eloop", "errno=40")
            if any(m in line.lower() for line in lines for m in _ELOOP_MARKERS):
                self.progress.emit(
                    "⚠ Symbolic link loop detected (NTFS junction point) — "
                    "retrying while excluding symbolic links…"
                )
                retry_cmd = cmd + ["-xr!@"]
                rc, _ = self._run(retry_cmd, cwd=work_cwd, track_pct=bool(_bsp))
                if rc != 0:
                    self.op_finished.emit(
                        False,
                        "Archive creation failed even after excluding symbolic links — "
                        "see log for details."
                    )
                    return
                # Fall through to success path; note symlinks were skipped.
                symlinks_excluded = True
                msg_suffix = " (symbolic links excluded — NTFS junction points skipped)"
            else:
                self.op_finished.emit(False, "Archive creation failed — see log for details.")
                return
        else:
            msg_suffix = ""

        if tar_suffix:
            ok, err = self._finish_tar_compound(
                archive, final_archive, level, split_size, split_unit
            )
            if not ok:
                self.op_finished.emit(False, err)
                return
            archive = final_archive

        # The archive may hold less than the source tree: -xr!.* dropped every
        # dot-file, and the ELOOP retry dropped every symlink.  Deleting the
        # sources in that case destroys data that was never archived.
        _excluded = []
        if excl_hidden:
            _excluded.append("hidden files")
        if symlinks_excluded:
            _excluded.append("symbolic links")
        if delete_src and _excluded:
            delete_src = False
            _kept_reason = " and ".join(_excluded)
            self.progress.emit(
                f"⚠ Source files NOT deleted — the archive excludes {_kept_reason}, "
                "so deleting the sources would lose data that was never archived."
            )
            msg_suffix += f" — sources kept (archive excludes {_kept_reason})"

        deleted, errors = 0, 0
        if delete_src:
            for src in sources:
                try:
                    if os.path.isdir(src):
                        shutil.rmtree(src)
                    else:
                        os.remove(src)
                    deleted += 1
                except Exception as e:
                    self.progress.emit(f"⚠ Could not delete '{src}': {e}")
                    errors += 1
            msg = f"Archive created. Deleted {deleted} source item(s).{msg_suffix}"
            if errors:
                msg += f" ({errors} deletion error(s) — see log.)"
        else:
            msg = f"Archive created successfully.{msg_suffix}"
        self.op_finished.emit(True, msg)

    def _finish_tar_compound(self, tar_path, final_archive, level,
                             split_size, split_unit):
        """Compress the staged .tar into the requested .tar.gz/.bz2/.xz.

        Returns (ok, error_message).  The scratch directory is always removed.
        """
        try:
            self.progress.emit("── Compressing archive ──")
            self.progress_pct.emit(0)
            cmd = ["7z", "a", f"-mx={level}"]
            if split_size > 0:
                cmd += [f"-v{split_size}{split_unit}"]
            # -bsp1 is omitted: gz/bz2/xz drive a second internal stream and the
            # switch makes 7z emit progress for the wrong stage.
            cmd += [final_archive, tar_path]
            rc, _ = self._run(cmd, cwd=os.path.dirname(tar_path))
            if rc != 0:
                return False, "Archive created, but compressing it failed — see log."
            self.progress_pct.emit(100)
            return True, ""
        finally:
            if self._stage_dir:
                shutil.rmtree(self._stage_dir, ignore_errors=True)
                self._stage_dir = None

    def _extract(self):
        archive   = self.kwargs["archive"]
        dest      = self.kwargs["dest"]
        password  = self.kwargs.get("password", "")
        mode      = self.kwargs.get("mode", "x")      # x=full paths  e=flat
        overwrite = self.kwargs.get("overwrite", True)
        paths     = self.kwargs.get("paths", [])       # specific items to extract

        cmd = ["7z", mode, "-bsp1", archive, f"-o{dest}"]
        if overwrite:
            cmd += ["-y"]
        if password:
            cmd += [f"-p{password}"]
        if paths:
            cmd += paths   # 7z accepts archive-internal paths as trailing args

        rc, _ = self._run(cmd, track_pct=True)
        if rc not in (0, 1):
            self.op_finished.emit(False, "Extraction failed — see log.")
            return

        # A .tar.gz unpacks to a bare .tar — one more pass gets the actual files.
        if _tar_compound_suffix(archive):
            ok, err = self._unwrap_inner_tar(dest, archive, mode, overwrite)
            if not ok:
                self.op_finished.emit(False, err)
                return

        if rc == 1:
            # 7z exit code 1 = non-fatal warnings (e.g. locked files, minor issues).
            # Extraction completed; surface the warning rather than treating as failure.
            self.op_finished.emit(True, f"Extracted to: {dest} (warnings — see log)")
        else:
            self.op_finished.emit(True, f"Extracted to: {dest}")

    def _unwrap_inner_tar(self, dest, archive, mode="x", overwrite=True):
        """Unpack the .tar that a .tar.gz/.tar.bz2/.tar.xz extracts to, then
        remove it, so the user is left with their files rather than a tarball.

        Returns (ok, error_message).
        """
        suffix = _tar_compound_suffix(archive)
        inner  = os.path.join(dest, Path(archive).name[:-len(suffix)] + ".tar")
        if not os.path.exists(inner):
            # The inner member is not always named after the archive.  Only act
            # when there is exactly one candidate, so we never guess wrong.
            candidates = glob.glob(os.path.join(dest, "*.tar"))
            if len(candidates) != 1:
                return True, ""      # nothing unambiguous to unwrap; leave it
            inner = candidates[0]

        self.progress.emit("── Unpacking inner tar ──")
        self.progress_pct.emit(0)
        cmd = ["7z", mode, "-bsp1", inner, f"-o{dest}"]
        if overwrite:
            cmd += ["-y"]
        rc, _ = self._run(cmd, track_pct=True)
        if rc not in (0, 1):
            return False, "Decompressed the archive, but unpacking the tar failed — see log."
        try:
            os.remove(inner)
        except OSError:
            pass
        return True, ""

    def _test(self):
        archive  = self.kwargs["archive"]
        password = self.kwargs.get("password", "")
        cmd = ["7z", "t", archive]
        if password:
            cmd += [f"-p{password}"]
        rc, lines = self._run(cmd)
        if rc == 0:
            self.op_finished.emit(True, "✓ Archive integrity OK — no errors found.")
        elif any(m in l.lower() for l in lines for m in _PW_MARKERS_TEST):
            # Reporting this as corruption sends people hunting a damaged file.
            self.op_finished.emit(
                False, "🔒 Wrong or missing password — cannot test this archive."
            )
        else:
            self.op_finished.emit(False, "✗ Archive test FAILED — errors detected!")

    def _delete(self):
        archive  = self.kwargs["archive"]
        paths    = self.kwargs["paths"]
        password = self.kwargs.get("password", "")
        cmd = ["7z", "d", archive]
        if password:
            cmd += [f"-p{password}"]
        cmd += paths
        rc, _ = self._run(cmd)
        n = len(paths)
        if rc == 0:
            self.op_finished.emit(True, f"Deleted {n} item(s) from archive.")
        else:
            self.op_finished.emit(False, "Delete failed — see log.")

    def _copy_to_archive(self):
        src_archive = self.kwargs["src_archive"]
        src_paths   = self.kwargs["src_paths"]
        dst_archive = self.kwargs["dst_archive"]
        password     = self.kwargs.get("password", "")       # source archive
        dst_password = self.kwargs.get("dst_password", "")   # destination archive

        tmp = tempfile.mkdtemp(prefix="squatchzip_copy_")
        try:
            self.progress.emit("── Phase 1: extracting from source ──")
            cmd = ["7z", "x", "-bsp1", src_archive, f"-o{tmp}", "-y"]
            if password:
                cmd += [f"-p{password}"]
            cmd += src_paths
            rc, _ = self._run(cmd, track_pct=True)
            if rc not in (0, 1):
                self.op_finished.emit(False, "Copy failed — could not extract from source archive.")
                return

            self.progress.emit("── Phase 2: adding to destination ──")
            self.progress_pct.emit(0)   # reset bar for phase 2
            dst_lower = dst_archive.lower()
            _bsp = [] if any(dst_lower.endswith(e) for e in ('.gz', '.bz2', '.xz')) else ['-bsp1']
            cmd = ["7z", "a"] + _bsp + [dst_archive]
            if dst_password:
                # Without this, copying into an encrypted archive silently wrote
                # the new entries in the clear.
                cmd += [f"-p{dst_password}"]
                if dst_lower.endswith(".7z"):
                    cmd += ["-mhe=on"]
            cmd += ["."]
            rc, _ = self._run(cmd, cwd=tmp, track_pct=bool(_bsp))
            if rc not in (0, 1):
                self.op_finished.emit(False, "Copy failed — could not add to destination archive.")
                return

            n = len(src_paths)
            self.op_finished.emit(True, f"Copied {n} item(s) to {Path(dst_archive).name}.")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _rename(self):
        archive  = self.kwargs["archive"]
        old_name = self.kwargs["old_name"]
        new_name = self.kwargs["new_name"]
        password = self.kwargs.get("password", "")
        cmd = ["7z", "rn", archive]
        if password:
            cmd += [f"-p{password}"]
        cmd += [old_name, new_name]
        rc, _ = self._run(cmd)
        if rc == 0:
            self.op_finished.emit(True, f"Renamed to \"{Path(new_name).name}\".")
        else:
            self.op_finished.emit(False, "Rename failed — see log.")


# ═════════════════════════════════════════════════════
#  Progress Log Dialog
# ═════════════════════════════════════════════════════

class ProgressDialog(QDialog):
    def __init__(self, parent=None, title="Working…"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(560, 300)
        self.setModal(True)
        self._operation_done   = False   # True once finish() is called
        self._cancel_requested = False
        self._worker           = None    # set by set_worker(); powers Cancel
        # Clock starts now, not at the first non-zero percentage — 7z spends its
        # scan phase at 0%, and excluding it made every early ETA far too low.
        self._start_time       = time.monotonic()
        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        lay.setContentsMargins(14, 14, 14, 14)

        self.status = QLabel("Please wait…")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        # Progress bar + ETA row (hidden until first progress signal)
        pct_row = QHBoxLayout()
        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._bar.setVisible(False)
        self._eta_lbl = QLabel("")
        self._eta_lbl.setMinimumWidth(120)
        self._eta_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._eta_lbl.setVisible(False)
        pct_row.addWidget(self._bar, 1)
        pct_row.addWidget(self._eta_lbl)
        lay.addLayout(pct_row)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(QFont("Monospace", 9))
        self.log.setMinimumHeight(180)
        lay.addWidget(self.log)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.cancel_btn = QPushButton(QIcon.fromTheme("process-stop"), "Cancel")
        self.cancel_btn.setEnabled(False)   # enabled by set_worker()
        self.cancel_btn.clicked.connect(self.request_cancel)
        btn_row.addWidget(self.cancel_btn)
        self.close_btn = QPushButton(QIcon.fromTheme("dialog-close"), "Close")
        self.close_btn.setEnabled(False)
        self.close_btn.clicked.connect(self.accept)
        btn_row.addWidget(self.close_btn)
        lay.addLayout(btn_row)

    def set_worker(self, worker):
        """Attach the worker this dialog is reporting on, so Cancel can stop it."""
        self._worker = worker
        self.cancel_btn.setEnabled(worker is not None)

    def request_cancel(self):
        """Signal the worker to stop.  The dialog stays up until the worker
        reports back through finish() — killing 7z is not instantaneous."""
        if self._operation_done or self._cancel_requested:
            return
        self._cancel_requested = True
        self.cancel_btn.setEnabled(False)
        self.status.setText("Cancelling…")
        self.append("⏹ Cancelling — stopping 7-Zip…")
        if self._worker is not None:
            self._worker.cancel()

    def reject(self):
        # Escape / window-X mid-operation cancels rather than doing nothing.
        if self._operation_done:
            super().reject()
        else:
            self.request_cancel()

    def closeEvent(self, event):
        if not self._operation_done:
            event.ignore()   # cannot vanish while 7z is still winding down
            self.request_cancel()
        else:
            super().closeEvent(event)

    def set_progress(self, pct: int):
        self._bar.setVisible(True)
        self._eta_lbl.setVisible(True)
        self._bar.setValue(pct)
        if pct > 0:
            elapsed = time.monotonic() - self._start_time
            remaining = elapsed * (100 - pct) / pct
            mins, secs = divmod(int(remaining), 60)
            eta = f"ETA: {mins}m {secs:02d}s" if mins else f"ETA: {secs}s"
            self._eta_lbl.setText(eta)
        else:
            self._eta_lbl.setText("ETA: calculating…")

    def append(self, line):
        self.log.append(line)
        sb = self.log.verticalScrollBar()
        sb.setValue(sb.maximum())

    def finish(self, success, message):
        self._operation_done = True
        self.cancel_btn.setEnabled(False)
        if self._bar.isVisible():
            self._bar.setValue(100)
            self._eta_lbl.setText("")
        color = "#2ecc71" if success else "#e74c3c"
        icon  = "✓" if success else "✗"
        self.status.setText(
            f'<span style="color:{color}; font-weight:bold;">{icon} {message}</span>'
        )
        if success or self._cancel_requested:
            # A cancel is a deliberate act, not an error to be acknowledged.
            self.accept()
        else:
            self.close_btn.setEnabled(True)


# ═════════════════════════════════════════════════════
#  Create Archive Dialog
# ═════════════════════════════════════════════════════

class CreateArchiveDialog(QDialog):
    def __init__(self, parent=None, initial_files=None, pre_delete=False):
        super().__init__(parent)
        self.setWindowTitle("Create Archive")
        self.setMinimumSize(580, 560)
        self._pre_delete = pre_delete
        self._base_stem        = ""
        self._base_dir         = ""
        self._user_edited_path = False
        self._prev_ts_idx      = 0
        self._custom_ts_format = ""   # loaded from QSettings in _build
        self._build(initial_files or [])

    def _build(self, initial_files):
        _s = QSettings(ORG_NAME, APP_NAME)
        self._custom_ts_format = _s.value("custom_ts_format", "")
        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(16, 16, 16, 16)

        # ── Output ───────────────────────────────────
        grp_out = QGroupBox("Output Archive")
        form_out = QFormLayout(grp_out)

        out_row = QHBoxLayout()
        self.out_path = QLineEdit()
        self.out_path.setPlaceholderText("Choose output path…")
        self.out_path.textEdited.connect(self._set_user_edited)
        self._btn_browse = QPushButton(QIcon.fromTheme("document-save-as"), "Browse…")
        self._btn_browse.clicked.connect(self._browse_out)
        out_row.addWidget(self.out_path)
        out_row.addWidget(self._btn_browse)
        form_out.addRow("Save as:", out_row)

        self.fmt = QComboBox()
        for label in FORMATS:
            self.fmt.addItem(label)
        self.fmt.currentTextChanged.connect(self._rebuild_outpath)
        form_out.addRow("Format:", self.fmt)

        ts_row = QHBoxLayout()
        self.ts_combo = QComboBox()
        self.ts_combo.addItems([
            "No timestamp",
            "Date  (2026-04-13)",
            "Date + Time  (2026-04-13_14-30)",
            "Custom…",
        ])
        self.ts_combo.currentIndexChanged.connect(self._on_ts_changed)
        self.ts_pos = QComboBox()
        self.ts_pos.addItems(["Suffix", "Prefix"])
        self.ts_pos.setFixedWidth(72)
        self.ts_pos.currentIndexChanged.connect(self._rebuild_outpath)
        ts_row.addWidget(self.ts_combo)
        ts_row.addWidget(self.ts_pos)
        form_out.addRow("Timestamp:", ts_row)

        self.indiv_each = QCheckBox("Create individual archive for each file/folder")
        self.indiv_each.toggled.connect(self._on_indiv_toggled)
        form_out.addRow("", self.indiv_each)

        root.addWidget(grp_out)

        # ── Source files ─────────────────────────────
        grp_src = QGroupBox("Files & Folders to Archive")
        lay_src = QVBoxLayout(grp_src)

        self.file_list = QListWidget()
        self.file_list.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.file_list.setAcceptDrops(True)
        self.file_list.setMinimumHeight(90)
        self.file_list.setAlternatingRowColors(True)
        self.file_list.model().rowsInserted.connect(self._on_files_changed)
        self.file_list.model().rowsRemoved.connect(self._on_files_changed)
        for f in initial_files:
            self.file_list.addItem(f)
        lay_src.addWidget(self.file_list)

        btn_row = QHBoxLayout()
        b_files = QPushButton(QIcon.fromTheme("list-add"),       "Add Files…")
        b_dir   = QPushButton(QIcon.fromTheme("folder-new"),     "Add Folder…")
        b_del   = QPushButton(QIcon.fromTheme("list-remove"),    "Remove")
        b_clear = QPushButton(QIcon.fromTheme("edit-clear-all"), "Clear All")
        b_files.clicked.connect(self._add_files)
        b_dir.clicked.connect(self._add_folder)
        b_del.clicked.connect(self._remove_sel)
        b_clear.clicked.connect(self.file_list.clear)
        for b in (b_files, b_dir, b_del, b_clear):
            btn_row.addWidget(b)
        btn_row.addStretch()
        lay_src.addLayout(btn_row)
        root.addWidget(grp_src)

        # ── Compression options ───────────────────────
        grp_opts = QGroupBox("Compression Options")
        form_opts = QFormLayout(grp_opts)
        form_opts.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)

        # Level slider
        lvl_row = QHBoxLayout()
        self.level = QSlider(Qt.Orientation.Horizontal)
        self.level.setRange(0, 9)
        saved_level = int(_s.value("compress_level", 5))
        self.level.setValue(saved_level)
        self.level.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.level.setSingleStep(1)
        self.level.setPageStep(2)
        lbl_text = f"{COMPRESS_LABELS.get(saved_level, f'Level {saved_level}')} ({saved_level})"
        self.lvl_lbl = QLabel(lbl_text)
        self.lvl_lbl.setMinimumWidth(110)
        self.level.valueChanged.connect(self._update_lvl)
        lvl_row.addWidget(self.level)
        lvl_row.addWidget(self.lvl_lbl)
        form_opts.addRow("Level:", lvl_row)

        # Password
        pw_row = QHBoxLayout()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("Leave blank for no encryption")
        self.pw_toggle = QPushButton("Show")
        self.pw_toggle.setCheckable(True)
        self.pw_toggle.setFixedWidth(54)
        self.pw_toggle.toggled.connect(
            lambda v: self.password.setEchoMode(
                QLineEdit.EchoMode.Normal if v else QLineEdit.EchoMode.Password
            )
        )
        pw_row.addWidget(self.password)
        pw_row.addWidget(self.pw_toggle)
        form_opts.addRow("Password:", pw_row)

        # Split
        split_row = QHBoxLayout()
        self.split = QSpinBox()
        self.split.setRange(0, 999999)
        self.split.setValue(0)
        self.split.setSpecialValueText("Disabled")
        self.split_unit = QComboBox()
        self.split_unit.addItems(["KB", "MB", "GB"])
        self.split_unit.setCurrentText("MB")
        self.split_unit.setFixedWidth(60)
        split_row.addWidget(self.split)
        split_row.addWidget(self.split_unit)
        form_opts.addRow("Split volumes:", split_row)

        # Solid archive (7z only)
        self.solid = QCheckBox("Solid archive (better ratio, 7z only)")
        self.solid.setChecked(_s.value("compress_solid", True, type=bool))
        form_opts.addRow("", self.solid)

        # Exclude hidden files
        self.excl_hidden = QCheckBox("Exclude hidden files and folders (dot-files)")
        self.excl_hidden.setChecked(_s.value("compress_excl_hidden", False, type=bool))
        form_opts.addRow("", self.excl_hidden)

        root.addWidget(grp_opts)

        # ── Delete after archiving ────────────────────   ★ THE FEATURE
        del_frame = QFrame()
        del_frame.setFrameShape(QFrame.Shape.StyledPanel)
        del_frame.setStyleSheet("""
            QFrame {
                border: 1.5px solid palette(highlight);
                border-radius: 5px;
                background: palette(base);
                padding: 2px;
            }
        """)
        del_lay = QHBoxLayout(del_frame)
        del_lay.setContentsMargins(10, 8, 10, 8)

        del_icon = QLabel()
        del_icon.setText("🗑")
        del_icon.setStyleSheet("font-size: 18px; border: none;")
        del_lay.addWidget(del_icon)

        self.delete_src = QCheckBox(
            "Delete source files after successful archiving"
        )
        self.delete_src.setFont(QFont(self.font().family(), -1, QFont.Weight.Medium))
        self.delete_src.setStyleSheet("border: none; background: transparent;")
        self.delete_src.setChecked(self._pre_delete)
        del_lay.addWidget(self.delete_src)
        del_lay.addStretch()

        root.addWidget(del_frame)

        # ── Buttons ───────────────────────────────────
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("Create Archive")
        btns.accepted.connect(self._validate)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

    # ── slot helpers ──────────────────────────────────

    def _browse_out(self):
        if self.indiv_each.isChecked():
            # In individual mode: browse for a destination folder
            start = self.out_path.text().strip() or self._base_dir or str(Path.home())
            folder = QFileDialog.getExistingDirectory(
                self, "Output Folder for Individual Archives", start
            )
            if folder:
                self.out_path.setText(folder)
            return
        fmt = self.fmt.currentText()
        ext = FORMATS[fmt]
        start = self.out_path.text().strip() or self._base_dir or str(Path.home())
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Archive As", start,
            f"Archives (*.{ext});;All files (*)"
        )
        if path:
            if not path.endswith(f".{ext}"):
                path += f".{ext}"
            p = Path(path)
            self._base_dir  = str(p.parent)
            self._base_stem = self._strip_ext(p.name)
            self._user_edited_path = False
            self._rebuild_outpath()

    def _set_user_edited(self):
        self._user_edited_path = True

    def _on_ts_changed(self, idx):
        if idx == 3:
            self._open_custom_ts_dialog()
        else:
            self._prev_ts_idx = idx
            self._rebuild_outpath()

    def _open_custom_ts_dialog(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Custom Timestamp Format")
        dlg.setMinimumWidth(400)
        lay = QVBoxLayout(dlg)
        lay.setSpacing(8)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.addWidget(QLabel("strftime format string (e.g. _%Y-%m-%d_%H%M%S):"))
        fmt_edit = QLineEdit(self._custom_ts_format or "_%Y-%m-%d")
        lay.addWidget(fmt_edit)
        preview = QLabel()
        preview.setStyleSheet("color: gray; font-style: italic;")
        lay.addWidget(preview)

        stem        = self._base_stem or "filename"
        ext         = FORMATS[self.fmt.currentText()]
        prefix_mode = self.ts_pos.currentIndex() == 1

        def _upd():
            try:
                ts = datetime.now().strftime(fmt_edit.text())
                name = (f"{ts}{stem}.{ext}" if prefix_mode
                        else f"{stem}{ts}.{ext}")
                preview.setText(f"Preview:  {name}")
            except Exception:
                preview.setText("Preview:  (invalid format)")
        fmt_edit.textChanged.connect(_upd)
        _upd()

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        lay.addWidget(btns)

        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._custom_ts_format = fmt_edit.text()
            QSettings(ORG_NAME, APP_NAME).setValue("custom_ts_format",
                                                    self._custom_ts_format)
            self._prev_ts_idx = 3
            self._rebuild_outpath()
        else:
            self.ts_combo.blockSignals(True)
            self.ts_combo.setCurrentIndex(self._prev_ts_idx)
            self.ts_combo.blockSignals(False)
            self._rebuild_outpath()

    def _on_indiv_toggled(self, checked):
        if checked:
            self.out_path.setEnabled(True)
            self.out_path.clear()
            self.out_path.setPlaceholderText(
                "Optional: choose output folder (blank = save next to each source)"
            )
            self._btn_browse.setEnabled(True)
            self._btn_browse.setText("Output Folder…")
            self._btn_browse.setIcon(QIcon.fromTheme("folder"))
        else:
            self._user_edited_path = False
            self.out_path.setPlaceholderText("Choose output path…")
            self._btn_browse.setText("Browse…")
            self._btn_browse.setIcon(QIcon.fromTheme("document-save-as"))
            self._rebuild_outpath()

    def _on_files_changed(self):
        if not self._user_edited_path:
            self._auto_populate()

    def _auto_populate(self):
        files = [self.file_list.item(i).text() for i in range(self.file_list.count())]
        if not files:
            return
        first = Path(files[0])
        self._base_dir = str(first.parent)
        if len(files) == 1:
            self._base_stem = first.name if first.is_dir() else self._strip_ext(first.name)
        else:
            parents = {str(Path(f).parent) for f in files}
            self._base_stem = Path(files[0]).parent.name if len(parents) == 1 else "archive"
        self._rebuild_outpath()

    @staticmethod
    def _strip_ext(name):
        """Strip known archive extension(s) from a filename stem."""
        for ext in sorted(FORMATS.values(), key=len, reverse=True):
            if name.endswith(f".{ext}"):
                return name[:-(len(ext) + 1)]
        return Path(name).stem

    def _rebuild_outpath(self, _=None):
        ext = FORMATS[self.fmt.currentText()]
        ts = self.ts_combo.currentIndex()
        if ts == 1:
            suffix = datetime.now().strftime("_%Y-%m-%d")
        elif ts == 2:
            suffix = datetime.now().strftime("_%Y-%m-%d_%H-%M")
        elif ts == 3 and self._custom_ts_format:
            try:
                suffix = datetime.now().strftime(self._custom_ts_format)
            except Exception:
                suffix = ""
        else:
            suffix = ""
        prefix_mode = self.ts_pos.currentIndex() == 1
        if self._base_stem:
            name = (f"{suffix}{self._base_stem}.{ext}" if prefix_mode
                    else f"{self._base_stem}{suffix}.{ext}")
            self.out_path.setText(str(Path(self._base_dir) / name))
        else:
            cur = self.out_path.text().strip()
            if not cur:
                return
            p = Path(cur)
            stem = self._strip_ext(p.name)
            name = (f"{suffix}{stem}.{ext}" if prefix_mode
                    else f"{stem}{suffix}.{ext}")
            self.out_path.setText(str(p.parent / name))

    def _add_files(self):
        start = self._base_dir or str(Path.home())
        files, _ = QFileDialog.getOpenFileNames(self, "Add Files", start)
        for f in files:
            if not self.file_list.findItems(f, Qt.MatchFlag.MatchExactly):
                self.file_list.addItem(f)

    def _add_folder(self):
        start = self._base_dir or str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "Add Folder", start)
        if folder and not self.file_list.findItems(folder, Qt.MatchFlag.MatchExactly):
            self.file_list.addItem(folder)

    def _remove_sel(self):
        for item in self.file_list.selectedItems():
            self.file_list.takeItem(self.file_list.row(item))

    def _update_lvl(self, v):
        label = COMPRESS_LABELS.get(v, f"Level {v}")
        self.lvl_lbl.setText(f"{label} ({v})")

    def _validate(self):
        if not self.indiv_each.isChecked() and not self.out_path.text().strip():
            QMessageBox.warning(self, "Missing Output", "Please specify an output path.")
            return
        if self.file_list.count() == 0:
            QMessageBox.warning(self, "No Files", "Add at least one file or folder.")
            return

        out = self.out_path.text().strip()
        # Only check for existing archive file in normal (non-individual) mode.
        # In individual mode the per-file archive paths aren't known yet.
        if not self.indiv_each.isChecked() and os.path.exists(out):
            mb = QMessageBox(self)
            mb.setWindowTitle("Archive Already Exists")
            mb.setText(f"\u201c{Path(out).name}\u201d already exists.\n\nWhat would you like to do?")
            mb.setIcon(QMessageBox.Icon.Question)
            btn_add      = mb.addButton("Add to Archive",     QMessageBox.ButtonRole.AcceptRole)
            btn_overwrite= mb.addButton("Overwrite",          QMessageBox.ButtonRole.DestructiveRole)
            btn_cancel   = mb.addButton("Cancel",             QMessageBox.ButtonRole.RejectRole)
            mb.setDefaultButton(btn_add)
            mb.exec()
            clicked = mb.clickedButton()
            if clicked == btn_cancel or clicked is None:
                return
            if clicked == btn_overwrite:
                try:
                    # A split archive has no file at the base name at all — its
                    # parts are out.001, out.002…  Removing only "out" left the
                    # old parts on disk to be mixed in with the new set.
                    for _part in _archive_part_paths(out):
                        os.remove(_part)
                except Exception as e:
                    QMessageBox.critical(self, "Error", f"Could not remove existing archive:\n{e}")
                    return
            # btn_add: leave the file alone — 7z 'a' will append by default

        if self.delete_src.isChecked():
            answer = QMessageBox.question(
                self, "Confirm Deletion",
                "Source files will be permanently deleted after archiving.\n"
                "Are you sure you want to continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        # Persist compression settings for next session
        s = QSettings(ORG_NAME, APP_NAME)
        s.setValue("compress_level",       self.level.value())
        s.setValue("compress_solid",       self.solid.isChecked())
        s.setValue("compress_excl_hidden", self.excl_hidden.isChecked())
        self.accept()

    def get_params(self):
        return {
            "archive":          self.out_path.text().strip(),
            "sources":          [self.file_list.item(i).text()
                                 for i in range(self.file_list.count())],
            "level":            self.level.value(),
            "password":         self.password.text(),
            "split_size":       self.split.value(),
            "split_unit":       self.split_unit.currentText(),
            "solid":            self.solid.isChecked(),
            "delete_src":       self.delete_src.isChecked(),
            "excl_hidden":      self.excl_hidden.isChecked(),
            "individual":       self.indiv_each.isChecked(),
            "indiv_out_dir":    self.out_path.text().strip() if self.indiv_each.isChecked() else "",
            "ext":              FORMATS[self.fmt.currentText()],
            "ts_idx":           self.ts_combo.currentIndex(),
            "ts_prefix":        self.ts_pos.currentIndex() == 1,
            "custom_ts_format": self._custom_ts_format,
        }


# ═════════════════════════════════════════════════════
#  Extract Dialog
# ═════════════════════════════════════════════════════

class ExtractDialog(QDialog):
    def __init__(self, parent=None, archive_path="", default_subfolder=True):
        super().__init__(parent)
        self.setWindowTitle("Extract Archive")
        self.setMinimumWidth(500)
        self._arc              = archive_path
        self._default_subfolder = default_subfolder
        self._build()

    def _build(self):
        _s  = QSettings(ORG_NAME, APP_NAME)
        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        lay.setContentsMargins(16, 16, 16, 16)
        form = QFormLayout()

        dest_row = QHBoxLayout()
        self.dest = QLineEdit()
        self.dest.setText(str(Path(self._arc).parent) if self._arc else str(Path.home()))
        btn = QPushButton(QIcon.fromTheme("folder"), "Browse…")
        btn.clicked.connect(self._browse)
        dest_row.addWidget(self.dest)
        dest_row.addWidget(btn)
        form.addRow("Extract to:", dest_row)

        self.subfolder = QCheckBox("Create subfolder using archive name")
        # The saved value is the user's standing preference; _default_subfolder
        # is a per-archive heuristic that can only suppress it, never set it.
        self._subfolder_pref = _s.value("extract_subfolder", True, type=bool)
        self._subfolder_computed = self._default_subfolder and self._subfolder_pref
        self.subfolder.setChecked(self._subfolder_computed)
        form.addRow("", self.subfolder)

        self.preserve = QCheckBox("Preserve full directory paths")
        self.preserve.setChecked(_s.value("extract_preserve", True, type=bool))
        form.addRow("", self.preserve)

        self.overwrite = QCheckBox("Overwrite existing files without asking")
        self.overwrite.setChecked(_s.value("extract_overwrite", True, type=bool))
        form.addRow("", self.overwrite)

        pw_row = QHBoxLayout()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("Only if archive is encrypted")
        pw_show = QPushButton("Show")
        pw_show.setCheckable(True)
        pw_show.setFixedWidth(54)
        pw_show.toggled.connect(
            lambda v: self.password.setEchoMode(
                QLineEdit.EchoMode.Normal if v else QLineEdit.EchoMode.Password
            )
        )
        pw_row.addWidget(self.password)
        pw_row.addWidget(pw_show)
        form.addRow("Password:", pw_row)

        lay.addLayout(form)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("Extract")
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, "Extract To", self.dest.text())
        if path:
            self.dest.setText(path)

    @staticmethod
    def _arc_stem(arc_path):
        """Return clean stem for a (possibly multi-volume) archive path."""
        name = Path(arc_path).name
        m = _MV_NUMBERED.match(name)
        if m:
            stem = Path(m.group(1)).stem
        else:
            m2 = _MV_PART_RAR.match(name)
            if m2:
                stem = m2.group(1)
            else:
                stem = Path(arc_path).stem
        if stem.endswith(".tar"):
            stem = stem[:-4]
        return stem

    def get_params(self):
        # Persist extract settings for next session
        s = QSettings(ORG_NAME, APP_NAME)
        # Persist only a real user change.  Writing back the computed value meant
        # one self-contained archive turned the preference off permanently.
        if self.subfolder.isChecked() != self._subfolder_computed:
            s.setValue("extract_subfolder", self.subfolder.isChecked())
        s.setValue("extract_preserve",  self.preserve.isChecked())
        s.setValue("extract_overwrite", self.overwrite.isChecked())

        dest = self.dest.text().strip()
        if self.subfolder.isChecked() and self._arc:
            dest = str(Path(dest) / self._arc_stem(self._arc))
        return {
            "dest":      dest,
            "password":  self.password.text(),
            "mode":      "x" if self.preserve.isChecked() else "e",
            "overwrite": self.overwrite.isChecked(),
        }


# ═════════════════════════════════════════════════════
#  Archive Info Panel
# ═════════════════════════════════════════════════════

class InfoPanel(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setMaximumWidth(200)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 10, 8, 8)
        lay.setSpacing(6)

        title = QLabel("Archive Info")
        title.setFont(QFont(title.font().family(), -1, QFont.Weight.Bold))
        lay.addWidget(title)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(sep)

        # "Volumes" must be in this tuple: update() can only write into a row
        # that was built here, so a multi-volume archive's part count used to be
        # computed and then silently dropped.
        self._labels = {}
        self._keys   = {}
        for key in ("Format", "Files", "Folders", "Size", "Packed",
                    "Volumes", "Ratio", "Path"):
            lbl_key = QLabel(f"{key}:")
            lbl_key.setStyleSheet("color: palette(mid); font-size: 8pt;")
            lbl_val = QLabel("—")
            lbl_val.setWordWrap(True)
            lbl_val.setStyleSheet("font-size: 8pt;")
            lay.addWidget(lbl_key)
            lay.addWidget(lbl_val)
            self._labels[key] = lbl_val
            self._keys[key]   = lbl_key

        # Rows that only apply to some archives, hidden unless update() sets them
        self._optional = ("Volumes",)
        self._show_optional(False)

        lay.addStretch()
        self.setVisible(False)   # hidden until an archive is loaded

    def _show_optional(self, visible, keys=None):
        for k in (keys if keys is not None else self._optional):
            self._keys[k].setVisible(visible)
            self._labels[k].setVisible(visible)

    def update(self, data: dict):
        for k, v in data.items():
            if k in self._labels:
                self._labels[k].setText(str(v))
        for k in self._optional:
            self._show_optional(k in data, keys=(k,))

    def clear(self):
        for lbl in self._labels.values():
            lbl.setText("—")
        self._show_optional(False)
        self.setVisible(False)


# ═════════════════════════════════════════════════════
#  Filesystem Browser  (shown when no archive is open)
# ═════════════════════════════════════════════════════

class FilesystemBrowser(QWidget):
    """
    Two-panel filesystem browser:
      Left  — collapsible directory tree
      Right — contents of the selected folder (multi-selectable)
    Signals:
      selection_changed(count) — right-panel selection changed
      navigated()              — directory changed (so main window can refresh nav buttons)
    """
    selection_changed     = pyqtSignal(int)
    navigated             = pyqtSignal()
    open_archive_requested = pyqtSignal(str)    # open a specific archive file
    archive_requested      = pyqtSignal(list)   # create archive from these paths

    def __init__(self, parent=None):
        super().__init__(parent)
        self._history = []   # back stack
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # ── Location bar ──────────────────────────────
        loc_frame = QFrame()
        loc_frame.setFrameShape(QFrame.Shape.StyledPanel)
        loc_frame.setMaximumHeight(34)
        loc_lay = QHBoxLayout(loc_frame)
        loc_lay.setContentsMargins(6, 4, 6, 4)
        loc_lay.setSpacing(4)
        loc_lay.addWidget(QLabel("📂"))
        self._loc_edit = QLineEdit(str(Path.home()))
        self._loc_edit.setPlaceholderText("Type a path and press Enter…")
        self._loc_edit.returnPressed.connect(self._go_typed)
        loc_lay.addWidget(self._loc_edit, 1)
        lay.addWidget(loc_frame)

        # ── Split: folder tree | content view ─────────
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)

        # Left: directory tree (folders only)
        self._dir_model = QFileSystemModel()
        self._dir_model.setRootPath(QDir.rootPath())
        self._dir_model.setFilter(QDir.Filter.Dirs | QDir.Filter.NoDotAndDotDot)

        self._dir_tree = QTreeView()
        self._dir_tree.setModel(self._dir_model)
        self._dir_tree.setRootIndex(self._dir_model.index(QDir.rootPath()))
        for col in range(1, self._dir_model.columnCount()):
            self._dir_tree.hideColumn(col)
        self._dir_tree.setHeaderHidden(True)
        self._dir_tree.setAnimated(True)
        self._dir_tree.setMinimumWidth(150)
        self._dir_tree.setMaximumWidth(300)
        self._dir_tree.selectionModel().currentChanged.connect(self._on_dir_changed)

        # Right: content view (everything in current folder)
        self._content_model = QFileSystemModel()
        self._content_model.setFilter(
            QDir.Filter.AllEntries | QDir.Filter.NoDotAndDotDot
        )
        self._content_model.setRootPath(str(Path.home()))

        self._content_view = QTreeView()
        self._content_view.setModel(self._content_model)
        self._content_view.setSortingEnabled(True)
        self._content_view.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self._content_view.setAlternatingRowColors(True)
        self._content_view.setUniformRowHeights(True)
        self._content_view.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self._content_view.activated.connect(self._on_content_activated)
        self._content_view.selectionModel().selectionChanged.connect(
            lambda: self.selection_changed.emit(len(self.get_selected_paths()))
        )
        self._content_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._content_view.customContextMenuRequested.connect(self._show_context_menu)
        hdr = self._content_view.header()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)

        split.addWidget(self._dir_tree)
        split.addWidget(self._content_view)
        split.setSizes([210, 590])
        lay.addWidget(split, 1)

        # Navigate to home on startup
        self._navigate_to(str(Path.home()))

    # ── Navigation ────────────────────────────────────

    def _navigate_to(self, path, push_history=True):
        if not os.path.isdir(path):
            return
        prev = self._loc_edit.text()
        if push_history and prev and prev != path:
            self._history.append(prev)
        self._loc_edit.setText(path)
        self._content_model.setRootPath(path)
        self._content_view.setRootIndex(self._content_model.index(path))
        self._content_view.clearSelection()
        # Sync dir tree without triggering _on_dir_changed loop
        idx = self._dir_model.index(path)
        if idx.isValid():
            # _on_dir_changed is connected to the *selection model*, so blocking
            # the view's own signals never suppressed the re-entrant call.
            sel = self._dir_tree.selectionModel()
            sel.blockSignals(True)
            self._dir_tree.setCurrentIndex(idx)
            self._dir_tree.scrollTo(idx)
            self._dir_tree.expand(idx)
            sel.blockSignals(False)
        self.navigated.emit()

    def _on_dir_changed(self, current, _prev):
        path = self._dir_model.filePath(current)
        if path and path != self._loc_edit.text():
            self._navigate_to(path)

    def _on_content_activated(self, index):
        path = self._content_model.filePath(index)
        if os.path.isdir(path):
            self._navigate_to(path)
        elif self._is_archive(path):
            self.open_archive_requested.emit(path)

    def _go_typed(self):
        self._navigate_to(self._loc_edit.text().strip())

    # ── Public API ────────────────────────────────────

    def _show_context_menu(self, pos):
        index = self._content_view.indexAt(pos)
        clicked_path = self._content_model.filePath(index) if index.isValid() else ""
        selected = self.get_selected_paths()

        menu = QMenu(self)

        # ── Item-specific actions at top ───────────────
        if clicked_path:
            if os.path.isdir(clicked_path):
                a_enter = menu.addAction(QIcon.fromTheme("folder-open"), "Open Folder")
                a_enter.triggered.connect(lambda: self._navigate_to(clicked_path))
            elif self._is_archive(clicked_path):
                a_open = menu.addAction(QIcon.fromTheme("archive-manager"),
                                        "Open Archive in SquatchZip")
                a_open.triggered.connect(
                    lambda: self.open_archive_requested.emit(clicked_path)
                )
            menu.addSeparator()

        # ── Archive actions ────────────────────────────
        targets = selected if selected else (
            [clicked_path] if clicked_path else []
        )
        if targets:
            label = (f"Archive {len(targets)} Selected Item(s)…"
                     if len(targets) > 1 else
                     f'Archive \u201c{Path(targets[0]).name}\u201d\u2026')
            a_arc = menu.addAction(QIcon.fromTheme("document-new"), label)
            a_arc.triggered.connect(lambda: self.archive_requested.emit(targets))

            a_arc_del = menu.addAction(QIcon.fromTheme("edit-delete"),
                                       "Archive and Delete Originals…")
            a_arc_del.triggered.connect(
                lambda: self.archive_requested.emit(["--delete"] + targets)
            )
        else:
            a_arc = menu.addAction(QIcon.fromTheme("document-new"), "Archive Selection…")
            a_arc.setEnabled(False)

        menu.exec(QCursor.pos())

    @staticmethod
    def _is_archive(path):
        EXTS = (".7z", ".zip", ".tar", ".gz", ".bz2", ".xz",
                ".tar.gz", ".tar.bz2", ".tar.xz", ".rar", ".iso", ".tgz",
                ".cab", ".wim")
        return any(path.lower().endswith(e) for e in EXTS)

    def go_back(self):
        if self._history:
            prev = self._history.pop()
            self._navigate_to(prev, push_history=False)

    def go_up(self):
        current = Path(self._loc_edit.text())
        parent = current.parent
        if parent != current:
            self._navigate_to(str(parent))

    def can_go_back(self):
        return bool(self._history)

    def can_go_up(self):
        current = Path(self._loc_edit.text())
        return current.parent != current

    def get_selected_paths(self):
        """Return list of selected filesystem paths (files and/or folders)."""
        paths = []
        for idx in self._content_view.selectionModel().selectedRows(0):
            p = self._content_model.filePath(idx)
            if p:
                paths.append(p)
        return paths

    def current_dir(self):
        return self._loc_edit.text()


# Patterns for multi-volume archive detection
_MV_NUMBERED = re.compile(r'^(.+\.(7z|zip|rar|tar|bz2|xz))\.\d{3}$', re.IGNORECASE)
_MV_PART_RAR  = re.compile(r'^(.+)\.part(\d+)\.rar$', re.IGNORECASE)


def _multivolume_info(path):
    """
    If path is part of a multi-volume archive, return:
        (first_part, total_parts, missing_first)
    Otherwise return None.
    """
    p      = Path(path)
    name   = p.name
    parent = p.parent

    m = _MV_NUMBERED.match(name)
    if m:
        base  = m.group(1)
        parts = sorted(parent.glob(f"{glob.escape(base)}.*"),
                       key=lambda x: x.name)
        parts = [x for x in parts if re.search(r'\.\d{3}$', x.name)]
        first = parent / f"{base}.001"
        return str(first), len(parts), not first.exists()

    m = _MV_PART_RAR.match(name)
    if m:
        base      = m.group(1)
        num_len   = len(m.group(2))
        parts     = sorted(parent.glob(f"{glob.escape(base)}.part*.rar"),
                           key=lambda x: x.name)
        first_num = "1".zfill(num_len)
        first     = parent / f"{base}.part{first_num}.rar"
        return str(first), len(parts), not first.exists()

    return None




_7z_info_cache: str | None = None

def _get_7z_info() -> str:
    """Return cached output of `7z i` (version + capability info)."""
    global _7z_info_cache
    if _7z_info_cache is None:
        try:
            result = subprocess.run(["7z", "i"], capture_output=True, text=True, timeout=5)
            _7z_info_cache = (result.stdout + result.stderr).strip()
        except Exception as e:
            _7z_info_cache = f"Could not retrieve 7-Zip info: {e}"
    return _7z_info_cache


# ═════════════════════════════════════════════════════
#  Archive tree widget — drag-out support
# ═════════════════════════════════════════════════════

# ─────────────────────────────────────────────────────────────
#  Archive virtual model + view
# ─────────────────────────────────────────────────────────────

ArchiveRow = namedtuple(
    "ArchiveRow",
    ["name", "is_dir", "path", "raw_size",
     "col_size", "col_packed", "col_ratio", "col_modified", "col_method",
     "icon"],
)


class ArchiveModel(QAbstractItemModel):
    """Flat virtual model for the current directory level of an open archive.

    Stores a list of ArchiveRow namedtuples; QTreeView asks for data only for
    visible rows — O(visible) rendering instead of O(n) for the whole archive.
    """

    _HEADERS   = ["Name", "Size", "Packed", "Ratio", "Modified", "Method"]
    _COL_ATTRS = ["name", "col_size", "col_packed", "col_ratio", "col_modified", "col_method"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list = []

    def reset_data(self, rows):
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def row_data(self, row):
        """Return the ArchiveRow at *row*, or None if out of range."""
        return self._rows[row] if 0 <= row < len(self._rows) else None

    # ── QAbstractItemModel required interface ─────────────────────

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()):
        return 6

    def index(self, row, col, parent=QModelIndex()):
        if parent.isValid() or not (0 <= row < len(self._rows)) or not (0 <= col < 6):
            return QModelIndex()
        return self.createIndex(row, col)

    def parent(self, child=QModelIndex()):   # noqa: A003
        return QModelIndex()

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        f = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if not self._rows[index.row()].is_dir:
            f |= Qt.ItemFlag.ItemIsDragEnabled
        return f

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if (orientation == Qt.Orientation.Horizontal
                and role == Qt.ItemDataRole.DisplayRole
                and 0 <= section < 6):
            return self._HEADERS[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        col = index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            return getattr(row, self._COL_ATTRS[col], "") or ""

        if role == Qt.ItemDataRole.DecorationRole and col == 0:
            return row.icon

        if role == Qt.ItemDataRole.UserRole:
            return ("dir" if row.is_dir else "file", row.path)

        if role == Qt.ItemDataRole.UserRole + 1:
            return row.raw_size

        return None

    def sort(self, column, order=Qt.SortOrder.AscendingOrder):
        if not self._rows:
            return
        self.layoutAboutToBeChanged.emit()
        reverse = (order == Qt.SortOrder.DescendingOrder)

        def value_key(r):
            if column in (1, 2):                     # Size / Packed — numeric
                return r.raw_size
            attr = self._COL_ATTRS[column] if column < 6 else "name"
            return (getattr(r, attr, "") or "").lower()

        # Two stable passes.  Reversing a single (type_rank, value) tuple also
        # reversed type_rank, which pushed folders *below* files in descending
        # order — the opposite of "dirs always before files".
        self._rows.sort(key=value_key, reverse=reverse)
        self._rows.sort(key=lambda r: 0 if r.is_dir else 1)
        self.layoutChanged.emit()


class ArchiveView(QTreeView):
    """QTreeView backed by ArchiveModel.  Overrides startDrag to extract
    selected files synchronously and hand them to the desktop as uri-list."""

    def __init__(self, model, parent=None):
        super().__init__(parent)
        self._arc_model = model
        self.setModel(model)
        self.archive_path = None          # kept in sync by MainWindow
        self.password     = ""            # likewise; drag-out runs its own 7z
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)

    def startDrag(self, supportedActions):
        from PyQt6.QtGui import QDrag
        from PyQt6.QtCore import QMimeData, QUrl

        if not self.archive_path:
            super().startDrag(supportedActions)
            return

        # Collect unique file rows from the multi-column selection
        seen, file_paths = set(), []
        for idx in self.selectedIndexes():
            r = idx.row()
            if r in seen:
                continue
            seen.add(r)
            row = self._arc_model.row_data(r)
            if row and not row.is_dir:
                file_paths.append(row.path.rstrip("/"))

        if not file_paths:
            super().startDrag(supportedActions)
            return

        # Extract synchronously to a temp dir — acceptable latency for small batches.
        # 'x', not 'e': a flat extract collapses a/notes.txt and b/notes.txt onto
        # the same name and the drag silently hands over only one of them.
        tmp_dir = tempfile.mkdtemp(prefix="squatchzip_drag_")
        cmd = ["7z", "x", self.archive_path, f"-o{tmp_dir}", "-y"]
        if self.password:
            cmd += [f"-p{self.password}"]
        cmd += file_paths
        # stdin=DEVNULL: this runs on the GUI thread, so a password prompt here
        # would freeze the whole window with no dialog to cancel.
        result = subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL)
        if result.returncode != 0:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return

        urls = []
        for p in file_paths:
            local = Path(tmp_dir) / p          # 'x' preserved the internal path
            if local.exists():
                urls.append(QUrl.fromLocalFile(str(local)))

        if not urls:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return

        # The drop target reads these files after exec() returns, so the dir has
        # to outlive the drag.  MainWindow removes it when the window closes.
        win = self.window()
        if hasattr(win, "_temp_dirs"):
            win._temp_dirs.add(tmp_dir)
        else:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return

        mime = QMimeData()
        mime.setUrls(urls)
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction)


# ═════════════════════════════════════════════════════
#  Archive Tab  — per-archive state + UI bundle
# ═════════════════════════════════════════════════════

class ArchiveTab(QWidget):
    """Bundles all per-archive data state and view widgets for one tab."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.archive_path = None
        self.mv_info      = None
        self.preview_dir  = None   # reused scratch dir for the preview pane
        self.all_entries  = []
        self.current_path = ""
        self.nav_history  = []
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # ── Archive path bar ──────────────────────────
        self.path_bar = QFrame()
        self.path_bar.setFrameShape(QFrame.Shape.StyledPanel)
        self.path_bar.setMaximumHeight(34)
        pb_lay = QHBoxLayout(self.path_bar)
        pb_lay.setContentsMargins(8, 4, 8, 4)
        pb_lay.setSpacing(6)
        self.path_lbl = QLabel()
        pb_lay.addWidget(QLabel("📦"))
        pb_lay.addWidget(self.path_lbl, 1)
        self.path_bar.setVisible(False)
        lay.addWidget(self.path_bar)

        # ── Archive breadcrumb bar ────────────────────
        self.loc_bar = QFrame()
        self.loc_bar.setFrameShape(QFrame.Shape.StyledPanel)
        self.loc_bar.setMaximumHeight(28)
        loc_lay = QHBoxLayout(self.loc_bar)
        loc_lay.setContentsMargins(8, 2, 8, 2)
        self.loc_lbl = QLabel()
        self.loc_lbl.setStyleSheet("font-family: monospace; font-size: 9pt;")
        loc_lay.addWidget(self.loc_lbl, 1)
        self.loc_bar.setVisible(False)
        lay.addWidget(self.loc_bar)

        # ── Archive contents area ─────────────────────
        arc_area = QWidget()
        arc_lay = QHBoxLayout(arc_area)
        arc_lay.setContentsMargins(0, 0, 0, 0)
        arc_lay.setSpacing(0)

        self.info = InfoPanel()
        arc_lay.addWidget(self.info)

        self.arc_model = ArchiveModel()
        self.tree = ArchiveView(self.arc_model)
        hdr = self.tree.header()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in range(1, 6):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSortingEnabled(True)
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setIconSize(QSize(20, 20))
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        # ── Preview pane ──────────────────────────────
        self.preview_pane = QWidget()
        self.preview_pane.setMinimumWidth(180)
        prev_lay = QVBoxLayout(self.preview_pane)
        prev_lay.setContentsMargins(4, 4, 4, 4)
        prev_lay.setSpacing(4)
        prev_hdr = QLabel("Preview")
        prev_hdr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        f = prev_hdr.font(); f.setBold(True); prev_hdr.setFont(f)
        prev_lay.addWidget(prev_hdr)
        self.preview_stack = QStackedWidget()
        self.preview_placeholder = QLabel("Select a file to preview")
        self.preview_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_placeholder.setWordWrap(True)
        self.preview_stack.addWidget(self.preview_placeholder)
        self.preview_img = QLabel()
        self.preview_img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_stack.addWidget(self.preview_img)
        self.preview_txt = QTextEdit()
        self.preview_txt.setReadOnly(True)
        self.preview_txt.setFont(QFont("Monospace", 9))
        self.preview_stack.addWidget(self.preview_txt)
        prev_lay.addWidget(self.preview_stack, 1)
        self.preview_pane.setVisible(False)

        tree_split = QSplitter(Qt.Orientation.Horizontal)
        tree_split.addWidget(self.tree)
        tree_split.addWidget(self.preview_pane)
        tree_split.setStretchFactor(0, 2)
        tree_split.setStretchFactor(1, 1)
        arc_lay.addWidget(tree_split, 1)
        lay.addWidget(arc_area, 1)

        # ── Preview debounce timer ────────────────────
        self.preview_timer = QTimer(self)
        self.preview_timer.setSingleShot(True)
        self.preview_timer.setInterval(300)


# ═════════════════════════════════════════════════════
#  Main Window
# ═════════════════════════════════════════════════════

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(860, 540)
        self._settings          = QSettings(ORG_NAME, APP_NAME)
        self._session_passwords = {}   # archive_path → password, cleared on app exit
        # Running QThreads.  A single self._worker attribute was not enough:
        # starting a second operation rebound it and the first thread could be
        # garbage-collected while still running, aborting the process.
        self._workers = set()
        self._temp_dirs = set()        # drag/open scratch dirs, removed on exit

        geom = self._settings.value("geometry")
        if geom:
            self.restoreGeometry(geom)
        else:
            self.resize(1040, 640)

        self._build_ui()
        self._build_menus()
        self._build_toolbar()
        self.setAcceptDrops(True)
        self._refresh_actions()
        self._update_nav_actions()

    def closeEvent(self, ev):
        self._settings.setValue("geometry", self.saveGeometry())
        # Drag-out and open-with scratch dirs have to outlive the operation that
        # made them (the drop target / launched app still reads from them), so
        # this is the first safe point to remove them.
        for d in self._temp_dirs:
            shutil.rmtree(d, ignore_errors=True)
        self._temp_dirs.clear()
        super().closeEvent(ev)

    def current_tab(self):
        w = self._tab_widget.currentWidget()
        return w if isinstance(w, ArchiveTab) else None

    def _new_tab(self):
        tab = ArchiveTab()
        tab.tree.customContextMenuRequested.connect(self._ctx_menu)
        tab.tree.activated.connect(self._on_item_activated)
        tab.tree.selectionModel().currentChanged.connect(self._schedule_preview)
        tab.preview_timer.timeout.connect(self._update_preview)
        idx = self._tab_widget.addTab(tab, "New Tab")
        self._tab_widget.setCurrentIndex(idx)
        return tab

    def _close_tab(self, index):
        if index == 0:   # browser tab — permanent (its close button is removed
                         # in __init__, so this is only a belt-and-braces guard)
            return
        tab = self._tab_widget.widget(index)
        if isinstance(tab, ArchiveTab):
            tab.preview_timer.stop()
            if tab.preview_dir:
                shutil.rmtree(tab.preview_dir, ignore_errors=True)
                tab.preview_dir = None
        self._tab_widget.removeTab(index)
        if self._tab_widget.count() == 1:   # only browser tab remains
            self._tab_widget.setCurrentIndex(0)
        self._refresh_actions()
        self._update_nav_actions()

    def _on_tab_changed(self, index):
        if index == 0:   # browser tab
            self.setWindowTitle(APP_NAME)
            self._sb_left.setText("Ready — browse files on the left, then Ctrl+N to archive")
            self._sb_right.setText("")
            self._refresh_actions()
            self._update_nav_actions()
            return
        tab = self._tab_widget.widget(index)
        if isinstance(tab, ArchiveTab) and tab.archive_path:
            self.setWindowTitle(f"{Path(tab.archive_path).name} — {APP_NAME}")
            tab.preview_pane.setVisible(self._a_show_preview.isChecked())
            tab.info.setVisible(self._a_show_info.isChecked())
            self._show_level(tab.current_path, push_history=False)
        else:
            self.setWindowTitle(APP_NAME)
        self._refresh_actions()
        self._update_nav_actions()

    # ── Layout ────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # QTabWidget: tab 0 = filesystem browser (permanent), tabs 1+ = archive views
        self._tab_widget = QTabWidget()
        self._tab_widget.setTabsClosable(True)
        self._tab_widget.setMovable(True)
        self._tab_widget.tabCloseRequested.connect(self._close_tab)
        # currentChanged connected after status bar is built (avoids early signal during addTab)

        # Tab 0 — Filesystem browser (permanent, no close button)
        self._fs_browser = FilesystemBrowser()
        self._fs_browser.selection_changed.connect(self._on_fs_selection_changed)
        self._fs_browser.navigated.connect(self._update_nav_actions)
        self._fs_browser.open_archive_requested.connect(self._open_path)
        self._fs_browser.archive_requested.connect(self._handle_fs_archive_request)
        self._tab_widget.addTab(self._fs_browser, QIcon.fromTheme("folder"), "Browse")
        # _close_tab refuses to close tab 0, so leaving a close button on it just
        # gave the user a control that does nothing.
        for _side in (QTabBar.ButtonPosition.RightSide, QTabBar.ButtonPosition.LeftSide):
            self._tab_widget.tabBar().setTabButton(0, _side, None)

        root.addWidget(self._tab_widget, 1)

        # ── Status bar ─────────────────────────────────
        sb = QStatusBar()
        self.setStatusBar(sb)
        self._sb_left  = QLabel("Ready — browse files on the left, then Ctrl+N to archive")
        self._sb_right = QLabel("")
        sb.addWidget(self._sb_left, 1)
        sb.addPermanentWidget(self._sb_right)

        # Connect after status bar exists so _on_tab_changed can safely access _sb_left
        self._tab_widget.currentChanged.connect(self._on_tab_changed)

    def _build_menus(self):
        mb = self.menuBar()

        # File
        fm = mb.addMenu("&File")
        self._a_open  = QAction(QIcon.fromTheme("document-open"),    "&Open Archive…", self, shortcut="Ctrl+O")
        self._a_new   = QAction(QIcon.fromTheme("document-new"),     "&New Archive…",  self, shortcut="Ctrl+N")
        self._a_close = QAction(QIcon.fromTheme("window-close"),     "&Close Tab",     self, shortcut="Ctrl+W")
        self._a_quit  = QAction(QIcon.fromTheme("application-exit"), "&Quit",          self, shortcut="Ctrl+Q")
        self._a_open.triggered.connect(self._open)
        self._a_new.triggered.connect(self._new)
        self._a_close.triggered.connect(self._close_archive)
        self._a_quit.triggered.connect(self.close)
        for a in (self._a_open, self._a_new, None, self._a_close, None, self._a_quit):
            fm.addAction(a) if a else fm.addSeparator()

        # Archive
        am = mb.addMenu("&Archive")
        self._a_extract      = QAction(QIcon.fromTheme("archive-extract"),      "E&xtract…",              self, shortcut="Ctrl+E")
        self._a_extract_sel  = QAction(QIcon.fromTheme("archive-extract"),      "Extract &Selected…",     self, shortcut="Ctrl+Shift+S")
        self._a_extract_here = QAction(QIcon.fromTheme("archive-extract"),      "Extract &Here",          self, shortcut="Ctrl+Shift+E")
        self._a_add          = QAction(QIcon.fromTheme("list-add"),              "Add &Files…",            self, shortcut="Ctrl+A")
        self._a_add_folder   = QAction(QIcon.fromTheme("folder-new"),           "Add F&older…",           self, shortcut="Ctrl+Shift+A")
        self._a_test         = QAction(QIcon.fromTheme("tools-check-spelling"), "&Test Integrity",        self)
        self._a_rename_arc   = QAction(QIcon.fromTheme("edit-rename"),          "Re&name in Archive…",    self, shortcut="F2")
        self._a_delete_arc   = QAction(QIcon.fromTheme("edit-delete"),          "D&elete from Archive…",  self, shortcut="Delete")
        self._a_extract.triggered.connect(self._extract)
        self._a_extract_sel.triggered.connect(self._extract_selected)
        self._a_extract_here.triggered.connect(self._extract_here)
        self._a_add.triggered.connect(self._add_files_to_archive)
        self._a_add_folder.triggered.connect(self._add_folder_to_archive)
        self._a_test.triggered.connect(self._test)
        self._a_rename_arc.triggered.connect(self._rename_item)
        self._a_delete_arc.triggered.connect(self._delete_items)
        for a in (self._a_extract, self._a_extract_sel, self._a_extract_here,
                  None, self._a_add, self._a_add_folder, None,
                  self._a_rename_arc, self._a_delete_arc, None,
                  self._a_test):
            am.addAction(a) if a else am.addSeparator()

        # Go  (navigation within the open archive)
        gm = mb.addMenu("&Go")
        self._a_back = QAction(QIcon.fromTheme("go-previous"), "&Back",  self, shortcut="Alt+Left")
        self._a_up   = QAction(QIcon.fromTheme("go-up"),       "&Up",    self, shortcut="Alt+Up")
        self._a_root = QAction(QIcon.fromTheme("go-home"),     "&Root",  self, shortcut="Alt+Home")
        self._a_back.triggered.connect(self._navigate_back)
        self._a_up.triggered.connect(self._navigate_up)
        self._a_root.triggered.connect(lambda: self._show_level(""))
        gm.addAction(self._a_back)
        gm.addAction(self._a_up)
        gm.addAction(self._a_root)

        # View
        vm = mb.addMenu("&View")
        self._a_show_info = QAction("Show &Info Panel", self, checkable=True, checked=False)
        self._a_show_info.toggled.connect(
            lambda v: self.current_tab().info.setVisible(v) if self.current_tab() else None
        )
        vm.addAction(self._a_show_info)
        self._a_show_preview = QAction("Show &Preview Panel", self, checkable=True, checked=False)
        self._a_show_preview.toggled.connect(self._toggle_preview_panel)
        vm.addAction(self._a_show_preview)

        # Help
        hm = mb.addMenu("&Help")
        a_deps = QAction("Check &Dependencies", self)
        a_deps.triggered.connect(self._check_deps)
        a_7z_info = QAction("7-Zip &Version Info", self)
        a_7z_info.triggered.connect(self._sevenz_info)
        a_about = QAction(f"&About {APP_NAME}", self)
        a_about.triggered.connect(self._about)
        hm.addAction(a_deps)
        hm.addAction(a_7z_info)
        hm.addSeparator()
        hm.addAction(a_about)

    def _build_toolbar(self):
        tb = self.addToolBar("Main")
        tb.setMovable(False)
        tb.setIconSize(QSize(22, 22))
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)

        for a in (self._a_back, self._a_up, None,
                  self._a_open, self._a_new, self._a_close, None,
                  self._a_extract, self._a_add, None,
                  self._a_test):
            if a:
                tb.addAction(a)
            else:
                tb.addSeparator()

    # ── DnD ──────────────────────────────────────────

    def dragEnterEvent(self, e: QDragEnterEvent):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e: QDropEvent):
        urls = e.mimeData().urls()
        if not urls:
            return
        paths = [u.toLocalFile() for u in urls]
        tab = self.current_tab()
        if tab and tab.archive_path:
            # Archive already open — dropping onto it is a no-op
            return
        if len(paths) == 1 and self._looks_like_archive(paths[0]):
            self._open_path(paths[0])
        else:
            self._new(initial_files=paths)

    @staticmethod
    def _looks_like_archive(path):
        EXTS = (".7z", ".zip", ".tar", ".gz", ".bz2", ".xz",
                ".tar.gz", ".tar.bz2", ".tar.xz", ".rar", ".iso",
                ".tgz", ".cab", ".wim")
        pl = path.lower()
        if any(pl.endswith(e) for e in EXTS):
            return True
        # multi-volume: name.7z.001, name.part1.rar, etc.
        return bool(_MV_NUMBERED.match(Path(path).name) or
                    _MV_PART_RAR.match(Path(path).name))

    # ── Archive actions ───────────────────────────────

    def _open(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Archive", str(Path.home()), ARCHIVE_FILTER
        )
        if path:
            self._open_path(path)

    def _open_path(self, path):
        tab = self._new_tab()
        tab.mv_info = None
        mv = _multivolume_info(path)
        if mv:
            first, total, missing_first = mv
            tab.mv_info = mv
            if missing_first:
                QMessageBox.warning(
                    self, "Multi-Volume Archive",
                    f"This is part of a {total}-part archive but "
                    f"the first part was not found:\n{first}\n\n"
                    "Extraction may fail without all parts present."
                )
            elif path != first:
                QMessageBox.information(
                    self, "Multi-Volume Archive",
                    f"Opening from the first part of this {total}-part archive."
                )
                path = first
        tab.archive_path = path
        tab.tree.archive_path = path
        tab.tree.password     = self._archive_password(path)
        tab.all_entries  = []
        tab.nav_history  = []
        tab.current_path = ""
        self.setWindowTitle(f"{Path(path).name} — {APP_NAME}")
        self._tab_widget.setTabText(self._tab_widget.currentIndex(), Path(path).name)
        tab.path_lbl.setText(path)
        tab.path_bar.setVisible(True)
        self._refresh_actions()
        self._list()

    def _close_archive(self):
        idx = self._tab_widget.currentIndex()
        if idx >= 0:
            self._close_tab(idx)

    def _on_fs_selection_changed(self, count):
        if count > 0:
            self._sb_left.setText(
                f"{count} item(s) selected — press Ctrl+N to archive them"
            )
        else:
            self._sb_left.setText("Ready — browse files on the left, then Ctrl+N to archive")

    def _handle_fs_archive_request(self, paths):
        """Called from FilesystemBrowser context menu — paths may start with '--delete'."""
        pre_delete = "--delete" in paths
        actual_paths = [p for p in paths if p != "--delete"]
        self._new(initial_files=actual_paths, pre_delete=pre_delete)

    def _new(self, initial_files=None, pre_delete=False):
        # Auto-populate from the filesystem browser when in that mode
        if initial_files is None and self._tab_widget.currentIndex() == 0:
            sel = self._fs_browser.get_selected_paths()
            if sel:
                initial_files = sel
        dlg = CreateArchiveDialog(self, initial_files=initial_files or [],
                                  pre_delete=pre_delete)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        p = dlg.get_params()
        if p.pop("individual", False):
            self._new_individual(p)
        else:
            # Strip individual-mode-only keys before passing to worker
            p.pop("ext", None); p.pop("ts_idx", None)
            p.pop("ts_prefix", None); p.pop("custom_ts_format", None)
            arc = p["archive"]
            self._run_worker("create", "Creating Archive…", **p,
                             _on_success=lambda: self._open_path(arc)
                             if os.path.exists(arc) else None)

    def _new_individual(self, p):
        """Create one archive per source file/folder."""
        sources        = p["sources"]
        ext            = p.pop("ext", "7z")
        ts_idx         = p.pop("ts_idx", 0)
        ts_prefix      = p.pop("ts_prefix", False)
        custom_ts_fmt  = p.pop("custom_ts_format", "")
        indiv_out_dir  = p.pop("indiv_out_dir", "")
        p.pop("archive", None)

        if ts_idx == 1:
            suffix = datetime.now().strftime("_%Y-%m-%d")
        elif ts_idx == 2:
            suffix = datetime.now().strftime("_%Y-%m-%d_%H-%M")
        elif ts_idx == 3 and custom_ts_fmt:
            try:
                suffix = datetime.now().strftime(custom_ts_fmt)
            except Exception:
                suffix = ""
        else:
            suffix = ""

        remaining = len(sources)
        for src in sources:
            remaining -= 1
            src_path  = Path(src)
            stem      = src_path.name if src_path.is_dir() else src_path.stem
            arc_name  = (f"{suffix}{stem}.{ext}" if ts_prefix
                         else f"{stem}{suffix}.{ext}")
            out_dir   = indiv_out_dir if indiv_out_dir else str(src_path.parent)
            arc_path  = str(Path(out_dir) / arc_name)
            ok = self._run_worker(
                "create", f"Archiving {src_path.name}…",
                archive=arc_path, sources=[src],
                level=p["level"], password=p["password"],
                split_size=p["split_size"], split_unit=p["split_unit"], solid=p["solid"],
                delete_src=p["delete_src"], excl_hidden=p["excl_hidden"],
            )
            if not ok and remaining > 0:
                answer = QMessageBox.question(
                    self, "Archive Failed",
                    f"\u201c{src_path.name}\u201d could not be archived.\n\n"
                    f"{remaining} file(s) remaining.\n\n"
                    "Continue with the remaining files?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    break

    def _add_files_to_archive(self):
        tab = self.current_tab()
        if not tab or not tab.archive_path:
            return
        files, _ = QFileDialog.getOpenFileNames(
            self, "Add Files to Archive", str(Path.home())
        )
        if not files:
            return
        self._add_sources_to_archive(files)

    def _add_folder_to_archive(self):
        tab = self.current_tab()
        if not tab or not tab.archive_path:
            return
        folder = QFileDialog.getExistingDirectory(
            self, "Add Folder to Archive", str(Path.home())
        )
        if not folder:
            return
        self._add_sources_to_archive([folder])

    def _add_sources_to_archive(self, sources):
        tab = self.current_tab()
        if not tab or not tab.archive_path:
            return
        s = QSettings(ORG_NAME, APP_NAME)
        self._run_worker("create", "Adding to Archive…",
                         archive=tab.archive_path,
                         sources=sources,
                         level=int(s.value("compress_level", 5)),
                         solid=s.value("compress_solid", True, type=bool),
                         password=self._archive_password(tab.archive_path),
                         delete_src=False,
                         _on_success=self._list)

    def _archive_root_name(self):
        """The archive's single top-level entry name, or None if it has several."""
        tab = self.current_tab()
        if not tab or not tab.all_entries:
            return None
        roots = set()
        for entry in tab.all_entries:
            path = (entry.get("Path") or "").strip().replace("\\", "/").strip("/")
            if path:
                roots.add(path.split("/")[0])
        return roots.pop() if len(roots) == 1 else None

    def _should_create_subfolder(self):
        """
        Return False when the archive already has a single root-level directory
        containing all content — creating another subfolder would double-nest.
        Return True (create subfolder) in all other cases.
        """
        tab = self.current_tab()
        if not tab or not tab.all_entries:
            return True
        root_names = set()
        has_nested = False
        for entry in tab.all_entries:
            path = entry.get("Path", "").strip().replace("\\", "/").lstrip("/").rstrip("/")
            if not path:
                continue
            parts = path.split("/")
            root_names.add(parts[0])
            if len(parts) > 1:
                has_nested = True
        if len(root_names) == 1 and has_nested:
            return False   # archive is self-contained — no extra wrapper needed
        return True

    def _extract(self):
        tab = self.current_tab()
        if not tab or not tab.archive_path:
            return
        dlg = ExtractDialog(self, archive_path=tab.archive_path,
                            default_subfolder=self._should_create_subfolder())
        stored_pw = self._archive_password(tab.archive_path)
        if stored_pw:
            dlg.password.setText(stored_pw)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            p = dlg.get_params()
            arc_path = tab.archive_path
            def _save_pw():
                self._remember_password(arc_path, p["password"])
            self._run_worker("extract", "Extracting…",
                             _on_success=_save_pw,
                             archive=arc_path, **p)

    def _extract_selected(self):
        tab = self.current_tab()
        if not tab or not tab.archive_path:
            return
        selected_rows = self._selected_arc_rows()
        if not selected_rows:
            QMessageBox.information(self, "No Selection",
                                    "Select one or more files or folders first.")
            return
        paths = [r.path.rstrip("/") for r in selected_rows]
        dlg = ExtractDialog(self, archive_path=tab.archive_path,
                            default_subfolder=self._should_create_subfolder())
        stored_pw = self._archive_password(tab.archive_path)
        if stored_pw:
            dlg.password.setText(stored_pw)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            p = dlg.get_params()
            arc_path = tab.archive_path
            def _save_pw():
                self._remember_password(arc_path, p["password"])
            self._run_worker("extract", "Extracting Selected…",
                             _on_success=_save_pw,
                             archive=arc_path, paths=paths, **p)

    def _extract_here(self):
        tab = self.current_tab()
        if not tab or not tab.archive_path:
            return
        arc  = Path(tab.archive_path)
        stem = ExtractDialog._arc_stem(tab.archive_path)
        # Match the Extract dialog: an archive that already holds a single root
        # directory needs no wrapper, or "foo.7z" lands in ./foo/foo/.
        if self._should_create_subfolder():
            dest, clash = str(arc.parent / stem), stem
        else:
            # Extracting straight into the archive's own directory, so the thing
            # that can be clobbered is the archive's root entry, not the stem.
            dest  = str(arc.parent)
            clash = self._archive_root_name() or stem
        if os.path.exists(os.path.join(str(arc.parent), clash)) or (
                dest != str(arc.parent) and os.path.exists(dest)):
            answer = QMessageBox.question(
                self, "Folder Already Exists",
                f"\"{clash}\" already exists here.\n\n"
                "Existing files may be overwritten. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        stored_pw = self._archive_password(tab.archive_path)
        self._run_worker("extract", "Extracting Here…",
                         archive=tab.archive_path, dest=dest,
                         mode="x", overwrite=True,
                         password=stored_pw)

    def _test(self):
        tab = self.current_tab()
        if not tab or not tab.archive_path:
            return
        stored_pw = self._archive_password(tab.archive_path)
        self._run_worker("test", "Testing Archive…",
                         archive=tab.archive_path,
                         password=stored_pw)

    def _list(self):
        tab = self.current_tab()
        if not tab or not tab.archive_path:
            return
        self._sb_left.setText("Loading…")
        tab.arc_model.reset_data([])
        arc_path  = tab.archive_path
        stored_pw = self._archive_password(arc_path)
        w = ArchiveWorker("list", archive=arc_path, password=stored_pw)
        self._track_worker(w)
        w.listing.connect(self._store_entries)

        def on_list_done(ok, msg):
            if not ok and msg == "__ENCRYPTED__":
                pw, accepted = QInputDialog.getText(
                    self, "Password Required",
                    f"Enter password for {Path(arc_path).name}:",
                    QLineEdit.EchoMode.Password,
                )
                if accepted and pw:
                    self._remember_password(arc_path, pw)
                    self._list()
                return
            if not ok:
                QMessageBox.critical(self, "Error", msg)

        w.op_finished.connect(on_list_done)
        w.start()

    # ── Session passwords ─────────────────────────────

    def _archive_password(self, arc_path):
        """Password learned for this archive during this session, or ""."""
        return self._session_passwords.get(arc_path, "") if arc_path else ""

    def _remember_password(self, arc_path, pw):
        """Cache a working password for the session and push it onto the tab's
        view, which runs its own 7z calls for drag-out and preview."""
        if not arc_path or not pw:
            return
        self._session_passwords[arc_path] = pw
        for i in range(self._tab_widget.count()):
            t = self._tab_widget.widget(i)
            if isinstance(t, ArchiveTab) and t.archive_path == arc_path:
                t.tree.password = pw

    # ── Worker runner ─────────────────────────────────

    def _track_worker(self, worker):
        """Hold a reference until Qt itself reports the thread has finished."""
        self._workers.add(worker)
        worker.finished.connect(self._untrack_worker)   # QThread.finished

    def _untrack_worker(self):
        self._workers.discard(self.sender())

    def _run_worker(self, operation, title, _on_success=None, **kwargs):
        prog = ProgressDialog(self, title)
        w = ArchiveWorker(operation, **kwargs)
        self._track_worker(w)
        prog.set_worker(w)
        w.progress.connect(prog.append)
        w.progress_pct.connect(prog.set_progress)
        result = [None]   # filled by on_done; None means worker never reported back

        def on_done(success, message):
            result[0] = success
            prog.finish(success, message)
            self._sb_left.setText(message)
            if success and _on_success:
                _on_success()

        w.op_finished.connect(on_done)
        w.start()
        prog.exec()
        return result[0]   # True = success, False = failure

    # ── Archive navigation (file-manager style) ───────

    def _store_entries(self, files):
        """Receive flat listing from worker, display root level, update info panel."""
        tab = self.current_tab()
        if not tab:
            return
        tab.all_entries = files
        tab.nav_history.clear()
        self._show_level("", push_history=False)

        # Archive-wide stats for info panel
        total_size = total_packed = file_count = folder_count = 0
        for e in files:
            is_dir = _entry_is_dir(e)
            if not is_dir:
                total_size   += self._safe_int(e.get("Size", "0"))
                total_packed += self._safe_int(e.get("Packed Size", "0"))
                file_count   += 1
            else:
                folder_count += 1

        arc_name = Path(tab.archive_path).name.lower() if tab.archive_path else ""
        if arc_name.endswith((".tar.gz", ".tgz")):
            fmt_label = "TAR+GZ"
        elif arc_name.endswith(".tar.bz2"):
            fmt_label = "TAR+BZ2"
        elif arc_name.endswith(".tar.xz"):
            fmt_label = "TAR+XZ"
        else:
            ext = Path(arc_name).suffix.lstrip(".")
            fmt_label = ext.upper() if ext else "—"

        info = {
            "Format":  fmt_label,
            "Files":   file_count,
            "Folders": folder_count,
            "Size":    self._fmt_size(total_size),
            "Packed":  self._fmt_size(total_packed),
            "Ratio":   (f"{int(100*total_packed/total_size)}%"
                        if total_size > 0 else "—"),
            "Path":    tab.archive_path,
        }
        if tab.mv_info:
            _, total_parts, missing_first = tab.mv_info
            status = "⚠ first part missing" if missing_first else "all parts found"
            info["Volumes"] = f"{total_parts} parts ({status})"
        tab.info.update(info)

    def _get_level_items(self, arc_path):
        """
        Return [(name, is_dir, entry_or_None)] for immediate children of arc_path.
        arc_path is "" for root, or "folder/" for a subfolder.
        """
        dirs_seen = set()
        items = []

        pfx = arc_path.rstrip("/")   # "" or "folder" or "folder/sub"

        for entry in self.current_tab().all_entries:
            raw = entry.get("Path", "").strip().replace("\\", "/").lstrip("/")
            if not raw:
                continue
            is_dir_entry = _entry_is_dir(entry) or raw.endswith("/")
            raw = raw.rstrip("/")

            # Filter to entries under current path
            if pfx:
                if raw == pfx:
                    continue                        # this IS the current dir
                if not raw.startswith(pfx + "/"):
                    continue
                rel = raw[len(pfx) + 1:]           # path relative to current dir
            else:
                rel = raw

            if not rel:
                continue

            parts = rel.split("/")
            if len(parts) == 1:
                name = parts[0]
                if is_dir_entry:
                    if name not in dirs_seen:
                        dirs_seen.add(name)
                        items.append((name, True, entry))
                else:
                    items.append((name, False, entry))
            else:
                # File is inside a subdirectory → surface the directory
                dir_name = parts[0]
                if dir_name not in dirs_seen:
                    dirs_seen.add(dir_name)
                    items.append((dir_name, True, None))   # implicit/virtual dir

        return items

    def _show_level(self, arc_path, push_history=True):
        """Populate the tree with the immediate contents of arc_path."""
        tab = self.current_tab()
        if push_history and arc_path != tab.current_path:
            tab.nav_history.append(tab.current_path)

        tab.current_path = arc_path
        self._update_location_bar()
        self._update_nav_actions()

        items = self._get_level_items(arc_path)
        # Folders first, then files, both alpha-sorted
        items.sort(key=lambda x: (0 if x[1] else 1, x[0].lower()))

        visible_files = visible_dirs = 0
        model_rows = []

        for name, is_dir, entry in items:
            full_path = arc_path + name + ("/" if is_dir else "")
            if is_dir:
                arc_row = ArchiveRow(
                    name=name, is_dir=True, path=full_path, raw_size=0,
                    col_size="", col_packed="", col_ratio="",
                    col_modified="", col_method="›",
                    icon=QIcon.fromTheme("folder"),
                )
                visible_dirs += 1
            else:
                raw_size = 0
                col_size = col_packed = col_ratio = col_modified = col_method = ""
                if entry:
                    size         = self._safe_int(entry.get("Size", "0"))
                    packed       = self._safe_int(entry.get("Packed Size", "0"))
                    raw_size     = size
                    col_size     = self._fmt_size(size)
                    col_packed   = self._fmt_size(packed)
                    col_ratio    = f"{int(100*packed/size)}%" if size > 0 else "—"
                    col_modified = entry.get("Modified", "")[:16]
                    col_method   = entry.get("Method", "")
                arc_row = ArchiveRow(
                    name=name, is_dir=False, path=full_path, raw_size=raw_size,
                    col_size=col_size, col_packed=col_packed, col_ratio=col_ratio,
                    col_modified=col_modified, col_method=col_method,
                    icon=self._file_icon(name),
                )
                visible_files += 1
            model_rows.append(arc_row)

        tab.arc_model.reset_data(model_rows)

        total_label = f"{visible_files} file(s), {visible_dirs} folder(s)"
        if arc_path:
            total_label += "  —  double-click folder to enter, Alt+Left/Up to navigate"
        self._sb_left.setText(total_label)

        # Right status only meaningful at root (archive-wide totals)
        if not arc_path:
            total_size = sum(
                self._safe_int(e.get("Size", "0"))
                for e in tab.all_entries
                if not _entry_is_dir(e)
            )
            total_packed = sum(
                self._safe_int(e.get("Packed Size", "0"))
                for e in tab.all_entries
                if not _entry_is_dir(e)
            )
            self._sb_right.setText(
                f"Total: {self._fmt_size(total_size)}  "
                f"Packed: {self._fmt_size(total_packed)}"
            )

    def _on_item_activated(self, index):
        """Double-click or Enter: enter a directory, or extract-and-open a file."""
        tab = self.current_tab()
        if not tab:
            return
        row = tab.arc_model.row_data(index.row())
        if not row:
            return
        if row.is_dir:
            self._show_level(row.path)
        else:
            self._open_item_in_app(row.path)

    def _open_item_in_app(self, internal_path):
        """Extract a single archive entry to a temp dir and open it with xdg-open."""
        tab = self.current_tab()
        if not tab or not tab.archive_path:
            return
        # The launched app needs the file to outlive this call, so the directory
        # cannot be removed here — it is cleaned up when the window closes.
        tmp_dir = tempfile.mkdtemp(prefix="squatchzip_")
        self._temp_dirs.add(tmp_dir)
        file_name = Path(internal_path).name
        tmp_file  = str(Path(tmp_dir) / file_name)
        self._run_worker(
            "extract", f"Opening {file_name}…",
            archive=tab.archive_path,
            dest=tmp_dir,
            mode="e",        # flat extract — no path structure in temp dir
            overwrite=True,
            paths=[internal_path.rstrip("/")],
            password=self._archive_password(tab.archive_path),
            _on_success=lambda: subprocess.Popen(["xdg-open", tmp_file])
        )

    # ── Selection helpers ─────────────────────────────

    def _current_arc_row(self):
        """Return the ArchiveRow for the currently focused item, or None."""
        tab = self.current_tab()
        if not tab:
            return None
        idx = tab.tree.currentIndex()
        return tab.arc_model.row_data(idx.row()) if idx.isValid() else None

    def _selected_arc_rows(self):
        """Return ArchiveRow objects for all selected rows (de-duplicated)."""
        tab = self.current_tab()
        if not tab:
            return []
        seen, rows = set(), []
        for idx in tab.tree.selectedIndexes():
            r = idx.row()
            if r not in seen:
                seen.add(r)
                row = tab.arc_model.row_data(r)
                if row:
                    rows.append(row)
        return rows

    # ── Preview panel ──────────────────────────────────

    def _toggle_preview_panel(self, visible):
        tab = self.current_tab()
        if not tab:
            return
        tab.preview_pane.setVisible(visible)
        if not visible:
            tab.preview_timer.stop()
        else:
            self._schedule_preview()

    def _schedule_preview(self):
        tab = self.current_tab()
        if tab and tab.preview_pane.isVisible():
            tab.preview_timer.start()

    _PREVIEW_SIZE_LIMIT = 2 * 1024 * 1024   # 2 MB

    def _update_preview(self):
        """Extract and display a preview for the currently selected archive file."""
        tab = self.current_tab()
        if not tab:
            return
        arc_row = self._current_arc_row()
        if not arc_row or arc_row.is_dir:
            tab.preview_placeholder.setText("Select a file to preview")
            tab.preview_stack.setCurrentIndex(0)
            return

        internal_path = arc_row.path.rstrip("/")
        raw_size = arc_row.raw_size

        if raw_size > self._PREVIEW_SIZE_LIMIT:
            tab.preview_placeholder.setText(
                f"File too large to preview\n({self._fmt_size(raw_size)})"
            )
            tab.preview_stack.setCurrentIndex(0)
            return

        if not tab.archive_path:
            return

        # One scratch dir per tab, emptied between previews.  This used to
        # mkdtemp on every selection change and never clean up, so arrowing
        # through an archive left hundreds of directories behind in /tmp.
        if not tab.preview_dir or not os.path.isdir(tab.preview_dir):
            tab.preview_dir = tempfile.mkdtemp(prefix="squatchzip_prev_")
        tmp_dir = tab.preview_dir
        for stale in Path(tmp_dir).iterdir():
            if stale.is_dir():
                shutil.rmtree(stale, ignore_errors=True)
            else:
                stale.unlink(missing_ok=True)

        cmd = ["7z", "e", tab.archive_path, f"-o{tmp_dir}", "-y", internal_path]
        pw = self._archive_password(tab.archive_path)
        if pw:
            cmd += [f"-p{pw}"]
        # stdin=DEVNULL — this runs on the GUI thread; see startDrag.
        result = subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL)
        if result.returncode != 0:
            tab.preview_placeholder.setText("Could not extract file for preview.")
            tab.preview_stack.setCurrentIndex(0)
            return

        tmp_file = Path(tmp_dir) / Path(internal_path).name
        if not tmp_file.exists():
            tab.preview_stack.setCurrentIndex(0)
            return

        # Try as image first (QPixmap returns null if format unsupported)
        pixmap = QPixmap(str(tmp_file))
        if not pixmap.isNull():
            scaled = pixmap.scaled(
                tab.preview_img.size().expandedTo(QSize(1, 1)),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            tab.preview_img.setPixmap(scaled)
            tab.preview_stack.setCurrentIndex(1)
            return

        # Try as text (reject binary — null bytes in first 1 KB)
        try:
            raw_bytes = tmp_file.read_bytes()
            if b"\x00" in raw_bytes[:1024]:
                raise ValueError("binary")
            text = raw_bytes.decode("utf-8", errors="replace")
            lines = text.splitlines()[:500]
            tab.preview_txt.setPlainText("\n".join(lines))
            tab.preview_stack.setCurrentIndex(2)
        except Exception:
            tab.preview_placeholder.setText("Cannot preview this file type.")
            tab.preview_stack.setCurrentIndex(0)

    def _navigate_back(self):
        tab = self.current_tab()
        if tab and tab.archive_path:
            if tab.nav_history:
                prev = tab.nav_history.pop()
                self._show_level(prev, push_history=False)
        else:
            self._fs_browser.go_back()

    def _navigate_up(self):
        tab = self.current_tab()
        if tab and tab.archive_path:
            if not tab.current_path:
                return
            parent = tab.current_path.rstrip("/")
            parent = parent.rsplit("/", 1)[0] if "/" in parent else ""
            if parent:
                parent += "/"
            self._show_level(parent)
        else:
            self._fs_browser.go_up()

    def _update_location_bar(self):
        tab = self.current_tab()
        if not tab:
            return
        if not tab.archive_path:
            tab.loc_bar.setVisible(False)
            return
        arc_name = Path(tab.archive_path).name
        parts = [arc_name] + [p for p in tab.current_path.rstrip("/").split("/") if p]
        tab.loc_lbl.setText("  ›  ".join(parts))
        tab.loc_bar.setVisible(True)

    def _update_nav_actions(self):
        tab = self.current_tab()
        if tab and tab.archive_path:
            self._a_back.setEnabled(bool(tab.nav_history))
            self._a_up.setEnabled(bool(tab.current_path))
            self._a_root.setEnabled(bool(tab.current_path))
        else:
            # Filesystem browser mode
            self._a_back.setEnabled(self._fs_browser.can_go_back())
            self._a_up.setEnabled(self._fs_browser.can_go_up())
            self._a_root.setEnabled(False)

    # ── Helpers ───────────────────────────────────────

    @staticmethod
    def _safe_int(s):
        try:
            return int(s)
        except (ValueError, TypeError):
            return 0

    @staticmethod
    def _fmt_size(n):
        if n == 0:
            return "—"
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024:
                return (f"{n} {unit}" if unit == "B" else f"{n:.1f} {unit}")
            n /= 1024
        return f"{n:.1f} PB"

    @staticmethod
    def _file_icon(name):
        ext = Path(name).suffix.lower().lstrip(".")
        icon = QIcon.fromTheme(ICON_EXT_MAP.get(ext, "text-x-generic"))
        if icon.isNull():
            icon = QIcon.fromTheme("text-x-generic")
        return icon

    def _refresh_actions(self):
        tab      = self.current_tab()
        has      = bool(tab and tab.archive_path)
        editable = self._is_editable_format()
        for a in (self._a_close, self._a_extract, self._a_extract_sel,
                  self._a_extract_here, self._a_add, self._a_add_folder,
                  self._a_test, self._a_root):
            a.setEnabled(has)
        self._a_rename_arc.setEnabled(editable)
        self._a_delete_arc.setEnabled(editable)
        self._update_nav_actions()

    # ── Context menu ──────────────────────────────────

    def _ctx_menu(self, _pos):
        tab = self.current_tab()
        if not tab or not tab.archive_path:
            return
        menu = QMenu(self)
        menu.addAction(self._a_extract)
        selected_rows = self._selected_arc_rows()
        if selected_rows:
            menu.addAction(self._a_extract_sel)
        menu.addAction(self._a_extract_here)
        if self._is_editable_format():
            menu.addSeparator()
            if selected_rows and len(selected_rows) == 1:
                menu.addAction(self._a_rename_arc)
            if selected_rows:
                menu.addAction(self._a_delete_arc)
        # "Copy to Tab →" — only when items selected and other writable tabs exist
        if selected_rows:
            cur_idx = self._tab_widget.currentIndex()
            dest_tabs = []
            for i in range(self._tab_widget.count()):
                if i == cur_idx:
                    continue
                w = self._tab_widget.widget(i)
                if not isinstance(w, ArchiveTab) or not w.archive_path:
                    continue
                arc = w.archive_path.lower()
                if arc.endswith(".7z") or arc.endswith(".zip"):
                    dest_tabs.append((i, w.archive_path))
            if dest_tabs:
                menu.addSeparator()
                copy_menu = menu.addMenu("Copy to Tab →")
                for tab_idx, arc_path in dest_tabs:
                    label = Path(arc_path).name
                    act = copy_menu.addAction(label)
                    act.triggered.connect(
                        lambda checked, ti=tab_idx, ap=arc_path: self._copy_to_tab(ti, ap)
                    )
        menu.addSeparator()
        menu.addAction(self._a_test)
        menu.exec(QCursor.pos())

    def _copy_to_tab(self, dest_tab_idx, dest_archive):
        tab = self.current_tab()
        if not tab or not tab.archive_path:
            return
        selected_rows = self._selected_arc_rows()
        if not selected_rows:
            return
        src_paths = [r.path.rstrip("/") for r in selected_rows]
        stored_pw = self._archive_password(tab.archive_path)
        result = self._run_worker(
            "copy_to_archive", "Copying to Archive…",
            src_archive=tab.archive_path,
            src_paths=src_paths,
            dst_archive=dest_archive,
            password=stored_pw,                                    # source
            dst_password=self._archive_password(dest_archive),     # destination
        )
        if result:
            self._tab_widget.setCurrentIndex(dest_tab_idx)
            self._list()

    # ── Misc dialogs ──────────────────────────────────

    def _is_editable_format(self):
        tab = self.current_tab()
        if not tab or not tab.archive_path:
            return False
        if tab.mv_info:
            return False
        arc = tab.archive_path.lower()
        return arc.endswith(".7z") or arc.endswith(".zip")

    def _delete_items(self):
        if not self._is_editable_format():
            return
        selected_rows = self._selected_arc_rows()
        if not selected_rows:
            QMessageBox.information(self, "No Selection",
                                    "Select one or more items to delete first.")
            return
        paths = [r.path.rstrip("/") for r in selected_rows]
        n = len(paths)
        answer = QMessageBox.question(
            self, "Delete from Archive",
            f"Permanently delete {n} item(s) from the archive?\n\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        tab = self.current_tab()
        if not tab or not tab.archive_path:
            return
        self._run_worker("delete", "Deleting from Archive…",
                         archive=tab.archive_path, paths=paths,
                         password=self._archive_password(tab.archive_path),
                         _on_success=self._list)

    def _rename_item(self):
        if not self._is_editable_format():
            return
        selected_rows = self._selected_arc_rows()
        if not selected_rows or len(selected_rows) != 1:
            QMessageBox.information(self, "Rename",
                                    "Select exactly one item to rename.")
            return
        old_path = selected_rows[0].path.rstrip("/")
        old_name = Path(old_path).name
        new_name, ok = QInputDialog.getText(
            self, "Rename", "New name:", text=old_name
        )
        if not ok or not new_name.strip() or new_name.strip() == old_name:
            return
        if "/" in new_name.strip():
            QMessageBox.warning(self, "Invalid Name",
                                "File name cannot contain \"/\".")
            return
        # Path(".") is what parent gives for a root-level entry.  Stripping "."
        # off the string instead also ate the leading dot of a real dot-directory,
        # so renaming inside ".config" silently created a new "config" tree.
        parent = Path(old_path).parent
        parent_str = "" if parent in (Path("."), Path("/")) else str(parent)
        new_path = (f"{parent_str}/{new_name.strip()}" if parent_str
                    else new_name.strip())
        tab = self.current_tab()
        if not tab or not tab.archive_path:
            return
        self._run_worker("rename", "Renaming…",
                         archive=tab.archive_path,
                         old_name=old_path, new_name=new_path,
                         password=self._archive_password(tab.archive_path),
                         _on_success=self._list)

    def _check_deps(self):
        lines = []
        for tool in ("7z", "7za", "7zr"):
            found = shutil.which(tool)
            lines.append(f"{'✓' if found else '✗'}  {tool:<8}  "
                         f"{found or 'NOT FOUND — install p7zip / p7zip-full'}")
        QMessageBox.information(self, "Dependency Check", "\n".join(lines))

    def _sevenz_info(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("7-Zip Version Info")
        dlg.setMinimumSize(520, 360)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(14, 14, 14, 14)
        txt = QTextEdit()
        txt.setReadOnly(True)
        txt.setFont(QFont("Monospace", 9))
        txt.setPlainText(_get_7z_info())
        lay.addWidget(txt)
        btn = QPushButton(QIcon.fromTheme("dialog-close"), "Close")
        btn.clicked.connect(dlg.accept)
        lay.addWidget(btn, alignment=Qt.AlignmentFlag.AlignRight)
        dlg.exec()

    def _about(self):
        QMessageBox.about(
            self, f"About {APP_NAME}",
            f"<h2>{APP_NAME} {VERSION}</h2>"
            f"<p>A modern archive manager for <b>openSUSE Tumbleweed</b> / KDE Plasma 6.</p>"
            f"<p>Backend: <tt>p7zip</tt> (7z)</p>"
            f"<p>Built with PyQt6 &mdash; SquatchLabs</p>"
            f"<p>Features: 7z · ZIP · TAR · GZ · BZ2 · XZ · RAR (read) · ISO (read)</p>"
            f"<p>Install deps:<br>"
            f"<tt>sudo zypper install p7zip p7zip-full python3-PyQt6</tt></p>"
        )


# ═════════════════════════════════════════════════════
#  Entry point
# ═════════════════════════════════════════════════════

def _parse_args(argv):
    """
    Parse command-line arguments from Dolphin service menus and direct calls.

    Modes
    -----
    (no args)                    → open GUI
    <file.7z>                    → open archive
    --new <file> [<file> …]      → create new archive from files/folders
    --new --delete <file> …      → create new archive, delete sources after
    --extract-here <file>        → extract archive to sibling subfolder
    --extract-to <dest> <file>   → extract archive to specific destination
    """
    args = argv[1:]
    mode = "open"
    delete = False
    files = []

    i = 0
    while i < len(args):
        a = args[i]
        if a == "--new":
            mode = "new"
        elif a == "--delete":
            delete = True
        elif a == "--extract-here":
            mode = "extract-here"
        elif a == "--extract-to":
            mode = "extract-to"
            i += 1
            if i < len(args):
                files.insert(0, args[i])   # dest first, then archive appended below
        elif not a.startswith("--"):
            files.append(a)
        i += 1

    return mode, delete, files


def main():
    args = sys.argv[1:]
    if args and args[0] in ("--version", "-v"):
        print(f"{APP_NAME} {VERSION}")
        sys.exit(0)
    if args and args[0] in ("--help", "-h"):
        print(f"Usage: squatchzip [OPTIONS] [FILE]")
        print()
        print(f"  {APP_NAME} {VERSION} — archive manager for openSUSE / KDE Plasma 6")
        print()
        print("Options:")
        print("  --version, -v          Show version and exit")
        print("  --help,    -h          Show this help and exit")
        print()
        print("Arguments:")
        print("  <file.7z>              Open archive in GUI")
        print("  --new <file> …         Create new archive from files/folders")
        print("  --new --delete <file>… Create archive and delete sources after")
        print("  --extract-here <file>  Extract to sibling subfolder")
        print("  --extract-to <dest> <file>")
        print("                         Extract archive to specific destination")
        sys.exit(0)

    if not shutil.which("7z"):
        app = QApplication(sys.argv)
        QMessageBox.critical(
            None, f"{APP_NAME} — Missing Dependency",
            "7z was not found on your system.\n\n"
            "Please install p7zip:\n\n"
            "  sudo zypper install p7zip p7zip-full"
        )
        sys.exit(1)

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORG_NAME)
    app.setApplicationVersion(VERSION)
    app.setDesktopFileName("squatchzip")   # KDE: ties window to .desktop for taskbar grouping

    # Load custom icon from hicolor theme or bundled SVG
    icon = QIcon.fromTheme("squatchzip")
    if icon.isNull():
        svg_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "squatchzip.svg")
        if os.path.exists(svg_path):
            icon = QIcon(svg_path)
    if not icon.isNull():
        app.setWindowIcon(icon)

    win = MainWindow()
    win.show()

    mode, delete, files = _parse_args(sys.argv)

    if mode == "new":
        # Opened from service menu: create archive from selected files/folders
        # Pre-check the delete checkbox if --delete was passed
        win._new(initial_files=files, pre_delete=delete)

    elif mode == "extract-here":
        # Extract each selected archive to a sibling subfolder
        for path in files:
            if os.path.isfile(path):
                win._open_path(path)
                win._extract_here()
                break   # MainWindow only handles one archive at a time

    elif mode == "extract-to":
        # files[0] is dest, files[1] is archive (if --extract-to <dest> <arc>)
        if len(files) >= 2:
            dest, archive = files[0], files[1]
            if os.path.isfile(archive):
                win._open_path(archive)
                win._run_worker("extract", "Extracting…",
                                archive=archive, dest=dest,
                                mode="x", overwrite=True,
                                password=win._archive_password(archive))

    else:
        # Default: open the first positional file argument if it looks like an archive
        for path in files:
            if os.path.isfile(path):
                win._open_path(path)
                break

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
