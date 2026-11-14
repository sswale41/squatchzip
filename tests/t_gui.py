import os, sys, shutil, subprocess
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt, QSettings
import importlib.util
spec = importlib.util.spec_from_file_location("sz", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "squatchzip.py"))
sz = importlib.util.module_from_spec(spec); spec.loader.exec_module(sz)
app = QApplication(sys.argv)

fails = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   [{detail}]" if detail and not cond else ""))
    if not cond: fails.append(name)

print("\n=== _archive_part_paths ===")
B = os.path.abspath("t4"); shutil.rmtree(B, ignore_errors=True); os.makedirs(B)
for f in ("v.7z.001","v.7z.002","v.7z.003"): open(f"{B}/{f}","w").write("x")
open(f"{B}/plain.7z","w").write("x")
open(f"{B}/r.part1.rar","w").write("x"); open(f"{B}/r.part2.rar","w").write("x")
check("split parts found", len(sz._archive_part_paths(f"{B}/v.7z")) == 3,
      str(sz._archive_part_paths(f"{B}/v.7z")))
check("plain archive found", sz._archive_part_paths(f"{B}/plain.7z") == [f"{B}/plain.7z"])
check("rar parts found", len(sz._archive_part_paths(f"{B}/r.rar")) == 2,
      str(sz._archive_part_paths(f"{B}/r.rar")))
check("nonexistent -> empty", sz._archive_part_paths(f"{B}/none.7z") == [])

print("\n=== _tar_compound_suffix ===")
for p, exp in [("a.tar.gz",".tar.gz"), ("a.TAR.GZ",".tar.gz"), ("a.tgz",".tgz"),
               ("a.tar.bz2",".tar.bz2"), ("a.gz",None), ("a.tar",None), ("a.7z",None)]:
    check(f"{p} -> {exp}", sz._tar_compound_suffix(p) == exp, str(sz._tar_compound_suffix(p)))

print("\n=== descending sort keeps folders on top ===")
m = sz.ArchiveModel()
rows = [
    ("banana", False, 10), ("apple", False, 20),
    ("zebra",  True,   0), ("alpha", True,   0),
]
m.reset_data([
    sz.ArchiveRow(name=nm, is_dir=d, path=nm + ("/" if d else ""), raw_size=sz_,
                  col_size="", col_packed="", col_ratio="", col_modified="",
                  col_method="", icon=None)
    for nm, d, sz_ in rows
])
def order(): return [r.name for r in m._rows]
m.sort(0, Qt.SortOrder.AscendingOrder)
asc = order()
m.sort(0, Qt.SortOrder.DescendingOrder)
desc = order()
print(f"    asc={asc}\n    desc={desc}")
check("ascending: folders first", asc[:2] == ["alpha","zebra"], str(asc))
check("descending: folders STILL first", desc[:2] == ["zebra","alpha"], str(desc))
check("descending: files reversed", desc[2:] == ["banana","apple"], str(desc))

print("\n=== InfoPanel Volumes row ===")
ip = sz.InfoPanel()
check("Volumes label exists", "Volumes" in ip._labels)
check("Volumes hidden by default", not ip._labels["Volumes"].isVisible())
ip.update({"Format":"7z","Files":3,"Volumes":"4 parts (all parts found)"})
check("Volumes text set", ip._labels["Volumes"].text() == "4 parts (all parts found)",
      ip._labels["Volumes"].text())
ip.update({"Format":"7z","Files":3})
check("Volumes hidden again for a single-file archive", ip._labels["Volumes"].isHidden())

print("\n=== ExtractDialog does not poison the subfolder preference ===")
QSettings("SquatchLabs","SquatchZip").setValue("extract_subfolder", True)
d = sz.ExtractDialog(None, archive_path="/tmp/foo.7z", default_subfolder=False)
check("heuristic unchecks the box", not d.subfolder.isChecked())
d.get_params()                                   # user accepted without touching it
saved = QSettings("SquatchLabs","SquatchZip").value("extract_subfolder", True, type=bool)
check("preference NOT overwritten by the heuristic", saved is True, str(saved))
d2 = sz.ExtractDialog(None, archive_path="/tmp/foo.7z", default_subfolder=True)
d2.subfolder.setChecked(False)                   # a real user change
d2.get_params()
saved2 = QSettings("SquatchLabs","SquatchZip").value("extract_subfolder", True, type=bool)
check("a real user change IS saved", saved2 is False, str(saved2))
QSettings("SquatchLabs","SquatchZip").setValue("extract_subfolder", True)

print("\n=== ProgressDialog cancel wiring ===")
pd = sz.ProgressDialog(None, "Test")
check("cancel button exists", hasattr(pd, "cancel_btn"))
check("cancel disabled before a worker is attached", not pd.cancel_btn.isEnabled())
class FakeWorker:
    def __init__(self): self.cancelled = False
    def cancel(self): self.cancelled = True
fw = FakeWorker(); pd.set_worker(fw)
check("cancel enabled once attached", pd.cancel_btn.isEnabled())
pd.reject()                                       # Escape mid-operation
check("Escape triggers cancel", fw.cancelled)
check("dialog stays open until the worker reports back", pd.isVisible() or not pd._operation_done)
pd.finish(False, "Cancelled.")
check("closes itself after a cancel", pd._operation_done and pd.result() == sz.QDialog.DialogCode.Accepted,
      str(pd.result()))
pd2 = sz.ProgressDialog(None, "Test2")
pd2.finish(False, "Real failure")
check("a real failure keeps Close available", pd2.close_btn.isEnabled())

print("\n=== MainWindow constructs and the Browse tab has no close button ===")
win = sz.MainWindow()
bar = win._tab_widget.tabBar()
check("Browse tab close button removed",
      bar.tabButton(0, sz.QTabBar.ButtonPosition.RightSide) is None and
      bar.tabButton(0, sz.QTabBar.ButtonPosition.LeftSide) is None)
check("worker set initialised", isinstance(win._workers, set))
check("temp dir set initialised", isinstance(win._temp_dirs, set))
check("_archive_password on unknown archive returns ''", win._archive_password("/nope") == "")
check("_archive_password(None) is safe", win._archive_password(None) == "")

print("\n=== Browse tab: every no-arg action must not raise ===")
win._tab_widget.setCurrentIndex(0)
check("current_tab() is None on Browse", win.current_tab() is None)

import inspect
SKIP = {"__new__", "__reduce__", "__reduce_ex__", "__sizeof__", "__dir__", "_new", "_open", "_add_files", "_add_folder", "_check_deps", "_sevenz_info",
        "_about", "_quit", "close", "_show_settings", "_preferences"}
raised = []
for name in sorted(dir(win)):
    if not name.startswith("_") or name in SKIP:
        continue
    fn = getattr(win, name, None)
    if not callable(fn) or inspect.isclass(fn):
        continue
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        continue
    if any(p.default is p.empty and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
           for p in sig.parameters.values()):
        continue           # needs arguments; not a plain user action
    try:
        fn()
    except Exception as e:
        raised.append(f"{name}: {type(e).__name__}: {e}")
check("no no-arg action raises on the Browse tab", not raised, "; ".join(raised))
print(f"    (swept {len([n for n in dir(win) if n.startswith('_')])} attributes)")

check("_current_arc_row() -> None", win._current_arc_row() is None)
check("_selected_arc_rows() -> []", win._selected_arc_rows() == [])
try:
    win._add_sources_to_archive(["/tmp"])
    check("_add_sources_to_archive() on Browse tab", True)
except Exception as e:
    check("_add_sources_to_archive() on Browse tab", False, f"{type(e).__name__}: {e}")

print("\n=== 2.3.3: _individual_archive_paths ===")
IA = os.path.abspath("t5ia"); shutil.rmtree(IA, ignore_errors=True)
os.makedirs(f"{IA}/a/report"); os.makedirs(f"{IA}/b/report"); os.makedirs(f"{IA}/out")
for f in ("report.odt", "report.pdf", "notes.txt", "Makefile"):
    open(f"{IA}/a/{f}", "w").write("x")
iap = sz._individual_archive_paths
check("unique names unchanged",
      iap([f"{IA}/a/notes.txt"], "7z") == [f"{IA}/a/notes.7z"])
check("same stem -> extension kept",
      iap([f"{IA}/a/report.odt", f"{IA}/a/report.pdf"], "7z")
      == [f"{IA}/a/report.odt.7z", f"{IA}/a/report.pdf.7z"],
      str(iap([f"{IA}/a/report.odt", f"{IA}/a/report.pdf"], "7z")))
check("collision only affects the clashing group",
      iap([f"{IA}/a/report.odt", f"{IA}/a/report.pdf", f"{IA}/a/notes.txt"], "7z")[2]
      == f"{IA}/a/notes.7z")
check("folder keeps plain name, colliding files keep extension",
      iap([f"{IA}/a/report", f"{IA}/a/report.odt", f"{IA}/a/report.pdf"], "zip")
      == [f"{IA}/a/report.zip", f"{IA}/a/report.odt.zip", f"{IA}/a/report.pdf.zip"],
      str(iap([f"{IA}/a/report", f"{IA}/a/report.odt", f"{IA}/a/report.pdf"], "zip")))
check("same-named folders into one out_dir get -2",
      iap([f"{IA}/a/report", f"{IA}/b/report"], "7z", out_dir=f"{IA}/out")
      == [f"{IA}/out/report.7z", f"{IA}/out/report-2.7z"],
      str(iap([f"{IA}/a/report", f"{IA}/b/report"], "7z", out_dir=f"{IA}/out")))
check("same-named folders in different parents do not collide",
      iap([f"{IA}/a/report", f"{IA}/b/report"], "7z")
      == [f"{IA}/a/report.7z", f"{IA}/b/report.7z"])
check("timestamp prefix applied after disambiguation",
      iap([f"{IA}/a/report.odt", f"{IA}/a/report.pdf"], "7z", suffix="20261114_", ts_prefix=True)
      == [f"{IA}/a/20261114_report.odt.7z", f"{IA}/a/20261114_report.pdf.7z"])
res = iap([f"{IA}/a/report.odt", f"{IA}/a/report.pdf", f"{IA}/a/notes.txt",
           f"{IA}/a/Makefile", f"{IA}/a/report"], "7z")
check("every result path is unique", len(set(res)) == len(res), str(res))

print("\n=== 2.3.3: individual mode keeps the output box a folder ===")
dlg = sz.CreateArchiveDialog(None, initial_files=[f"{IA}/a/report.odt", f"{IA}/a/report.pdf"])
check("normal mode auto-fills an archive path", dlg.out_path.text().endswith(".7z"),
      dlg.out_path.text())
dlg.indiv_each.setChecked(True)
check("individual mode clears the box", dlg.out_path.text() == "")
dlg.fmt.setCurrentIndex((dlg.fmt.currentIndex() + 1) % dlg.fmt.count())
dlg.ts_pos.setCurrentIndex(1)
dlg.ts_combo.setCurrentIndex(1)
check("format/timestamp changes leave the box empty", dlg.out_path.text() == "",
      dlg.out_path.text())
dlg.file_list.addItem(f"{IA}/a/notes.txt")
check("adding a file leaves the box empty", dlg.out_path.text() == "", dlg.out_path.text())
check("get_params indiv_out_dir is empty", dlg.get_params()["indiv_out_dir"] == "")
dlg.indiv_each.setChecked(False)
check("leaving individual mode restores an archive path",
      dlg.out_path.text().endswith(tuple("." + e for e in sz.FORMATS.values())),
      dlg.out_path.text())

warned = []
_orig_warn = sz._msg_warning
sz._msg_warning = lambda parent, title, text, *a: warned.append(title)
try:
    dlg2 = sz.CreateArchiveDialog(None, initial_files=[f"{IA}/a/notes.txt"])
    dlg2.indiv_each.setChecked(True)
    dlg2.out_path.setText(f"{IA}/out/missing.7z")
    dlg2._validate()
    check("nonexistent output folder is refused",
          warned == ["Output Folder Not Found"] and dlg2.result() == 0, str(warned))
    warned.clear()
    dlg2.out_path.setText(f"{IA}/out")
    dlg2._validate()
    check("existing output folder is accepted", warned == [] and dlg2.result() == 1,
          str(warned))
finally:
    sz._msg_warning = _orig_warn

print("\n=== 2.3.3: individual + delete originals end to end ===")
E = f"{IA}/docs"; os.makedirs(E)
for f in ("itinerary.odt", "itinerary.pdf", "guide.odt", "guide.pdf"):
    open(f"{E}/{f}", "w").write(f * 40)
win2 = sz.MainWindow()
made = []
def _sync_worker(op, title, _on_success=None, **kw):
    w = sz.ArchiveWorker(op, **kw); r = {}
    w.op_finished.connect(lambda ok, msg: r.update(ok=ok))
    w.run(); made.append(kw["archive"]); return r.get("ok")
win2._run_worker = _sync_worker
win2._new_individual({
    "sources": [f"{E}/itinerary.odt", f"{E}/itinerary.pdf", f"{E}/guide.odt", f"{E}/guide.pdf"],
    "level": 1, "password": "", "split_size": 0, "split_unit": "MB", "solid": True,
    "delete_src": True, "excl_hidden": True, "archive": "", "indiv_out_dir": "",
    "ext": "7z", "ts_idx": 0, "ts_prefix": False, "custom_ts_format": "",
})
check("4 files -> 4 archives", sorted(os.listdir(E)) ==
      ["guide.odt.7z", "guide.pdf.7z", "itinerary.odt.7z", "itinerary.pdf.7z"],
      str(sorted(os.listdir(E))))
check("no extra folder created", all(os.path.isfile(f"{E}/{n}") for n in os.listdir(E)))
one = subprocess.run(["7z", "l", "-slt", f"{E}/guide.pdf.7z"], capture_output=True,
                     text=True, stdin=subprocess.DEVNULL).stdout
check("each archive holds only its own file",
      "Path = guide.pdf" in one and "Path = guide.odt" not in one)
win2.close()
shutil.rmtree(IA, ignore_errors=True)

print("\n=== 2.3.4: avoid_existing naming ===")
X = os.path.abspath("t5ex"); shutil.rmtree(X, ignore_errors=True); os.makedirs(X)
open(f"{X}/a.txt", "w").write("x"); open(f"{X}/b.txt", "w").write("x")
open(f"{X}/a.7z", "w").write("old")
check("existing archive -> -2",
      iap([f"{X}/a.txt"], "7z", avoid_existing=True) == [f"{X}/a-2.7z"])
open(f"{X}/a-2.7z", "w").write("old")
check("-2 also taken -> -3",
      iap([f"{X}/a.txt"], "7z", avoid_existing=True) == [f"{X}/a-3.7z"])
open(f"{X}/b.7z.001", "w").write("old")
check("split volumes count as existing",
      iap([f"{X}/b.txt"], "7z", avoid_existing=True) == [f"{X}/b-2.7z"])
check("default still ignores what is on disk",
      iap([f"{X}/a.txt"], "7z") == [f"{X}/a.7z"])
shutil.rmtree(X, ignore_errors=True)

print("\n=== 2.3.4: existing-archive prompt in individual mode ===")
def _lst(arc):
    out = subprocess.run(["7z", "l", "-slt", arc], capture_output=True, text=True,
                         stdin=subprocess.DEVNULL).stdout
    return sorted(l[7:] for l in out.splitlines() if l.startswith("Path = ") and l[7:] != arc)

def _scenario(choice, delete=False):
    """Fresh dir with src a.txt and an existing a.7z holding old.txt."""
    shutil.rmtree(X, ignore_errors=True); os.makedirs(X)
    open(f"{X}/old.txt", "w").write("old\n" * 20)
    subprocess.run(["7z", "a", f"{X}/a.7z", f"{X}/old.txt"], capture_output=True,
                   stdin=subprocess.DEVNULL)
    os.remove(f"{X}/old.txt")
    open(f"{X}/a.txt", "w").write("new\n" * 20)
    asked = []
    sz._ask_existing_archives = lambda parent, paths: (asked.append(list(paths)), choice)[1]
    w = sz.MainWindow(); w._run_worker = _sync_worker; made.clear()
    w._new_individual({
        "sources": [f"{X}/a.txt"], "level": 1, "password": "", "split_size": 0,
        "split_unit": "MB", "solid": True, "delete_src": delete, "excl_hidden": False,
        "archive": "", "indiv_out_dir": "", "ext": "7z", "ts_idx": 0,
        "ts_prefix": False, "custom_ts_format": "",
    })
    w.close()
    return asked

_orig_ask = sz._ask_existing_archives
try:
    asked = _scenario("cancel")
    check("prompt lists the existing archive", asked == [[f"{X}/a.7z"]], str(asked))
    check("cancel: nothing created", made == [], str(made))
    check("cancel: existing archive untouched", _lst(f"{X}/a.7z") == ["old.txt"],
          str(_lst(f"{X}/a.7z")))
    check("cancel: source kept", os.path.exists(f"{X}/a.txt"))

    _scenario("add")
    check("add: file appended to existing archive",
          _lst(f"{X}/a.7z") == ["a.txt", "old.txt"], str(_lst(f"{X}/a.7z")))

    _scenario("keep")
    check("keep both: new archive is a-2.7z", made == [f"{X}/a-2.7z"], str(made))
    check("keep both: existing archive untouched", _lst(f"{X}/a.7z") == ["old.txt"])
    check("keep both: new archive holds the file", _lst(f"{X}/a-2.7z") == ["a.txt"])

    _scenario("overwrite", delete=True)
    check("overwrite: archive replaced", _lst(f"{X}/a.7z") == ["a.txt"],
          str(_lst(f"{X}/a.7z")))
    check("overwrite + delete: source removed", not os.path.exists(f"{X}/a.txt"))

    shutil.rmtree(X, ignore_errors=True); os.makedirs(X)
    open(f"{X}/c.txt", "w").write("x")
    asked2 = []
    sz._ask_existing_archives = lambda parent, paths: (asked2.append(paths), "cancel")[1]
    w = sz.MainWindow(); w._run_worker = _sync_worker; made.clear()
    w._new_individual({
        "sources": [f"{X}/c.txt"], "level": 1, "password": "", "split_size": 0,
        "split_unit": "MB", "solid": True, "delete_src": False, "excl_hidden": False,
        "archive": "", "indiv_out_dir": "", "ext": "7z", "ts_idx": 0,
        "ts_prefix": False, "custom_ts_format": "",
    })
    w.close()
    check("no clash: no prompt, archive created",
          asked2 == [] and made == [f"{X}/c.7z"], f"{asked2} {made}")
finally:
    sz._ask_existing_archives = _orig_ask

print("\n=== 2.3.4: the prompt itself ===")
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QMessageBox
def _drive(action):
    def go():
        box = app.activeModalWidget()
        if not isinstance(box, QMessageBox):
            QTimer.singleShot(20, go); return
        if action == "escape":
            box.reject()
        else:
            next(b for b in box.buttons() if b.text() == action).click()
    QTimer.singleShot(20, go)
for label, want in (("Keep Both", "keep"), ("Add to Existing", "add"),
                    ("Overwrite", "overwrite"), ("Cancel", "cancel"), ("escape", "cancel")):
    _drive(label)
    got = sz._ask_existing_archives(None, [f"/x/{i}.7z" for i in range(10)])
    check(f"{label} -> {want}", got == want, got)
shutil.rmtree(X, ignore_errors=True)

print(f"\n{'ALL PASS' if not fails else 'FAILURES: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)
