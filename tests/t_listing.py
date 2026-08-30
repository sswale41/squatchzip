import os, sys, shutil, subprocess
os.environ["QT_QPA_PLATFORM"]="offscreen"
from PyQt6.QtWidgets import QApplication
import importlib.util
spec = importlib.util.spec_from_file_location("sz",os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "squatchzip.py"))
sz = importlib.util.module_from_spec(spec); spec.loader.exec_module(sz)
app = QApplication(sys.argv)
fails=[]
def check(n,c,d=""):
    print(("  PASS  " if c else "  FAIL  ")+n+(f"   [{d}]" if d and not c else ""));  fails.append(n) if not c else None

B=os.path.abspath("t6"); shutil.rmtree(B,ignore_errors=True); os.makedirs(f"{B}/stage/proj/sub")
open(f"{B}/stage/proj/a.txt","w").write("a\n"); open(f"{B}/stage/proj/sub/b.txt","w").write("b\n")

print("\n=== the -slt header block is not an entry ===")
for ext in ("7z","zip","tar"):
    arc=f"{B}/x.{ext}"
    subprocess.run(["7z","a","-mx=1",arc,"proj"],cwd=f"{B}/stage",
                   capture_output=True,stdin=subprocess.DEVNULL)
    w=sz.ArchiveWorker("list",archive=arc); got=[]
    w.listing.connect(got.append); w.run()
    entries=got[0] if got else []
    paths=[e.get("Path") for e in entries]
    check(f".{ext}: no entry named after the archive itself",
          not any(p and os.path.basename(p)==f"x.{ext}" for p in paths), str(paths))
    check(f".{ext}: exactly 4 entries (proj, proj/sub, 2 files)", len(entries)==4, str(paths))
    dirs  = [e.get("Path") for e in entries if sz._entry_is_dir(e)]
    files = [e.get("Path") for e in entries if not sz._entry_is_dir(e)]
    check(f".{ext}: directories detected as directories", sorted(dirs)==["proj","proj/sub"], str(dirs))
    check(f".{ext}: files detected as files", sorted(files)==["proj/a.txt","proj/sub/b.txt"], str(files))

print("\n=== file/folder counts in the info panel ===")
for ext in ("7z","zip","tar"):
    w2=sz.MainWindow(); w2._open_path(f"{B}/x.{ext}")
    t2=w2.current_tab()
    ww=sz.ArchiveWorker("list",archive=f"{B}/x.{ext}"); g=[]
    ww.listing.connect(g.append); ww.run(); w2._store_entries(g[0])
    check(f".{ext}: Files=2 Folders=2",
          t2.info._labels["Files"].text()=="2" and t2.info._labels["Folders"].text()=="2",
          f'Files={t2.info._labels["Files"].text()} Folders={t2.info._labels["Folders"].text()}')
    names=[i[0] for i in w2._get_level_items("")]
    check(f".{ext}: root shows just 'proj'", names==["proj"], str(names))

win=sz.MainWindow(); win._open_path(f"{B}/x.7z")
tab=win.current_tab()
w=sz.ArchiveWorker("list",archive=f"{B}/x.7z"); got=[]
w.listing.connect(got.append); w.run()
win._store_entries(got[0])
check("Files = 2", tab.info._labels["Files"].text()=="2", tab.info._labels["Files"].text())
check("Folders = 2", tab.info._labels["Folders"].text()=="2", tab.info._labels["Folders"].text())
check("Format = 7Z", tab.info._labels["Format"].text()=="7Z", tab.info._labels["Format"].text())

print("\n=== root level shows only real entries ===")
items=win._get_level_items("")
names=[i[0] for i in items]
check("root shows just 'proj'", names==["proj"], str(names))

print(f"\n{'ALL PASS' if not fails else 'FAILURES: '+', '.join(fails)}")
sys.exit(1 if fails else 0)
