"""py2app build script — produces a self-contained Notetaker.app.

Build with:  ./.venv/bin/python setup.py py2app

The resulting app embeds its own Python runtime and dependencies, so it
does not depend on this machine's .venv and can be copied to another Mac
(Apple Silicon; see README note on soundfile's arm64-only bundled library).
"""

from setuptools import setup

APP = ["main.py"]
DATA_FILES = [
    ("ui", ["ui/index.html"]),
    (".", ["build/sysaudio"]),
]

OPTIONS = {
    "argv_emulation": False,
    "packages": [
        "notetaker",
        "sounddevice",
        "soundfile",
        "_sounddevice_data",  # bundles libportaudio.dylib — must stay unzipped on disk
        "_soundfile_data",  # bundles libsndfile dylib — same reason
        "numpy",
        "webview",
        "openai",
        "httpx",
        "httpcore",
        "certifi",
        "notion_client",
    ],
    "includes": [
        "objc",
        "Cocoa",
        "Quartz",
        "WebKit",
        "Security",
        "UniformTypeIdentifiers",
    ],
    "plist": {
        "CFBundleName": "Notetaker",
        "CFBundleDisplayName": "Notetaker",
        "CFBundleIdentifier": "local.greg.notetaker",
        "CFBundleShortVersionString": "1.0",
        "CFBundleVersion": "1.0",
        "LSMinimumSystemVersion": "13.0",
        "NSHighResolutionCapable": True,
        "NSMicrophoneUsageDescription": "Notetaker records your microphone to transcribe meetings.",
    },
}

setup(
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
