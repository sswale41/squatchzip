import os, sys, shutil, subprocess, time, threading
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import QCoreApplication
import importlib.util
spec = importlib.util.spec_from_file_location("sz", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "squatchzip.py"))
sz = importlib.util.module_from_spec(spec); spec.loader.exec_module(sz)
app = QCoreApplication(sys.argv)

BASE = os.path.abspath("t2"); shutil.rmtree(BASE, ignore_errors=True)
os.makedirs(f"{BASE}/src/sub", exist_ok=True)
open(f"{BASE}/src/a.txt","w").write("alpha\n"*200)
open(f"{BASE}/src/sub/b.txt","w").write("beta\n"*200)

def run(op, worker_out=None, **kw):
    w = sz.ArchiveWorker(op, **kw)
    if worker_out is not None: worker_out.append(w)
    out, log = {}, []
    w.op_finished.connect(lambda ok, msg: out.update(ok=ok, msg=msg))
    w.progress.connect(log.append)
    w.listing.connect(lambda f: out.update(listing=f))
    w.run(); out["log"] = log
    return out

fails = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   [{detail}]" if detail and not cond else ""))
    if not cond: fails.append(name)

print("\n=== tar-compound create produces a REAL tarball ===")
for ext in ("tar.gz", "tar.bz2", "tar.xz", "tgz"):
    arc = f"{BASE}/out.{ext}"
    r = run("create", archive=arc, sources=[f"{BASE}/src"], level=5, solid=False,
            delete_src=False)
    ok = r.get("ok") is True and os.path.exists(arc)
    check(f"create .{ext}", ok, str(r.get("msg")))
    if not ok: continue
    # GNU tar is the real arbiter: does it see the actual files?
    p = subprocess.run(["tar", "tf", arc], capture_output=True, text=True)
    names = p.stdout.split()
    check(f".{ext} is a genuine tar readable by GNU tar", p.returncode == 0, p.stderr.strip()[:120])
    check(f".{ext} contains src/a.txt and src/sub/b.txt",
          any(x.endswith("src/a.txt") for x in names) and
          any(x.endswith("src/sub/b.txt") for x in names), str(names))
    check(f".{ext} has no stray scratch dir", not any("squatchzip_tar_" in x for x in names), str(names))

print("\n=== no scratch dirs left behind ===")
import glob as _g
check("no squatchzip_tar_* in /tmp", not _g.glob("/tmp/squatchzip_tar_*"),
      str(_g.glob("/tmp/squatchzip_tar_*")))

print("\n=== tar-compound extract yields files, not a .tar ===")
dest = f"{BASE}/ex"; os.makedirs(dest, exist_ok=True)
r = run("extract", archive=f"{BASE}/out.tar.gz", dest=dest, mode="x", overwrite=True)
check("extract .tar.gz", r.get("ok") is True, str(r.get("msg")))
check("files present", os.path.exists(f"{dest}/src/a.txt") and os.path.exists(f"{dest}/src/sub/b.txt"),
      str(os.listdir(dest)))
check("intermediate .tar removed", not _g.glob(f"{dest}/*.tar"), str(_g.glob(f"{dest}/*.tar")))

print("\n=== password on a tar.gz is refused, not silently dropped ===")
r = run("create", archive=f"{BASE}/pw.tar.gz", sources=[f"{BASE}/src"], level=1,
        solid=False, password="nope", delete_src=False)
check("create still succeeds", r.get("ok") is True, str(r.get("msg")))
check("user is warned the password was ignored",
      any("Password ignored" in l for l in r["log"]), str(r["log"]))

print("\n=== single-file .gz is still a plain gzip (unchanged) ===")
r = run("create", archive=f"{BASE}/one.gz", sources=[f"{BASE}/src/a.txt"], level=1,
        solid=False, delete_src=False)
check("create .gz", r.get("ok") is True, str(r.get("msg")))

# (cancellation is covered by t_cancel.py, which drives a real QThread +
# event loop; queued signals never arrive on a bare Python thread.)

print(f"\n{'ALL PASS' if not fails else 'FAILURES: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)
