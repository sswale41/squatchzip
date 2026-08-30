# SquatchZip 🗜️

**Modern archive manager for openSUSE Tumbleweed / KDE Plasma 6**

A clean, fast 7-Zip GUI that looks and feels like Ark — with the features PeaZip buries or misses entirely.

---

## Features

| Feature | SquatchZip |
|---|---|
| 7-Zip backend (7z, zip, tar, tar.gz, tar.bz2, tar.xz, gz, bz2) | ✓ |
| Full directory tree view with icons | ✓ |
| File size / packed size / ratio columns | ✓ |
| Drag & drop archives to open | ✓ |
| Drag & drop files onto Create dialog | ✓ |
| Compression level slider (Store → Ultra) | ✓ |
| Password / AES-256 encryption | ✓ |
| Header encryption (7z) | ✓ |
| Split into volumes | ✓ |
| **Delete source files after archiving** | ✓ ← the whole point |
| Confirmation dialog before deletion | ✓ |
| Test archive integrity | ✓ |
| Cancel a running operation | ✓ |
| Extract with full paths or flat | ✓ |
| Extract here / extract to folder | ✓ |
| Right-click service menu in Dolphin | ✓ |
| KDE native look (Breeze auto-detected) | ✓ |
| Window geometry persistence | ✓ |
| Live progress log for all operations | ✓ |
| Info panel (toggleable) | ✓ |
| Dependency checker built in | ✓ |

---

## Dependencies

```bash
sudo zypper install p7zip p7zip-full python3-PyQt6
```

---

## Install

```bash
chmod +x install.sh
./install.sh
```

The installer:
1. Checks/installs `p7zip` and `python3-PyQt6`
2. Copies `squatchzip` to `~/.local/bin/`
3. Installs the `.desktop` launcher
4. Installs the Dolphin right-click service menu
5. Optionally sets SquatchZip as default for all archive types

---

## Usage

```bash
# Launch GUI
squatchzip

# Open an archive directly
squatchzip archive.7z

# From Dolphin: right-click any file/folder → SquatchZip submenu
```

### "Delete After Archiving"

When creating an archive, the highlighted option **"Delete source files after successful archiving"** will permanently remove source files/folders using `shutil.rmtree` (folders) or `os.remove` (files) — but **only** after `7z` exits with code 0. A confirmation dialog appears before any deletion. Any deletion errors are logged but don't affect the archive.

**Sources are never deleted when the archive is incomplete.** If "Exclude hidden files"
was checked (`-xr!.*`), or if the NTFS junction-point retry had to re-run with symbolic
links excluded (`-xr!@`), the archive holds less than the source tree — so deletion is
skipped entirely and the log says which exclusion caused it. The archive still gets
created; only the deletion is suppressed.

---

## Notes on formats

`.tar.gz`, `.tar.bz2` and `.tar.xz` are built in two passes — the sources are tarred
first, then compressed — because gzip/bzip2/xz each wrap a single stream and 7-Zip
cannot produce them in one step. Extraction reverses this automatically, so you get
your files rather than an intermediate `.tar`. Browsing one of these archives still
shows the inner `.tar` as a single entry, which is what 7-Zip reports.

These three formats cannot be encrypted; a password entered for them is ignored, with a
warning in the log. Use `.7z` or `.zip` for an encrypted archive.

---

## Uninstall

```bash
rm ~/.local/bin/squatchzip
rm ~/.local/share/applications/squatchzip.desktop
rm ~/.local/share/kio/servicemenus/squatchzip-servicemenu.desktop
rm -rf ~/.local/share/icons/hicolor/{scalable,256x256,128x128,64x64,48x48,32x32,22x22,16x16}/apps/squatchzip.*
gtk-update-icon-cache -f -t ~/.local/share/icons/hicolor/
kbuildsycoca6 --noincremental
```
