# CLAUDE.md

Guidance for AI coding assistants (Claude Code etc.) working on **Klartext** — a
private, 100% local macOS menu-bar app that transcribes and summarizes WhatsApp
voice messages. Read this before editing.

## The one rule that matters most

**Never commit user data. Klartext is read-only and local.**
- Never write to WhatsApp's databases. Never add message-sending. It reads only.
- Never commit transcripts, caches, `.sqlite` files, `.opus`/`.wav`, `config.json`,
  logs, or anything derived from real chats. The `.gitignore` enforces this — keep it strict.
- No cloud calls except pulling the models once during install.

## What this is (architecture)

One file: **`app.py`** (~1400 lines), a PyObjC agent. No framework, no build step.

```
NSStatusItem (waveform icon) -> NSPopover -> WKWebView -> inline HTML/CSS/JS UI
                                   ^                          |
                                   |   window.webkit.messageHandlers.bridge   (JS -> Python)
                                   |   webview.evaluateJavaScript(...)         (Python -> JS)
```

`app.py` is organized top-to-bottom as:
1. **Constants** — WhatsApp container paths, model paths, Ollama config, `DEFAULT_CFG`.
2. **Data layer (read-only SQLite)** — `list_chats`, `list_messages`, `resolve_meta`,
   `avatar_uri`, `search_messages`, `resolve_mentions`, `_contact_name`, `media_abspath`.
3. **AI / media** — `transcribe` (ffmpeg + whisper.cpp), `summarize` / `translate` (Ollama).
4. **Caches** — `cache_file`/`load_cache`/`save_cache` (transcripts), `sum_file`/… (summaries).
5. **`AppDelegate`** — status item, popover, WebView, the JS bridge, per-message actions,
   the auto-transcribe scanner.
6. **`UI_HTML`** — the entire front end as one raw string (3 screens + settings + search).
7. **`main()`** — single-instance `flock`, then the Cocoa run loop.

## Run / develop / verify

```bash
# reload the running agent after editing app.py
launchctl kickstart -k gui/$(id -u)/com.local.watranscribe

# syntax check
~/.wa-transcribe/venv/bin/python -m py_compile ~/.wa-transcribe/app.py

# logs
tail -f ~/.wa-transcribe/app.log
```

**Verify a UI change without clicking the menu bar** — render `UI_HTML` with mock data
in any browser. The Python bridge won't exist, but `post()` swallows that, and you can
drive the screens directly:

```js
window.__chats([{pk:1,name:"Alex",kind:"dm",avatar:null,time:"Heute 12:00",preview:"hi"}], "ok");
window.__messages({pk:1,name:"Alex",kind:"dm",avatar:null},
  [{pk:9,kind:"voice",me:false,time:"12:01",sender:"Alex",cached:false}]);
window.__tx(9, "transcript text");     // fills the voice card
window.__sum(9, "- point one");        // summary card
window.__settings({auto:false,translate_to:"Englisch"});
```

Model test (data layer works headless):
```bash
~/.wa-transcribe/venv/bin/python -c "import importlib.util as u; \
s=u.spec_from_file_location('a','$HOME/.wa-transcribe/app.py'); m=u.module_from_spec(s); \
s.loader.exec_module(m); print(len(m.list_chats(5)))"
```

## PyObjC gotchas (these will bite you)

- **Every method on an `NSObject` subclass is treated as an Objective-C selector.**
  Any helper that isn't an ObjC callback and takes arguments **must** be decorated
  `@objc.python_method`, or you get `objc.BadPrototypeError` at import. Only these stay
  bare selectors: `init`, `applicationDidFinishLaunching_`, `toggle_`,
  `userContentController_didReceiveScriptMessage_`.
- **Touch the WebView only on the main thread.** All DB/whisper/Ollama work runs in
  `threading.Thread`; deliver results back with `AppHelper.callAfter(self._eval, js)`.
  `_open_chat` / `_send_chats` are threaded on purpose — never block the run loop.
- Pass Python → JS by building a JS string with `json.dumps(...)` for every value.

## WhatsApp data model (undocumented — verify before trusting)

Container (needs **Full Disk Access**):
`~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/`

- Voice files: `Message/Media/**/<uuid>.opus`.
  **`ZWAMEDIAITEM.ZMEDIALOCALPATH` is stored as `Media/...` but the files live under
  `Message/Media/...`** — join against `<container>/Message`, not the container root.
  (`media_abspath` handles this; do **not** reintroduce a recursive `glob` — it scans
  ~1.4 GB and hangs the UI.)
- `ChatStorage.sqlite`: `ZWACHATSESSION` (chats: `ZPARTNERNAME`, `ZCONTACTJID`,
  `ZSESSIONTYPE` 0=dm/1=group, `ZLASTMESSAGE`), `ZWAMESSAGE`
  (`ZMESSAGETYPE` **0=text, 3=voice**, 1=image, 2=video; `ZISFROMME`, `ZTEXT`,
  `ZMESSAGEDATE`, `ZGROUPMEMBER`), `ZWAGROUPMEMBER.ZMEMBERJID`.
- `ContactsV2.sqlite`: `ZWAADDRESSBOOKCONTACT` maps `ZLID` ⟷ `ZWHATSAPPID` ⟷ `ZFULLNAME`.
  Group senders and `@mentions` use `@lid` ids → resolve via `ZLID`, not the phone number.
- Profile pics: `Media/Profile/<id>-*.thumb|jpg`. Groups keyed by the group id, 1:1 by
  the contact's **@lid** number (not phone). Served inline as base64 data URIs.
- Timestamps are Core Data seconds → add `978307200` for Unix. Some rows have corrupt
  future dates; `time_label`/`time_hm` must stay guarded against `fromtimestamp` errors.

## JS ⟷ Python bridge

JS sends `post({action, ...})`; Python replies by calling a `window.__*` function.

| action (JS→Py) | Python | reply (Py→JS) |
|---|---|---|
| `chats` | `_send_chats` | `__chats(list, access)` |
| `open {pk}` | `_open_chat` | `__messages(chat, msgs)` |
| `transcribe {pk}` | `_do_transcribe` | `__tx(pk, text)` |
| `summary {pk}` | `_do_summary` | `__sum(pk, text)` |
| `translate {pk}` | `_do_translate` | `__tr(pk, text)` |
| `search {q}` | `_do_search` | `__search(q, results)` |
| `settings` / `setcfg {key,value}` | `_send_settings` / `_set_cfg` | `__settings(cfg)` |
| `copy` / `openfolder` / `fda` / `quit` | — | — |

`pk` = `ZWAMESSAGE.Z_PK`; the app keeps a `self.msgmap` from `pk` to `{path, sender, chat}`.

## UI conventions

- Palette is black / white / grey with a **"liquid glass"** treatment
  (`backdrop-filter` on translucent cards, CSS vars in `:root` + a dark `@media` block).
  Keep light **and** dark working.
- **Icons are inline SVG line icons — never emojis.** Loading states are **skeleton
  shimmer blocks (`.sk`) — never spinners.**
- User-facing text is German; keep code/identifiers ASCII.

## Configuration

`~/.wa-transcribe/config.json`: `auto`, `auto_summary`, `translate_to`, `n_chats`,
`n_messages`, `lang`. Models: whisper `large-v3-turbo`, Ollama `qwen2.5:7b`.
