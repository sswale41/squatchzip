#!/bin/bash
# Headless regression suite for SquatchZip.  Needs python3-PyQt6 and p7zip.
# Runs offscreen, so no display is required.  ~2 minutes.
cd "$(dirname "$0")"
pass=0; fail=0
for t in t_worker t_tar t_gui t_integ t_listing t_cancel; do
    timeout 560 python3 "$t.py" > "/tmp/squatchzip_$t.log" 2>&1 && rc=0 || rc=1
    printf '%-12s %s\n' "$t" "$(tail -1 "/tmp/squatchzip_$t.log")"
    grep '  FAIL  ' "/tmp/squatchzip_$t.log"
    [ $rc -eq 0 ] && pass=$((pass+1)) || fail=$((fail+1))
done
echo
echo "suites passed: $pass   failed: $fail"
exit $([ $fail -eq 0 ] && echo 0 || echo 1)
