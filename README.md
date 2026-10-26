# Notetaker

A local-first meeting notetaker for macOS. It records your microphone and
the other side of a call (Zoom, Google Meet, Chrome, anything) at the same
time, and only *after* the call ends sends the audio off for transcription
and summarization — no third-party bot joining your meetings, no
always-on cloud sync burning your bandwidth.

It was built as a lighter alternative to tools like Notion's AI meeting
notes, which join calls or sync continuously and can use surprisingly
large amounts of bandwidth for what is, in the end, just some audio and
text.

## What it does

1. **Records** your microphone and the system audio (what you hear from
   the call) as two separate tracks, using a native
   [ScreenCaptureKit](https://developer.apple.com/documentation/screencapturekit)
   helper — no virtual audio device or extra setup required.
2. **Mixes** the two tracks into a single MP3, normalizing each one
   independently first (mic and system audio are rarely at the same
   volume, and which one is quieter isn't predictable).
3. **Transcribes** the recording with OpenAI's Whisper API, auto-detecting
   whatever language was actually spoken.
4. **Summarizes** the transcript with GPT into structured notes — TL;DR,
   action items (with owner/deadline when mentioned), decisions, and
   topics with concrete facts, not a vague "budget was discussed"
   meta-summary.
5. **Saves everything locally** under `~/Documents/Notetaker/<date>/`
   (audio, transcript, notes) — that's the source of truth.
6. **Optionally syncs the notes to Notion**, creating a page under a
   parent page you choose, with a link back shown in the app.

## Requirements

- macOS 13 (Ventura) or later, **Apple Silicon** (the bundled `soundfile`
  library only ships an arm64 binary, so Intel Macs aren't supported)
- [Homebrew](https://brew.sh) + `ffmpeg`: `brew install ffmpeg`
- An [OpenAI API key](https://platform.openai.com/api-keys) (for Whisper +
  GPT — this is the only paid dependency; a few minutes of audio costs a
  fraction of a cent)
- Optionally, a [Notion integration token](https://www.notion.so/my-integrations)
  if you want notes synced to Notion

## Installing (prebuilt app)

If you have a built `Notetaker.app` (see below for how to build one):

1. Copy it to `/Applications`.
2. First launch: right-click → **Open** (the app isn't notarized by
   Apple, so Gatekeeper will otherwise refuse to open it — this is a
   one-time step).
3. Click the ⚙ icon and paste your OpenAI API key (and Notion details, if
   you want them). Keys are stored in a local file at
   `~/Library/Application Support/Notetaker/config.json`, readable only by
   your user account — not in the macOS Keychain, since the app isn't
   signed with a stable Developer ID and Keychain access would otherwise
   re-prompt for your login password on every rebuild.
4. Click record. The first time, macOS will ask for **Screen Recording**
   permission (needed to capture system audio) and **Microphone**
   permission. After granting Screen Recording, **fully quit and reopen
   the app once** — macOS only applies a fresh screen-recording grant to a
   new launch, not the process that asked for it.

## Notion setup (optional)

1. Create an integration at <https://www.notion.so/my-integrations>.
2. Open the Notion page under which you want meeting notes to appear,
   click **"..."** → **Connections**, and add your integration — without
   this, the API will return a 404 (no access), not a helpful error.
3. In the app's settings, paste the integration token and the parent
   page's ID (the long ID in its URL).

## Building from source

```bash
git clone https://github.com/fredd4/notetaker.git
cd notetaker

python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt

# Compiles native/sysaudio.swift into build/sysaudio (universal arm64+x86_64)
./scripts/build_helper.sh

# Renders the app icon into build/Notetaker.icns
./scripts/build_icon.sh
```

Run it directly, without packaging:

```bash
./.venv/bin/python -m notetaker.app
```

Or build a real, standalone `.app` (embeds its own Python runtime, so it
no longer depends on this `.venv`):

```bash
./.venv/bin/python setup.py py2app
codesign --force --deep --sign - dist/Notetaker.app
cp -R dist/Notetaker.app /Applications/
```

Note: with ad-hoc signing (no paid Apple Developer account), every
rebuild produces a new code signature, which invalidates any
previously-granted microphone/screen-recording permissions. If you
rebuild and permissions stop working, reset them and grant them again:

```bash
tccutil reset ScreenCapture local.greg.notetaker
tccutil reset Microphone local.greg.notetaker
```

## Project layout

```
native/sysaudio.swift     Native helper: captures system audio via ScreenCaptureKit
notetaker/audio/          Microphone recording, device listing, mixing (ffmpeg + loudness normalization)
notetaker/pipeline/       Transcription (Whisper) and note generation (GPT)
notetaker/outputs/        Writes local notes/transcript files, creates the Notion page
notetaker/session.py      Orchestrates one recording session end-to-end
notetaker/app.py          pywebview GUI application + JS bridge
ui/index.html             The interface itself
setup.py                  py2app packaging config
scripts/build_helper.sh   Compiles the native audio helper
scripts/build_icon.sh     Renders the app icon (scripts/make_icon.swift) into an .icns
```

## Known limitations

- Apple Silicon only.
- Not notarized — first launch needs a right-click → Open.
- Ad-hoc signed — rebuilding invalidates granted permissions (see above).
- Requires `ffmpeg` on the system `PATH` (via Homebrew); it isn't bundled.
