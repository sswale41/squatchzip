#!/usr/bin/env bash
# Replace the hand-placed SquatchZip files with the packaged build from OBS.
#
# Right now the machine has two unmanaged copies — /usr/bin/squatchzip (2.3.0)
# and /usr/local/bin/squatchzip (2.2.1) — and /usr/local/bin comes first in
# PATH, so typing "squatchzip" runs the OLD one.  Neither is owned by a package,
# so zypper would refuse to install over them.  This removes both, adds the OBS
# repo, and installs squatchzip properly.
#
#   sudo bash install-from-obs.sh
set -euo pipefail

REPO_URL="https://download.opensuse.org/repositories/home:/sswale41/openSUSE_Tumbleweed/"
REPO_ALIAS="home_sswale41"

[[ $EUID -eq 0 ]] || { echo "Run with sudo: sudo bash $0" >&2; exit 1; }

echo "── Removing unmanaged copies ─────────────────────────────"
for f in /usr/bin/squatchzip \
         /usr/local/bin/squatchzip \
         /usr/share/applications/squatchzip.desktop \
         /usr/share/kio/servicemenus/squatchzip-servicemenu.desktop \
         /usr/share/icons/hicolor/scalable/apps/squatchzip.svg; do
    if [[ -e "$f" ]]; then
        # Never touch anything a package owns.
        if rpm -qf "$f" >/dev/null 2>&1; then
            echo "  keeping  $f  (owned by $(rpm -qf "$f"))"
        else
            rm -f "$f" && echo "  removed  $f"
        fi
    fi
done

echo "── Adding the OBS repository ─────────────────────────────"
if zypper lr "$REPO_ALIAS" >/dev/null 2>&1; then
    echo "  repo already present"
else
    zypper --non-interactive addrepo --refresh "$REPO_URL" "$REPO_ALIAS"
fi
zypper --non-interactive --gpg-auto-import-keys refresh "$REPO_ALIAS"

echo "── Installing ────────────────────────────────────────────"
zypper --non-interactive install --allow-vendor-change squatchzip

echo "── Refreshing desktop caches ─────────────────────────────"
command -v kbuildsycoca6      >/dev/null && kbuildsycoca6 --noincremental >/dev/null 2>&1 || true
command -v update-desktop-database >/dev/null && update-desktop-database /usr/share/applications || true
command -v gtk-update-icon-cache   >/dev/null && gtk-update-icon-cache -f -t /usr/share/icons/hicolor || true

echo
echo "── Result ────────────────────────────────────────────────"
rpm -q squatchzip
echo "  binary:   $(command -v squatchzip)"
echo "  owned by: $(rpm -qf "$(command -v squatchzip)")"
grep -m1 '^VERSION' "$(command -v squatchzip)"
echo
echo "Done.  Run 'hash -r' in any open shell so it picks up the new path."
