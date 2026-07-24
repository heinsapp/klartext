# Klartext

**A private, 100% local macOS menu-bar app that transcribes and summarizes your WhatsApp voice messages.**

Klartext reads your WhatsApp desktop data locally, lets you browse chats, and turns voice messages into text — using [whisper.cpp](https://github.com/ggerganov/whisper.cpp) for transcription and a local [Ollama](https://ollama.com) model for summaries and translation. Nothing ever leaves your Mac. No cloud, no account, no API keys.

> Not affiliated with WhatsApp or Meta. Read-only: Klartext never sends messages or modifies your WhatsApp data.

---

## Why

WhatsApp voice messages are slow to listen to and impossible to skim. Klartext gives you the transcript, a one-glance summary, and a translation — all computed locally, so your private conversations stay private.

## Features

- **Chat list** — your recent WhatsApp chats with real names, contact/group profile pictures, and last-message preview.
- **Chat history** — text messages and voice messages in one chronological view, with `@mentions` resolved to names.
- **Local transcription** — one click turns a voice message into text (whisper.cpp, `large-v3-turbo`, auto language detection).
- **Local summaries** — a named, to-the-point summary ("Alex reminds you three times to send the video") via Ollama `qwen2.5`.
- **Local translation** — translate any transcript into a language of your choice.
- **Search** — full-text search across message text and your saved transcripts.
- **Transcript cache** — nothing is transcribed twice; readable copies are saved to `~/Transcribe-out`.
- **Auto mode** — optionally transcribe (and summarize) every new voice message as it arrives.
- **Native menu-bar app** — black/white/grey "liquid glass" UI, light & dark mode, skeleton loaders, no dock icon, starts at login.

## How it works

WhatsApp's macOS app stores its data **unencrypted** in a group container:

```
~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/
├── Message/Media/**/*.opus      # voice messages
├── ChatStorage.sqlite           # chats, messages, senders
├── ContactsV2.sqlite            # contact names, @lid <-> phone mapping
└── Media/Profile/<id>-*.thumb    # profile pictures
```

Klartext opens these databases **read-only**, resolves names/avatars, and for a voice message runs:

```
ffmpeg (opus -> 16 kHz WAV) -> whisper.cpp -> text
text -> Ollama (qwen2.5) -> summary / translation
```

The UI is a `WKWebView` popover hosted by a small PyObjC menu-bar agent (`NSStatusItem` + `NSPopover`). Everything runs on your machine.

## Requirements

- macOS 13+ (Apple Silicon recommended — an M-series chip transcribes a 30 s clip in a few seconds)
- [Homebrew](https://brew.sh)
- The WhatsApp **desktop app**, installed and logged in
- Installed automatically by the script: `whisper-cpp`, `ffmpeg`, `ollama`, Python 3 + `pyobjc`
- ~7 GB disk for the models (whisper `large-v3-turbo` ~1.6 GB, `qwen2.5:7b` ~4.7 GB)

## Install

```bash
git clone https://github.com/<your-user>/klartext.git
cd klartext
chmod +x install.sh
./install.sh
```

Then grant **Full Disk Access** so Klartext can read the WhatsApp container:

> System Settings → Privacy & Security → Full Disk Access → add the Python binary at
> `~/.wa-transcribe/venv/bin/python3` and enable it → the app restarts automatically.

The waveform icon appears in your menu bar. Click it, pick a chat, click a voice message.

## Usage

- **Transcribe** — open a chat, click **Transkribieren** under a voice message.
- **Summarize / Translate / Copy** — buttons appear under each transcript.
- **Search** — type in the search box on the chat list (searches text messages and transcripts).
- **Settings** (gear icon) — toggle auto-transcription and auto-summary, choose the translation target language, open the transcript folder, quit.

## Configuration

Settings are stored in `~/.wa-transcribe/config.json`:

| Key            | Meaning                                             | Default     |
|----------------|-----------------------------------------------------|-------------|
| `auto`         | auto-transcribe new voice messages                  | `false`     |
| `auto_summary` | auto-summarize right after transcription            | `false`     |
| `translate_to` | target language for the Translate button            | `Englisch`  |
| `n_chats`      | number of chats to list                             | `30`        |
| `n_messages`   | messages loaded per chat                             | `80`        |

The summary/translation model and prompts live near the top of `app.py`.

## Privacy

- **Fully local.** Transcription and summarization run on your Mac; no network calls except pulling the models once during install.
- **Read-only.** Klartext only reads WhatsApp's databases. It never writes to them and never sends messages.
- **Your data stays yours.** Transcripts are cached under `~/.wa-transcribe/cache` and `~/Transcribe-out`. The `.gitignore` in this repo makes sure none of that is ever committed.

## Limitations

- Reading WhatsApp's local SQLite schema is **undocumented** and may break with future WhatsApp updates.
- macOS + WhatsApp desktop only. Requires Full Disk Access.
- Summaries/translation need Ollama running (`brew services start ollama`).
- Sending messages is intentionally out of scope (would require WhatsApp's encrypted transport and violate its terms).

## Uninstall

```bash
launchctl bootout gui/$(id -u)/com.local.klartext
rm -rf ~/.wa-transcribe ~/Library/LaunchAgents/com.local.klartext.plist
# optional: rm -rf ~/Transcribe-out ~/.whisper-models
```

## License

MIT — see [LICENSE](LICENSE).
