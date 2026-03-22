#!/usr/bin/env python3
"""
Build script for Canopy macOS app.

This script:
1. Builds venvstacks layers (runtime + framework + app)
2. Creates macOS .app bundle
3. Signs the app (ad-hoc)
4. Packages into DMG

Usage:
    python build.py              # Build everything
    python build.py --skip-venv  # Skip venvstacks build (use existing)
    python build.py --dmg-only   # Only create DMG from existing build
"""

import argparse
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
PROJECT_DIR = SCRIPT_DIR.parent
BUILD_DIR = SCRIPT_DIR / "_build"
EXPORT_DIR = SCRIPT_DIR / "_export"
DIST_DIR = SCRIPT_DIR / "dist"
APP_NAME = "Canopy"
APP_BUNDLE = f"{APP_NAME}.app"
BUNDLE_ID = "com.canopy.app"


def _read_version() -> str:
    version_file = PROJECT_DIR / "canopy" / "_version.py"
    content = version_file.read_text()
    match = re.search(r'__version__\s*=\s*"([^"]+)"', content)
    if not match:
        raise RuntimeError(f"Cannot find __version__ in {version_file}")
    return match.group(1)


VERSION = _read_version()


def run_cmd(cmd, **kwargs):
    print(f"  → {' '.join(str(c) for c in cmd)}")
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        print(f"  ✗ Command failed (exit {result.returncode})")
        sys.exit(1)
    return result


def clean_all(preserve_venv: bool = False):
    print("\n[Clean] Removing build artifacts...")
    dirs_to_clean = [DIST_DIR]
    if not preserve_venv:
        dirs_to_clean.extend([BUILD_DIR, EXPORT_DIR])

    for d in dirs_to_clean:
        if d.exists():
            shutil.rmtree(d, onerror=lambda f, p, e: (os.chmod(p, 0o777), f(p)))
            print(f"  Removed {d.name}/")
    print("  ✓ Clean complete\n")


# === Phase 1: Venvstacks ===


def build_venvstacks():
    print("\n[1/4] Building venvstacks layers...")

    toml_file = SCRIPT_DIR / "venvstacks.toml"

    # Lock
    print("\n  Locking environments...")
    run_cmd([
        "pipx", "run", "venvstacks", "lock",
        str(toml_file), "--if-needed",
    ])

    # Build
    print("\n  Building environments (this may take a while)...")
    run_cmd([
        "pipx", "run", "venvstacks", "build",
        str(toml_file), "--no-lock",
    ])

    # Export
    print("\n  Exporting environments...")
    if EXPORT_DIR.exists():
        shutil.rmtree(EXPORT_DIR)
    run_cmd([
        "pipx", "run", "venvstacks", "local-export",
        str(toml_file),
        "--output-dir", str(EXPORT_DIR),
    ])

    return EXPORT_DIR


# === Phase 2: App Bundle ===


def _create_c_launcher(macos_dir: Path):
    """Compile native Mach-O launcher that loads Python in-process."""
    launcher_c = macos_dir / "_launcher.c"
    launcher_c.write_text(r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <limits.h>
#include <dlfcn.h>
#include <mach-o/dyld.h>

typedef int (*py_bytes_main_fn)(int, char **);

static void show_error(const char *msg) {
    char cmd[2048];
    snprintf(cmd, sizeof(cmd),
        "osascript -e 'display dialog \"%s\" buttons {\"OK\"} "
        "default button 1 with icon stop with title \"Canopy\"'",
        msg);
    system(cmd);
}

int main(int argc, char *argv[]) {
    char exe_buf[PATH_MAX];
    char resolved[PATH_MAX];
    uint32_t size = sizeof(exe_buf);

    if (_NSGetExecutablePath(exe_buf, &size) != 0) {
        show_error("Failed to get executable path.");
        return 1;
    }
    if (!realpath(exe_buf, resolved)) {
        show_error("Failed to resolve executable path.");
        return 1;
    }

    /* Trim executable name to get MacOS/ directory */
    char *slash = strrchr(resolved, '/');
    if (!slash) { show_error("Invalid path."); return 1; }
    *slash = '\0';
    char macos_dir[PATH_MAX];
    strncpy(macos_dir, resolved, sizeof(macos_dir) - 1);

    /* Trim MacOS to get Contents/ directory */
    slash = strrchr(resolved, '/');
    if (!slash) { show_error("Invalid bundle structure."); return 1; }
    *slash = '\0';
    char contents_dir[PATH_MAX];
    strncpy(contents_dir, resolved, sizeof(contents_dir) - 1);

    /* Detect Python layer directory: Python/ (release) or Frameworks/ (dev) */
    char layers_dir[PATH_MAX];
    snprintf(layers_dir, sizeof(layers_dir), "%s/Python", contents_dir);
    if (access(layers_dir, F_OK) != 0) {
        snprintf(layers_dir, sizeof(layers_dir), "%s/Frameworks", contents_dir);
        if (access(layers_dir, F_OK) != 0) {
            show_error("Python runtime not found in app bundle.");
            return 1;
        }
    }

    /* Set PYTHONHOME */
    char pythonhome[PATH_MAX];
    snprintf(pythonhome, sizeof(pythonhome), "%s/cpython-3.11", layers_dir);
    setenv("PYTHONHOME", pythonhome, 1);

    /* Set PYTHONPATH */
    char pythonpath[PATH_MAX * 4];
    snprintf(pythonpath, sizeof(pythonpath),
        "%s/Resources:%s/app-canopy-app/lib/python3.11/site-packages:"
        "%s/framework-canopy-framework/lib/python3.11/site-packages",
        contents_dir, layers_dir, layers_dir);
    setenv("PYTHONPATH", pythonpath, 1);

    /* Prevent .pyc generation at runtime */
    setenv("PYTHONDONTWRITEBYTECODE", "1", 1);

    /* Ensure bundled python3 exists */
    char python_bin[PATH_MAX];
    snprintf(python_bin, sizeof(python_bin), "%s/python3", macos_dir);
    if (access(python_bin, X_OK) != 0) {
        show_error("Python executable not found in app bundle.");
        return 1;
    }

    /* Load bundled libpython and run -m canopy_app in-process */
    char libpython[PATH_MAX];
    snprintf(libpython, sizeof(libpython), "%s/lib/libpython3.11.dylib", contents_dir);
    void *py = dlopen(libpython, RTLD_NOW | RTLD_GLOBAL);
    if (!py) {
        char err[1024];
        snprintf(err, sizeof(err), "Failed to load libpython: %s", dlerror());
        show_error(err);
        return 1;
    }

    py_bytes_main_fn py_bytes_main = (py_bytes_main_fn)dlsym(py, "Py_BytesMain");
    if (!py_bytes_main) {
        char err[1024];
        snprintf(err, sizeof(err), "Failed to resolve Py_BytesMain: %s", dlerror());
        show_error(err);
        return 1;
    }

    char *py_argv[] = {"Canopy", "-m", "canopy_app", NULL};
    int rc = py_bytes_main(3, py_argv);
    return rc;
}
''')

    launcher_bin = macos_dir / APP_NAME
    result = subprocess.run(
        ["cc", "-arch", "arm64", "-mmacosx-version-min=15.0", "-O2",
         "-o", str(launcher_bin), str(launcher_c)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"  ✗ Launcher compilation failed: {result.stderr}")
        sys.exit(1)

    launcher_c.unlink()
    launcher_bin.chmod(0o755)


def create_app_bundle():
    print("\n[2/4] Creating app bundle...")

    app_dir = DIST_DIR / APP_BUNDLE
    contents_dir = app_dir / "Contents"
    macos_dir = contents_dir / "MacOS"
    resources_dir = contents_dir / "Resources"
    frameworks_dir = contents_dir / "Frameworks"

    if app_dir.exists():
        shutil.rmtree(app_dir)

    macos_dir.mkdir(parents=True)
    resources_dir.mkdir(parents=True)
    frameworks_dir.mkdir(parents=True)

    # Copy venvstacks layers to Frameworks
    print("  Copying Python environment...")
    for layer in ["cpython-3.11", "canopy-framework", "canopy-app"]:
        # venvstacks exports with prefixes: framework-, app-
        possible_names = [layer, f"framework-{layer}", f"app-{layer}"]
        for name in possible_names:
            src = EXPORT_DIR / name
            if src.exists():
                dst = frameworks_dir / name
                shutil.copytree(src, dst, symlinks=True)
                print(f"    Copied {name}")
                break

    # Copy venvstacks metadata
    venvstacks_meta = EXPORT_DIR / "__venvstacks__"
    if venvstacks_meta.exists():
        shutil.copytree(venvstacks_meta, frameworks_dir / "__venvstacks__", symlinks=True)

    # Copy canopy_app (menubar app) to Resources
    print("  Copying canopy_app...")
    canopy_app_src = SCRIPT_DIR / "canopy_app"
    shutil.copytree(canopy_app_src, resources_dir / "canopy_app",
                     ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    # Copy canopy (server package) to Resources
    print("  Copying canopy package...")
    canopy_src = PROJECT_DIR / "canopy"
    shutil.copytree(canopy_src, resources_dir / "canopy",
                     ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    # Copy Python binary into MacOS/
    print("  Copying Python runtime into MacOS/...")
    src_python = frameworks_dir / "cpython-3.11" / "bin" / "python3"
    dst_python = macos_dir / "python3"
    shutil.copy2(src_python, dst_python)
    dst_python.chmod(0o755)

    # Symlink libpython
    lib_dir = contents_dir / "lib"
    lib_dir.mkdir(exist_ok=True)
    (lib_dir / "libpython3.11.dylib").symlink_to(
        "../Frameworks/cpython-3.11/lib/libpython3.11.dylib"
    )

    # Create C launcher
    print("  Creating launcher...")
    _create_c_launcher(macos_dir)

    # Create Info.plist
    print("  Creating Info.plist...")
    info_plist = {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleVersion": VERSION,
        "CFBundleShortVersionString": VERSION,
        "CFBundleExecutable": APP_NAME,
        "CFBundlePackageType": "APPL",
        "CFBundleSignature": "????",
        "CFBundleIconFile": "AppIcon",
        "LSMinimumSystemVersion": "15.0",
        "LSUIElement": True,  # Menubar app, no dock icon
        "NSHighResolutionCapable": True,
        "LSArchitecturePriority": ["arm64"],
        "NSHumanReadableCopyright": f"Canopy v{VERSION}",
    }
    with open(contents_dir / "Info.plist", "wb") as f:
        plistlib.dump(info_plist, f)

    # Create a simple app icon
    _create_icon(resources_dir)

    print(f"  ✓ Created {app_dir}")
    return app_dir


def _create_icon(resources_dir: Path):
    """Create app icon — a tree emoji rendered to ICNS."""
    try:
        # Try using AppKit to render an emoji to PNG
        from AppKit import NSFont, NSString, NSMakeRect, NSImage, NSBitmapImageRep, NSPNGFileType
        from AppKit import NSGraphicsContext, NSCompositingOperationSourceOver

        sizes = [1024, 512, 256, 128, 64, 32, 16]
        iconset_dir = resources_dir / "AppIcon.iconset"
        iconset_dir.mkdir(exist_ok=True)

        for size in sizes:
            img = NSImage.alloc().initWithSize_((size, size))
            img.lockFocus()
            emoji = NSString.stringWithString_("🌲")
            font = NSFont.systemFontOfSize_(size * 0.8)
            attrs = {
                "NSFont": font,
            }
            emoji.drawAtPoint_withAttributes_((size * 0.1, size * 0.05), attrs)
            img.unlockFocus()

            rep = NSBitmapImageRep.alloc().initWithData_(img.TIFFRepresentation())
            png_data = rep.representationUsingType_properties_(NSPNGFileType, {})

            # Write standard and @2x sizes
            if size <= 512:
                png_data.writeToFile_atomically_(str(iconset_dir / f"icon_{size}x{size}.png"), True)
            if size >= 32:
                half = size // 2
                if half >= 16:
                    png_data.writeToFile_atomically_(str(iconset_dir / f"icon_{half}x{half}@2x.png"), True)

        # Convert iconset to icns
        result = subprocess.run(
            ["iconutil", "-c", "icns", str(iconset_dir), "-o", str(resources_dir / "AppIcon.icns")],
            capture_output=True,
        )
        shutil.rmtree(iconset_dir)

        if result.returncode == 0:
            print("    Created app icon (AppKit)")
            return
    except Exception as e:
        print(f"    Icon creation failed ({e}), skipping")


# === Phase 3: Sign ===


def sign_app(app_dir: Path):
    print("\n[3/4] Signing app bundle...")
    result = subprocess.run(
        ["codesign", "--force", "--deep", "--sign", "-", str(app_dir)],
        capture_output=True,
    )
    if result.returncode != 0:
        codesig = app_dir / "Contents" / "_CodeSignature"
        if codesig.exists():
            shutil.rmtree(codesig)
        print("  ⚠ Deep signing failed (expected for dev builds), running unsigned")
    else:
        print(f"  ✓ Signed {app_dir}")


# === Phase 4: DMG ===


def create_dmg(app_dir: Path):
    print("\n[4/4] Creating DMG...")

    dmg_path = DIST_DIR / f"{APP_NAME}-{VERSION}.dmg"
    dmg_staging = DIST_DIR / "_dmg_staging"

    if dmg_path.exists():
        dmg_path.unlink()
    if dmg_staging.exists():
        shutil.rmtree(dmg_staging)

    dmg_staging.mkdir(parents=True)
    shutil.copytree(app_dir, dmg_staging / APP_BUNDLE, symlinks=True)

    # Applications symlink for drag-and-drop install
    (dmg_staging / "Applications").symlink_to("/Applications")

    print("  Creating DMG with Applications shortcut...")
    run_cmd([
        "hdiutil", "create",
        "-volname", APP_NAME,
        "-srcfolder", str(dmg_staging),
        "-ov", "-format", "UDZO",
        str(dmg_path),
    ])

    shutil.rmtree(dmg_staging)
    print(f"  ✓ Created {dmg_path}")
    return dmg_path


# === Main ===


def main():
    parser = argparse.ArgumentParser(description=f"Build {APP_NAME} macOS app")
    parser.add_argument("--skip-venv", action="store_true",
                        help="Skip venvstacks build (use existing)")
    parser.add_argument("--dmg-only", action="store_true",
                        help="Only create DMG from existing build")
    args = parser.parse_args()

    print(f"Building {APP_NAME} v{VERSION}")
    print("=" * 50)

    if not args.dmg_only:
        clean_all(preserve_venv=args.skip_venv)

    DIST_DIR.mkdir(parents=True, exist_ok=True)

    if args.dmg_only:
        app_dir = DIST_DIR / APP_BUNDLE
        if not app_dir.exists():
            print(f"Error: {app_dir} not found. Run full build first.")
            sys.exit(1)
        create_dmg(app_dir)
    else:
        if not args.skip_venv:
            build_venvstacks()
        elif not EXPORT_DIR.exists():
            print("Warning: No existing envs found, building venvstacks...")
            build_venvstacks()

        app_dir = create_app_bundle()
        sign_app(app_dir)
        create_dmg(app_dir)

    print(f"\n{'='*50}")
    print(f"Build complete!")
    print(f"  App: {DIST_DIR / APP_BUNDLE}")
    print(f"  DMG: {DIST_DIR / f'{APP_NAME}-{VERSION}.dmg'}")


if __name__ == "__main__":
    main()
