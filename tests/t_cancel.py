import os, sys, shutil, subprocess, time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import QCoreApplication, QTimer
import importlib.util
spec = importlib.util.spec_from_file_location("sz", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "squatchzip.py"))
sz = importlib.util.module_from_spec(spec); spec.loader.exec_module(sz)
app = QCoreApplication(sys.argv)

BASE = os.path.abspath("t3"); shutil.rmtree(BASE, ignore_errors=True)
os.makedirs(f"{BASE}/big", exist_ok=True)
with open(f"{BASE}/big/blob.bin","wb") as f:
    f.write(os.urandom(220 * 1024 * 1024))

fails = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   [{detail}]" if detail and not cond else ""))
    if not cond: fails.append(name)

def cancel_run(archive, label, delay_ms=3000):
    """Start a real QThread worker, cancel it mid-flight, drive the event loop."""
    w = sz.ArchiveWorker("create", archive=archive, sources=[f"{BASE}/big"],
                         level=9, solid=True, delete_src=False)
    res = {}
    w.op_finished.connect(lambda ok, msg: res.update(ok=ok, msg=msg))
    pids = {}
    def do_cancel():
        p = w._proc
        if p is not None:
            pids["pid"] = p.pid
        w.cancel()
    w.finished.connect(app.quit)                 # QThread.finished
    QTimer.singleShot(delay_ms, do_cancel)
    QTimer.singleShot(90000, app.quit)           # hard stop if it wedges
    t0 = time.time()
    w.start()
    app.exec()
    w.wait(30000)
    print(f"    ({label}: {time.time()-t0:.1f}s, worker pid {pids.get('pid')})")
    return res, pids.get("pid")

print("\n=== cancel a NEW archive ===")
arc = f"{BASE}/cancel.7z"
res, pid = cancel_run(arc, "new")
check("reported as cancelled", res.get("ok") is False and "ancel" in str(res.get("msg")), str(res))
check("partial archive removed", not os.path.exists(arc))
check("no leftover 7z temp file", not [p for p in os.listdir(BASE) if p.startswith("cancel.7z.tmp")],
      str(os.listdir(BASE)))
if pid:
    alive = subprocess.run(["ps","-o","stat=","-p",str(pid)], capture_output=True, text=True)
    check("7z process is gone", alive.returncode != 0 or not alive.stdout.strip(),
          f"stat={alive.stdout.strip()!r}")
live = subprocess.run(["pgrep","-x","7z"], capture_output=True, text=True).stdout.split()
check("no stray 7z process left running", not live, f"pids={live}")

print("\n=== cancel an APPEND leaves the archive alone ===")
keep = f"{BASE}/keep.7z"
subprocess.run(["7z","a","-mx=1",keep,f"{BASE}/big/blob.bin"], capture_output=True,
               stdin=subprocess.DEVNULL)
before = os.path.getsize(keep)
check("base archive exists", os.path.exists(keep))
res, _ = cancel_run(keep, "append")
check("append reported as cancelled", res.get("ok") is False and "ancel" in str(res.get("msg")), str(res))
check("existing archive NOT deleted", os.path.exists(keep), "archive was destroyed!")
check("existing archive intact",
      os.path.exists(keep) and os.path.getsize(keep) == before,
      f"{before} -> {os.path.getsize(keep) if os.path.exists(keep) else 'gone'}")

print("\n=== cancel a tar.gz mid tar-stage cleans the scratch dir ===")
import glob as _g
res, _ = cancel_run(f"{BASE}/c.tar.gz", "targz", delay_ms=2000)
check("no squatchzip_tar_* scratch dir left", not _g.glob("/tmp/squatchzip_tar_*"),
      str(_g.glob("/tmp/squatchzip_tar_*")))

print(f"\n{'ALL PASS' if not fails else 'FAILURES: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)
