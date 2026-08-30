import os, sys, shutil, subprocess, time, threading
sys.path.insert(0, "work")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import QCoreApplication
import importlib.util
spec = importlib.util.spec_from_file_location("sz", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "squatchzip.py"))
sz = importlib.util.module_from_spec(spec); spec.loader.exec_module(sz)

app = QCoreApplication(sys.argv)
BASE = os.path.abspath("t")
shutil.rmtree(BASE, ignore_errors=True)
os.makedirs(f"{BASE}/src/sub", exist_ok=True)
open(f"{BASE}/src/a.txt","w").write("hello alpha\n"*50)
open(f"{BASE}/src/sub/b.txt","w").write("hello beta\n"*50)
open(f"{BASE}/src/.hidden","w").write("secret\n")

def run(op, **kw):
    """Run a worker synchronously and collect its result + log."""
    w = sz.ArchiveWorker(op, **kw)
    out = {}
    log = []
    w.op_finished.connect(lambda ok, msg: out.update(ok=ok, msg=msg))
    w.progress.connect(log.append)
    w.listing.connect(lambda f: out.update(listing=f))
    w.run()
    out["log"] = log
    return out

fails = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   [{detail}]" if detail and not cond else ""))
    if not cond: fails.append(name)

print("\n=== Phase 2: -ms is 7z-only (solid unchecked) ===")
for ext in ("zip", "tar.gz", "7z"):
    arc = f"{BASE}/nosolid.{ext}"
    r = run("create", archive=arc, sources=[f"{BASE}/src"], level=5,
            solid=False, delete_src=False)
    check(f"create .{ext} with solid=False", r.get("ok") is True,
          f"{r.get('msg')} | {r['log'][-3:]}")

print("\n=== Phase 2: -mhe case-insensitive ===")
arc = f"{BASE}/UPPER.7Z"
r = run("create", archive=arc, sources=[f"{BASE}/src"], level=1, solid=True,
        password="pw123", delete_src=False)
check("create UPPER.7Z encrypted", r.get("ok") is True, str(r.get("msg")))
# header encryption means listing without a password must fail
p = subprocess.run(["7z","l","-slt",arc], capture_output=True, stdin=subprocess.DEVNULL, text=True)
check("-mhe=on applied to .7Z (header encrypted)", p.returncode != 0,
      f"rc={p.returncode}")

print("\n=== Phase 4: delete guard ===")
shutil.rmtree(f"{BASE}/g", ignore_errors=True); os.makedirs(f"{BASE}/g")
open(f"{BASE}/g/keep.txt","w").write("x\n"); open(f"{BASE}/g/.dot","w").write("y\n")
r = run("create", archive=f"{BASE}/guard.7z", sources=[f"{BASE}/g"], level=1,
        solid=True, delete_src=True, excl_hidden=True)
check("archive created", r.get("ok") is True, str(r.get("msg")))
check("sources NOT deleted", os.path.exists(f"{BASE}/g/keep.txt"))
check("message explains why", "sources kept" in str(r.get("msg")), str(r.get("msg")))
check("warning in log", any("NOT deleted" in l for l in r["log"]))

print("\n=== Phase 4: deletion still happens with no exclusions ===")
shutil.rmtree(f"{BASE}/g2", ignore_errors=True); os.makedirs(f"{BASE}/g2")
open(f"{BASE}/g2/keep.txt","w").write("x\n")
r = run("create", archive=f"{BASE}/nodel.7z", sources=[f"{BASE}/g2"], level=1,
        solid=True, delete_src=True, excl_hidden=False)
check("sources deleted when nothing excluded", not os.path.exists(f"{BASE}/g2"),
      str(r.get("msg")))

print("\n=== Phase 1: stdin=DEVNULL — encrypted ops do not hang ===")
enc = f"{BASE}/enc.7z"
r = run("create", archive=enc, sources=[f"{BASE}/src"], level=1, solid=True,
        password="s3cret", delete_src=False)
check("create encrypted .7z", r.get("ok") is True, str(r.get("msg")))
t0 = time.time()
r = run("list", archive=enc)                      # no password on purpose
el = time.time() - t0
check("list without password returns fast", el < 15, f"{el:.1f}s")
check("list without password reports __ENCRYPTED__", r.get("msg") == "__ENCRYPTED__", str(r.get("msg")))
r = run("list", archive=enc, password="s3cret")
check("list with password succeeds", r.get("ok") is True, str(r.get("msg")))
check("listing has entries", len(r.get("listing", [])) > 0)

print("\n=== Phase 3: encrypted test / delete / rename ===")
r = run("test", archive=enc, password="s3cret")
check("test with password OK", r.get("ok") is True, str(r.get("msg")))
r = run("test", archive=enc)
check("test w/o password says password, not corruption",
      r.get("ok") is False and "password" in str(r.get("msg")).lower(), str(r.get("msg")))

names = [e["Path"] for e in run("list", archive=enc, password="s3cret")["listing"]]
target = [p for p in names if p.endswith("a.txt")][0]
r = run("rename", archive=enc, old_name=target,
        new_name=target.rsplit("/",1)[0] + "/renamed.txt", password="s3cret")
check("rename inside encrypted archive", r.get("ok") is True, str(r.get("msg")))
names2 = [e["Path"] for e in run("list", archive=enc, password="s3cret")["listing"]]
check("renamed entry present", any(p.endswith("renamed.txt") for p in names2), str(names2))

r = run("delete", archive=enc, paths=[p for p in names2 if p.endswith("renamed.txt")],
        password="s3cret")
check("delete from encrypted archive", r.get("ok") is True, str(r.get("msg")))
names3 = [e["Path"] for e in run("list", archive=enc, password="s3cret")["listing"]]
check("entry gone", not any(p.endswith("renamed.txt") for p in names3))

print("\n=== Phase 3: copy into an encrypted destination ===")
dst = f"{BASE}/dst.7z"
run("create", archive=dst, sources=[f"{BASE}/src/a.txt"], level=1, solid=True,
    password="dstpw", delete_src=False)
plain = f"{BASE}/plain.7z"
run("create", archive=plain, sources=[f"{BASE}/src"], level=1, solid=True, delete_src=False)
srcnames = [e["Path"] for e in run("list", archive=plain)["listing"]]
pick = [p for p in srcnames if p.endswith("b.txt")]
r = run("copy_to_archive", src_archive=plain, src_paths=pick, dst_archive=dst,
        password="", dst_password="dstpw")
check("copy into encrypted destination", r.get("ok") is True, str(r.get("msg")))
p = subprocess.run(["7z","l","-slt",dst], capture_output=True, stdin=subprocess.DEVNULL, text=True)
check("destination stayed header-encrypted", p.returncode != 0, f"rc={p.returncode}")
r = run("list", archive=dst, password="dstpw")
check("copied entry is in the destination",
      any(e["Path"].endswith("b.txt") for e in r.get("listing", [])),
      str([e.get("Path") for e in r.get("listing", [])]))

print(f"\n{'ALL PASS' if not fails else 'FAILURES: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)
