#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────
#  SquatchZip Installer — openSUSE Tumbleweed / KDE Plasma 6
#
#  Usage:
#    ./install.sh            — install for current user (~/.local)
#    ./install.sh --system   — install system-wide (/usr/bin, /usr/share)
#                              requires root / sudo
# ──────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="squatchzip"

GREEN="\e[32m"; YELLOW="\e[33m"; RED="\e[31m"; RESET="\e[0m"
ok()   { echo -e "${GREEN}✓${RESET}  $*"; }
warn() { echo -e "${YELLOW}⚠${RESET}  $*"; }
err()  { echo -e "${RED}✗${RESET}  $*"; }
info() { echo -e "   $*"; }

# ── Mode selection ────────────────────────────────────────────

SYSTEM=false
for arg in "$@"; do
    [[ "$arg" == "--system" ]] && SYSTEM=true
done

if $SYSTEM; then
    if [[ "$EUID" -ne 0 ]]; then
        err "--system install requires root. Run with sudo:"
        info "  sudo ./install.sh --system"
        exit 1
    fi
    BIN_DIR="/usr/bin"
    APP_DIR="/usr/share/applications"
    SVC_DIR="/usr/share/kio/servicemenus"
    ICON_BASE="/usr/share/icons/hicolor"
    MODE_LABEL="system-wide"
else
    BIN_DIR="$HOME/.local/bin"
    APP_DIR="$HOME/.local/share/applications"
    SVC_DIR="$HOME/.local/share/kio/servicemenus"
    ICON_BASE="$HOME/.local/share/icons/hicolor"
    MODE_LABEL="user ($HOME/.local)"
fi

echo ""
echo "  SquatchZip Installer"
echo "  ───────────────────"
echo "  Mode: $MODE_LABEL"
echo ""

# ── Check / install dependencies ──────────────────────────────

check_dep() {
    local pkg="$1" cmd="${2:-$1}"
    if ! command -v "$cmd" &>/dev/null; then
        warn "$cmd not found — installing $pkg …"
        sudo zypper install -y "$pkg" || {
            err "Failed to install $pkg. Please install manually:  sudo zypper install $pkg"
            exit 1
        }
    fi
    ok "$cmd"
}

echo "Checking dependencies:"
check_dep p7zip 7z
check_dep python3-PyQt6 python3

if ! python3 -c "from PyQt6.QtWidgets import QApplication" 2>/dev/null; then
    warn "PyQt6 not importable — trying python3-PyQt6 …"
    sudo zypper install -y python3-PyQt6 || {
        err "PyQt6 install failed. Try:  sudo zypper install python3-PyQt6"
        exit 1
    }
fi
ok "PyQt6"

echo ""

# ── Create directories ────────────────────────────────────────

mkdir -p "$BIN_DIR" "$APP_DIR" "$SVC_DIR"

# ── Install main script ───────────────────────────────────────

install -m 755 "$SCRIPT_DIR/${APP}.py" "$BIN_DIR/${APP}"
ok "Installed:  $BIN_DIR/${APP}"

# ── Install icon (hicolor icon theme) ────────────────────────

ICON_SRC="$SCRIPT_DIR/${APP}.svg"
if [[ ! -f "$ICON_SRC" ]]; then
    err "Icon not found: $ICON_SRC"
    err "Make sure squatchzip.svg is in the same directory as install.sh"
    exit 1
fi

for SIZE in scalable 256 128 64 48 32 22 16; do
    if [[ "$SIZE" == "scalable" ]]; then
        ICON_DIR="$ICON_BASE/scalable/apps"
        mkdir -p "$ICON_DIR"
        install -m 644 "$ICON_SRC" "$ICON_DIR/${APP}.svg"
        ok "Installed icon (scalable SVG)"
    else
        ICON_DIR="$ICON_BASE/${SIZE}x${SIZE}/apps"
        mkdir -p "$ICON_DIR"
        if command -v inkscape &>/dev/null; then
            inkscape "$ICON_SRC" -w "$SIZE" -h "$SIZE" \
                -o "$ICON_DIR/${APP}.png" 2>/dev/null \
                || install -m 644 "$ICON_SRC" "$ICON_DIR/${APP}.svg"
        elif command -v rsvg-convert &>/dev/null; then
            rsvg-convert -w "$SIZE" -h "$SIZE" "$ICON_SRC" \
                -o "$ICON_DIR/${APP}.png" 2>/dev/null \
                || install -m 644 "$ICON_SRC" "$ICON_DIR/${APP}.svg"
        else
            install -m 644 "$ICON_SRC" "$ICON_DIR/${APP}.svg"
        fi
    fi
done
ok "Icons installed → $ICON_BASE/"
gtk-update-icon-cache -f -t "$ICON_BASE/" 2>/dev/null || true

# ── Install .desktop launcher ─────────────────────────────────

install -m 644 "$SCRIPT_DIR/${APP}.desktop" "$APP_DIR/${APP}.desktop"
update-desktop-database "$APP_DIR" 2>/dev/null || true
ok "Installed:  $APP_DIR/${APP}.desktop"

# ── Install KDE service menu ──────────────────────────────────

install -m 644 "$SCRIPT_DIR/${APP}-servicemenu.desktop" \
               "$SVC_DIR/${APP}-servicemenu.desktop"
ok "Installed:  $SVC_DIR/${APP}-servicemenu.desktop"

# ── Rebuild KDE service cache ─────────────────────────────────

if command -v kbuildsycoca6 &>/dev/null; then
    kbuildsycoca6 --noincremental 2>/dev/null || true
elif command -v kbuildsycoca5 &>/dev/null; then
    kbuildsycoca5 --noincremental 2>/dev/null || true
fi

# ── Set as default handler for archives (optional) ────────────

echo ""
echo "Set SquatchZip as default archive handler? (replaces Ark for archive mime types)"
read -rp "  [y/N] " ans
if [[ "${ans,,}" == "y" ]]; then
    MIME_TYPES=(
        application/x-7z-compressed
        application/zip
        application/x-tar
        application/gzip
        application/x-bzip2
        application/x-xz
        application/x-compressed-tar
        application/vnd.rar
        application/x-iso9660-image
    )
    for mime in "${MIME_TYPES[@]}"; do
        xdg-mime default "${APP}.desktop" "$mime" 2>/dev/null && ok "$mime" || warn "$mime (skipped)"
    done
fi

echo ""
ok "Installation complete! ($MODE_LABEL)"
echo ""
info "Run SquatchZip:"
info "  squatchzip"
info "  squatchzip /path/to/archive.7z"
info ""
info "Right-click in Dolphin to see 'SquatchZip' context menu options."
$SYSTEM || info "You may need to restart Dolphin or log out/in for the service menu to appear."
echo ""
