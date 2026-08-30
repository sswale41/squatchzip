import os, sys, shutil, subprocess, glob
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PyQt6.QtWidgets import QApplication, QMessageBox, QInputDialog, QDialog
from PyQt6.QtCore import Qt
import importlib.util
spec = importlib.util.spec_from_file_location("sz", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "squatchzip.py"))
sz = importlib.util.module_from_spec(spec); spec.loader.exec_module(sz)
app = QApplication(sys.argv)

fails = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   [{detail}]" if detail and not cond else ""))
    if not cond: fails.append(name)

B = os.path.abspath("t5"); shutil.rmtree(B, ignore_errors=True); os.makedirs(B)

def mk(archive, tree):
    """Build an archive from a staging dir, using RELATIVE paths run from inside
    it so 7z stores clean names and never sweeps up the archive itself."""
    root = os.path.join(B, "_stage"); shutil.rmtree(root, ignore_errors=True)
    for rel, content in tree.items():
        p = os.path.join(root, rel); os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").write(content)
    names = sorted(os.listdir(root))
    if os.path.exists(archive): os.remove(archive)
    r = subprocess.run(["7z","a","-mx=1",os.path.abspath(archive)] + names,
                       capture_output=True, stdin=subprocess.DEVNULL, cwd=root)
    assert r.returncode == 0, r.stdout.decode()[-400:]
    return archive

def open_tab(win, path):
    win._open_path(path)
    tab = win.current_tab()
    r = sz.ArchiveWorker("list", archive=path)
    got = []
    r.listing.connect(got.append); r.run()
    tab.all_entries = got[0] if got else []
    win._store_entries(tab.all_entries)
    return tab

print("\n=== rename inside a dot-directory keeps the dot ===")
arc = mk(f"{B}/dot.7z", {".config/settings.ini": "k=v\n", "readme.txt": "hi\n"})
win = sz.MainWindow()
tab = open_tab(win, arc)
paths = [e["Path"] for e in tab.all_entries]
check("archive really contains .config/settings.ini",
      any(p == ".config/settings.ini" for p in paths), str(paths))
# drive _rename_item with the real dialog stubbed out
target = ".config/settings.ini"
class FakeRow:
    path = target
win._selected_arc_rows = lambda: [FakeRow()]
win._is_editable_format = lambda: True
orig_getText = QInputDialog.getText
QInputDialog.getText = staticmethod(lambda *a, **k: ("renamed.ini", True))
captured = {}
orig_rw = win._run_worker
win._run_worker = lambda op, title, **kw: captured.update(op=op, **kw)
win._rename_item()
QInputDialog.getText = orig_getText
win._run_worker = orig_rw
print(f"    old_name={captured.get('old_name')!r}  new_name={captured.get('new_name')!r}")
check("new path keeps the leading dot",
      captured.get("new_name") == ".config/renamed.ini", str(captured.get("new_name")))

print("\n=== rename at archive root has no leading slash ===")
class FakeRow2:
    path = "readme.txt"
win._selected_arc_rows = lambda: [FakeRow2()]
QInputDialog.getText = staticmethod(lambda *a, **k: ("notes.txt", True))
captured.clear(); win._run_worker = lambda op, title, **kw: captured.update(op=op, **kw)
win._rename_item()
QInputDialog.getText = orig_getText; win._run_worker = orig_rw
check("root rename is a bare name", captured.get("new_name") == "notes.txt",
      str(captured.get("new_name")))

print("\n=== Extract Here honours the subfolder heuristic ===")
# self-contained: one root dir holding everything -> must NOT double-nest
sc = mk(f"{B}/selfcontained.7z", {"proj/a.txt": "a\n", "proj/sub/b.txt": "b\n"})
win2 = sz.MainWindow(); tab2 = open_tab(win2, sc)
check("heuristic says no wrapper", win2._should_create_subfolder() is False)
cap2 = {}
win2._run_worker = lambda op, title, **kw: cap2.update(op=op, **kw)
win2._extract_here()
check("Extract Here targets the archive's own directory",
      cap2.get("dest") == str(os.path.dirname(sc)), str(cap2.get("dest")))
# loose: several root entries -> must wrap
lo = mk(f"{B}/loose.7z", {"a.txt": "a\n", "b.txt": "b\n"})
win3 = sz.MainWindow(); tab3 = open_tab(win3, lo)
check("heuristic says wrap", win3._should_create_subfolder() is True)
cap3 = {}
win3._run_worker = lambda op, title, **kw: cap3.update(op=op, **kw)
win3._extract_here()
check("Extract Here wraps in a named folder",
      cap3.get("dest") == os.path.join(B, "loose"), str(cap3.get("dest")))

print("\n=== drag-out preserves paths (no same-name collision) ===")
col = mk(f"{B}/collide.7z", {"one/notes.txt": "FIRST\n", "two/notes.txt": "SECOND\n"})
win4 = sz.MainWindow(); tab4 = open_tab(win4, col)
view = tab4.tree
captured_urls = {}
from PyQt6.QtGui import QDrag
orig_exec = QDrag.exec
QDrag.exec = lambda self, *a, **k: captured_urls.update(
    urls=[u.toLocalFile() for u in self.mimeData().urls()])
# select both notes.txt rows
class FakeIdx:
    def __init__(self, r): self._r = r
    def row(self): return self._r
rows = [sz.ArchiveRow(name="notes.txt", is_dir=False, path=p, raw_size=6,
                      col_size="", col_packed="", col_ratio="", col_modified="",
                      col_method="", icon=None)
        for p in ("one/notes.txt", "two/notes.txt")]
view._arc_model.reset_data(rows)
view.selectedIndexes = lambda: [FakeIdx(0), FakeIdx(1)]
view.startDrag(Qt.DropAction.CopyAction)
QDrag.exec = orig_exec
urls = captured_urls.get("urls", [])
print(f"    urls={urls}")
check("both files handed to the drop target", len(urls) == 2, str(urls))
if len(urls) == 2:
    contents = sorted(open(u).read().strip() for u in urls)
    check("both distinct files survived (no collision)", contents == ["FIRST","SECOND"],
          str(contents))
check("temp dir registered for cleanup", len(win4._temp_dirs) >= 1)

print("\n=== preview reuses one temp dir instead of leaking ===")
before = set(glob.glob("/tmp/squatchzip_prev_*"))
win5 = sz.MainWindow(); tab5 = open_tab(win5, lo)
tab5.preview_pane.setVisible(True)
made = []
for i in range(6):
    r = sz.ArchiveRow(name="a.txt", is_dir=False, path="a.txt", raw_size=2,
                      col_size="", col_packed="", col_ratio="", col_modified="",
                      col_method="", icon=None)
    win5._current_arc_row = lambda r=r: r
    win5._update_preview()
    made.append(tab5.preview_dir)
check("preview produced a dir", tab5.preview_dir is not None)
check("same dir reused across 6 previews", len(set(made)) == 1, str(set(made)))
after = set(glob.glob("/tmp/squatchzip_prev_*"))
check("only one new preview dir on disk", len(after - before) <= 1, str(after - before))
win5._close_tab(win5._tab_widget.currentIndex())
check("closing the tab removes it", not glob.glob(str(made[0])) if made[0] else True,
      str(made[0]))

# cleanup
for w in (win, win2, win3, win4, win5):
    for d in list(w._temp_dirs): shutil.rmtree(d, ignore_errors=True)

print(f"\n{'ALL PASS' if not fails else 'FAILURES: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)
