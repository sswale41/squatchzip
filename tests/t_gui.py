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

print(f"\n{'ALL PASS' if not fails else 'FAILURES: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)
