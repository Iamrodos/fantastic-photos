#!/usr/bin/env python3
"""
Launcher for Fantastic Photos.

Checks whether a newer fantastic_photos.py has been published, asks before replacing
the local copy, then starts the app.

Standard library only, so it runs before anything is installed.
"""

import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "fantastic_photos.py")
BACKUP = os.path.join(HERE, "fantastic_photos.previous.py")

# Where new versions are published. A GitHub raw URL or an S3 object URL both
# work — it only needs to be a plain HTTPS GET that returns the file.
#   GitHub: https://raw.githubusercontent.com/<user>/<repo>/main/fantastic_photos.py
#   S3:     https://<bucket>.s3.<region>.amazonaws.com/fantastic_photos.py
UPDATE_URL = os.environ.get(
    "FANTASTIC_PHOTOS_UPDATE_URL",
    "https://raw.githubusercontent.com/Iamrodos/fantastic-photos/main/fantastic_photos.py",
)

TIMEOUT = 6


def version_of(text):
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.M)
    return m.group(1) if m else None


def local_version():
    try:
        with open(APP, encoding="utf-8") as f:
            return version_of(f.read())
    except OSError:
        return None


def fetch_remote():
    """(text, version) or (None, reason) — never raises."""
    if "CHANGE-ME" in UPDATE_URL:
        return None, "update URL not configured yet"
    try:
        req = urllib.request.Request(
            UPDATE_URL, headers={"Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            text = r.read().decode("utf-8")
        v = version_of(text)
        if not v:
            return None, "downloaded file has no version marker"
        return text, v
    except urllib.error.HTTPError as e:
        return None, f"server said {e.code}"
    except Exception as e:
        return None, f"{type(e).__name__}"


def ask(question, default_yes=True):
    suffix = "[Y/n]" if default_yes else "[y/N]"
    try:
        a = input(f"{question} {suffix} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    if not a:
        return default_yes
    return a.startswith("y")


def check_for_update():
    have = local_version()
    print(f"Fantastic Photos {have or '(unknown version)'}")

    text, info = fetch_remote()
    if text is None:
        # Offline, or not configured. Carry on with what we have.
        print(f"  (no update check: {info})")
        return

    remote = info
    if remote == have:
        print("  up to date")
        return

    print(f"\n  A different version is available: {have or '?'}  ->  {remote}")
    if not ask("  Update now?"):
        print("  Keeping the current version.")
        return

    try:
        if os.path.exists(APP):
            shutil.copy2(APP, BACKUP)
        with open(APP, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"  Updated to {remote}. Previous version kept as "
              f"{os.path.basename(BACKUP)}")
    except OSError as e:
        print(f"  Could not write the update: {e}")
        print("  Carrying on with the version you have.")


def run_app():
    if not os.path.exists(APP):
        print(f"\nCannot find {APP}")
        print("Download fantastic_photos.py into this folder and try again.")
        return 1

    # uv reads the dependency block at the top of fantastic_photos.py and provisions
    # everything itself. Fall back to the current interpreter if uv is absent.
    if shutil.which("uv"):
        cmd = ["uv", "run", APP]
    else:
        print("\nuv not found — falling back to this Python.")
        print("Install uv for automatic dependency handling: https://astral.sh/uv")
        try:
            import PIL  # noqa: F401
        except ImportError:
            print("\nPillow is not installed. Either install uv, or run:")
            print(f"   {sys.executable} -m pip install pillow")
            return 1
        cmd = [sys.executable, APP]

    print()
    try:
        return subprocess.call(cmd)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    if "--no-update" not in sys.argv:
        check_for_update()
    sys.exit(run_app())
