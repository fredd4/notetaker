"""notetaker — local recording and analysis of meeting notes."""

import os
import shutil
import sys
from pathlib import Path
from typing import Optional


def _resource_root() -> Path:
    """Where bundled resources (ui/, the sysaudio helper) live.

    Inside a py2app bundle, sys.frozen is set and the running executable is
    Contents/MacOS/<name> — resources live one level up, in Contents/Resources.
    In dev mode (running from source), it's just the project root.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent.parent / "Resources"
    return Path(__file__).resolve().parent.parent


ROOT = _resource_root()
SYSAUDIO_HELPER = ROOT / "sysaudio" if getattr(sys, "frozen", False) else ROOT / "build" / "sysaudio"

# User-visible recordings always go to ~/Documents, never inside the app
# bundle — the bundle should stay read-only and portable across machines.
RECORDINGS_DIR = Path.home() / "Documents" / "Notetaker"


def ensure_ffmpeg_on_path() -> None:
    """Makes sure ffmpeg/ffprobe (installed via Homebrew) are findable.

    Apps launched via Finder/Launchpad get a minimal PATH that doesn't
    include Homebrew's install locations, so we add the common ones
    ourselves rather than requiring the user to launch from a terminal.
    """
    extra = "/opt/homebrew/bin:/usr/local/bin"
    current = os.environ.get("PATH", "")
    if extra not in current:
        os.environ["PATH"] = f"{extra}:{current}"


def fix_ssl_cert_env() -> None:
    """Points SSL_CERT_FILE at the bundled CA certificates.

    py2app's boot script deliberately sets SSL_CERT_FILE/SSL_CERT_DIR to a
    bogus path ("…/openssl.ca/no-such-file") so a frozen app can't pick up
    the build machine's OpenSSL paths. But httpx trusts SSL_CERT_FILE first
    and crashes with FileNotFoundError when it points nowhere. certifi's
    bundle resolves correctly on disk inside the app, so use that.
    """
    try:
        import certifi

        cafile = certifi.where()
        if os.path.exists(cafile):
            os.environ["SSL_CERT_FILE"] = cafile
        else:
            os.environ.pop("SSL_CERT_FILE", None)
        os.environ.pop("SSL_CERT_DIR", None)
    except ImportError:
        pass


def check_ffmpeg_installed() -> Optional[str]:
    """Returns a user-facing error message if ffmpeg/ffprobe are missing, else None."""
    missing = [name for name in ("ffmpeg", "ffprobe") if not shutil.which(name)]
    if missing:
        return (
            f"Missing required tool(s): {', '.join(missing)}. "
            "Install with Homebrew: brew install ffmpeg"
        )
    return None
