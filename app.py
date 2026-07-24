#!/usr/bin/env python3
"""
Transkript - Menu-Bar-App (nativ, PyObjC)
Status-Item (SF-Symbol waveform, template) -> Popover mit WKWebView.
UI: schwarz/weiss, minimal, Inline-SVG-Line-Icons, Light/Dark automatisch.
Listet die letzten WhatsApp-Sprachnachrichten (Mac) -> Klick -> lokale
Transkription (ffmpeg + whisper.cpp) -> Anzeige + Zwischenablage + .txt.
Laeuft als Hintergrund-Agent, kein Dock-Icon.
"""
import os
import re
import glob
import json
import time
import queue
import base64
import sys
import fcntl
import sqlite3
import threading
import subprocess
import urllib.request
import datetime as dt

import objc
from Foundation import NSObject
from AppKit import (
    NSApplication, NSApplicationActivationPolicyAccessory,
    NSStatusBar, NSVariableStatusItemLength, NSImage, NSMinYEdge,
    NSViewController,
)
from WebKit import (
    WKWebView, WKWebViewConfiguration, WKUserContentController,
)
from PyObjCTools import AppHelper

HOME = os.path.expanduser("~")
WA_MEDIA = os.path.join(
    HOME,
    "Library/Group Containers/group.net.whatsapp.WhatsApp.shared/Message/Media",
)
MODEL = os.path.join(HOME, ".whisper-models/ggml-large-v3-turbo.bin")
OUT = os.path.join(HOME, "Transcribe-out")
APPDIR = os.path.join(HOME, ".wa-transcribe")
CACHE = os.path.join(APPDIR, "cache")
CFG = os.path.join(APPDIR, "config.json")
LOG = os.path.join(APPDIR, "app.log")

GC_SHARED = os.path.join(
    HOME, "Library/Group Containers/group.net.whatsapp.WhatsApp.shared")
APPLE_EPOCH = 978307200   # Core-Data-Zeit -> Unix
CHATDB = os.path.join(
    HOME,
    "Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite",
)
CONTACTSDB = os.path.join(
    HOME,
    "Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ContactsV2.sqlite",
)
PROFILE_DIR = os.path.join(
    HOME,
    "Library/Group Containers/group.net.whatsapp.WhatsApp.shared/Media/Profile",
)

def _bin(name, *fallbacks):
    import shutil
    p = shutil.which(name)
    if p:
        return p
    for f in fallbacks:
        if os.path.exists(f):
            return f
    return name


WHISPER = _bin("whisper-cli", "/opt/homebrew/bin/whisper-cli",
               "/usr/local/bin/whisper-cli")
FFMPEG = _bin("ffmpeg", "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg")
FFPROBE = _bin("ffprobe", "/opt/homebrew/bin/ffprobe", "/usr/local/bin/ffprobe")

N_RECENT = 14
SCAN_SECONDS = 5.0
LANG = "auto"

PYPATH = os.path.realpath(sys.executable)   # echtes Binary fuer FDA-Hinweis

OLLAMA_CHAT = "http://localhost:11434/api/chat"
SUMMARY_MODEL = "qwen2.5:7b"
SUMMARY_SYS = (
    "Du fasst eine deutsche WhatsApp-Sprachnachricht in 1 bis 3 knappen, "
    "vollstaendigen Stichpunkten zusammen, jeweils beginnend mit '- '. Jeder "
    "Punkt ist eine verstaendliche Aussage - kein einzelnes Wort, keine Zeile "
    "nur mit einem Namen. Antworte ausschliesslich auf Deutsch. Keine "
    "Einleitung, keine Ueberschrift, kein Kommentar, keine anderen Sprachen."
)

HTML_PATH = os.path.join(APPDIR, "ui.html")


def log(msg):
    try:
        with open(LOG, "a") as f:
            f.write(f"{dt.datetime.now().isoformat(timespec='seconds')} {msg}\n")
    except Exception:
        pass


DEFAULT_CFG = {
    "auto": False,            # Auto-Transkription neuer Sprachnachrichten
    "auto_summary": False,    # nach Transkription automatisch zusammenfassen
    "translate_to": "Englisch",
    "n_chats": 30,
    "n_messages": 80,
    "lang": "auto",           # whisper-Sprache
    "ui_lang": "de",          # Sprache der App-Oberflaeche (de/en)
    "onboarded": False,       # First-Run-Onboarding gesehen
}


def load_cfg():
    cfg = dict(DEFAULT_CFG)
    try:
        with open(CFG) as f:
            cfg.update(json.load(f) or {})
    except Exception:
        pass
    return cfg


def save_cfg(cfg):
    try:
        with open(CFG, "w") as f:
            json.dump(cfg, f)
    except Exception as e:
        log(f"cfg save: {e}")


def pbcopy(text):
    try:
        subprocess.run(["/usr/bin/pbcopy"], input=text.encode("utf-8"), check=False)
    except Exception as e:
        log(f"pbcopy: {e}")


_dur_cache = {}


def duration(path):
    if path in _dur_cache:
        return _dur_cache[path]
    d = None
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nk=1:nw=1", path],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        d = float(out)
    except Exception:
        d = None
    _dur_cache[path] = d
    return d


def check_access():
    """ok | denied | missing  (Full Disk Access noetig fuer WA-Container)."""
    try:
        os.listdir(WA_MEDIA)
        return "ok"
    except FileNotFoundError:
        return "missing"
    except (PermissionError, OSError):
        return "denied"


def scan_recent():
    files = []
    try:
        for root, _dirs, names in os.walk(WA_MEDIA):
            for n in names:
                if n.endswith(".opus"):
                    p = os.path.join(root, n)
                    try:
                        files.append((p, os.path.getmtime(p)))
                    except OSError:
                        pass
    except Exception as e:
        log(f"scan: {e}")
    files.sort(key=lambda x: x[1], reverse=True)
    return files[:N_RECENT]


# ---- Namensaufloesung aus ChatStorage.sqlite (Chat/Gruppe/Absender) ----
_db = None
_cdb = None
_meta_cache = {}      # opus-Pfad -> dict(name, kind, sender, avatar)
_contact_cache = {}   # jid -> name
_avatar_cache = {}    # thumb-Pfad -> data-URI
_lid_cache = {}       # phone-jid -> lid


def _lid_for_phone(jid):
    """phone@s.whatsapp.net -> lid-Nummer (ContactsV2), gecached."""
    if jid in _lid_cache:
        return _lid_cache[jid]
    lid = None
    con = _cdb_conn()
    if con:
        try:
            r = con.execute(
                "SELECT ZLID FROM ZWAADDRESSBOOKCONTACT WHERE ZWHATSAPPID=? LIMIT 1",
                (jid,)).fetchone()
            if r and r["ZLID"]:
                lid = r["ZLID"].split("@")[0]
        except Exception:
            pass
    _lid_cache[jid] = lid
    return lid


def _profile_file(numeric):
    if not numeric:
        return None
    hits = sorted(glob.glob(os.path.join(PROFILE_DIR, numeric + "-*")))
    return hits[-1] if hits else None   # neuestes Thumb (Timestamp im Namen)


def avatar_uri(jid):
    """WhatsApp-Profilbild als data-URI. Gruppen per Gruppen-ID, 1:1 per lid.
    Wird pro Scan neu aufgeloest -> Bildwechsel werden uebernommen."""
    if not jid:
        return None
    num = jid.split("@")[0].split(":")[0]
    path = _profile_file(num)
    if not path and jid.endswith("@s.whatsapp.net"):
        path = _profile_file(_lid_for_phone(jid))
    if not path:
        return None
    if path in _avatar_cache:
        return _avatar_cache[path]
    uri = None
    try:
        with open(path, "rb") as f:
            uri = "data:image/jpeg;base64," + base64.b64encode(f.read()).decode()
    except Exception:
        uri = None
    _avatar_cache[path] = uri
    return uri


def _db_conn():
    global _db
    if _db is None:
        try:
            _db = sqlite3.connect(
                f"file:{CHATDB}?mode=ro", uri=True, timeout=2,
                check_same_thread=False)
            _db.row_factory = sqlite3.Row
        except Exception as e:
            log(f"db open: {e}")
            _db = None
    return _db


def _cdb_conn():
    global _cdb
    if _cdb is None:
        try:
            _cdb = sqlite3.connect(
                f"file:{CONTACTSDB}?mode=ro", uri=True, timeout=2,
                check_same_thread=False)
            _cdb.row_factory = sqlite3.Row
        except Exception as e:
            log(f"cdb open: {e}")
            _cdb = None
    return _cdb


def _contact_name(jid):
    """@lid/JID -> Anzeigename (ContactsV2, sonst ChatStorage)."""
    if not jid:
        return None
    if jid in _contact_cache:
        return _contact_cache[jid]
    name = None
    con = _cdb_conn()
    if con:
        try:
            r = con.execute(
                "SELECT ZFULLNAME, ZGIVENNAME FROM ZWAADDRESSBOOKCONTACT "
                "WHERE ZLID=? LIMIT 1", (jid,)).fetchone()
            if r:
                name = r["ZFULLNAME"] or r["ZGIVENNAME"]
        except Exception:
            pass
    if not name:
        con2 = _db_conn()
        if con2:
            try:
                r = con2.execute(
                    "SELECT ZPARTNERNAME FROM ZWACHATSESSION "
                    "WHERE ZCONTACTJID=? LIMIT 1", (jid,)).fetchone()
                if r:
                    name = r["ZPARTNERNAME"]
            except Exception:
                pass
    _contact_cache[jid] = name
    return name


_MENTION_RE = re.compile(r"@(\d{6,})")


def resolve_mentions(text):
    """@<lid-Nummer> im Text durch @Name ersetzen."""
    if not text or "@" not in text:
        return text

    def repl(mo):
        num = mo.group(1)
        name = _contact_name(num + "@lid") or _contact_name(num + "@s.whatsapp.net")
        return "@" + name if name else mo.group(0)
    return _MENTION_RE.sub(repl, text)


def resolve_meta(path):
    """opus-Pfad -> dict(name, kind='dm'|'group', sender). Gecached."""
    if path in _meta_cache:
        return _meta_cache[path]
    meta = {"name": None, "kind": "dm", "sender": None, "jid": None}
    con = _db_conn()
    if con:
        try:
            base = os.path.basename(path)
            r = con.execute(
                """SELECT cs.ZPARTNERNAME AS chat, cs.ZSESSIONTYPE AS stype,
                          cs.ZCONTACTJID AS jid,
                          m.ZISFROMME AS fromme, m.ZFROMJID AS fromjid,
                          gm.ZMEMBERJID AS memjid
                   FROM ZWAMEDIAITEM mi
                   JOIN ZWAMESSAGE m ON mi.ZMESSAGE = m.Z_PK
                   JOIN ZWACHATSESSION cs ON m.ZCHATSESSION = cs.Z_PK
                   LEFT JOIN ZWAGROUPMEMBER gm ON m.ZGROUPMEMBER = gm.Z_PK
                   WHERE mi.ZMEDIALOCALPATH LIKE ? LIMIT 1""",
                ("%" + base,)).fetchone()
            if r:
                meta["name"] = r["chat"]
                meta["jid"] = r["jid"]
                if r["stype"] == 1:
                    meta["kind"] = "group"
                    if r["fromme"]:
                        meta["sender"] = "Du"
                    else:
                        jid = r["memjid"] or r["fromjid"]
                        meta["sender"] = _contact_name(jid) or "Unbekannt"
        except Exception as e:
            log(f"resolve_meta: {e}")
    _meta_cache[path] = meta
    return meta


# ---- Chats + Verlauf (v2) ----
def time_hm(unixts):
    try:
        return dt.datetime.fromtimestamp(unixts).strftime("%H:%M")
    except (ValueError, OSError, OverflowError):
        return ""


UI_LANG = "de"
_TXT = {
    "you": {"de": "Du", "en": "You"},
    "today": {"de": "Heute", "en": "Today"},
    "yesterday": {"de": "Gestern", "en": "Yesterday"},
    "voice": {"de": "Sprachnachricht", "en": "Voice message"},
    "image": {"de": "Bild", "en": "Image"},
    "video": {"de": "Video", "en": "Video"},
    "doc": {"de": "Dokument", "en": "Document"},
}


def tp(k):
    return _TXT.get(k, {}).get(UI_LANG) or _TXT.get(k, {}).get("de") or k


def day_label(unixts):
    try:
        d = dt.datetime.fromtimestamp(unixts).date()
    except (ValueError, OSError, OverflowError):
        return ""
    today = dt.date.today()
    if d == today:
        return tp("today")
    if d == today - dt.timedelta(days=1):
        return tp("yesterday")
    return d.strftime("%d.%m.%Y")


def media_abspath(rel):
    # DB speichert 'Media/...', Dateien liegen unter '<container>/Message/Media/...'
    if not rel:
        return None
    p = os.path.join(GC_SHARED, "Message", rel)
    if os.path.exists(p):
        return p
    p2 = os.path.join(GC_SHARED, rel)
    if os.path.exists(p2):
        return p2
    return None


def set_ui_lang(lang):
    globals()["UI_LANG"] = lang if lang in ("de", "en") else "de"


def _chat_preview(ltype, ltext, lme):
    pre = (tp("you") + ": ") if lme else ""
    if ltype == 0 and (ltext or "").strip():
        return (pre + resolve_mentions(ltext).replace("\n", " "))[:64]
    phmap = {3: "voice", 1: "image", 2: "video", 8: "doc", 4: "image"}
    k = phmap.get(ltype)
    return (pre + tp(k)) if k else ""


def list_chats(limit=30):
    con = _db_conn()
    if not con:
        return []
    out = []
    now_apple = time.time() - APPLE_EPOCH + 86400
    try:
        rows = con.execute(
            """SELECT cs.Z_PK pk, cs.ZPARTNERNAME name, cs.ZCONTACTJID jid,
                      cs.ZSESSIONTYPE stype, lm.ZMESSAGEDATE ld,
                      lm.ZTEXT ltext, lm.ZMESSAGETYPE ltype, lm.ZISFROMME lme
               FROM ZWACHATSESSION cs
               LEFT JOIN ZWAMESSAGE lm ON cs.ZLASTMESSAGE = lm.Z_PK
               WHERE cs.ZPARTNERNAME IS NOT NULL AND cs.ZSESSIONTYPE IN (0,1)
                     AND COALESCE(cs.ZARCHIVED,0)=0 AND COALESCE(cs.ZHIDDEN,0)=0
                     AND COALESCE(cs.ZREMOVED,0)=0
                     AND cs.ZLASTMESSAGEDATE IS NOT NULL
                     AND cs.ZLASTMESSAGEDATE < ?
               ORDER BY cs.ZLASTMESSAGEDATE DESC LIMIT ?""",
            (now_apple, limit)).fetchall()
        for r in rows:
            out.append({
                "pk": r["pk"], "name": r["name"],
                "kind": "group" if r["stype"] == 1 else "dm",
                "avatar": avatar_uri(r["jid"]),
                "time": time_label((r["ld"] or 0) + APPLE_EPOCH),
                "preview": _chat_preview(r["ltype"], r["ltext"], r["lme"]),
            })
    except Exception as e:
        log(f"list_chats: {e}")
    return out


_KIND_BY_TYPE = {0: "text", 3: "voice", 1: "image", 2: "video", 8: "doc"}


def list_messages(chat_pk, limit=80):
    con = _db_conn()
    if not con:
        return []
    out = []
    try:
        rows = con.execute(
            """SELECT m.Z_PK pk, m.ZMESSAGETYPE t, m.ZISFROMME me, m.ZTEXT text,
                      m.ZMESSAGEDATE d, gm.ZMEMBERJID mjid, mi.ZMEDIALOCALPATH media
               FROM ZWAMESSAGE m
               LEFT JOIN ZWAGROUPMEMBER gm ON m.ZGROUPMEMBER = gm.Z_PK
               LEFT JOIN ZWAMEDIAITEM mi ON mi.ZMESSAGE = m.Z_PK
               WHERE m.ZCHATSESSION = ?
               ORDER BY m.ZMESSAGEDATE DESC LIMIT ?""", (chat_pk, limit)).fetchall()
        for r in rows:
            me = bool(r["me"])
            media = r["media"]
            if r["t"] == 3 and media and media.endswith(".opus"):
                kind = "voice"
            elif r["t"] == 0 and (r["text"] or "").strip():
                kind = "text"
            else:
                kind = _KIND_BY_TYPE.get(r["t"], "other")
            if kind == "other":
                continue
            sender = "Du" if me else (_contact_name(r["mjid"]) if r["mjid"] else None)
            path = media_abspath(media) if kind == "voice" else None
            out.append({
                "pk": r["pk"], "kind": kind, "me": me,
                "text": (r["text"] or ""),
                "time": time_hm((r["d"] or 0) + APPLE_EPOCH),
                "day": day_label((r["d"] or 0) + APPLE_EPOCH),
                "sender": sender, "path": path,
                "cached": bool(path) and os.path.exists(cache_file(path)),
            })
            if out[-1]["kind"] == "text":
                out[-1]["text"] = resolve_mentions(out[-1]["text"])
    except Exception as e:
        log(f"list_messages: {e}")
    out.reverse()
    return out


# ---- Transkript-Cache (uuid-Dateiname ist eindeutig) ----
def cache_file(path):
    base = os.path.splitext(os.path.basename(path))[0]
    return os.path.join(CACHE, base + ".txt")


def load_cache(path):
    try:
        with open(cache_file(path), encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return None


def save_cache(path, text):
    try:
        os.makedirs(CACHE, exist_ok=True)
        with open(cache_file(path), "w", encoding="utf-8") as f:
            f.write(text)
    except Exception as e:
        log(f"cache save: {e}")


def sum_file(path):
    base = os.path.splitext(os.path.basename(path))[0]
    return os.path.join(CACHE, base + ".sum.txt")


def load_sum(path):
    try:
        with open(sum_file(path), encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return None


def save_sum(path, text):
    try:
        os.makedirs(CACHE, exist_ok=True)
        with open(sum_file(path), "w", encoding="utf-8") as f:
            f.write(text)
    except Exception as e:
        log(f"sum save: {e}")


def segs_file(path):
    base = os.path.splitext(os.path.basename(path))[0]
    return os.path.join(CACHE, base + ".segs.json")


def load_segs(path):
    try:
        with open(segs_file(path), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_segs(path, segs):
    try:
        os.makedirs(CACHE, exist_ok=True)
        with open(segs_file(path), "w", encoding="utf-8") as f:
            json.dump(segs, f)
    except Exception as e:
        log(f"segs save: {e}")


def summarize(text, speaker=None, chat=None, kind=None):
    """Lokale Zusammenfassung via Ollama (qwen2.5). Wirft bei Fehler."""
    if speaker == "Du":
        ctx = "Die Nachricht wurde selbst gesendet ('du')."
    elif speaker and kind == "group" and chat:
        ctx = (f"Die Nachricht ist von {speaker} in der Gruppe '{chat}'. "
               f"Nenne {speaker} wo passend beim Namen (z. B. '{speaker} moechte ...').")
    elif speaker:
        ctx = (f"Die Nachricht ist von {speaker}. Nenne {speaker} wo passend "
               f"beim Namen (z. B. '{speaker} erinnert ...').")
    else:
        ctx = ""
    user = (ctx + "\n\nNachricht:\n" + text) if ctx else text
    body = json.dumps({
        "model": SUMMARY_MODEL, "stream": False,
        "options": {"temperature": 0.1},
        "messages": [
            {"role": "system", "content": SUMMARY_SYS},
            {"role": "user", "content": user},
        ],
    }).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_CHAT, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read().decode("utf-8"))
    return (data.get("message", {}).get("content") or "").strip()


def translate(text, target="Englisch"):
    """Lokale Uebersetzung via Ollama (qwen2.5). Wirft bei Fehler."""
    sysmsg = (
        f"Du bist ein professioneller Uebersetzer. Uebersetze den Text des "
        f"Nutzers vollstaendig und natuerlich nach {target}. Gib AUSSCHLIESSLICH "
        f"die Uebersetzung aus - keine Einleitung, keine Erklaerung, keine "
        f"Anfuehrungszeichen, keine andere Sprache."
    )
    body = json.dumps({
        "model": SUMMARY_MODEL, "stream": False,
        "options": {"temperature": 0.2},
        "messages": [
            {"role": "system", "content": sysmsg},
            {"role": "user", "content": text},
        ],
    }).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_CHAT, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read().decode("utf-8"))
    return (data.get("message", {}).get("content") or "").strip()


def time_label(mtime):
    try:
        t = dt.datetime.fromtimestamp(mtime)
    except (ValueError, OSError, OverflowError):
        return ""
    today = dt.date.today()
    if t.date() == today:
        return "Heute " + t.strftime("%H:%M")
    if t.date() == today - dt.timedelta(days=1):
        return "Gestern " + t.strftime("%H:%M")
    return t.strftime("%d.%m.%Y %H:%M")


def dur_label(path):
    d = duration(path)
    if d is None:
        return ""
    m, s = divmod(int(round(d)), 60)
    return f"{m}:{s:02d}"


def transcribe(path):
    """opus -> (text, segments). segments = [{'s':start_s,'e':end_s,'t':text}]."""
    ts = int(time.time() * 1000)
    wav = os.path.join("/tmp", f"watx_{os.getpid()}_{ts}.wav")
    of = os.path.join("/tmp", f"watx_{os.getpid()}_{ts}")
    try:
        subprocess.run(
            [FFMPEG, "-y", "-i", path, "-ar", "16000", "-ac", "1",
             "-c:a", "pcm_s16le", wav],
            capture_output=True, check=True,
        )
        subprocess.run(
            [WHISPER, "-m", MODEL, "-l", LANG, "-np", "-ml", "60", "-sow",
             "-oj", "-of", of, "-f", wav],
            capture_output=True, text=True, check=True,
        )
        segs = []
        try:
            with open(of + ".json", encoding="utf-8") as f:
                data = json.load(f)
            for s in data.get("transcription", []):
                t = (s.get("text") or "").strip()
                if not t:
                    continue
                off = s.get("offsets") or {}
                segs.append({"s": (off.get("from") or 0) / 1000.0,
                             "e": (off.get("to") or 0) / 1000.0, "t": t})
        finally:
            try:
                os.remove(of + ".json")
            except OSError:
                pass
        text = " ".join(s["t"] for s in segs).strip()
        return text, segs
    finally:
        try:
            os.remove(wav)
        except OSError:
            pass


_audio_cache = {}


def audio_datauri(path):
    """opus -> abspielbares AAC/m4a als data-URI (fuer <audio> im WebView)."""
    if path in _audio_cache:
        return _audio_cache[path]
    ts = int(time.time() * 1000)
    m4a = os.path.join("/tmp", f"watxa_{os.getpid()}_{ts}.m4a")
    uri = None
    try:
        subprocess.run(
            [FFMPEG, "-y", "-i", path, "-c:a", "aac", "-b:a", "64k", m4a],
            capture_output=True, check=True,
        )
        with open(m4a, "rb") as f:
            uri = "data:audio/mp4;base64," + base64.b64encode(f.read()).decode()
    except Exception as e:
        log(f"audio: {e}")
    finally:
        try:
            os.remove(m4a)
        except OSError:
            pass
    _audio_cache[path] = uri
    return uri


def search_messages(q, limit=30):
    """Volltextsuche in Nachrichtentexten + Transkript-Cache."""
    q = (q or "").strip()
    if len(q) < 2:
        return []
    out = []
    seen = set()
    con = _db_conn()
    if con:
        try:
            rows = con.execute(
                """SELECT cs.Z_PK cpk, cs.ZPARTNERNAME name, cs.ZSESSIONTYPE st,
                          m.ZTEXT text, m.ZMESSAGEDATE d
                   FROM ZWAMESSAGE m
                   JOIN ZWACHATSESSION cs ON m.ZCHATSESSION = cs.Z_PK
                   WHERE m.ZTEXT LIKE ? AND cs.ZPARTNERNAME IS NOT NULL
                         AND cs.ZSESSIONTYPE IN (0,1)
                   ORDER BY m.ZMESSAGEDATE DESC LIMIT ?""",
                ("%" + q + "%", limit)).fetchall()
            for r in rows:
                key = (r["cpk"], r["text"])
                if key in seen:
                    continue
                seen.add(key)
                out.append({
                    "cpk": r["cpk"], "name": r["name"],
                    "kind": "group" if r["st"] == 1 else "dm",
                    "snippet": (r["text"] or "").replace("\n", " ")[:80],
                    "src": "text",
                })
        except Exception as e:
            log(f"search msg: {e}")
    # Transkripte durchsuchen
    try:
        ql = q.lower()
        for fn in os.listdir(CACHE):
            if not fn.endswith(".txt") or fn.endswith(".sum.txt"):
                continue
            fp = os.path.join(CACHE, fn)
            try:
                with open(fp, encoding="utf-8") as f:
                    txt = f.read()
            except Exception:
                continue
            if ql in txt.lower():
                uuid = fn[:-4]
                info = _chat_for_uuid(uuid)
                if info:
                    i = txt.lower().find(ql)
                    snip = txt[max(0, i - 20):i + 60].replace("\n", " ")
                    out.append({
                        "cpk": info["cpk"], "name": info["name"],
                        "kind": info["kind"], "snippet": "..." + snip + "...",
                        "src": "Sprachnachricht",
                    })
    except Exception as e:
        log(f"search tx: {e}")
    return out[:limit]


def search_chats(q, limit=15):
    """Chats deren Name zur Suche passt."""
    q = (q or "").strip()
    if len(q) < 2:
        return []
    con = _db_conn()
    if not con:
        return []
    out = []
    try:
        rows = con.execute(
            """SELECT Z_PK pk, ZPARTNERNAME name, ZCONTACTJID jid, ZSESSIONTYPE st
               FROM ZWACHATSESSION
               WHERE ZPARTNERNAME LIKE ? AND ZSESSIONTYPE IN (0,1)
                     AND COALESCE(ZARCHIVED,0)=0 AND COALESCE(ZHIDDEN,0)=0
                     AND COALESCE(ZREMOVED,0)=0
               ORDER BY ZLASTMESSAGEDATE DESC LIMIT ?""",
            ("%" + q + "%", limit)).fetchall()
        for r in rows:
            out.append({"cpk": r["pk"], "name": r["name"],
                        "kind": "group" if r["st"] == 1 else "dm",
                        "avatar": avatar_uri(r["jid"])})
    except Exception as e:
        log(f"search_chats: {e}")
    return out


def _chat_for_uuid(uuid):
    con = _db_conn()
    if not con:
        return None
    try:
        r = con.execute(
            """SELECT cs.Z_PK cpk, cs.ZPARTNERNAME name, cs.ZSESSIONTYPE st
               FROM ZWAMEDIAITEM mi
               JOIN ZWAMESSAGE m ON mi.ZMESSAGE = m.Z_PK
               JOIN ZWACHATSESSION cs ON m.ZCHATSESSION = cs.Z_PK
               WHERE mi.ZMEDIALOCALPATH LIKE ? LIMIT 1""",
            ("%" + uuid + "%",)).fetchone()
        if r and r["name"]:
            return {"cpk": r["cpk"], "name": r["name"],
                    "kind": "group" if r["st"] == 1 else "dm"}
    except Exception:
        pass
    return None


class AppDelegate(NSObject):
    def init(self):
        self = objc.super(AppDelegate, self).init()
        if self is None:
            return None
        self.cfg = load_cfg()
        set_ui_lang(self.cfg.get("ui_lang", "de"))
        self.chats = []
        self.msgmap = {}          # msg-pk -> dict(path, sender, chat, chatkind)
        self.cur_chat = None
        self.busy = False
        self.started = time.time()
        self._auto_done = set()
        os.makedirs(OUT, exist_ok=True)
        os.makedirs(CACHE, exist_ok=True)
        return self

    # ---- lifecycle ----
    def applicationDidFinishLaunching_(self, _notification):
        log("launched v2")
        bar = NSStatusBar.systemStatusBar()
        self.status = bar.statusItemWithLength_(NSVariableStatusItemLength)
        btn = self.status.button()
        img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            "waveform", "Klartext")
        if img is not None:
            img.setTemplate_(True)
            btn.setImage_(img)
        else:
            btn.setTitle_("Klartext")
        btn.setTarget_(self)
        btn.setAction_("toggle:")

        cfg = WKWebViewConfiguration.alloc().init()
        ucc = WKUserContentController.alloc().init()
        ucc.addScriptMessageHandler_name_(self, "bridge")
        cfg.setUserContentController_(ucc)
        frame = ((0, 0), (380, 560))
        self.web = WKWebView.alloc().initWithFrame_configuration_(frame, cfg)
        try:
            self.web.setValue_forKey_(False, "drawsBackground")
        except Exception:
            pass
        self.web.loadHTMLString_baseURL_(UI_HTML, None)

        vc = NSViewController.alloc().init()
        vc.setView_(self.web)
        from AppKit import NSPopover, NSPopoverBehaviorTransient
        self.pop = NSPopover.alloc().init()
        self.pop.setContentSize_((380, 560))
        self.pop.setBehavior_(NSPopoverBehaviorTransient)
        self.pop.setContentViewController_(vc)
        self.pop.setAnimates_(True)

        threading.Thread(target=self._auto_loop, daemon=True).start()

    def toggle_(self, sender):
        if self.pop.isShown():
            self.pop.performClose_(sender)
            return
        btn = self.status.button()
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.pop.showRelativeToRect_ofView_preferredEdge_(
            btn.bounds(), btn, NSMinYEdge)
        self._send_chats()

    # ---- JS bridge ----
    def userContentController_didReceiveScriptMessage_(self, ucc, message):
        try:
            body = message.body()
            action = str(body.get("action", ""))
        except Exception as e:
            log(f"bridge: {e}")
            return
        if action == "chats":
            self._send_chats()
        elif action == "open":
            self._open_chat(int(body.get("pk")))
        elif action == "transcribe":
            self._do_transcribe(int(body.get("pk")))
        elif action == "play":
            self._do_play(int(body.get("pk")))
        elif action == "summary":
            self._do_summary(int(body.get("pk")))
        elif action == "translate":
            self._do_translate(int(body.get("pk")))
        elif action == "search":
            self._do_search(str(body.get("q", "")))
        elif action == "settings":
            self._send_settings()
        elif action == "setcfg":
            self._set_cfg(str(body.get("key")), body.get("value"))
        elif action == "onboarded":
            self.cfg["onboarded"] = True
            save_cfg(self.cfg)
        elif action == "checkaccess":
            self._eval("window.__access(%s);" % json.dumps(check_access()))
        elif action == "copy":
            pbcopy(str(body.get("text", "")))
        elif action == "openfolder":
            subprocess.run(["/usr/bin/open", OUT], check=False)
        elif action == "fda":
            subprocess.run(
                ["/usr/bin/open",
                 "x-apple.systempreferences:com.apple.preference.security"
                 "?Privacy_AllFiles"], check=False)
        elif action == "quit":
            NSApplication.sharedApplication().terminate_(None)

    # ---- eval ----
    @objc.python_method
    def _eval(self, js):
        try:
            self.web.evaluateJavaScript_completionHandler_(js, None)
        except Exception as e:
            log(f"eval: {e}")

    @objc.python_method
    def _access(self):
        return check_access()

    # ---- chats ----
    @objc.python_method
    def _send_chats(self):
        def work():
            try:
                chats = list_chats(self.cfg.get("n_chats", 30))
            except Exception as e:
                log(f"send_chats: {e}")
                chats = []
            self.chats = chats
            acc = check_access()
            AppHelper.callAfter(self._eval, "window.__chats(%s, %s, %s);" % (
                json.dumps(chats), json.dumps(acc),
                json.dumps(self.cfg.get("ui_lang", "de"))))
            if not self.cfg.get("onboarded"):
                AppHelper.callAfter(
                    self._eval, "window.__onboard(%s);" % json.dumps(PYPATH))
        threading.Thread(target=work, daemon=True).start()

    @objc.python_method
    def _open_chat(self, pk):
        chat = next((c for c in self.chats if c["pk"] == pk), None)
        name = chat["name"] if chat else ""
        kind = chat["kind"] if chat else "dm"
        avatar = chat["avatar"] if chat else None

        def work():
            try:
                msgs = list_messages(pk, self.cfg.get("n_messages", 80))
            except Exception as e:
                log(f"open_chat: {e}")
                msgs = []
            msgmap = {}
            payload = []
            for msg in msgs:
                mp = msg["pk"]
                msgmap[mp] = {
                    "path": msg["path"], "sender": msg["sender"],
                    "chat": name, "chatkind": kind,
                }
                item = {
                    "pk": mp, "kind": msg["kind"], "me": msg["me"],
                    "time": msg["time"], "sender": msg["sender"],
                    "text": msg["text"] if msg["kind"] == "text" else "",
                    "cached": msg["cached"],
                }
                if msg["kind"] == "voice" and msg["cached"] and msg["path"]:
                    item["tx"] = load_cache(msg["path"]) or ""
                    item["sum"] = load_sum(msg["path"]) or ""
                    item["segs"] = load_segs(msg["path"]) or []
                payload.append(item)
            self.msgmap = msgmap
            self.cur_chat = {"pk": pk, "name": name, "kind": kind, "avatar": avatar}
            AppHelper.callAfter(self._eval, "window.__messages(%s, %s);" % (
                json.dumps(self.cur_chat), json.dumps(payload)))
        threading.Thread(target=work, daemon=True).start()

    # ---- transcribe / summary / translate (per Nachricht) ----
    @objc.python_method
    def _do_transcribe(self, mp):
        info = self.msgmap.get(mp)
        if not info or not info.get("path"):
            return
        path = info["path"]
        cached = load_cache(path)
        if cached is not None:
            pbcopy(cached)
            self._eval("window.__tx(%s, %s, %s);" % (
                json.dumps(mp), json.dumps(cached), json.dumps(load_segs(path))))
            if self.cfg.get("auto_summary"):
                self._do_summary(mp)
            return
        if self.busy:
            self._eval("window.__tx(%s, %s, %s);" % (
                json.dumps(mp), json.dumps("Bitte warten - laeuft schon..."), "[]"))
            return
        self.busy = True

        def work():
            try:
                text, segs = transcribe(path)
                save_cache(path, text)
                save_segs(path, segs)
                stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M")
                safe = "".join(c for c in (info.get("chat") or "") if c not in "/:\\")[:40]
                try:
                    with open(os.path.join(OUT, f"{safe} {stamp}.txt"),
                              "w", encoding="utf-8") as f:
                        f.write(text + "\n")
                except Exception:
                    pass
                pbcopy(text)
                AppHelper.callAfter(
                    self._eval,
                    "window.__tx(%s, %s, %s);" % (
                        json.dumps(mp), json.dumps(text or "(leer)"), json.dumps(segs)))
                if self.cfg.get("auto_summary") and text:
                    s = summarize(text, info.get("sender"), info.get("chat"),
                                  info.get("chatkind"))
                    if s:
                        save_sum(path, s)
                    AppHelper.callAfter(
                        self._eval,
                        "window.__sum(%s, %s);" % (json.dumps(mp), json.dumps(s or "")))
            except Exception as e:
                log(f"transcribe: {e}")
                AppHelper.callAfter(
                    self._eval,
                    "window.__tx(%s, %s);" % (json.dumps(mp), json.dumps("Fehler bei Transkription.")))
            finally:
                self.busy = False
        threading.Thread(target=work, daemon=True).start()

    @objc.python_method
    def _do_play(self, mp):
        info = self.msgmap.get(mp)
        if not info or not info.get("path"):
            return
        path = info["path"]

        def work():
            uri = audio_datauri(path)
            AppHelper.callAfter(
                self._eval,
                "window.__audio(%s, %s);" % (json.dumps(mp), json.dumps(uri or "")))
        threading.Thread(target=work, daemon=True).start()

    @objc.python_method
    def _do_summary(self, mp):
        info = self.msgmap.get(mp)
        if not info or not info.get("path"):
            return
        path = info["path"]
        cached = load_sum(path)
        if cached is not None:
            self._eval("window.__sum(%s, %s);" % (json.dumps(mp), json.dumps(cached)))
            return
        text = load_cache(path)
        if not text:
            self._eval("window.__sum(%s, %s);" % (
                json.dumps(mp), json.dumps("Bitte zuerst transkribieren.")))
            return

        def work():
            try:
                s = summarize(text, info.get("sender"), info.get("chat"),
                              info.get("chatkind"))
                if s:
                    save_sum(path, s)
                AppHelper.callAfter(
                    self._eval,
                    "window.__sum(%s, %s);" % (json.dumps(mp), json.dumps(s or "(leer)")))
            except Exception as e:
                log(f"summary: {e}")
                AppHelper.callAfter(
                    self._eval,
                    "window.__sum(%s, %s);" % (json.dumps(mp), json.dumps("Fehlgeschlagen. Laeuft Ollama?")))
        threading.Thread(target=work, daemon=True).start()

    @objc.python_method
    def _do_translate(self, mp):
        info = self.msgmap.get(mp)
        if not info or not info.get("path"):
            return
        text = load_cache(info["path"])
        if not text:
            self._eval("window.__tr(%s, %s);" % (
                json.dumps(mp), json.dumps("Bitte zuerst transkribieren.")))
            return
        target = self.cfg.get("translate_to", "Englisch")

        def work():
            try:
                t = translate(text, target)
                AppHelper.callAfter(
                    self._eval,
                    "window.__tr(%s, %s);" % (json.dumps(mp), json.dumps(t or "(leer)")))
            except Exception as e:
                log(f"translate: {e}")
                AppHelper.callAfter(
                    self._eval,
                    "window.__tr(%s, %s);" % (json.dumps(mp), json.dumps("Uebersetzung fehlgeschlagen.")))
        threading.Thread(target=work, daemon=True).start()

    # ---- search ----
    @objc.python_method
    def _do_search(self, q):
        def work():
            try:
                chats = search_chats(q)
                content = search_messages(q)
            except Exception as e:
                log(f"search: {e}")
                chats, content = [], []
            AppHelper.callAfter(
                self._eval,
                "window.__search(%s, %s, %s);" % (
                    json.dumps(q), json.dumps(chats), json.dumps(content)))
        threading.Thread(target=work, daemon=True).start()

    # ---- settings ----
    @objc.python_method
    def _send_settings(self):
        self._eval("window.__settings(%s);" % json.dumps(self.cfg))

    @objc.python_method
    def _set_cfg(self, key, value):
        if key:
            self.cfg[key] = value
            save_cfg(self.cfg)
            if key == "ui_lang":
                set_ui_lang(value)

    # ---- auto-scanner ----
    @objc.python_method
    def _auto_loop(self):
        while True:
            try:
                if self.cfg.get("auto"):
                    for p, mt in scan_recent():
                        if (p not in self._auto_done and mt >= self.started
                                and load_cache(p) is None):
                            self._auto_done.add(p)
                            try:
                                t, segs = transcribe(p)
                                save_cache(p, t)
                                save_segs(p, segs)
                                if self.cfg.get("auto_summary") and t:
                                    meta = resolve_meta(p)
                                    spk = (meta.get("sender")
                                           if meta.get("kind") == "group"
                                           else meta.get("name"))
                                    s = summarize(t, spk, meta.get("name"),
                                                  meta.get("kind"))
                                    if s:
                                        save_sum(p, s)
                            except Exception as e:
                                log(f"auto: {e}")
            except Exception as e:
                log(f"auto_loop: {e}")
            time.sleep(SCAN_SECONDS)


# ------------------------------------------------------------------ UI (HTML)
UI_HTML = r"""<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root{
    --ink:#0a0a0c; --sub:#66666e; --line:#dcdce2; --hover:#ececf0;
    --card:#ffffff; --btn:#0a0a0c; --btnink:#fff; --bubble:#e6e6ec;
    --panel:rgba(247,247,250,.80); --glass:rgba(255,255,255,.62);
    --glassbrd:rgba(0,0,0,.11); --elev:0 6px 22px rgba(0,0,0,.10);
    --ring:rgba(0,0,0,.06); --accent:#0a0a0c;
  }
  @media (prefers-color-scheme: dark){
    :root{--ink:#fbfbfd; --sub:#9c9ca6; --line:#343439; --hover:#2c2c32;
      --card:#161618; --btn:#fbfbfd; --btnink:#0a0a0c; --bubble:#303036;
      --panel:rgba(18,18,21,.78); --glass:rgba(48,48,54,.52);
      --glassbrd:rgba(255,255,255,.13); --elev:0 8px 28px rgba(0,0,0,.55);
      --ring:rgba(255,255,255,.09); --accent:#fbfbfd;}
  }
  *{box-sizing:border-box; -webkit-user-select:none; user-select:none;}
  html,body{margin:0; height:100%;}
  body{font:13px/1.4 -apple-system,BlinkMacSystemFont,"SF Pro Text",sans-serif;
    color:var(--ink); background:transparent; overflow:hidden;
    -webkit-font-smoothing:antialiased;}
  .screen{position:absolute; inset:0; display:flex; flex-direction:column;
    background:var(--panel);
    -webkit-backdrop-filter:blur(34px) saturate(1.8); backdrop-filter:blur(34px) saturate(1.8);}
  #s-chats{background:var(--panel);}
  .over{transform:translateX(100%); transition:transform .26s cubic-bezier(.32,.72,0,1);}
  .over.show{transform:translateX(0);}
  header{display:flex; align-items:center; gap:9px; padding:13px 15px 9px;}
  .wm{display:flex; align-items:center; gap:8px;}
  .wm h1{margin:0; font-size:13px; font-weight:600; letter-spacing:.06em;
    text-transform:uppercase;}
  .sp{flex:1;}
  .ib{display:inline-flex; align-items:center; justify-content:center;
    width:30px; height:30px; border-radius:8px; color:var(--sub);
    transition:background .15s,color .15s; cursor:default;}
  .ib:hover{background:var(--hover); color:var(--ink);}
  .hair{height:1px; background:var(--line); margin:0 12px;}
  .search{padding:4px 14px 10px;}
  .search input{width:100%; height:36px; border:1px solid var(--glassbrd);
    border-radius:10px; background:var(--glass);
    -webkit-backdrop-filter:blur(10px); backdrop-filter:blur(10px);
    color:var(--ink); padding:0 12px; font-size:13px; outline:none;
    -webkit-user-select:text; user-select:text;}
  .search input:focus{border-color:var(--sub);}
  .list{flex:1; overflow-y:auto; padding:4px 8px 10px;}
  .row{display:flex; align-items:center; gap:11px; padding:9px 10px;
    border-radius:11px; cursor:default; transition:background .12s;}
  .row:hover{background:var(--hover);}
  .av{flex:none; width:38px; height:38px; border-radius:50%;
    display:flex; align-items:center; justify-content:center;
    background:var(--hover); color:var(--ink); overflow:hidden;
    box-shadow:inset 0 0 0 1px var(--ring);}
  .av img{width:100%; height:100%; object-fit:cover;}
  .meta{flex:1; min-width:0;}
  .t{font-size:13.5px; font-weight:600; white-space:nowrap; overflow:hidden;
    text-overflow:ellipsis;}
  .d{font-size:11.5px; color:var(--sub); margin-top:1px; white-space:nowrap;
    overflow:hidden; text-overflow:ellipsis;}
  .rt{font-size:10.5px; color:var(--sub); flex:none; align-self:flex-start;
    margin-top:3px;}
  .empty{height:100%; display:flex; flex-direction:column; gap:10px;
    align-items:center; justify-content:center; color:var(--sub);
    text-align:center; padding:0 30px;}
  .empty p{margin:0; font-size:12.5px; line-height:1.5;}
  /* chat history */
  .chead{display:flex; align-items:center; gap:9px; padding:11px 12px 9px;}
  .back{display:flex; align-items:center; color:var(--sub); cursor:default;}
  .back:hover{color:var(--ink);}
  .cav{width:28px; height:28px; border-radius:50%; overflow:hidden; flex:none;
    background:var(--hover); display:flex; align-items:center; justify-content:center;}
  .cav img{width:100%;height:100%;object-fit:cover;}
  .cname{font-size:14px; font-weight:600; white-space:nowrap; overflow:hidden;
    text-overflow:ellipsis;}
  .msgs{flex:1; overflow-y:auto; padding:10px 12px 14px; display:flex;
    flex-direction:column; gap:7px;}
  .daysep{align-self:center; margin:6px 0 2px;}
  .daysep span{font-size:10.5px; color:var(--sub); background:var(--glass);
    border:1px solid var(--glassbrd); padding:3px 11px; border-radius:20px;
    -webkit-backdrop-filter:blur(10px); backdrop-filter:blur(10px);}
  .m{max-width:82%; display:flex; flex-direction:column;}
  .m.me{align-self:flex-end; align-items:flex-end;}
  .sname{font-size:10.5px; color:var(--sub); margin:0 4px 2px;}
  .bub{padding:8px 12px; border-radius:16px; font-size:13.5px; line-height:1.42;
    background:var(--bubble); border:1px solid var(--glassbrd);
    -webkit-user-select:text; user-select:text; word-wrap:break-word;}
  .m.me .bub{background:var(--btn); color:var(--btnink); border:none;
    box-shadow:0 2px 9px var(--ring);}
  .bt{font-size:9.5px; color:var(--sub); margin:2px 5px 0;}
  .m.me .bt{text-align:right;}
  .ph{font-style:italic; color:var(--sub);}
  /* voice card */
  .vc{align-self:stretch; max-width:100%; border:1px solid var(--glassbrd);
    border-radius:16px; padding:11px 13px; margin:3px 0; background:var(--glass);
    -webkit-backdrop-filter:blur(16px) saturate(1.7); backdrop-filter:blur(16px) saturate(1.7);
    box-shadow:var(--elev);}
  .vc.me{background:var(--glass);}
  .vhead{display:flex; align-items:center; gap:9px;}
  .vic{width:30px; height:30px; border-radius:8px; background:var(--hover);
    display:flex; align-items:center; justify-content:center; flex:none; color:var(--ink);}
  .vmeta{flex:1; min-width:0;}
  .vn{font-size:12.5px; font-weight:600;}
  .vd{font-size:11px; color:var(--sub); margin-top:1px;}
  .mini{height:30px; padding:0 13px; border-radius:9px; border:none;
    background:var(--btn); color:var(--btnink); font-size:12px; font-weight:600;
    display:inline-flex; align-items:center; gap:6px; cursor:default; flex:none;
    box-shadow:0 2px 10px var(--ring);}
  .mini:active{opacity:.7; transform:scale(.98);}
  .vbody{margin-top:9px;}
  .txt{font-size:13.5px; line-height:1.5; color:var(--ink); white-space:pre-wrap;
    -webkit-user-select:text; user-select:text;}
  .acts{display:flex; gap:7px; margin-top:9px; flex-wrap:wrap;}
  .chip{height:28px; padding:0 11px; border-radius:8px; border:1px solid var(--line);
    background:transparent; color:var(--ink); font-size:11.5px; font-weight:600;
    display:inline-flex; align-items:center; gap:5px; cursor:default;}
  .chip:hover{background:var(--hover);}
  .seg{border-radius:4px; padding:0 1px; transition:background .1s,color .1s;}
  .seg.active{background:var(--accent); color:var(--btnink);}
  .player{display:flex; align-items:center; gap:11px; margin:2px 0 9px;}
  .pbtn{width:36px; height:36px; border-radius:50%; border:none; flex:none;
    background:var(--btn); color:var(--btnink); display:flex; align-items:center;
    justify-content:center; cursor:default; box-shadow:0 2px 12px var(--ring);}
  .pbtn:active{transform:scale(.95);}
  .pbtn svg{width:15px; height:15px;}
  .wave{flex:1; display:flex; align-items:center; gap:2px; height:30px; cursor:default;}
  .wave i{flex:1; min-width:2px; background:var(--line); border-radius:2px;
    transition:background .12s;}
  .wave i.on{background:var(--accent);}
  .ptime{font-size:10.5px; color:var(--sub); min-width:30px; text-align:right;
    font-variant-numeric:tabular-nums;}
  .vtop{display:flex; align-items:center; gap:10px;}
  .vsub{font-size:11px; color:var(--sub); margin:5px 2px 2px 47px;}
  .speed{height:24px; min-width:36px; padding:0 7px; border-radius:7px;
    border:1px solid var(--glassbrd); background:transparent; color:var(--ink);
    font-size:11px; font-weight:700; flex:none; cursor:default;
    font-variant-numeric:tabular-nums;}
  .speed:hover{background:var(--hover);}
  .ib2{width:32px; height:30px; border-radius:8px; border:1px solid var(--glassbrd);
    background:transparent; color:var(--ink); display:inline-flex; align-items:center;
    justify-content:center; cursor:default;}
  .ib2:hover{background:var(--hover);}
  .ib2.ok{background:var(--btn); color:var(--btnink); border-color:transparent;}
  .txbtn{height:32px; padding:0 14px; border-radius:9px; border:none;
    background:var(--btn); color:var(--btnink); font-size:12px; font-weight:600;
    display:inline-flex; align-items:center; gap:6px; cursor:default;
    box-shadow:0 2px 10px var(--ring);}
  .card2{border:1px solid var(--glassbrd); border-radius:12px; padding:10px 12px;
    margin-top:10px; background:var(--hover); box-shadow:0 1px 6px var(--ring);}
  .c2h{font-size:10px; font-weight:700; letter-spacing:.05em; text-transform:uppercase;
    color:var(--sub); display:flex; align-items:center; gap:5px; margin-bottom:6px;}
  .c2b{font-size:13px; line-height:1.5; color:var(--ink); white-space:pre-wrap;
    -webkit-user-select:text; user-select:text;}
  /* settings */
  .sect{padding:6px 16px; overflow-y:auto; flex:1;}
  .srow{display:flex; align-items:center; gap:12px; padding:12px 2px;
    border-bottom:1px solid var(--line);}
  .srow .lab{flex:1;}
  .srow .lab .n{font-size:13.5px; font-weight:500;}
  .srow .lab .h{font-size:11px; color:var(--sub); margin-top:1px;}
  .sw{width:40px; height:24px; border-radius:24px; background:var(--line);
    position:relative; transition:background .2s; flex:none;}
  .sw::after{content:""; position:absolute; top:2px; left:2px; width:20px; height:20px;
    border-radius:50%; background:#fff; box-shadow:0 1px 2px rgba(0,0,0,.3);
    transition:transform .2s;}
  .sw.on{background:var(--btn);} .sw.on::after{transform:translateX(16px); background:var(--btnink);}
  select{height:30px; border-radius:8px; border:1px solid var(--line);
    background:var(--card); color:var(--ink); font-size:12.5px; padding:0 6px;}
  .srow.link .n{color:var(--ink);}
  .foot{padding:10px 16px 14px; font-size:10.5px; color:var(--sub); text-align:center;}
  .sk{background:linear-gradient(90deg,var(--hover) 25%,var(--line) 37%,var(--hover) 63%);
    background-size:400% 100%; animation:shim 1.3s ease infinite; border-radius:7px;}
  @keyframes shim{0%{background-position:100% 0} 100%{background-position:-100% 0}}
  .skln{height:11px; margin:8px 0; border-radius:6px;}
  .skrow{display:flex; align-items:center; gap:11px; padding:9px 10px;}
  .skcirc{width:38px;height:38px;border-radius:50%;flex:none;}
  .fade{animation:fx .3s ease;} @keyframes fx{from{opacity:0;transform:translateY(3px);}to{opacity:1;}}
  svg{display:block;}
  .cnt{display:flex; align-items:center; justify-content:center; height:100%;}
  .onb{z-index:20;}
  .onbwrap{flex:1; display:flex; flex-direction:column; padding:22px 24px 18px;}
  .onbstep{flex:1; display:flex; flex-direction:column; align-items:center;
    justify-content:center; text-align:center; gap:11px;}
  .onbicon{width:70px; height:70px; border-radius:22px; background:var(--btn);
    color:var(--btnink); display:flex; align-items:center; justify-content:center;
    box-shadow:var(--elev);}
  .onbicon.lite{background:var(--hover); color:var(--ink); box-shadow:none;}
  .onbstep h2{margin:6px 0 0; font-size:20px; font-weight:700; letter-spacing:-.01em;}
  .onbsub{margin:0; font-size:13px; line-height:1.5; color:var(--sub); max-width:300px;}
  .langseg{display:flex; gap:8px; margin-top:8px;}
  .langseg button{height:36px; padding:0 18px; border-radius:10px; border:1px solid var(--glassbrd);
    background:transparent; color:var(--ink); font-size:13px; font-weight:600; cursor:default;}
  .langseg button.on{background:var(--btn); color:var(--btnink); border-color:transparent;}
  .featrow{display:flex; align-items:center; gap:13px; width:100%; text-align:left; padding:8px 2px;}
  .featic{width:40px; height:40px; border-radius:11px; flex:none; background:var(--hover);
    color:var(--ink); display:flex; align-items:center; justify-content:center;}
  .featt{font-size:14px; font-weight:600;}
  .featd{font-size:12px; color:var(--sub); margin-top:1px;}
  .onbbtns{display:flex; gap:8px; width:100%; margin-top:2px;}
  .onbbtns button{flex:1; height:36px; border:1px solid var(--glassbrd); border-radius:10px;
    background:transparent; color:var(--ink); font-size:12px; font-weight:600; cursor:default;}
  .onbbtns button:hover{background:var(--hover);}
  .onbbtns button.ok2{background:var(--btn); color:var(--btnink); border-color:transparent;}
  .onbstatus{margin-top:12px; display:flex; align-items:center; gap:10px; justify-content:center;}
  .chkb{height:32px; padding:0 14px; border-radius:9px; border:1px solid var(--glassbrd);
    background:transparent; color:var(--ink); font-size:12px; font-weight:600; cursor:default;}
  .okline{display:flex; align-items:center; gap:7px; color:var(--ink); font-size:13px; font-weight:600;}
  .notyet{font-size:11.5px; color:var(--sub);}
  .onbnav{margin-top:14px; display:flex; flex-direction:column; gap:12px; align-items:center;}
  .dots{display:flex; gap:6px;}
  .dot{width:6px; height:6px; border-radius:50%; background:var(--line); transition:width .2s,background .2s;}
  .dot.on{background:var(--btn); width:18px; border-radius:3px;}
  .navb{display:flex; gap:8px; width:100%;}
  .obk{height:42px; padding:0 18px; border-radius:12px; border:1px solid var(--glassbrd);
    background:transparent; color:var(--ink); font-size:14px; font-weight:600; cursor:default; flex:none;}
  .onbstart{flex:1; height:42px; border:none; border-radius:12px; background:var(--btn);
    color:var(--btnink); font-size:14px; font-weight:600; cursor:default; box-shadow:0 3px 14px var(--ring);}
  .onbstart:active{opacity:.7;}
</style></head>
<body>
<!-- CHATS -->
<div class="screen" id="s-chats">
  <header>
    <span class="wm">
      <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M2 12h2m4-6v12M12 3v18m4-14v10m4-6v2"/></svg>
      <h1>Klartext</h1>
    </span>
    <span class="sp"></span>
    <span class="ib" onclick="openSettings()" title="Einstellungen">
      <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
    </span>
  </header>
  <div class="search">
    <input id="q" type="search" placeholder="Suchen in Chats und Transkripten..." oninput="onSearch(this.value)">
  </div>
  <div class="hair"></div>
  <div class="list" id="chatlist"></div>
</div>

<!-- CHAT HISTORY -->
<div class="screen over" id="s-chat">
  <div class="chead">
    <span class="back" onclick="closeChat()">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 18l-6-6 6-6"/></svg>
    </span>
    <span class="cav" id="chatav"></span>
    <span class="cname" id="chatname"></span>
  </div>
  <div class="hair"></div>
  <div class="msgs" id="msgs"></div>
</div>

<!-- SETTINGS -->
<div class="screen over" id="s-set">
  <div class="chead">
    <span class="back" onclick="closeSettings()">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 18l-6-6 6-6"/></svg>
    </span>
    <span class="cname" data-i18n="settings">Einstellungen</span>
  </div>
  <div class="hair"></div>
  <div class="sect" id="setbody"></div>
  <div class="foot" id="foot">Lokal &amp; privat &middot; whisper large-v3-turbo + qwen2.5</div>
</div>

<!-- ONBOARDING (mehrstufig, per JS gerendert) -->
<div class="screen over onb" id="s-onb"><div class="onbwrap" id="onbroot"></div></div>

<script>
  const $ = s => document.querySelector(s);
  const esc = s => (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  let CHATS=[], ACCESS='ok', CFG={}, searchTimer=null, UILANG='de';

  const I18N={
    de:{ search_ph:'Suchen in Chats und Transkripten...', settings:'Einstellungen',
      back:'Zurück', transcribe:'Transkribieren', summarize:'Zusammenfassen',
      translate:'Übersetzen', copy:'Kopieren', summary:'Zusammenfassung',
      translation:'Übersetzung', chats:'Chats', inmsgs:'In Nachrichten & Transkripten',
      nores:'Nichts gefunden für', novoice:'Keine Sprachnachrichten gefunden. Sobald WhatsApp welche empfängt, erscheinen sie hier.',
      wanf:'WhatsApp-Mac nicht gefunden.', fdatitle:'Zugriff nötig',
      fdatext:'Aktiviere „Vollzugriff auf Festplatte“ für Klartext.', fdabtn:'Einstellungen öffnen',
      auto:'Auto-Transkription', autoh:'Neue Sprachnachrichten automatisch transkribieren',
      autosum:'Auto-Zusammenfassung', autosumh:'Nach dem Transkribieren automatisch zusammenfassen',
      transto:'Übersetzen nach', transtoh:'Zielsprache für den Übersetzen-Button',
      uilang:'Sprache', uilangh:'Sprache der App', openfolder:'Transkript-Ordner öffnen',
      quit:'Klartext beenden', foot:'Lokal & privat · whisper large-v3-turbo + qwen2.5',
      image:'Bild', video:'Video', doc:'Dokument', media:'Medien',
      s_text:'Text', s_voice:'Sprachnachricht', you:'Du',
      onb_title:'Willkommen bei Klartext',
      onb_sub:'WhatsApp-Sprachnachrichten lokal transkribieren und zusammenfassen. Alles bleibt auf deinem Mac.',
      onb_perm:'Einmal „Vollzugriff auf Festplatte“ erlauben, damit Klartext WhatsApp lesen darf. Pfad kopieren und in den Systemeinstellungen hinzufügen.',
      onb_copy:'Pfad kopieren', onb_copied:'Kopiert', onb_open:'Vollzugriff öffnen', onb_start:'Los geht\'s',
      onb_next:'Weiter', onb_back:'Zurück', onb_finish:'Los geht\'s',
      onb_feat_title:'Was Klartext kann', onb_perm_title:'Einmalige Berechtigung',
      onb_check:'Zugriff prüfen', onb_granted:'Zugriff aktiv', onb_notyet:'Noch kein Zugriff',
      onb_ready_title:'Alles bereit', onb_ready_sub:'Klick auf das Waveform-Icon oben in der Menüleiste, wähle einen Chat und tippe auf eine Sprachnachricht.',
      feat_tx:'Transkription', feat_txd:'Sprachnachrichten zu Text, lokal mit whisper.',
      feat_sum:'Zusammenfassung', feat_sumd:'Kernaussagen auf einen Blick.',
      feat_tr:'Übersetzung', feat_trd:'In jede Sprache, lokal.',
      feat_priv:'100% privat', feat_privd:'Nichts verlässt deinen Mac.' },
    en:{ search_ph:'Search chats and transcripts...', settings:'Settings',
      back:'Back', transcribe:'Transcribe', summarize:'Summarize',
      translate:'Translate', copy:'Copy', summary:'Summary',
      translation:'Translation', chats:'Chats', inmsgs:'In messages & transcripts',
      nores:'Nothing found for', novoice:'No voice messages found yet. They appear here as WhatsApp receives them.',
      wanf:'WhatsApp for Mac not found.', fdatitle:'Access needed',
      fdatext:'Enable Full Disk Access for Klartext.', fdabtn:'Open Settings',
      auto:'Auto-transcription', autoh:'Automatically transcribe new voice messages',
      autosum:'Auto-summary', autosumh:'Summarize automatically after transcribing',
      transto:'Translate into', transtoh:'Target language for the Translate button',
      uilang:'Language', uilangh:'App language', openfolder:'Open transcript folder',
      quit:'Quit Klartext', foot:'Local & private · whisper large-v3-turbo + qwen2.5',
      image:'Image', video:'Video', doc:'Document', media:'Media',
      s_text:'Text', s_voice:'Voice message', you:'You',
      onb_title:'Welcome to Klartext',
      onb_sub:'Transcribe and summarize WhatsApp voice messages locally. Everything stays on your Mac.',
      onb_perm:'Grant Full Disk Access once so Klartext can read WhatsApp. Copy the path and add it in System Settings.',
      onb_copy:'Copy path', onb_copied:'Copied', onb_open:'Open Full Disk Access', onb_start:'Get started',
      onb_next:'Next', onb_back:'Back', onb_finish:'Get started',
      onb_feat_title:'What Klartext does', onb_perm_title:'One-time permission',
      onb_check:'Check access', onb_granted:'Access active', onb_notyet:'No access yet',
      onb_ready_title:'All set', onb_ready_sub:'Click the waveform icon in the menu bar, pick a chat and tap a voice message.',
      feat_tx:'Transcription', feat_txd:'Voice messages to text, locally with whisper.',
      feat_sum:'Summary', feat_sumd:'Key points at a glance.',
      feat_tr:'Translation', feat_trd:'Into any language, locally.',
      feat_priv:'100% private', feat_privd:'Nothing leaves your Mac.' } };
  function t(k){ return (I18N[UILANG]&&I18N[UILANG][k]) || I18N.de[k] || k; }
  function applyLang(){
    document.querySelectorAll('[data-i18n]').forEach(el=>{ el.textContent=t(el.dataset.i18n); });
    const q=$('#q'); if(q) q.placeholder=t('search_ph');
    const ft=$('#foot'); if(ft) ft.textContent=t('foot');
  }

  const icMic='<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10a7 7 0 0 0 14 0M12 19v3"/></svg>';
  const icUsers='<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87M16 3.13A4 4 0 0 1 16 11"/></svg>';
  const icSpark='<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/></svg>';
  const icGlobe='<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a15 15 0 0 1 0 18M12 3a15 15 0 0 0 0 18"/></svg>';
  const icCopy='<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>';
  const icPlay='<svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>';
  const icPause='<svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/></svg>';
  const icCheck='<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg>';
  const icLock='<svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>';

  function post(o){ try{ webkit.messageHandlers.bridge.postMessage(o);}catch(e){} }
  function avHTML(av, kind){ return av ? '<img src="'+av+'">' : (kind==='group'?icUsers:icMic); }
  function skLines(n){ let h=''; const w=['94%','80%','88%','62%']; for(let i=0;i<n;i++) h+='<div class="sk skln" style="width:'+w[i%w.length]+'"></div>'; return h; }
  function skRows(n){ let h=''; for(let i=0;i<(n||7);i++) h+='<div class="skrow"><div class="sk skcirc"></div><div style="flex:1"><div class="sk skln" style="width:'+(46+i%3*13)+'%"></div><div class="sk skln" style="width:'+(70-i%4*10)+'%"></div></div></div>'; return h; }

  // ---------- CHATS ----------
  window.__chats = function(chats, access, lang){
    CHATS=chats; ACCESS=access; if(lang){ UILANG=lang; } applyLang();
    const L=$('#chatlist');
    if(access==='denied'){ L.innerHTML=emptyFDA(); return; }
    if(access==='missing'){ L.innerHTML='<div class="empty">'+icMic+'<p>'+t('wanf')+'</p></div>'; return; }
    if(!chats.length){ L.innerHTML=skRows(7); return; }
    renderChats(chats);
  };
  function renderChats(chats){
    $('#chatlist').innerHTML = chats.map(c =>
      '<div class="row" onclick="openChat('+c.pk+')">'
      + '<span class="av">'+avHTML(c.avatar,c.kind)+'</span>'
      + '<span class="meta"><div class="t">'+esc(c.name)+'</div>'
      + '<div class="d">'+esc(c.preview||'')+'</div></span>'
      + '<span class="rt">'+esc(c.time||'')+'</span></div>'
    ).join('');
  }
  function emptyFDA(){
    return '<div class="empty"><svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>'
      + '<p><b>'+t('fdatitle')+'</b><br>'+t('fdatext')+'</p>'
      + '<button class="mini" onclick="post({action:\'fda\'})">'+t('fdabtn')+'</button></div>';
  }

  // ---------- SEARCH ----------
  function onSearch(v){
    clearTimeout(searchTimer);
    if(!v.trim()){ renderChats(CHATS); return; }
    searchTimer=setTimeout(()=>post({action:'search', q:v}), 250);
  }
  window.__search = function(q, chats, content){
    if($('#q').value.trim()!==q.trim()) return;
    const L=$('#chatlist');
    if(!chats.length && !content.length){
      L.innerHTML='<div class="empty"><p>'+t('nores')+' &bdquo;'+esc(q)+'&ldquo;.</p></div>'; return;
    }
    let h='';
    if(chats.length){
      h+='<div class="grp">'+t('chats')+'</div>';
      h+=chats.map(c=>'<div class="row" onclick="openChat('+c.cpk+')">'
        +'<span class="av">'+avHTML(c.avatar,c.kind)+'</span>'
        +'<span class="meta"><div class="t">'+esc(c.name)+'</div></span>'
        +'<svg class="chev" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18l6-6-6-6"/></svg></div>').join('');
    }
    if(content.length){
      h+='<div class="grp">'+t('inmsgs')+'</div>';
      h+=content.map(r=>'<div class="row" onclick="openChat('+r.cpk+')">'
        +'<span class="av">'+(r.kind==='group'?icUsers:icMic)+'</span>'
        +'<span class="meta"><div class="t">'+esc(r.name)+'</div>'
        +'<div class="d">'+esc(r.snippet)+'</div></span>'
        +'<span class="rt">'+t('s_'+r.src)+'</span></div>').join('');
    }
    L.innerHTML=h;
  };

  // ---------- CHAT HISTORY ----------
  function skMsgs(){ let h=''; const ws=['55%','40%','62%','48%','58%','44%']; for(let i=0;i<7;i++){ const me=i%2?' me':''; h+='<div class="m'+me+'"><div class="sk" style="height:32px;width:'+ws[i%ws.length]+';border-radius:14px"></div></div>'; } return h; }
  function openChat(pk){
    stopAllAudio();
    $('#chatname').textContent=''; $('#chatav').innerHTML='';
    $('#msgs').innerHTML = skMsgs();
    $('#s-chat').classList.add('show');
    post({action:'open', pk:pk});
  }
  window.__messages = function(chat, msgs){
    $('#chatav').innerHTML = avHTML(chat.avatar, chat.kind);
    $('#chatname').textContent = chat.name;
    const M=$('#msgs');
    let html='', lastDay=null;
    msgs.forEach(m=>{
      if(m.day && m.day!==lastDay){ html+='<div class="daysep"><span>'+esc(m.day)+'</span></div>'; lastDay=m.day; }
      html+=renderMsg(m);
    });
    M.innerHTML = html;
    $('#s-chat').classList.add('show');
    M.scrollTop = M.scrollHeight;
  };
  function renderMsg(m){
    if(m.kind==='voice') return renderVoice(m);
    const me = m.me ? ' me':'';
    const sname = (!m.me && m.sender) ? '<div class="sname">'+esc(m.sender)+'</div>' : '';
    let inner;
    if(m.kind==='text') inner = esc(m.text);
    else inner = '<span class="ph">['+t({image:'image',video:'video',doc:'doc'}[m.kind]||'media')+']</span>';
    return '<div class="m'+me+'">'+sname+'<div class="bub">'+inner+'</div><div class="bt">'+esc(m.time)+'</div></div>';
  }
  function renderVoice(m){
    const who = m.sender || (m.me?'Du':'');
    let inner;
    if(m.tx){ inner = transcriptBlock(m.pk, m.tx, m.segs) + actionsRow(m.pk, m.sum); }
    else { inner = '<button class="txbtn" onclick="txMsg('+m.pk+')">'+icMic+t('transcribe')+'</button>'; }
    return '<div class="vc" data-pk="'+m.pk+'">'
      + '<div class="vtop">'+player(m.pk)+'</div>'
      + '<div class="vsub">'+esc(who)+(who?' &middot; ':'')+esc(m.time)+'</div>'
      + '<div class="vbody" id="vb'+m.pk+'">'+inner+'</div></div>';
  }
  function player(pk){
    const spd=window['_spd'+pk]||1;
    return '<button class="pbtn" id="pb'+pk+'" onclick="playMsg('+pk+')">'+icPlay+'</button>'
      + '<div class="wave" id="wave'+pk+'" onclick="scrub(event,'+pk+')">'+waveBars(pk)+'</div>'
      + '<span class="ptime" id="pt'+pk+'">0:00</span>'
      + '<button class="speed" id="sp'+pk+'" onclick="cycleSpeed('+pk+')">'+fmtSpd(spd)+'</button>';
  }
  function transcriptBlock(pk, tx, segs){
    window['_tx'+pk]=tx; window['_segs'+pk]=segs||[];
    const txt=(segs&&segs.length)
      ? segs.map((s,i)=>'<span class="seg" data-i="'+i+'" onclick="seek('+pk+','+i+')">'+esc(s.t)+' </span>').join('')
      : esc(tx);
    return '<div class="txt fade" id="tx'+pk+'">'+txt+'</div>';
  }
  function actionsRow(pk, sum){
    let h='<div class="acts">'
      + '<button class="ib2" title="'+t('summarize')+'" onclick="sumMsg('+pk+')">'+icSpark+'</button>'
      + '<button class="ib2" title="'+t('translate')+'" onclick="trMsg('+pk+')">'+icGlobe+'</button>'
      + '<button class="ib2" id="cp'+pk+'" title="'+t('copy')+'" onclick="cpMsg('+pk+')">'+icCopy+'</button></div>'
      + '<div id="sum'+pk+'"></div><div id="tr'+pk+'"></div>';
    if(sum) h=h.replace('<div id="sum'+pk+'"></div>', card2('sum'+pk, icSpark+t('summary'), sum));
    return h;
  }
  function waveBars(pk){
    let seed=(pk||1)>>>0, h='';
    for(let i=0;i<40;i++){ seed=(seed*1103515245+12345)>>>0; const v=18+(seed%78); h+='<i style="height:'+v+'%"></i>'; }
    return h;
  }
  const SPEEDS=[0.5,0.75,1,1.25,1.5,1.75,2];
  function fmtSpd(s){ return (s+'').replace('.',',')+'×'; }
  function cycleSpeed(pk){
    let s=window['_spd'+pk]||1; let i=SPEEDS.indexOf(s); i=(i+1)%SPEEDS.length; s=SPEEDS[i];
    window['_spd'+pk]=s; const b=$('#sp'+pk); if(b) b.textContent=fmtSpd(s);
    const a=window['_au'+pk]; if(a) a.playbackRate=s;
  }
  function card2(id, head, body){
    return '<div class="card2 fade" id="'+id+'"><div class="c2h">'+head+'</div><div class="c2b">'+esc(body)+'</div></div>';
  }
  function txMsg(pk){
    $('#vb'+pk).innerHTML = '<div class="fade">'+skLines(3)+'</div>';
    post({action:'transcribe', pk:pk});
  }
  window.__tx = function(pk, text, segs){
    const el=$('#vb'+pk); if(!el) return;
    el.innerHTML = transcriptBlock(pk, text, segs||[]) + actionsRow(pk, '');
  };
  function sumMsg(pk){
    let box=$('#sum'+pk); if(box) box.outerHTML='<div id="sum'+pk+'"><div class="card2"><div class="c2h">'+icSpark+t('summary')+'</div><div class="c2b">'+skLines(2)+'</div></div></div>';
    post({action:'summary', pk:pk});
  }
  window.__sum = function(pk, text){
    const box=$('#sum'+pk); if(!box) return;
    box.innerHTML = '<div class="card2 fade"><div class="c2h">'+icSpark+t('summary')+'</div><div class="c2b">'+esc(text)+'</div></div>';
  };
  function trMsg(pk){
    let box=$('#tr'+pk); if(box) box.innerHTML='<div class="card2"><div class="c2h">'+icGlobe+t('translation')+'</div><div class="c2b">'+skLines(2)+'</div></div>';
    post({action:'translate', pk:pk});
  }
  window.__tr = function(pk, text){
    const box=$('#tr'+pk); if(!box) return;
    box.innerHTML = '<div class="card2 fade"><div class="c2h">'+icGlobe+t('translation')+'</div><div class="c2b">'+esc(text)+'</div></div>';
  };
  function cpMsg(pk){
    post({action:'copy', text: window['_tx'+pk]||''});
    const b=$('#cp'+pk); if(!b) return;
    b.innerHTML=icCheck; b.classList.add('ok');
    setTimeout(()=>{ if(b){ b.innerHTML=icCopy; b.classList.remove('ok'); } }, 1400);
  }

  // ---------- AUDIO + SYNC ----------
  const AUDIOS=[];
  function stopAllAudio(){ AUDIOS.forEach(a=>{ try{a.pause();}catch(e){} }); }
  function playMsg(pk){
    const a=window['_au'+pk];
    if(a){ if(a.paused){ stopAllAudio(); a.play(); setPB(pk,true);} else { a.pause(); setPB(pk,false);} return; }
    const b=$('#pb'+pk); if(b) b.innerHTML=icPlay+'&hellip;';
    post({action:'play', pk:pk});
  }
  window.__audio=function(pk,uri){
    if(!uri){ setPB(pk,false); return; }
    stopAllAudio();
    const a=new Audio(uri); window['_au'+pk]=a; AUDIOS.push(a);
    a.playbackRate = window['_spd'+pk]||1;
    a.addEventListener('timeupdate',()=>onTime(pk));
    a.addEventListener('ended',()=>{ setPB(pk,false); clearSeg(pk); });
    a.play(); setPB(pk,true);
  };
  function setPB(pk,on){ const b=$('#pb'+pk); if(b) b.innerHTML=(on?icPause:icPlay); }
  function fmtT(s){ s=Math.max(0,Math.floor(s||0)); return Math.floor(s/60)+':'+String(s%60).padStart(2,'0'); }
  function onTime(pk){
    const a=window['_au'+pk]; if(!a) return;
    const f=a.duration? a.currentTime/a.duration : 0;
    const w=$('#wave'+pk);
    if(w){ const bars=w.querySelectorAll('i'); const n=Math.floor(f*bars.length);
      bars.forEach((b,i)=>b.classList.toggle('on', i<=n)); }
    const pt=$('#pt'+pk); if(pt) pt.textContent=fmtT(a.currentTime);
    syncSeg(pk,a.currentTime);
  }
  function scrub(e,pk){
    const a=window['_au'+pk]; const w=$('#wave'+pk); if(!w) return;
    const r=w.getBoundingClientRect(); const f=Math.min(1,Math.max(0,(e.clientX-r.left)/r.width));
    if(a&&a.duration){ a.currentTime=f*a.duration; onTime(pk); } else { playMsg(pk); }
  }
  function syncSeg(pk,t){
    const segs=window['_segs'+pk]||[]; const c=$('#tx'+pk); if(!c) return;
    let idx=-1; for(let i=0;i<segs.length;i++){ if(t>=segs[i].s && t<segs[i].e){ idx=i; break; } }
    const sp=c.querySelectorAll('.seg');
    sp.forEach(s=>s.classList.toggle('active', +s.dataset.i===idx));
    if(idx>=0 && sp[idx]) sp[idx].scrollIntoView({block:'nearest'});
  }
  function clearSeg(pk){ const c=$('#tx'+pk); if(c) c.querySelectorAll('.seg').forEach(s=>s.classList.remove('active')); }
  function seek(pk,i){
    const segs=window['_segs'+pk]||[]; const a=window['_au'+pk];
    if(!a){ playMsg(pk); return; }
    if(segs[i]){ stopAllAudio(); a.currentTime=segs[i].s; a.play(); setPB(pk,true); }
  }
  function closeChat(){ stopAllAudio(); $('#s-chat').classList.remove('show'); }

  // ---------- Interaktiver Trackpad-Swipe (folgt dem Finger) -> zurueck ----------
  let sw={active:false, el:null, w:0, dx:0, tmr:null};
  window.addEventListener('wheel', e=>{
    if(Math.abs(e.deltaX) <= Math.abs(e.deltaY)) return;         // nur horizontal
    const el = $('#s-set').classList.contains('show') ? $('#s-set')
             : ($('#s-chat').classList.contains('show') ? $('#s-chat') : null);
    if(!el) return;
    if(!sw.active || sw.el!==el){ sw.active=true; sw.el=el; sw.w=el.offsetWidth||360; sw.dx=0; el.style.transition='none'; }
    sw.dx = Math.max(0, Math.min(sw.w, sw.dx - e.deltaX));       // Wisch rechts (deltaX<0) -> dx groesser
    sw.el.style.transform = 'translateX('+sw.dx+'px)';
    sw.el.style.opacity = String(1 - (sw.dx/sw.w)*0.25);
    clearTimeout(sw.tmr); sw.tmr=setTimeout(endSwipe, 90);
  }, {passive:true});
  function endSwipe(){
    if(!sw.active) return;
    const el=sw.el, done = sw.dx > sw.w*0.32;
    el.style.transition=''; el.style.transform=''; el.style.opacity='';
    if(done){ el.classList.remove('show'); if(el.id==='s-chat') stopAllAudio(); }
    sw.active=false; sw.el=null; sw.dx=0;
  }

  // ---------- SETTINGS ----------
  function openSettings(){ post({action:'settings'}); $('#s-set').classList.add('show'); }
  function closeSettings(){ $('#s-set').classList.remove('show'); }
  window.__settings = function(cfg){
    CFG=cfg; UILANG=cfg.ui_lang||'de'; applyLang();
    const LANGS=['Englisch','Deutsch','Türkisch','Spanisch','Französisch','Italienisch','Arabisch'];
    const opts=LANGS.map(l=>'<option'+(cfg.translate_to===l?' selected':'')+'>'+l+'</option>').join('');
    const langOpts=[['de','Deutsch'],['en','English']].map(x=>'<option value="'+x[0]+'"'+(UILANG===x[0]?' selected':'')+'>'+x[1]+'</option>').join('');
    $('#setbody').innerHTML =
      swRow('auto',t('auto'),t('autoh'),cfg.auto)
      + swRow('auto_summary',t('autosum'),t('autosumh'),cfg.auto_summary)
      + selRow(t('uilang'),t('uilangh'),'ui_lang',langOpts,true)
      + selRow(t('transto'),t('transtoh'),'translate_to',opts,false)
      + '<div class="srow link" onclick="post({action:\'openfolder\'})"><div class="lab"><div class="n">'+t('openfolder')+'</div></div></div>';
  };
  function selRow(name,hint,key,opts,isLang){
    return '<div class="srow"><div class="lab"><div class="n">'+name+'</div><div class="h">'+hint+'</div></div>'
      + '<select onchange="setCfg(\''+key+'\',this.value);'+(isLang?'reloadLang(this.value);':'')+'">'+opts+'</select></div>';
  }
  function reloadLang(l){ UILANG=l; CFG.ui_lang=l; applyLang(); window.__settings(CFG); post({action:'chats'}); }
  function swRow(key,name,hint,on){
    return '<div class="srow"><div class="lab"><div class="n">'+name+'</div><div class="h">'+hint+'</div></div>'
      + '<span class="sw'+(on?' on':'')+'" onclick="toggleCfg(this,\''+key+'\')"></span></div>';
  }
  function toggleCfg(el,key){ const on=!el.classList.contains('on'); el.classList.toggle('on',on); setCfg(key,on); }
  function setCfg(key,val){ CFG[key]=val; post({action:'setcfg', key:key, value:val}); }

  // ---------- ONBOARDING (mehrstufig, interaktiv) ----------
  let ONB=0; const ONB_N=4;
  const WAVES='<svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M2 12h2m4-6v12M12 3v18m4-14v10m4-6v2"/></svg>';
  window.__onboard=function(pypath){ window._pypath=pypath; ONB=0; renderOnb(); $('#s-onb').classList.add('show'); };
  function renderOnb(){ const r=$('#onbroot'); if(r) r.innerHTML=onbStep(ONB)+onbNav(); }
  function onbStep(i){
    if(i===0) return '<div class="onbstep fade"><div class="onbicon">'+WAVES+'</div>'
      +'<h2>'+t('onb_title')+'</h2><p class="onbsub">'+t('onb_sub')+'</p>'
      +'<div class="langseg"><button class="'+(UILANG==='de'?'on':'')+'" onclick="onbLang(\'de\')">Deutsch</button>'
      +'<button class="'+(UILANG==='en'?'on':'')+'" onclick="onbLang(\'en\')">English</button></div></div>';
    if(i===1) return '<div class="onbstep fade"><h2 class="onbh">'+t('onb_feat_title')+'</h2>'
      +feat(icMic,t('feat_tx'),t('feat_txd'))+feat(icSpark,t('feat_sum'),t('feat_sumd'))
      +feat(icGlobe,t('feat_tr'),t('feat_trd'))+feat(icLock,t('feat_priv'),t('feat_privd'))+'</div>';
    if(i===2) return '<div class="onbstep fade"><div class="onbicon lite">'+icLock+'</div><h2 class="onbh">'+t('onb_perm_title')+'</h2>'
      +'<p class="onbsub">'+t('onb_perm')+'</p>'
      +'<div class="onbbtns"><button id="onbcopy" onclick="onbCopy()">'+t('onb_copy')+'</button>'
      +'<button onclick="post({action:\'fda\'})">'+t('onb_open')+'</button></div>'
      +'<div class="onbstatus" id="onbstatus"><button class="chkb" onclick="post({action:\'checkaccess\'})">'+t('onb_check')+'</button></div></div>';
    return '<div class="onbstep fade"><div class="onbicon ok">'+icCheck+'</div><h2>'+t('onb_ready_title')+'</h2><p class="onbsub">'+t('onb_ready_sub')+'</p></div>';
  }
  function feat(ic,tt,dd){ return '<div class="featrow"><span class="featic">'+ic+'</span><span class="featx"><div class="featt">'+tt+'</div><div class="featd">'+dd+'</div></span></div>'; }
  function onbNav(){
    let dots=''; for(let i=0;i<ONB_N;i++) dots+='<span class="dot'+(i===ONB?' on':'')+'"></span>';
    const last=ONB===ONB_N-1;
    return '<div class="onbnav"><div class="dots">'+dots+'</div><div class="navb">'
      +(ONB>0?'<button class="obk" onclick="onbGo(-1)">'+t('onb_back')+'</button>':'')
      +'<button class="onbstart" onclick="'+(last?'onbStart()':'onbGo(1)')+'">'+(last?t('onb_finish'):t('onb_next'))+'</button></div></div>';
  }
  function onbGo(d){ ONB=Math.max(0,Math.min(ONB_N-1,ONB+d)); renderOnb(); }
  function onbLang(l){ UILANG=l; CFG.ui_lang=l; post({action:'setcfg',key:'ui_lang',value:l}); applyLang(); post({action:'chats'}); renderOnb(); }
  window.__access=function(status){
    const s=$('#onbstatus'); if(!s) return;
    if(status==='ok') s.innerHTML='<div class="okline">'+icCheck+'<span>'+t('onb_granted')+'</span></div>';
    else s.innerHTML='<button class="chkb" onclick="post({action:\'checkaccess\'})">'+t('onb_check')+'</button><span class="notyet">'+t('onb_notyet')+'</span>';
  };
  function onbCopy(){
    post({action:'copy', text: window._pypath||''});
    const b=$('#onbcopy'); if(!b) return;
    b.textContent=t('onb_copied'); b.classList.add('ok2');
    setTimeout(()=>{ if(b){ b.textContent=t('onb_copy'); b.classList.remove('ok2'); } },1500);
  }
  function onbStart(){ post({action:'onboarded'}); $('#s-onb').classList.remove('show'); }

  post({action:'chats'});
</script>
</body></html>
"""


_lock_fh = None


def main():
    global _lock_fh
    _lock_fh = open(os.path.join(APPDIR, "app.lock"), "w")
    try:
        fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("already running -> exit")
        return
    try:
        with open(HTML_PATH, "w") as f:
            f.write(UI_HTML)
    except Exception:
        pass
    log("start")
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    delegate = AppDelegate.alloc().init()
    app.setDelegate_(delegate)
    AppHelper.runEventLoop()


if __name__ == "__main__":
    main()
