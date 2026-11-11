"""
Giveaway Vote Bot  -  V3
Same UI kit as the Free Fire Tournament Bot (small-caps font, blockquote headers,
coloured buttons, premium emoji, self-cleaning panels).  aiogram >= 3.25

Flow
  Admin creates a giveaway  ->  bot gives a link  t.me/<bot>?start=gw_xxxx
  User opens the link       ->  sends a name      ->  name is registered for THAT giveaway
  Bot posts the name in the channel with a Vote button
  Vote button               ->  only channel members, one vote per person per giveaway
  Users see a live leaderboard inside the bot.
  Everything is backed up to a private Telegram channel (latest backup is pinned and
  the bot restores itself from it if its disk is wiped).
"""
import asyncio
import csv
import html
import io
import logging
import os
import random
import re
import secrets
import shutil
import sqlite3
import tempfile
import time as _time
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from threading import Thread
from urllib.parse import quote

from dotenv import load_dotenv
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

try:
    import qrcode  # pip install "qrcode[pil]"   (optional, for giveaway link QR codes)
except ImportError:
    qrcode = None


# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
load_dotenv()


def _chat(value):
    value = str(value or "").strip()
    return int(value) if value.lstrip("-").isdigit() else value


BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
ADMINS = {OWNER_ID} | {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
CHANNEL_ID = _chat(os.getenv("CHANNEL_ID", ""))                  # channel where entries are posted / voted
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "").strip()
if not CHANNEL_LINK and isinstance(CHANNEL_ID, str) and CHANNEL_ID.startswith("@"):
    CHANNEL_LINK = f"https://t.me/{CHANNEL_ID[1:]}"
_EXTRA = [_chat(c) for c in os.getenv("REQUIRED_CHATS", "").split(",") if c.strip()]
REQUIRED = [CHANNEL_ID] + [c for c in _EXTRA if c != CHANNEL_ID]   # users must be in all of these
STORAGE_CHANNEL_ID = int(os.getenv("STORAGE_CHANNEL_ID", "0") or 0)  # private channel, bot = admin
DB_PATH = Path(os.getenv("DB_PATH", "data/votebot.sqlite3"))
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", "backups"))
BACKUP_EVERY_HOURS = float(os.getenv("BACKUP_EVERY_HOURS", "1"))
BACKUP_EVERY_CHANGES = int(os.getenv("BACKUP_EVERY_CHANGES", "25"))
HOST_NAME = os.getenv("HOST_NAME", "Erebus")
DEV_NAME = os.getenv("DEV_NAME", "Haze")
DEV_HANDLE = os.getenv("DEV_HANDLE", "@Noxmirage")
USE_CUSTOM_EMOJI = os.getenv("USE_CUSTOM_EMOJI", "1") == "1"
BOT_USERNAME = ""   # filled at start-up

if not BOT_TOKEN or not OWNER_ID or not CHANNEL_ID:
    raise RuntimeError("Set BOT_TOKEN, OWNER_ID and CHANNEL_ID in your .env file")

for _d in (DB_PATH.parent, BACKUP_DIR):
    _d.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()],
    force=True,
)

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


def keep_alive():
    """Tiny web page so free hosts (Render etc.) keep the bot awake. Only starts if PORT is set."""
    port = os.getenv("PORT")
    if not port and os.getenv("KEEP_ALIVE") != "1":
        return
    from flask import Flask, send_from_directory
    web = Flask(__name__)
    here = os.path.dirname(os.path.abspath(__file__))

    @web.route("/")
    def home():
        if os.path.exists(os.path.join(here, "index.html")):
            return send_from_directory(here, "index.html")
        return "Bot is running", 200

    @web.route("/health")
    def health():
        return "OK", 200

    Thread(target=lambda: web.run(host="0.0.0.0", port=int(port or 10000)), daemon=True).start()


# ----------------------------------------------------------------------------
# UI kit (same as the tournament bot): font, emojis, panels, buttons, auto-cleaning
# ----------------------------------------------------------------------------
_SMALL_CAPS = str.maketrans(
    "abcdefghijklmnopqrstuvwxyz",
    "ᴀʙᴄᴅᴇғɢʜɪᴊᴋʟᴍɴᴏᴘǫʀsᴛᴜᴠᴡxʏᴢ",
)


def sc(text):
    """Bot font: 'free plan' -> 'Fʀᴇᴇ Pʟᴀɴ'. ACRONYMS, numbers, @names and /commands stay untouched."""
    def word(w):
        if any(c.isdigit() or c in "@/₹_<>&" for c in w) or (w.isupper() and len(w) > 1):
            return w
        m = re.search(r"[A-Za-z]", w)
        if not m:
            return w
        i = m.end()
        return w[:i - 1] + w[i - 1].upper() + w[i:].translate(_SMALL_CAPS)
    return "".join(p if p.isspace() else word(p) for p in re.split(r"(\s+)", text))


EMOJI = {
    "title": "✨", "star": "⭐", "camera": "📸", "bolt": "⚡", "user": "👤", "id": "🆔",
    "bot": "👾", "dev": "👨‍💻", "ok": "✅", "err": "❌", "warn": "⚠️", "time": "⏱",
    "pay": "💳", "box": "📦", "money": "💰", "calendar": "📅", "bell": "🔔", "ban": "🚫",
    "admin": "🛠", "mail": "✉️", "link": "🔗", "channel": "📢", "group": "👥",
    "stats": "📊", "lock": "🔒", "ghost": "👻", "home": "🏠", "bank": "🏦",
    "party": "🎉", "pin": "📝", "spin": "🌀",
    "new": "🆕", "web": "🌐", "chat": "💬", "trash": "🗑️", "dot": "🔹", "dot2": "🔸",
    "radio": "🔘", "host": "🏛️", "earth": "🌍", "wait": "⏳", "cat": "🐈‍⬛",
    "trophy": "🏆", "game": "🎮", "reply": "↩️", "backup": "💾", "restore": "♻️",
    "csv": "📄", "info": "ℹ️", "plus": "➕", "list": "📋",
    "medal": "🏅", "history": "🗂️", "cert": "🎓", "crown": "👑",
    # new for the vote bot
    "gift": "🎁", "vote": "🗳", "rank": "🔢", "share": "📤", "dice": "🎲", "qr": "🔳",
}
EMOJI_IDS = {}
if USE_CUSTOM_EMOJI:
    EMOJI_IDS.update({
        "title": "5424972470023104089", "star": "5438496463044752972", "bolt": "5224607267797606837",
        "ok": "5206607081334906820", "err": "5210952531676504517", "time": "5440621591387980068",
        "box": "6046233639644042899", "money": "5278467510604160626", "calendar": "5413879192267805083",
        "bell": "5458603043203327669", "ban": "5260293700088511294", "admin": "5341715473882955310",
        "mail": "5253742260054409879", "link": "5271604874419647061", "channel": "5424818078833715060",
        "group": "5332724926216428039", "stats": "5231200819986047254", "lock": "5296369303661067030",
        "ghost": "5431609822288033666", "bank": "5332455502917949981", "party": "5276032951342088188",
        "pin": "5397782960512444700", "spin": "5375338737028841420",
        "new": "5386367538735104399", "warn": "4915853119839011973", "pay": "5039623284056917259",
        "web": "5039895103947146186", "chat": "5040042498634810056", "trash": "5042329873662609701",
        "dot": "5039753786638205957", "dot2": "5042020176455795565", "radio": "5042097984083330584",
        "host": "5343636681473935403", "earth": "5042176294222037888", "wait": "5039579582764680065",
        "user": "6237927637906364256", "cat": "5039653765439816618",
    })

# Set by setup_emoji() at start-up after really testing Telegram.
MSG_EMOJI_OK = True    # premium emoji inside message text
BTN_ICON_OK = True     # premium emoji icons on inline buttons


def E(name):
    fallback = EMOJI.get(name, "•")
    cid = EMOJI_IDS.get(name) if MSG_EMOJI_OK else None
    return f'<tg-emoji emoji-id="{cid}">{fallback}</tg-emoji>' if cid else fallback


_BTN_FIELDS = InlineKeyboardButton.model_fields
HAS_ICON = "icon_custom_emoji_id" in _BTN_FIELDS   # needs aiogram >= 3.25
HAS_STYLE = "style" in _BTN_FIELDS


def btn(text, emoji=None, style=None, **kw):
    """Inline button in the bot font with an icon. style: 'primary' (blue) / 'success' (green) / 'danger' (red)."""
    label = sc(text)
    extra = {}
    cid = EMOJI_IDS.get(emoji) if (emoji and BTN_ICON_OK) else None
    if cid and HAS_ICON:
        extra["icon_custom_emoji_id"] = cid
    elif emoji:
        label = f"{EMOJI.get(emoji, '')} {label}"
    if style and HAS_STYLE:
        extra["style"] = style
    return InlineKeyboardButton(text=label, **extra, **kw)


SEP = "──────────────────"
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def bq(text):
    return f"<blockquote>{text}</blockquote>"


def head(emoji, title, tail=""):
    return f"{E(emoji)} <b>{sc(title)}</b>{tail}"


def row(emoji, label, value):
    return f"{E(emoji)} <b>{sc(label)}</b> ➛ {value}"


def esc(value):
    return html.escape(str(value if value is not None else ""))


_TRASH = {}     # chat_id -> ids of temporary messages (prompts, QR pictures ...)
_PANEL = {}     # chat_id -> id of the current main-menu message
_TASKS = set()  # keeps background tasks alive
_PENDING = {}   # user_id -> giveaway code from a link they opened before joining the channel


async def _delete(chat_id, message_id):
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass


def remember(message):
    if message:
        _TRASH.setdefault(message.chat.id, []).append(message.message_id)
    return message


async def sweep(chat_id, keep=None):
    for mid in _TRASH.pop(chat_id, []):
        if mid != keep:
            await _delete(chat_id, mid)


async def tidy(message):
    await _delete(message.chat.id, message.message_id)


async def _delete_later(chat_id, message_id, ttl):
    await asyncio.sleep(ttl)
    await _delete(chat_id, message_id)


def spawn(coro):
    task = asyncio.create_task(coro)
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return task


async def notice(chat_id, text, ttl=8, **kw):
    try:
        msg = await bot.send_message(chat_id, text, **kw)
    except Exception:
        logging.exception("notice to %s failed", chat_id)
        return None
    spawn(_delete_later(chat_id, msg.message_id, ttl))
    return msg


async def ask(chat_id, text):
    """Send a prompt that sweep() removes when the step is done."""
    return remember(await bot.send_message(chat_id, text))


async def show_panel(chat_id, text, markup):
    old = _PANEL.pop(chat_id, None)
    if old:
        await _delete(chat_id, old)
    msg = await bot.send_message(chat_id, text, reply_markup=markup)
    _PANEL[chat_id] = msg.message_id
    return msg


async def safe_edit(callback: CallbackQuery, text, reply_markup=None, parse_mode="HTML"):
    msg = callback.message
    if getattr(msg, "photo", None):
        await _delete(msg.chat.id, msg.message_id)
        sent = await bot.send_message(msg.chat.id, text, reply_markup=reply_markup, parse_mode=parse_mode)
        _PANEL[msg.chat.id] = sent.message_id
        return
    try:
        await msg.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


# ----------------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------------
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def q_all(sql, args=()):
    with db() as con:
        return con.execute(sql, args).fetchall()


def q_one(sql, args=()):
    with db() as con:
        return con.execute(sql, args).fetchone()


def run(sql, args=()):
    with db() as con:
        return con.execute(sql, args).lastrowid


def count(sql, args=()):
    return q_one(sql, args)[0]


REQUIRED_TABLES = {"users", "giveaways", "entries", "votes"}


def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
            joined_at TEXT NOT NULL, last_seen TEXT NOT NULL, banned INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS giveaways (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, prize TEXT,
            code TEXT UNIQUE NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',        -- open / closed / deleted
            winner_entry_id INTEGER, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT, giveaway_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            name TEXT NOT NULL, message_id INTEGER, votes INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, UNIQUE(giveaway_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS votes (
            giveaway_id INTEGER NOT NULL, voter_id INTEGER NOT NULL, entry_id INTEGER NOT NULL,
            created_at TEXT NOT NULL, PRIMARY KEY(giveaway_id, voter_id)
        );
        CREATE TABLE IF NOT EXISTS updates (
            id INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_entries_gw ON entries(giveaway_id, votes DESC);
        CREATE TABLE IF NOT EXISTS history_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT, giveaway_id INTEGER NOT NULL, entry_id INTEGER,
            user_id INTEGER NOT NULL, name TEXT NOT NULL, username TEXT, first_name TEXT,
            votes INTEGER NOT NULL DEFAULT 0, rank INTEGER, is_winner INTEGER NOT NULL DEFAULT 0, created_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_hist_gw ON history_entries(giveaway_id, rank);
        """)
        # light migrations for databases created by the first V3 version
        for ddl in ("ALTER TABLE giveaways ADD COLUMN deleted_at TEXT",
                    "ALTER TABLE giveaways ADD COLUMN final_voters INTEGER NOT NULL DEFAULT 0"):
            try:
                con.execute(ddl)
            except sqlite3.OperationalError:
                pass


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def is_admin(uid):
    return uid in ADMINS


def upsert_user(user):
    stamp = now_iso()
    run("""INSERT INTO users(user_id, username, first_name, joined_at, last_seen) VALUES(?,?,?,?,?)
           ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,
           first_name=excluded.first_name, last_seen=excluded.last_seen""",
        (user.id, user.username, user.first_name, stamp, stamp))


def is_banned(uid):
    r = q_one("SELECT banned FROM users WHERE user_id=?", (uid,))
    return bool(r and r["banned"])


def get_user(uid):
    return q_one("SELECT * FROM users WHERE user_id=?", (uid,))


def get_giveaway(gid):
    return q_one("SELECT * FROM giveaways WHERE id=? AND status!='deleted'", (gid,))


def get_entry(eid):
    return q_one("SELECT * FROM entries WHERE id=?", (eid,))


def my_entry(gid, uid):
    return q_one("SELECT * FROM entries WHERE giveaway_id=? AND user_id=?", (gid, uid))


def gw_counts(gid):
    r = q_one("SELECT COUNT(*), COALESCE(SUM(votes),0) FROM entries WHERE giveaway_id=?", (gid,))
    return r[0], r[1]


def rank_of(e):
    return count("SELECT COUNT(*) FROM entries WHERE giveaway_id=? AND (votes>? OR (votes=? AND id<?))",
                 (e["giveaway_id"], e["votes"], e["votes"], e["id"])) + 1


def cast_vote(gid, voter_id, entry_id):
    """Returns the new vote count, or None if this user already voted in this giveaway."""
    with db() as con:
        try:
            con.execute("INSERT INTO votes(giveaway_id, voter_id, entry_id, created_at) VALUES(?,?,?,?)",
                        (gid, voter_id, entry_id, now_iso()))
        except sqlite3.IntegrityError:
            return None
        con.execute("UPDATE entries SET votes = votes + 1 WHERE id=?", (entry_id,))
        return con.execute("SELECT votes FROM entries WHERE id=?", (entry_id,)).fetchone()[0]


def gw_link(g):
    return f"https://t.me/{BOT_USERNAME}?start=gw_{g['code']}"


def post_link(message_id):
    if not message_id:
        return None
    if isinstance(CHANNEL_ID, str) and CHANNEL_ID.startswith("@"):
        return f"https://t.me/{CHANNEL_ID[1:]}/{message_id}"
    s = str(CHANNEL_ID)
    if s.startswith("-100"):
        return f"https://t.me/c/{s[4:]}/{message_id}"
    return None


def share_url(link, text):
    return f"https://t.me/share/url?url={quote(link)}&text={quote(text)}"


# ----------------------------------------------------------------------------
# Storage channel + backups
# ----------------------------------------------------------------------------
_DIRTY = 0


async def log_channel(text=None, document=None):
    if not STORAGE_CHANNEL_ID:
        return
    try:
        if document:
            await bot.send_document(STORAGE_CHANNEL_ID, document, caption=text)
        else:
            await bot.send_message(STORAGE_CHANNEL_ID, text)
    except Exception:
        logging.exception("storage channel send failed")


def mark_dirty():
    global _DIRTY
    _DIRTY += 1
    if _DIRTY >= BACKUP_EVERY_CHANGES:
        _DIRTY = 0
        spawn(safe_backup("auto"))


async def backup_now(reason="manual"):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = BACKUP_DIR / f"votebot_{stamp}.db"

    def _copy():
        with closing(sqlite3.connect(DB_PATH)) as src, closing(sqlite3.connect(path)) as dst:
            src.backup(dst)
    await asyncio.to_thread(_copy)
    for old in sorted(BACKUP_DIR.glob("votebot_*.db"))[:-20]:
        old.unlink(missing_ok=True)
    if STORAGE_CHANNEL_ID:
        n_g = count("SELECT COUNT(*) FROM giveaways WHERE status!='deleted'")
        n_e = count("SELECT COUNT(*) FROM entries")
        n_v = count("SELECT COUNT(*) FROM votes")
        caption = (f"{E('backup')} <b>{sc('Database backup')}</b> ({esc(reason)})\n{stamp}\n"
                   f"{n_g} giveaways · {n_e} entries · {n_v} votes")
        try:
            msg = await bot.send_document(STORAGE_CHANNEL_ID,
                                          BufferedInputFile(path.read_bytes(), filename=path.name), caption=caption)
            try:   # the pinned message is the "latest backup" pointer used by restore
                await bot.pin_chat_message(STORAGE_CHANNEL_ID, msg.message_id, disable_notification=True)
            except Exception:
                logging.warning("Could not pin the backup - give the bot 'Pin messages' in the storage channel")
        except Exception:
            logging.exception("could not send the backup to the storage channel")
    return path


async def safe_backup(reason):
    try:
        await backup_now(reason)
    except Exception:
        logging.exception("backup failed")


async def backup_loop():
    global _DIRTY
    while True:
        await asyncio.sleep(BACKUP_EVERY_HOURS * 3600)
        if _DIRTY > 0:
            _DIRTY = 0
            await safe_backup("auto")


def valid_backup(path):
    try:
        with closing(sqlite3.connect(path)) as c:
            names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return REQUIRED_TABLES <= names
    except sqlite3.DatabaseError:
        return False


async def download_latest_backup():
    """The newest backup is the pinned document in the storage channel."""
    if not STORAGE_CHANNEL_ID:
        return None
    chat = await bot.get_chat(STORAGE_CHANNEL_ID)
    pm = getattr(chat, "pinned_message", None)
    if not pm or not pm.document:
        return None
    tmp = Path(tempfile.mkdtemp()) / "latest.db"
    await bot.download(pm.document, destination=tmp)
    return tmp


async def apply_restore(path):
    """Replace the live database with `path` (validated). A local safety copy is kept."""
    if not valid_backup(path):
        return False
    if DB_PATH.exists():
        shutil.copyfile(DB_PATH, BACKUP_DIR / f"pre_restore_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db")
    tmp = Path(f"{DB_PATH}.restoring")
    shutil.copyfile(path, tmp)
    os.replace(tmp, DB_PATH)
    init_db()
    spawn(refresh_buttons())
    spawn(safe_backup("after restore"))   # re-pins the state that is now live
    return True


async def refresh_buttons():
    """After a restore, bring the vote counts on the channel posts back in sync."""
    for e in q_all("SELECT id, message_id, votes FROM entries WHERE message_id IS NOT NULL"):
        try:
            await bot.edit_message_reply_markup(chat_id=CHANNEL_ID, message_id=e["message_id"],
                                                reply_markup=vote_kb(e["id"], e["votes"]))
        except Exception:
            pass
        await asyncio.sleep(1.2)


async def auto_restore():
    """Fresh server / wiped disk: pull the pinned backup from the storage channel."""
    try:
        path = await download_latest_backup()
        if path and valid_backup(path):
            shutil.copyfile(path, DB_PATH)
            logging.info("Database auto-restored from the pinned backup.")
    except Exception:
        logging.exception("auto-restore failed")


# ----------------------------------------------------------------------------
# Keyboards
# ----------------------------------------------------------------------------
def vote_kb(entry_id, votes):
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn(f"Vote ({votes})", "vote", style="success", callback_data=f"v:{entry_id}")]])


def home_keyboard(admin=False):
    rows = [
        [btn("Giveaways", "gift", style="primary", callback_data="gws"),
         btn("Leaderboard", "medal", style="success", callback_data="lb")],
        [btn("My Entries", "box", callback_data="mye"),
         btn("Updates", "bell", callback_data="updates")],
        [btn("How To Join", "pin", callback_data="howto"),
         btn("Rules", "lock", callback_data="rules")],
        [btn("Message Admin", "mail", style="primary", callback_data="support"),
         btn("Developer & Host", "dev", callback_data="about")],
    ]
    if admin:
        rows.append([btn("Admin Panel", "admin", callback_data="admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_keyboard(*top_rows):
    return InlineKeyboardMarkup(inline_keyboard=[*top_rows, [btn("Main Menu", "home", callback_data="home")]])


def chat_link(chat):
    if isinstance(chat, str) and chat.startswith("@"):
        return f"https://t.me/{chat[1:]}"
    if chat == CHANNEL_ID and CHANNEL_LINK:
        return CHANNEL_LINK
    return None


def join_keyboard():
    rows = []
    for c in REQUIRED:
        link = chat_link(c)
        if link:
            rows.append([btn(f"Join {c}" if isinstance(c, str) else "Join Channel", "channel", url=link)])
    rows.append([btn("I've Joined", "ok", style="success", callback_data="checkjoin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("New Giveaway", "plus", style="success", callback_data="agnew"),
         btn("Manage Giveaways", "gift", style="primary", callback_data="agws")],
        [btn("Post Update", "bell", callback_data="aupd"),
         btn("Broadcast", "channel", callback_data="abc")],
        [btn("Statistics", "stats", callback_data="astats"),
         btn("Users", "user", callback_data="ausers")],
        [btn("Giveaway History", "history", style="primary", callback_data="ahist"),
         btn("Search User", "web", callback_data="ausearch")],
        [btn("Export CSV", "csv", callback_data="acsv"),
         btn("Backup Now", "backup", callback_data="abackup")],
        [btn("Restore Backup", "restore", style="danger", callback_data="arestore")],
        [btn("Main Menu", "home", callback_data="home")],
    ])


def admin_back(*top_rows):
    return InlineKeyboardMarkup(inline_keyboard=[*top_rows, [btn("Admin Panel", "admin", callback_data="admin")]])


# ----------------------------------------------------------------------------
# Screens (text)
# ----------------------------------------------------------------------------
def home_text(name):
    return "\n".join([
        bq(f"{E('user')} {sc('Welcome')}, {esc(name or 'friend')}"),
        head("title", "Giveaway Vote Bot", " ❗️"),
        "",
        row("gift", "Open Giveaways", count("SELECT COUNT(*) FROM giveaways WHERE status='open'")),
        row("group", "Entries", count("SELECT COUNT(*) FROM entries")),
        row("vote", "Votes Cast", count("SELECT COUNT(*) FROM votes")),
        SEP,
        f"{E('bolt')} {sc('Open a giveaway link, send your name, then collect votes!')}",
    ])


def howto_text():
    return "\n".join([
        bq(f"{E('pin')} {sc('How to join')}"),
        "",
        row("dot", "Step 1", sc("Open the giveaway link you were given")),
        row("dot", "Step 2", sc("Join the channel if the bot asks")),
        row("dot", "Step 3", sc("Send the name you want to enter")),
        row("dot", "Step 4", sc("Your name is posted in the channel with a Vote button")),
        row("vote", "Step 5", sc("Share it and collect votes")),
        row("bot", "Voting", sc("To vote, start this bot once. Tap Vote on a channel post and the bot opens for you")),
        row("medal", "Step 6", sc("Track your rank on the Leaderboard")),
        SEP,
        f"{E('lock')} {sc('Only channel members can vote.')}",
        f"{E('warn')} {sc('Each person gets one vote per giveaway.')}",
    ])


def rules_text():
    return "\n".join([
        bq(f"{E('lock')} {sc('Rules & info')}"),
        "",
        row("dot2", "Entry", sc("One name per person for each giveaway")),
        row("dot2", "Voting", sc("One vote per person for each giveaway")),
        row("dot2", "Eligibility", sc("Voters must be members of the channel")),
        row("dot2", "Start The Bot", sc("Voters must have started this bot at least once")),
        row("dot2", "Fair Play", sc("No fake accounts, bots or vote trading")),
        row("dot2", "Winner", sc("Chosen by the admin when the giveaway ends")),
        row("dot2", "Decision", sc("Admin decision is final")),
        SEP,
        f"{E('trophy')} {sc('Prizes are shown on each giveaway.')}",
    ])


def about_text():
    return "\n".join([
        bq(f"{E('host')} {sc('Developer & host')}"),
        "",
        row("dev", "Developer", f"{esc(DEV_NAME)} {esc(DEV_HANDLE)}"),
        row("host", "Hosted By", esc(HOST_NAME)),
        SEP,
        f"{E('mail')} {sc('Need help? Message the developer.')}",
    ])


def about_keyboard():
    rows = [[btn("Developer", "dev", url=f"https://t.me/{DEV_HANDLE.lstrip('@')}")]]
    if CHANNEL_LINK:
        rows.append([btn("Channel", "channel", url=CHANNEL_LINK)])
    return back_keyboard(*rows)


def join_text(first_visit=True):
    title = "Join To Unlock" if first_visit else "Not Joined Yet"
    return "\n".join([
        bq(f"{E('lock')} {sc('Access locked')}"),
        head("channel", title, " ❗️"),
        "",
        sc("Join the channel below, then tap I've Joined."),
    ])


def giveaway_text(g):
    n, v = gw_counts(g["id"])
    lines = [
        bq(f"{E('gift')} {sc('Giveaway')} #{g['id']}"),
        f"{E('title')} <b>{esc(g['name'])}</b>",
        "",
        row("star", "Prize", esc(g["prize"]) if g["prize"] else sc("To be announced")),
        row("group", "Entries", n),
        row("vote", "Total Votes", v),
        row("time", "Status", sc("Open" if g["status"] == "open" else "Closed")),
    ]
    w = get_entry(g["winner_entry_id"]) if g["winner_entry_id"] else None
    if w:
        lines.append(row("crown", "Winner", f"<b>{esc(w['name'])}</b> ({w['votes']} {sc('votes')})"))
    return "\n".join(lines)


def entry_post_text(g, name):
    lines = [
        bq(f"{E('gift')} {sc('Giveaway')} ➛ {esc(g['name'])}"),
        f"{E('title')} <b>{esc(name)}</b>",
        "",
    ]
    if g["prize"]:
        lines.append(row("star", "Prize", esc(g["prize"])))
    lines += [SEP, f"{E('vote')} {sc('Tap Vote to support this entry. One vote per person.')}"]
    return "\n".join(lines)


def entry_text(g, e, headline):
    return "\n".join([
        bq(f"{E('ok')} {sc(headline)}"),
        "",
        row("gift", "Giveaway", esc(g["name"])),
        row("user", "Name", f"<b>{esc(e['name'])}</b>"),
        row("vote", "Votes", e["votes"]),
        row("rank", "Rank", f"#{rank_of(e)}"),
        SEP,
        f"{E('bolt')} {sc('Share your entry to collect more votes!')}",
    ])


def entry_kb(g, e):
    rows = []
    link = post_link(e["message_id"])
    if link:
        rows.append([btn("View Post", "channel", url=link),
                     btn("Share", "share", style="success",
                         url=share_url(link, f"Vote for {e['name']} in {g['name']}!"))])
    rows.append([btn("Leaderboard", "medal", style="primary", callback_data=f"lbg:{g['id']}")])
    return back_keyboard(*rows)


def leaderboard_text(g, viewer=None):
    rows = q_all("SELECT * FROM entries WHERE giveaway_id=? ORDER BY votes DESC, id ASC LIMIT 15", (g["id"],))
    n, v = gw_counts(g["id"])
    lines = [bq(f"{E('medal')} {sc('Leaderboard')} ➛ {esc(g['name'])}"), ""]
    if not rows:
        lines.append(sc("No entries yet. Be the first!"))
    for i, r in enumerate(rows, 1):
        you = f"  ⟵ {sc('You')}" if viewer and r["user_id"] == viewer else ""
        lines.append(f"{MEDALS.get(i, f'{i}.')} <b>{esc(r['name'])}</b> ➛ {r['votes']} {sc('votes')}{you}")
    mine = my_entry(g["id"], viewer) if viewer else None
    if mine and rank_of(mine) > 15:
        lines += ["…", f"{rank_of(mine)}. <b>{esc(mine['name'])}</b> ➛ {mine['votes']} {sc('votes')}  ⟵ {sc('You')}"]
    lines += [SEP, row("group", "Entries", n), row("vote", "Total Votes", v)]
    w = get_entry(g["winner_entry_id"]) if g["winner_entry_id"] else None
    if w:
        lines.append(row("crown", "Winner", f"<b>{esc(w['name'])}</b>"))
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# States
# ----------------------------------------------------------------------------
class NameFlow(StatesGroup):
    name = State()


class SupportFlow(StatesGroup):
    message = State()


class AdminFlow(StatesGroup):
    gw_name = State()
    gw_prize = State()
    broadcast = State()
    update = State()
    support_reply = State()
    restore = State()
    rename = State()
    user_search = State()


async def admin_only(callback: CallbackQuery):
    if callback.from_user.id not in ADMINS:
        await callback.answer("Not allowed.", show_alert=True)
        return False
    return True


# ----------------------------------------------------------------------------
# Membership gate + private-chat-only middleware
# ----------------------------------------------------------------------------
_JOINED = {}


def _is_in(member):
    if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
        return False
    if member.status == ChatMemberStatus.RESTRICTED:
        return bool(getattr(member, "is_member", True))
    return True


async def membership_ok(user_id, fresh=False):
    ts = _JOINED.get(user_id)
    if not fresh and ts and _time.time() - ts < 120:
        return True
    for chat in REQUIRED:
        try:
            member = await bot.get_chat_member(chat_id=chat, user_id=user_id)
            if not _is_in(member):
                _JOINED.pop(user_id, None)
                return False
        except Exception:
            logging.exception("Membership check failed for %s (is the bot admin there?)", chat)
            return False
    _JOINED[user_id] = _time.time()
    return True


class Gate(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = event.from_user
        # Vote buttons live on the channel posts: the vote handler does its own checks.
        if isinstance(event, CallbackQuery) and (event.data or "").startswith("v:"):
            return await handler(event, data)
        chat = event.chat if isinstance(event, Message) else (event.message.chat if event.message else None)
        if user is None or chat is None or chat.type != "private":
            return None                       # the bot only works in private chats
        upsert_user(user)
        if user.id in ADMINS:
            return await handler(event, data)
        if is_banned(user.id):
            if isinstance(event, CallbackQuery):
                await event.answer("Access disabled.", show_alert=True)
            else:
                await notice(chat.id, f"{E('ban')} {sc('Your access has been disabled.')}", ttl=10)
            return None
        if isinstance(event, CallbackQuery) and event.data == "checkjoin":
            return await handler(event, data)
        if await membership_ok(user.id):
            return await handler(event, data)
        if isinstance(event, CallbackQuery):
            if (event.data or "").startswith("bv:"):    # remember which entry they wanted to vote for
                _PENDING[user.id] = "vote_" + event.data.split(":", 1)[1]
            await safe_edit(event, join_text(), join_keyboard())
            await event.answer()
        else:
            parts = (event.text or "").split(maxsplit=1)
            if len(parts) == 2 and parts[0].split("@")[0] == "/start" and parts[1].startswith(("gw_", "vote_")):
                _PENDING[user.id] = parts[1].strip()     # remember the link until they have joined
            await tidy(event)
            await sweep(chat.id)
            await show_panel(chat.id, join_text(), join_keyboard())
        return None


dp.message.outer_middleware(Gate())
dp.callback_query.outer_middleware(Gate())


# ----------------------------------------------------------------------------
# Commands (registered first so they win over state handlers)
# ----------------------------------------------------------------------------
async def go_home(chat, user):
    await show_panel(chat, home_text(user.first_name), home_keyboard(is_admin(user.id)))


async def begin_giveaway(chat_id, user, g, state: FSMContext):
    """Start name registration for giveaway `g` (or show the existing entry)."""
    await state.clear()
    await sweep(chat_id)
    if g["status"] != "open":
        return await show_panel(chat_id, f"{E('warn')} {sc('This giveaway is closed.')}\n\n{giveaway_text(g)}",
                                back_keyboard([btn("Leaderboard", "medal", callback_data=f"lbg:{g['id']}")]))
    e = my_entry(g["id"], user.id)
    if e:
        return await show_panel(chat_id, entry_text(g, e, "You are already registered"), entry_kb(g, e))
    await state.set_state(NameFlow.name)
    await state.update_data(gid=g["id"])
    await ask(chat_id,
              f"{E('gift')} <b>{esc(g['name'])}</b>\n"
              f"{E('pin')} {sc('Send the name you want to enter (max 40 characters).')}\n"
              f"{sc('Send /cancel to stop.')}")


def voted_for(gid, uid):
    """The entry this user already voted for in giveaway `gid` (or None)."""
    return q_one("SELECT e.* FROM votes v JOIN entries e ON e.id=v.entry_id "
                 "WHERE v.giveaway_id=? AND v.voter_id=?", (gid, uid))


async def show_vote_card(chat_id, user, g, e):
    """Shown in the bot when someone taps Vote in the channel before starting the bot."""
    done = voted_for(g["id"], user.id)
    lines = [bq(f"{E('vote')} {sc('Vote')} ➛ {esc(g['name'])}"),
             f"{E('title')} <b>{esc(e['name'])}</b>", "",
             row("vote", "Votes", e["votes"]), row("rank", "Rank", f"#{rank_of(e)}"), SEP]
    kb = []
    if g["status"] != "open":
        lines.append(f"{E('warn')} {sc('Voting for this giveaway is closed.')}")
    elif done and done["id"] == e["id"]:
        lines.append(f"{E('ok')} {sc('You already voted for this entry.')}")
    elif done:
        lines.append(f"{E('warn')} {sc('You already voted for')} <b>{esc(done['name'])}</b>. {sc('One vote per person.')}")
    else:
        lines.append(f"{E('bolt')} {sc('Tap the button below to cast your vote.')}")
        kb.append([btn(f"Vote For {e['name'][:25]}", "vote", style="success", callback_data=f"bv:{e['id']}")])
    link = post_link(e["message_id"])
    if link:
        kb.append([btn("View Post", "channel", url=link)])
    kb.append([btn("Leaderboard", "medal", style="primary", callback_data=f"lbg:{g['id']}")])
    await show_panel(chat_id, "\n".join(lines), back_keyboard(*kb))


async def handle_arg(chat_id, user, arg, state):
    """Open a deep link: gw_<code> (register a name) or vote_<entry id> (vote for an entry).
    Returns True if a screen was shown."""
    if arg.startswith("gw_"):
        g = q_one("SELECT * FROM giveaways WHERE code=? AND status!='deleted'", (arg[3:],))
        if g:
            await begin_giveaway(chat_id, user, g, state)
            return True
        await notice(chat_id, f"{E('err')} {sc('This giveaway link is invalid.')}", ttl=8)
    elif arg.startswith("vote_") and arg[5:].isdigit():
        e = get_entry(int(arg[5:]))
        g = get_giveaway(e["giveaway_id"]) if e else None
        if e and g:
            await state.clear()
            await sweep(chat_id)
            await show_vote_card(chat_id, user, g, e)
            return True
        await notice(chat_id, f"{E('err')} {sc('This entry is no longer active.')}", ttl=8)
    return False


@dp.message(CommandStart(deep_link=True))
async def start_link(message: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    await tidy(message)
    await sweep(message.chat.id)
    if await handle_arg(message.chat.id, message.from_user, command.args or "", state):
        return
    await go_home(message.chat.id, message.from_user)


@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    await tidy(message)
    await sweep(message.chat.id)
    await go_home(message.chat.id, message.from_user)


@dp.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    await tidy(message)
    await sweep(message.chat.id)
    await notice(message.chat.id, f"{E('err')} {sc('Cancelled.')}", ttl=5)
    await go_home(message.chat.id, message.from_user)


@dp.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    await tidy(message)
    await sweep(message.chat.id)
    await show_panel(message.chat.id, admin_text(), admin_keyboard())


@dp.message(Command("backup"))
async def cmd_backup(message: Message):
    if not is_admin(message.from_user.id):
        return
    await tidy(message)
    path = await backup_now("manual")
    await bot.send_document(message.chat.id, BufferedInputFile(path.read_bytes(), filename=path.name),
                            caption=f"{E('backup')} {sc('Backup saved')}")


@dp.message(Command("ban"))
@dp.message(Command("unban"))
async def cmd_ban(message: Message):
    if not is_admin(message.from_user.id):
        return
    await tidy(message)
    parts = (message.text or "").split()
    ban = parts[0].lstrip("/").split("@")[0].lower() == "ban"
    if len(parts) != 2:
        return await notice(message.chat.id, "Usage: <code>/ban @username</code> or <code>/ban 123456</code>", ttl=10)
    tok = parts[1]
    u = get_user(int(tok)) if tok.isdigit() else q_one("SELECT * FROM users WHERE lower(username)=?", (tok.lstrip("@").lower(),))
    if not u:
        return await notice(message.chat.id, f"{E('warn')} {sc('User not found. They must have started the bot.')}")
    run("UPDATE users SET banned=? WHERE user_id=?", (1 if ban else 0, u["user_id"]))
    await notice(message.chat.id, f"{E('ok')} {sc('Banned' if ban else 'Unbanned')} <code>{u['user_id']}</code>", ttl=8)


@dp.message(Command("user"))
async def cmd_user(message: Message, command: CommandObject):
    """Admin: /user @username | /user 123456 | /user <name>  -> user info card."""
    if not is_admin(message.from_user.id):
        return
    await tidy(message)
    term = (command.args or "").strip()
    if not term:
        return await notice(message.chat.id, "Usage: <code>/user @username</code>, <code>/user 123456</code> or a name", ttl=10)
    await show_user_results(message.chat.id, term)


# ----------------------------------------------------------------------------
# User: menus
# ----------------------------------------------------------------------------
@dp.callback_query(F.data == "checkjoin")
async def check_join(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    _JOINED.pop(uid, None)
    if not await membership_ok(uid):
        return await callback.answer("You must join first.", show_alert=True)
    await callback.answer("Verified!")
    chat = callback.message.chat.id
    arg = _PENDING.pop(uid, None)
    if arg:
        await _delete(chat, callback.message.message_id)
        _PANEL.pop(chat, None)
        if not await handle_arg(chat, callback.from_user, arg, state):
            await go_home(chat, callback.from_user)
        return
    await safe_edit(callback, home_text(callback.from_user.first_name), home_keyboard(is_admin(uid)))


@dp.callback_query(F.data == "home")
async def home(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await sweep(callback.message.chat.id, keep=callback.message.message_id)
    await safe_edit(callback, home_text(callback.from_user.first_name), home_keyboard(is_admin(callback.from_user.id)))
    await callback.answer()


@dp.callback_query(F.data == "howto")
async def howto(callback: CallbackQuery):
    await safe_edit(callback, howto_text(), back_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "rules")
async def rules(callback: CallbackQuery):
    await safe_edit(callback, rules_text(), back_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "about")
async def about(callback: CallbackQuery):
    await safe_edit(callback, about_text(), about_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "updates")
async def updates(callback: CallbackQuery):
    ups = q_all("SELECT * FROM updates ORDER BY id DESC LIMIT 6")
    lines = [bq(f"{E('bell')} {sc('Latest updates')}"), ""]
    if ups:
        for u in ups:
            lines.append(f"{E('dot2')} {esc(u['body'])}  <i>({u['created_at'][:10]})</i>")
    else:
        lines.append(sc("No updates yet."))
    await safe_edit(callback, "\n".join(lines),
                    back_keyboard([btn("Giveaways", "gift", style="primary", callback_data="gws")]))
    await callback.answer()


# ----------------------------------------------------------------------------
# User: giveaways, registration, leaderboard
# ----------------------------------------------------------------------------
@dp.callback_query(F.data == "gws")
async def giveaways(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await sweep(callback.message.chat.id, keep=callback.message.message_id)
    rows = q_all("SELECT * FROM giveaways WHERE status='open' ORDER BY id DESC LIMIT 10")
    lines = [bq(f"{E('gift')} {sc('Giveaways')}"), ""]
    kb = []
    if rows:
        for g in rows:
            n, v = gw_counts(g["id"])
            lines.append(f"{E('dot')} <b>{esc(g['name'])}</b> ➛ {n} {sc('entries')} | {v} {sc('votes')}")
            kb.append([btn(g["name"][:40], "gift", style="primary", callback_data=f"g:{g['id']}")])
        lines += [SEP, f"{E('bolt')} {sc('Tap a giveaway to see details and register.')}"]
    else:
        lines.append(sc("No giveaways are open right now. Check Updates later."))
    await safe_edit(callback, "\n".join(lines), back_keyboard(*kb))
    await callback.answer()


@dp.callback_query(F.data.startswith("g:"))
async def giveaway_detail(callback: CallbackQuery):
    g = get_giveaway(int(callback.data.split(":")[1]))
    if not g:
        return await callback.answer("Giveaway not found.", show_alert=True)
    kb = []
    e = my_entry(g["id"], callback.from_user.id)
    if e:
        kb.append([btn("My Entry", "user", style="primary", callback_data=f"gme:{g['id']}")])
    elif g["status"] == "open":
        kb.append([btn("Register My Name", "ok", style="success", callback_data=f"gj:{g['id']}")])
    kb.append([btn("Leaderboard", "medal", callback_data=f"lbg:{g['id']}"),
               btn("All Giveaways", "gift", callback_data="gws")])
    await safe_edit(callback, giveaway_text(g), back_keyboard(*kb))
    await callback.answer()


@dp.callback_query(F.data.startswith("gj:"))
async def giveaway_join(callback: CallbackQuery, state: FSMContext):
    g = get_giveaway(int(callback.data.split(":")[1]))
    if not g:
        return await callback.answer("Giveaway not found.", show_alert=True)
    if g["status"] != "open":
        return await callback.answer("This giveaway is closed.", show_alert=True)
    await callback.answer()
    await begin_giveaway(callback.message.chat.id, callback.from_user, g, state)


@dp.callback_query(F.data.startswith("gme:"))
async def my_entry_view(callback: CallbackQuery):
    g = get_giveaway(int(callback.data.split(":")[1]))
    e = my_entry(g["id"], callback.from_user.id) if g else None
    if not e:
        return await callback.answer("No entry found.", show_alert=True)
    await safe_edit(callback, entry_text(g, e, "Your entry"), entry_kb(g, e))
    await callback.answer()


@dp.message(NameFlow.name)
async def got_name(message: Message, state: FSMContext):
    chat = message.chat.id
    user = message.from_user
    await tidy(message)
    name = re.sub(r"\s+", " ", message.text or "").strip()
    if not name or len(name) > 40:
        return await notice(chat, f"{E('warn')} {sc('Send a valid name (max 40 characters).')}", ttl=6)
    data = await state.get_data()
    g = get_giveaway(data.get("gid"))
    if not g or g["status"] != "open":
        await state.clear()
        await sweep(chat)
        await notice(chat, f"{E('warn')} {sc('This giveaway is no longer open.')}", ttl=8)
        return await go_home(chat, user)
    if my_entry(g["id"], user.id):
        await state.clear()
        await sweep(chat)
        return await go_home(chat, user)
    try:
        eid = run("INSERT INTO entries(giveaway_id, user_id, name, created_at) VALUES(?,?,?,?)",
                  (g["id"], user.id, name, now_iso()))
    except sqlite3.IntegrityError:
        await state.clear()
        await sweep(chat)
        return await go_home(chat, user)
    try:
        post = await bot.send_message(CHANNEL_ID, entry_post_text(g, name), reply_markup=vote_kb(eid, 0))
    except Exception:
        logging.exception("could not post the entry to the channel (is the bot admin there?)")
        run("DELETE FROM entries WHERE id=?", (eid,))
        return await notice(chat, f"{E('err')} {sc('Could not post to the channel right now. Try again later.')}", ttl=8)
    run("UPDATE entries SET message_id=? WHERE id=?", (post.message_id, eid))
    await state.clear()
    await sweep(chat)
    e = get_entry(eid)
    await show_panel(chat, entry_text(g, e, "Name registered"), entry_kb(g, e))
    handle = f"@{user.username}" if user.username else "no username"
    await log_channel(f"{E('new')} <b>{sc('New entry')}</b> #{eid} ➛ {esc(name)}\n"
                      f"{esc(g['name'])} · {user.id} ({esc(handle)})")
    mark_dirty()


async def process_vote(user, entry_id):
    """All vote rules in one place (used by the channel button and the bot button).
    Returns (status, votes, entry, giveaway); status is ok / gone / closed / banned / notmember / dup."""
    e = get_entry(entry_id)
    g = get_giveaway(e["giveaway_id"]) if e else None
    if not e or not g:
        return "gone", None, e, g
    if g["status"] != "open":
        return "closed", None, e, g
    if is_banned(user.id):
        return "banned", None, e, g
    if not await membership_ok(user.id, fresh=True):
        return "notmember", None, e, g
    votes = cast_vote(g["id"], user.id, e["id"])
    if votes is None:
        return "dup", None, e, g
    return "ok", votes, e, g


def vote_error(status, user, g):
    if status == "gone":
        return "This entry is no longer active."
    if status == "closed":
        return "Voting for this giveaway is closed."
    if status == "banned":
        return "Access disabled."
    if status == "notmember":
        return "❌ Join the channel first, then tap Vote again."
    done = voted_for(g["id"], user.id) if g else None
    who = f" for {done['name']}" if done else ""
    return f"You have already voted{who} in this giveaway - one vote per person."


async def sync_post(e, votes):
    """Update the Vote (N) button on the channel post."""
    if not e["message_id"]:
        return
    try:
        await bot.edit_message_reply_markup(chat_id=CHANNEL_ID, message_id=e["message_id"],
                                            reply_markup=vote_kb(e["id"], votes))
    except Exception:
        pass   # the vote is saved; the button catches up on the next vote


def vote_done_text(g, e):
    return "\n".join([
        bq(f"{E('ok')} {sc('Vote counted')}"),
        "",
        row("gift", "Giveaway", esc(g["name"])),
        row("user", "Voted For", f"<b>{esc(e['name'])}</b>"),
        row("vote", "Votes", e["votes"]),
        row("rank", "Rank", f"#{rank_of(e)}"),
        SEP,
        f"{E('bolt')} {sc('Thanks for voting! Check the live ranking below.')}",
    ])


@dp.callback_query(F.data.startswith("v:"))
async def vote(callback: CallbackQuery):
    """Vote button on a channel post. The voter must have started the bot first."""
    user = callback.from_user
    eid = int(callback.data.split(":")[1])
    if not get_user(user.id):
        text = "Start the bot first, then tap Vote there to finish your vote."
        try:   # t.me/<bot>?start=... links are allowed here: Telegram opens the bot straight away
            return await callback.answer(text, url=f"https://t.me/{BOT_USERNAME}?start=vote_{eid}")
        except Exception:
            return await callback.answer(text, show_alert=True)
    status, votes, e, g = await process_vote(user, eid)
    if status != "ok":
        return await callback.answer(vote_error(status, user, g), show_alert=True)
    await callback.answer("✅ Your vote has been counted!")
    await sync_post(e, votes)
    mark_dirty()
    # also show the voter the result inside the bot
    await notice(user.id, vote_done_text(g, get_entry(e["id"])), ttl=60,
                 reply_markup=back_keyboard([btn("Leaderboard", "medal", style="primary", callback_data=f"lbg:{g['id']}")]))


@dp.callback_query(F.data.startswith("bv:"))
async def vote_in_bot(callback: CallbackQuery):
    """Vote button shown inside the bot (after tapping Vote in the channel as a new user)."""
    user = callback.from_user
    status, votes, e, g = await process_vote(user, int(callback.data.split(":")[1]))
    if status != "ok":
        return await callback.answer(vote_error(status, user, g), show_alert=True)
    await callback.answer("✅ Your vote has been counted!")
    await sync_post(e, votes)
    mark_dirty()
    await safe_edit(callback, vote_done_text(g, get_entry(e["id"])),
                    back_keyboard([btn("Leaderboard", "medal", style="primary", callback_data=f"lbg:{g['id']}")]))


@dp.callback_query(F.data == "lb")
async def lb_menu(callback: CallbackQuery):
    rows = q_all("SELECT * FROM giveaways WHERE status IN ('open','closed') ORDER BY id DESC LIMIT 10")
    lines = [bq(f"{E('medal')} {sc('Leaderboards')}"), ""]
    kb = []
    if rows:
        lines.append(sc("Pick a giveaway to see the live ranking."))
        for g in rows:
            mark = "" if g["status"] == "open" else " (closed)"
            kb.append([btn(f"{g['name'][:30]}{mark}", "medal", style="primary" if g["status"] == "open" else None,
                           callback_data=f"lbg:{g['id']}")])
    else:
        lines.append(sc("No giveaways yet."))
    await safe_edit(callback, "\n".join(lines), back_keyboard(*kb))
    await callback.answer()


@dp.callback_query(F.data.startswith("lbg:"))
async def lb_giveaway(callback: CallbackQuery):
    g = get_giveaway(int(callback.data.split(":")[1]))
    if not g:
        return await callback.answer("Giveaway not found.", show_alert=True)
    rows = [[btn("Refresh", "spin", callback_data=f"lbg:{g['id']}")]]
    if CHANNEL_LINK:
        rows.append([btn("Open Channel To Vote", "vote", style="success", url=CHANNEL_LINK)])
    rows.append([btn("All Leaderboards", "medal", callback_data="lb")])
    await safe_edit(callback, leaderboard_text(g, callback.from_user.id), back_keyboard(*rows))
    await callback.answer()


@dp.callback_query(F.data == "mye")
async def my_entries(callback: CallbackQuery):
    rows = q_all("SELECT e.*, g.name AS gname FROM entries e JOIN giveaways g ON g.id=e.giveaway_id "
                 "WHERE e.user_id=? AND g.status!='deleted' ORDER BY e.id DESC LIMIT 10", (callback.from_user.id,))
    lines = [bq(f"{E('box')} {sc('My entries')}"), ""]
    kb = []
    if rows:
        for r in rows:
            lines.append(f"{E('dot')} <b>{esc(r['gname'])}</b> ➛ {esc(r['name'])} · "
                         f"{r['votes']} {sc('votes')} · #{rank_of(r)}")
            kb.append([btn(r["gname"][:40], "gift", callback_data=f"gme:{r['giveaway_id']}")])
    else:
        lines.append(sc("You have not entered any giveaway yet."))
        kb.append([btn("Giveaways", "gift", style="primary", callback_data="gws")])
    await safe_edit(callback, "\n".join(lines), back_keyboard(*kb))
    await callback.answer()


# ----------------------------------------------------------------------------
# User <-> admin messages
# ----------------------------------------------------------------------------
@dp.callback_query(F.data == "support")
async def support_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await sweep(callback.message.chat.id, keep=callback.message.message_id)
    await state.set_state(SupportFlow.message)
    await ask(callback.message.chat.id, f"{E('mail')} {sc('Type your message for the admin.')}\n{sc('Send /cancel to stop.')}")
    await callback.answer()


@dp.message(SupportFlow.message)
async def support_send(message: Message, state: FSMContext):
    chat = message.chat.id
    await tidy(message)
    text = (message.text or "").strip()
    if not text:
        return await notice(chat, f"{E('warn')} {sc('Please send text only.')}", ttl=6)
    await state.clear()
    await sweep(chat)
    u = message.from_user
    handle = f"@{u.username}" if u.username else "no username"
    body = "\n".join([
        bq(f"{E('mail')} {sc('Message from user')}"),
        "",
        row("user", "User", f"{esc(u.first_name)} ({esc(handle)})"),
        row("id", "ID", f"<code>{u.id}</code>"),
        SEP,
        esc(text[:3500]),
    ])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("Reply To User", "mail", style="primary", callback_data=f"sreply:{u.id}")]])
    for admin in ADMINS:
        try:
            await bot.send_message(admin, body, reply_markup=kb)
        except Exception:
            logging.exception("could not notify admin %s", admin)
    await log_channel(body)
    await notice(chat, f"{E('ok')} {sc('Message sent to the admin.')}", ttl=6)
    await go_home(chat, u)


@dp.callback_query(F.data.startswith("sreply:"))
async def support_reply_start(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return
    await state.clear()
    await state.set_state(AdminFlow.support_reply)
    await state.update_data(uid=int(callback.data.split(":")[1]))
    await ask(callback.message.chat.id, f"{E('mail')} {sc('Type your reply.')}\n{sc('Send /cancel to stop.')}")
    await callback.answer()


@dp.message(AdminFlow.support_reply)
async def support_reply_send(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    chat = message.chat.id
    await tidy(message)
    data = await state.get_data()
    await state.clear()
    await sweep(chat)
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn("Message Admin", "mail", style="primary", callback_data="support")]])
    try:
        await bot.send_message(data["uid"], "\n".join([bq(f"{E('mail')} {sc('Message from admin')}"), "",
                                                        esc(message.text or "")]), reply_markup=kb)
        await notice(chat, f"{E('ok')} {sc('Reply delivered.')}", ttl=6)
    except Exception:
        await notice(chat, f"{E('err')} {sc('Could not deliver the reply.')}", ttl=8)


# ----------------------------------------------------------------------------
# Admin panel
# ----------------------------------------------------------------------------
def admin_text():
    return "\n".join([
        bq(f"{E('admin')} {sc('Admin panel')}"),
        "",
        row("gift", "Open Giveaways", count("SELECT COUNT(*) FROM giveaways WHERE status='open'")),
        row("group", "Entries", count("SELECT COUNT(*) FROM entries")),
        row("vote", "Votes", count("SELECT COUNT(*) FROM votes")),
        row("user", "Users", count("SELECT COUNT(*) FROM users")),
        row("history", "In History", count("SELECT COUNT(*) FROM giveaways WHERE status='deleted'")),
        SEP,
        f"{E('plus')} {sc('Create a giveaway to get a link. Everyone who opens it can register a name.')}",
        f"{E('backup')} {sc('Everything is backed up to the private storage channel.')}",
    ])


@dp.callback_query(F.data == "admin")
async def admin_panel(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return
    await state.clear()
    await sweep(callback.message.chat.id, keep=callback.message.message_id)
    await safe_edit(callback, admin_text(), admin_keyboard())
    await callback.answer()


# ---- create giveaway ---------------------------------------------------------------
@dp.callback_query(F.data == "agnew")
async def gw_new(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return
    await state.clear()
    await sweep(callback.message.chat.id, keep=callback.message.message_id)
    await state.set_state(AdminFlow.gw_name)
    await ask(callback.message.chat.id, f"{E('gift')} {sc('Send the giveaway name (max 60 characters).')}\n{sc('Send /cancel to stop.')}")
    await callback.answer()


@dp.message(AdminFlow.gw_name)
async def gw_name(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    chat = message.chat.id
    await tidy(message)
    name = re.sub(r"\s+", " ", message.text or "").strip()
    if not name or len(name) > 60:
        return await notice(chat, f"{E('warn')} {sc('Send a valid name (max 60 characters).')}", ttl=6)
    await sweep(chat)
    await state.update_data(name=name)
    await state.set_state(AdminFlow.gw_prize)
    await ask(chat, f"{E('star')} {sc('Send the prize, or send - to skip.')}")


@dp.message(AdminFlow.gw_prize)
async def gw_prize(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    chat = message.chat.id
    await tidy(message)
    prize = (message.text or "").strip()[:100]
    data = await state.get_data()
    await state.clear()
    await sweep(chat)
    code = secrets.token_urlsafe(6)
    gid = run("INSERT INTO giveaways(name, prize, code, created_at) VALUES(?,?,?,?)",
              (data["name"], None if prize in ("", "-") else prize, code, now_iso()))
    g = get_giveaway(gid)
    await log_channel(f"{E('gift')} <b>{sc('Giveaway created')}</b> #{gid} ➛ {esc(g['name'])}\n{esc(gw_link(g))}")
    mark_dirty()
    await show_panel(chat, admin_gw_text(g, created=True), admin_gw_kb(g))


def admin_gw_text(g, created=False):
    n, v = gw_counts(g["id"])
    lines = [
        bq(f"{E('gift')} {sc('Giveaway created' if created else 'Giveaway')} #{g['id']}"),
        f"{E('title')} <b>{esc(g['name'])}</b>",
        "",
        row("star", "Prize", esc(g["prize"]) if g["prize"] else sc("To be announced")),
        row("time", "Status", sc("Open" if g["status"] == "open" else "Closed")),
        row("group", "Entries", n),
        row("vote", "Votes", v),
        row("link", "Link", f"<code>{esc(gw_link(g))}</code>"),
    ]
    w = get_entry(g["winner_entry_id"]) if g["winner_entry_id"] else None
    if w:
        lines.append(row("crown", "Winner", f"<b>{esc(w['name'])}</b> ({w['votes']} {sc('votes')})"))
    lines += [SEP, f"{E('bolt')} {sc('Share the link. Opening it lets people register a name for this giveaway.')}"]
    return "\n".join(lines)


def admin_gw_kb(g):
    gid = g["id"]
    rows = [
        [btn("Share Link", "share", style="primary",
             url=share_url(gw_link(g), f"Join the giveaway: {g['name']}")),
         btn("QR Code", "qr", callback_data=f"agq:{gid}")],
        [btn("Leaderboard", "medal", callback_data=f"lbg:{gid}"),
         btn("Participants", "list", callback_data=f"age:{gid}")],
        [btn("Close Giveaway" if g["status"] == "open" else "Reopen Giveaway",
             "lock", callback_data=f"agt:{gid}")],
        [btn("Top Voted Winner", "crown", style="success", callback_data=f"agwin:{gid}:top"),
         btn("Random Winner", "dice", callback_data=f"agwin:{gid}:rand")],
    ]
    if g["winner_entry_id"]:
        rows.append([btn("Announce Winner In Channel", "trophy", style="primary", callback_data=f"agann:{gid}")])
    rows.append([btn("Delete Giveaway", "trash", style="danger", callback_data=f"agdel:{gid}")])
    rows.append([btn("All Giveaways", "gift", callback_data="agws")])
    return admin_back(*rows)


@dp.callback_query(F.data == "agws")
async def gw_list(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return
    await state.clear()
    await sweep(callback.message.chat.id, keep=callback.message.message_id)
    rows = q_all("SELECT * FROM giveaways WHERE status!='deleted' ORDER BY id DESC LIMIT 15")
    lines = [bq(f"{E('gift')} {sc('Manage giveaways')}"), ""]
    kb = []
    if rows:
        for g in rows:
            n, v = gw_counts(g["id"])
            icon = "✅" if g["status"] == "open" else "⛔"
            lines.append(f"{icon} <b>#{g['id']} {esc(g['name'])}</b> ➛ {n} {sc('entries')} | {v} {sc('votes')}")
            kb.append([btn(f"#{g['id']} {g['name'][:32]}", "gift", callback_data=f"agw:{g['id']}")])
    else:
        lines.append(sc("No giveaways yet. Create one first."))
    await safe_edit(callback, "\n".join(lines), admin_back(*kb))
    await callback.answer()


async def _gw_from(callback):
    if not await admin_only(callback):
        return None
    g = get_giveaway(int(callback.data.split(":")[1]))
    if not g:
        await callback.answer("Giveaway not found.", show_alert=True)
    return g


@dp.callback_query(F.data.startswith("agw:"))
async def gw_detail(callback: CallbackQuery):
    g = await _gw_from(callback)
    if not g:
        return
    await safe_edit(callback, admin_gw_text(g), admin_gw_kb(g))
    await callback.answer()


@dp.callback_query(F.data.startswith("agt:"))
async def gw_toggle(callback: CallbackQuery):
    g = await _gw_from(callback)
    if not g:
        return
    run("UPDATE giveaways SET status=? WHERE id=?", ("closed" if g["status"] == "open" else "open", g["id"]))
    mark_dirty()
    g = get_giveaway(g["id"])
    await safe_edit(callback, admin_gw_text(g), admin_gw_kb(g))
    await callback.answer("Updated.")


@dp.callback_query(F.data.startswith("agq:"))
async def gw_qr(callback: CallbackQuery):
    g = await _gw_from(callback)
    if not g:
        return
    if qrcode is None:
        return await callback.answer('QR needs: pip install "qrcode[pil]"', show_alert=True)
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=4)
    qr.add_data(gw_link(g))
    qr.make(fit=True)
    buf = BytesIO()
    qr.make_image(fill_color="black", back_color="white").save(buf)
    await callback.answer()
    remember(await bot.send_photo(callback.message.chat.id, BufferedInputFile(buf.getvalue(), filename="giveaway_qr.png"),
                                  caption=f"{E('gift')} <b>{esc(g['name'])}</b>\n<code>{esc(gw_link(g))}</code>"))


@dp.callback_query(F.data.startswith("agwin:"))
async def gw_winner(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    _, gid, mode = callback.data.split(":")
    g = get_giveaway(int(gid))
    if not g:
        return await callback.answer("Giveaway not found.", show_alert=True)
    if mode == "top":
        w = q_one("SELECT * FROM entries WHERE giveaway_id=? ORDER BY votes DESC, id ASC LIMIT 1", (g["id"],))
    else:
        w = None
        pool = q_all("SELECT * FROM entries WHERE giveaway_id=?", (g["id"],))
        random.shuffle(pool)
        for cand in pool[:40]:          # only pick someone who is still in the channel
            if await membership_ok(cand["user_id"], fresh=True):
                w = cand
                break
    if not w:
        return await callback.answer("No eligible entries.", show_alert=True)
    run("UPDATE giveaways SET winner_entry_id=? WHERE id=?", (w["id"], g["id"]))
    mark_dirty()
    g = get_giveaway(g["id"])
    await safe_edit(callback, admin_gw_text(g), admin_gw_kb(g))
    await callback.answer("Winner selected.")


@dp.callback_query(F.data.startswith("agann:"))
async def gw_announce(callback: CallbackQuery):
    g = await _gw_from(callback)
    if not g:
        return
    w = get_entry(g["winner_entry_id"]) if g["winner_entry_id"] else None
    if not w:
        return await callback.answer("Pick a winner first.", show_alert=True)
    lines = [bq(f"{E('trophy')} {sc('Winner announced')}"), f"{E('title')} <b>{esc(g['name'])}</b>", "",
             row("crown", "Winner", f"<b>{esc(w['name'])}</b>"), row("vote", "Votes", w["votes"])]
    if g["prize"]:
        lines.append(row("star", "Prize", esc(g["prize"])))
    try:
        await bot.send_message(CHANNEL_ID, "\n".join(lines))
    except Exception:
        return await callback.answer("Could not post in the channel.", show_alert=True)
    try:
        await bot.send_message(w["user_id"], f"{E('party')} <b>{sc('Congratulations!')}</b> "
                                             f"{sc('You won')} {esc(g['name'])}. {sc('The admin will contact you.')}")
    except Exception:
        pass
    await callback.answer("Announced in the channel.", show_alert=True)


ENTRIES_PER_PAGE = 8
PER_PAGE = 8


def like(term):
    """LIKE pattern that treats % and _ typed by the admin literally."""
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def user_info_lines(uid):
    """Telegram account info for a user (refreshed every time they use the bot)."""
    u = get_user(uid)
    if u:
        handle = f"@{u['username']}" if u["username"] else sc("No username")
        lines = [row("user", "Telegram Name", esc(u["first_name"] or "-")),
                 row("link", "Username", esc(handle)),
                 row("id", "User ID", f"<code>{uid}</code>"),
                 row("calendar", "Started Bot", esc((u["joined_at"] or "")[:16])),
                 row("time", "Last Seen", esc((u["last_seen"] or "")[:16])),
                 row("ban", "Banned", sc("Yes") if u["banned"] else sc("No"))]
    else:
        lines = [row("id", "User ID", f"<code>{uid}</code>"), sc("No profile saved for this user.")]
    lines.append(f'{E("link")} <a href="tg://user?id={uid}">{sc("Open profile")}</a>')
    return lines


def entry_admin_text(g, e):
    return "\n".join([
        bq(f"{E('user')} {sc('Participant')} #{e['id']}"),
        f"{E('title')} <b>{esc(e['name'])}</b>",
        "",
        row("gift", "Giveaway", esc(g["name"])),
        row("vote", "Votes", e["votes"]),
        row("rank", "Rank", f"#{rank_of(e)}"),
        row("calendar", "Registered", esc((e["created_at"] or "")[:16])),
        SEP,
        *user_info_lines(e["user_id"]),
    ])


def entry_admin_kb(g, e):
    u = get_user(e["user_id"])
    banned = bool(u and u["banned"])
    rows = [
        [btn("Rename", "pin", callback_data=f"aer:{e['id']}"),
         btn("Message User", "mail", style="primary", callback_data=f"sreply:{e['user_id']}")],
        [btn("Unban User" if banned else "Ban User", "ban", style="success" if banned else "danger",
             callback_data=f"aub:{e['user_id']}:{e['id']}"),
         btn("Delete Entry", "trash", style="danger", callback_data=f"aed:{e['id']}")],
    ]
    links = []
    if u and u["username"]:
        links.append(btn("Open Chat", "user", url=f"https://t.me/{u['username']}"))
    post = post_link(e["message_id"])
    if post:
        links.append(btn("View Post", "channel", url=post))
    if links:
        rows.append(links)
    rows.append([btn("Participants", "list", callback_data=f"age:{g['id']}")])
    return admin_back(*rows)


async def render_entries(callback, g, page=0):
    total = count("SELECT COUNT(*) FROM entries WHERE giveaway_id=?", (g["id"],))
    pages = max(1, -(-total // ENTRIES_PER_PAGE))
    page = max(0, min(page, pages - 1))
    rows = q_all("SELECT e.*, u.username FROM entries e LEFT JOIN users u ON u.user_id=e.user_id "
                 "WHERE e.giveaway_id=? ORDER BY e.votes DESC, e.id ASC LIMIT ? OFFSET ?",
                 (g["id"], ENTRIES_PER_PAGE, page * ENTRIES_PER_PAGE))
    lines = [bq(f"{E('list')} {sc('Participants')} ➛ {esc(g['name'])}"), ""]
    kb = []
    if rows:
        for i, r in enumerate(rows, page * ENTRIES_PER_PAGE + 1):
            handle = f"@{r['username']}" if r["username"] else "-"
            lines.append(f"{i}. <b>{esc(r['name'])}</b> · {esc(handle)} · <code>{r['user_id']}</code> "
                         f"➛ {r['votes']} {sc('votes')}")
            label = f"#{r['id']} {r['name'][:22]} {handle if r['username'] else ''}".strip()
            kb.append([btn(label[:48], "user", callback_data=f"aei:{r['id']}")])
        lines += [SEP, f"{E('bolt')} {sc('Tap a participant to see their info and manage them.')}"]
        if pages > 1:
            lines.append(sc(f"Page {page + 1} of {pages}"))
    else:
        lines.append(sc("No entries yet."))
    nav = []
    if page > 0:
        nav.append(btn("Prev", "reply", callback_data=f"age:{g['id']}:{page - 1}"))
    if page < pages - 1:
        nav.append(btn("Next", "bolt", callback_data=f"age:{g['id']}:{page + 1}"))
    if nav:
        kb.append(nav)
    kb.append([btn("Back", "gift", callback_data=f"agw:{g['id']}")])
    await safe_edit(callback, "\n".join(lines), admin_back(*kb))


@dp.callback_query(F.data.startswith("age:"))
async def gw_entries(callback: CallbackQuery):
    g = await _gw_from(callback)
    if not g:
        return
    parts = callback.data.split(":")
    await render_entries(callback, g, int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0)
    await callback.answer()


@dp.callback_query(F.data.startswith("aei:"))
async def entry_detail(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    e = get_entry(int(callback.data.split(":")[1]))
    g = get_giveaway(e["giveaway_id"]) if e else None
    if not e or not g:
        return await callback.answer("Entry not found.", show_alert=True)
    await safe_edit(callback, entry_admin_text(g, e), entry_admin_kb(g, e))
    await callback.answer()


@dp.callback_query(F.data.startswith("aer:"))
async def entry_rename_start(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return
    e = get_entry(int(callback.data.split(":")[1]))
    if not e:
        return await callback.answer("Entry not found.", show_alert=True)
    await state.clear()
    await state.set_state(AdminFlow.rename)
    await state.update_data(eid=e["id"])
    await ask(callback.message.chat.id, f"{E('pin')} {sc('Send the new name for')} <b>{esc(e['name'])}</b> "
                                        f"{sc('(max 40 characters).')}\n{sc('Send /cancel to stop.')}")
    await callback.answer()


@dp.message(AdminFlow.rename)
async def entry_rename_do(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    chat = message.chat.id
    await tidy(message)
    name = re.sub(r"\s+", " ", message.text or "").strip()
    if not name or len(name) > 40:
        return await notice(chat, f"{E('warn')} {sc('Send a valid name (max 40 characters).')}", ttl=6)
    data = await state.get_data()
    e = get_entry(data.get("eid"))
    g = get_giveaway(e["giveaway_id"]) if e else None
    await state.clear()
    await sweep(chat)
    if not e or not g:
        return await notice(chat, f"{E('err')} {sc('Entry not found.')}", ttl=8)
    run("UPDATE entries SET name=? WHERE id=?", (name, e["id"]))
    if e["message_id"]:   # keep the channel post in sync
        try:
            await bot.edit_message_text(text=entry_post_text(g, name), chat_id=CHANNEL_ID, message_id=e["message_id"],
                                        reply_markup=vote_kb(e["id"], e["votes"]))
        except Exception:
            logging.exception("could not edit the channel post")
    mark_dirty()
    e = get_entry(e["id"])
    await show_panel(chat, entry_admin_text(g, e), entry_admin_kb(g, e))


@dp.callback_query(F.data.startswith("aub:"))
async def user_ban_toggle(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    _, uid, eid = callback.data.split(":")
    uid, eid = int(uid), int(eid)
    u = get_user(uid)
    if not u:
        return await callback.answer("User not found.", show_alert=True)
    if uid in ADMINS:
        return await callback.answer("You can't ban an admin.", show_alert=True)
    now_banned = 0 if u["banned"] else 1
    run("UPDATE users SET banned=? WHERE user_id=?", (now_banned, uid))
    mark_dirty()
    await callback.answer("User banned." if now_banned else "User unbanned.")
    e = get_entry(eid) if eid else None
    g = get_giveaway(e["giveaway_id"]) if e else None
    if e and g:
        await safe_edit(callback, entry_admin_text(g, e), entry_admin_kb(g, e))
    else:
        await safe_edit(callback, user_admin_text(uid), user_admin_kb(uid))


@dp.callback_query(F.data.startswith("aed:"))
async def entry_delete_ask(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    e = get_entry(int(callback.data.split(":")[1]))
    if not e:
        return await callback.answer("Already deleted.", show_alert=True)
    await safe_edit(callback, f"{E('warn')} <b>{sc('Delete this entry?')}</b>\n\n<b>{esc(e['name'])}</b>\n"
                              f"{sc('Its votes and channel post are removed, and its voters can vote again.')}",
                    InlineKeyboardMarkup(inline_keyboard=[
                        [btn("Yes, Delete", "trash", style="danger", callback_data=f"aedy:{e['id']}"),
                         btn("Cancel", "err", callback_data=f"aei:{e['id']}")]]))
    await callback.answer()


@dp.callback_query(F.data.startswith("aedy:"))
async def entry_delete_do(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    e = get_entry(int(callback.data.split(":")[1]))
    if not e:
        return await callback.answer("Already deleted.", show_alert=True)
    if e["message_id"]:
        await _delete(CHANNEL_ID, e["message_id"])
    run("DELETE FROM votes WHERE entry_id=?", (e["id"],))
    run("DELETE FROM entries WHERE id=?", (e["id"],))
    run("UPDATE giveaways SET winner_entry_id=NULL WHERE winner_entry_id=?", (e["id"],))
    mark_dirty()
    g = get_giveaway(e["giveaway_id"])
    await callback.answer("Entry deleted.")
    if g:
        await render_entries(callback, g)


# ---- delete giveaway -> saved in History --------------------------------------------
@dp.callback_query(F.data.startswith("agdel:"))
async def gw_delete_ask(callback: CallbackQuery):
    g = await _gw_from(callback)
    if not g:
        return
    await safe_edit(callback, f"{E('warn')} <b>{sc('Delete this giveaway?')}</b>\n\n{esc(g['name'])}\n"
                              f"{sc('Entries, votes and channel posts are removed and the link stops working.')}\n"
                              f"{E('history')} {sc('A full copy (names, usernames, votes, winner) is saved in History.')}",
                    InlineKeyboardMarkup(inline_keyboard=[
                        [btn("Yes, Delete", "trash", style="danger", callback_data=f"agdely:{g['id']}"),
                         btn("Cancel", "err", callback_data=f"agw:{g['id']}")]]))
    await callback.answer()


def archive_giveaway(g):
    """Copy the giveaway's final standings (with Telegram username/name of each participant) into History."""
    gid = g["id"]
    entries = q_all("SELECT e.*, u.username, u.first_name FROM entries e LEFT JOIN users u ON u.user_id=e.user_id "
                    "WHERE e.giveaway_id=? ORDER BY e.votes DESC, e.id ASC", (gid,))
    voters = count("SELECT COUNT(*) FROM votes WHERE giveaway_id=?", (gid,))
    with db() as con:
        con.execute("DELETE FROM history_entries WHERE giveaway_id=?", (gid,))
        for i, e in enumerate(entries, 1):
            con.execute("INSERT INTO history_entries(giveaway_id, entry_id, user_id, name, username, first_name, "
                        "votes, rank, is_winner, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (gid, e["id"], e["user_id"], e["name"], e["username"], e["first_name"], e["votes"], i,
                         1 if e["id"] == g["winner_entry_id"] else 0, e["created_at"]))
        con.execute("UPDATE giveaways SET deleted_at=?, final_voters=? WHERE id=?", (now_iso(), voters, gid))


@dp.callback_query(F.data.startswith("agdely:"))
async def gw_delete_do(callback: CallbackQuery):
    g = await _gw_from(callback)
    if not g:
        return
    await callback.answer("Deleting...")
    archive_giveaway(g)                      # save to History first
    for e in q_all("SELECT message_id FROM entries WHERE giveaway_id=? AND message_id IS NOT NULL", (g["id"],)):
        await _delete(CHANNEL_ID, e["message_id"])
    run("DELETE FROM votes WHERE giveaway_id=?", (g["id"],))
    run("DELETE FROM entries WHERE giveaway_id=?", (g["id"],))
    run("UPDATE giveaways SET status='deleted', winner_entry_id=NULL WHERE id=?", (g["id"],))
    mark_dirty()
    await log_channel(f"{E('history')} <b>{sc('Giveaway moved to history')}</b> #{g['id']} ➛ {esc(g['name'])}")
    await safe_edit(callback, f"{E('ok')} {sc('Giveaway deleted and saved to History.')}",
                    admin_back([btn("Open In History", "history", style="primary", callback_data=f"ahv:{g['id']}")]))


def hist_totals(gid):
    r = q_one("SELECT COUNT(*), COALESCE(SUM(votes),0) FROM history_entries WHERE giveaway_id=?", (gid,))
    return r[0], r[1]


@dp.callback_query(F.data.startswith("ahist"))
async def hist_list(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    parts = callback.data.split(":")
    page = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    total = count("SELECT COUNT(*) FROM giveaways WHERE status='deleted'")
    pages = max(1, -(-total // PER_PAGE))
    page = max(0, min(page, pages - 1))
    rows = q_all("SELECT * FROM giveaways WHERE status='deleted' ORDER BY COALESCE(deleted_at, created_at) DESC, id DESC "
                 "LIMIT ? OFFSET ?", (PER_PAGE, page * PER_PAGE))
    lines = [bq(f"{E('history')} {sc('Giveaway history')} ({total})"), ""]
    kb = []
    if rows:
        for g in rows:
            n, v = hist_totals(g["id"])
            when = (g["deleted_at"] or "")[:10] or "-"
            lines.append(f"{E('dot')} <b>#{g['id']} {esc(g['name'])}</b> ➛ {n} {sc('entries')} · {v} {sc('votes')} · {when}")
            kb.append([btn(f"#{g['id']} {g['name'][:34]}", "history", callback_data=f"ahv:{g['id']}")])
        lines += [SEP, f"{E('bolt')} {sc('Tap a giveaway to see the final standings and participants.')}"]
    else:
        lines.append(sc("Nothing here yet. Deleted giveaways are saved in this list."))
    nav = []
    if page > 0:
        nav.append(btn("Prev", "reply", callback_data=f"ahist:{page - 1}"))
    if page < pages - 1:
        nav.append(btn("Next", "bolt", callback_data=f"ahist:{page + 1}"))
    if nav:
        kb.append(nav)
    await safe_edit(callback, "\n".join(lines), admin_back(*kb))
    await callback.answer()


def history_text(g):
    rows = q_all("SELECT * FROM history_entries WHERE giveaway_id=? ORDER BY rank ASC LIMIT 15", (g["id"],))
    n, v = hist_totals(g["id"])
    lines = [bq(f"{E('history')} {sc('History')} #{g['id']}"),
             f"{E('title')} <b>{esc(g['name'])}</b>", "",
             row("star", "Prize", esc(g["prize"]) if g["prize"] else sc("None")),
             row("calendar", "Created", esc((g["created_at"] or "")[:16])),
             row("trash", "Deleted", esc((g["deleted_at"] or "-")[:16])),
             row("group", "Entries", n),
             row("vote", "Total Votes", v),
             row("user", "Voters", g["final_voters"] or 0)]
    win = q_one("SELECT * FROM history_entries WHERE giveaway_id=? AND is_winner=1", (g["id"],))
    if win:
        handle = f" @{win['username']}" if win["username"] else ""
        lines.append(row("crown", "Winner", f"<b>{esc(win['name'])}</b>{esc(handle)} ({win['votes']} {sc('votes')})"))
    lines.append(SEP)
    if not rows:
        lines.append(sc("No entries were saved for this giveaway."))
    for r in rows:
        handle = f"@{r['username']}" if r["username"] else "-"
        lines.append(f"{MEDALS.get(r['rank'], str(r['rank']) + '.')} <b>{esc(r['name'])}</b> · {esc(handle)} · "
                     f"<code>{r['user_id']}</code> ➛ {r['votes']} {sc('votes')}")
    if n > 15:
        lines.append(sc(f"…and {n - 15} more. Export the CSV for the full list."))
    return "\n".join(lines)


@dp.callback_query(F.data.startswith("ahv:"))
async def hist_view(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    g = q_one("SELECT * FROM giveaways WHERE id=? AND status='deleted'", (int(callback.data.split(":")[1]),))
    if not g:
        return await callback.answer("Not found in history.", show_alert=True)
    await safe_edit(callback, history_text(g), admin_back(
        [btn("Export CSV", "csv", style="primary", callback_data=f"ahc:{g['id']}"),
         btn("Erase Forever", "trash", style="danger", callback_data=f"ahx:{g['id']}")],
        [btn("History", "history", callback_data="ahist")]))
    await callback.answer()


def make_history_csv(gid):
    rows = q_all("SELECT * FROM history_entries WHERE giveaway_id=? ORDER BY rank", (gid,))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["rank", "name", "votes", "winner", "user_id", "username", "first_name", "registered_at"])
    for r in rows:
        w.writerow([r["rank"], _csv_safe(r["name"]), r["votes"], "yes" if r["is_winner"] else "",
                    r["user_id"], _csv_safe(r["username"]), _csv_safe(r["first_name"]), r["created_at"]])
    return BufferedInputFile(buf.getvalue().encode("utf-8-sig"), filename=f"history_{gid}.csv")


@dp.callback_query(F.data.startswith("ahc:"))
async def hist_csv(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    gid = int(callback.data.split(":")[1])
    if not q_one("SELECT 1 FROM giveaways WHERE id=? AND status='deleted'", (gid,)):
        return await callback.answer("Not found in history.", show_alert=True)
    await callback.answer()
    await bot.send_document(callback.message.chat.id, make_history_csv(gid),
                            caption=f"{E('csv')} {sc('History export')} #{gid}")


@dp.callback_query(F.data.startswith("ahx:"))
async def hist_erase_ask(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    gid = int(callback.data.split(":")[1])
    await safe_edit(callback, f"{E('warn')} <b>{sc('Erase this history entry forever?')}</b>\n\n"
                              f"{sc('This cannot be undone (only an old backup can bring it back).')}",
                    InlineKeyboardMarkup(inline_keyboard=[
                        [btn("Yes, Erase", "trash", style="danger", callback_data=f"ahxy:{gid}"),
                         btn("Cancel", "err", callback_data=f"ahv:{gid}")]]))
    await callback.answer()


@dp.callback_query(F.data.startswith("ahxy:"))
async def hist_erase_do(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    gid = int(callback.data.split(":")[1])
    run("DELETE FROM history_entries WHERE giveaway_id=?", (gid,))
    run("DELETE FROM giveaways WHERE id=? AND status='deleted'", (gid,))
    mark_dirty()
    await callback.answer("Erased from history.")
    await safe_edit(callback, f"{E('ok')} {sc('Erased from history.')}",
                    admin_back([btn("History", "history", callback_data="ahist")]))


# ---- stats / users / csv -----------------------------------------------------------
@dp.callback_query(F.data == "astats")
async def admin_stats(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    lines = [bq(f"{E('stats')} {sc('Statistics')}"), "",
             row("user", "Users", count("SELECT COUNT(*) FROM users")),
             row("ban", "Banned", count("SELECT COUNT(*) FROM users WHERE banned=1")),
             row("gift", "Giveaways", count("SELECT COUNT(*) FROM giveaways WHERE status!='deleted'")),
             row("group", "Entries", count("SELECT COUNT(*) FROM entries")),
             row("vote", "Votes", count("SELECT COUNT(*) FROM votes")), SEP]
    for g in q_all("SELECT * FROM giveaways WHERE status!='deleted' ORDER BY id DESC LIMIT 5"):
        n, v = gw_counts(g["id"])
        top = q_one("SELECT name, votes FROM entries WHERE giveaway_id=? ORDER BY votes DESC, id LIMIT 1", (g["id"],))
        lines.append(f"{E('dot')} <b>{esc(g['name'])}</b> ➛ {n} {sc('entries')} · {v} {sc('votes')}"
                     + (f" · 🥇 {esc(top['name'])} ({top['votes']})" if top else ""))
    await safe_edit(callback, "\n".join(lines), admin_back())
    await callback.answer()


def user_admin_text(uid):
    entries = q_all("SELECT e.*, g.name AS gname FROM entries e JOIN giveaways g ON g.id=e.giveaway_id "
                    "WHERE e.user_id=? AND g.status!='deleted' ORDER BY e.id DESC LIMIT 8", (uid,))
    lines = [bq(f"{E('user')} {sc('User')}"), "", *user_info_lines(uid),
             row("vote", "Votes Cast", count("SELECT COUNT(*) FROM votes WHERE voter_id=?", (uid,))), SEP]
    if entries:
        for r in entries:
            lines.append(f"{E('dot')} <b>{esc(r['gname'])}</b> ➛ {esc(r['name'])} · "
                         f"{r['votes']} {sc('votes')} · #{rank_of(r)}")
    else:
        lines.append(sc("No entries in active giveaways."))
    return "\n".join(lines)


def user_admin_kb(uid):
    u = get_user(uid)
    banned = bool(u and u["banned"])
    entries = q_all("SELECT e.*, g.name AS gname FROM entries e JOIN giveaways g ON g.id=e.giveaway_id "
                    "WHERE e.user_id=? AND g.status!='deleted' ORDER BY e.id DESC LIMIT 8", (uid,))
    rows = [[btn(f"{r['gname'][:18]}: {r['name'][:20]}", "gift", callback_data=f"aei:{r['id']}")] for r in entries]
    rows.append([btn("Message User", "mail", style="primary", callback_data=f"sreply:{uid}"),
                 btn("Unban User" if banned else "Ban User", "ban", style="success" if banned else "danger",
                     callback_data=f"aub:{uid}:0")])
    if u and u["username"]:
        rows.append([btn("Open Chat", "user", url=f"https://t.me/{u['username']}")])
    rows.append([btn("Users", "user", callback_data="ausers")])
    return admin_back(*rows)


def search_users(term):
    """Find users by @username, numeric id, Telegram name, or the name they entered in a giveaway."""
    t = term.strip().lstrip("@")
    uid = int(t) if t.isdigit() else -1
    pat = like(t.lower())
    return q_all("SELECT * FROM users WHERE user_id=? OR lower(username)=? OR lower(first_name) LIKE ? ESCAPE '\\' "
                 "OR user_id IN (SELECT user_id FROM entries WHERE lower(name) LIKE ? ESCAPE '\\') "
                 "ORDER BY last_seen DESC LIMIT 8", (uid, t.lower(), pat, pat))


async def show_user_results(chat_id, term):
    rows = search_users(term)
    if not rows:
        return await notice(chat_id, f"{E('warn')} {sc('No user found. They must have started the bot.')}", ttl=8)
    if len(rows) == 1:
        uid = rows[0]["user_id"]
        return await show_panel(chat_id, user_admin_text(uid), user_admin_kb(uid))
    lines = [bq(f"{E('user')} {sc('Search results')}"), ""]
    kb = []
    for u in rows:
        handle = f"@{u['username']}" if u["username"] else "-"
        lines.append(f"{E('dot')} {esc(u['first_name'])} {esc(handle)} <code>{u['user_id']}</code>")
        kb.append([btn(f"{(u['first_name'] or '')[:20]} {handle if u['username'] else ''}".strip(), "user",
                       callback_data=f"auv:{u['user_id']}")])
    await show_panel(chat_id, "\n".join(lines), admin_back(*kb))


@dp.callback_query(F.data.startswith("ausers"))
async def admin_users(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return
    await state.clear()
    parts = callback.data.split(":")
    page = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    total = count("SELECT COUNT(*) FROM users")
    pages = max(1, -(-total // PER_PAGE))
    page = max(0, min(page, pages - 1))
    rows = q_all("SELECT * FROM users ORDER BY joined_at DESC, user_id DESC LIMIT ? OFFSET ?",
                 (PER_PAGE, page * PER_PAGE))
    lines = [bq(f"{E('user')} {sc('Users')} ({total})"), ""]
    kb = [[btn("Search User", "web", style="primary", callback_data="ausearch")]]
    for u in rows:
        handle = f"@{u['username']}" if u["username"] else "-"
        ban = " 🚫" if u["banned"] else ""
        lines.append(f"{E('dot')} {esc(u['first_name'])} {esc(handle)} <code>{u['user_id']}</code>{ban}")
        kb.append([btn(f"{(u['first_name'] or '')[:20]} {handle if u['username'] else ''}".strip() + (" 🚫" if u["banned"] else ""),
                       "user", callback_data=f"auv:{u['user_id']}")])
    lines += [SEP, sc("Tap a user to see their info, entries and manage them.")]
    nav = []
    if page > 0:
        nav.append(btn("Prev", "reply", callback_data=f"ausers:{page - 1}"))
    if page < pages - 1:
        nav.append(btn("Next", "bolt", callback_data=f"ausers:{page + 1}"))
    if nav:
        kb.append(nav)
    await safe_edit(callback, "\n".join(lines), admin_back(*kb))
    await callback.answer()


@dp.callback_query(F.data.startswith("auv:"))
async def admin_user_detail(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    uid = int(callback.data.split(":")[1])
    if not get_user(uid):
        return await callback.answer("User not found.", show_alert=True)
    await safe_edit(callback, user_admin_text(uid), user_admin_kb(uid))
    await callback.answer()


@dp.callback_query(F.data == "ausearch")
async def admin_user_search_start(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return
    await state.clear()
    await state.set_state(AdminFlow.user_search)
    await ask(callback.message.chat.id, f"{E('web')} {sc('Send an @username, a user ID, a Telegram name, or the name they entered.')}\n"
                                        f"{sc('Send /cancel to stop.')}")
    await callback.answer()


@dp.message(AdminFlow.user_search)
async def admin_user_search(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await tidy(message)
    term = (message.text or "").strip()
    if not term:
        return await notice(message.chat.id, f"{E('warn')} {sc('Send some text to search for.')}", ttl=6)
    await state.clear()
    await sweep(message.chat.id)
    await show_user_results(message.chat.id, term)


def _csv_safe(v):
    s = str(v if v is not None else "")
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


def make_csv():
    rows = q_all("SELECT g.name AS giveaway, e.id, e.name, e.votes, e.user_id, u.username, e.created_at "
                 "FROM entries e JOIN giveaways g ON g.id=e.giveaway_id LEFT JOIN users u ON u.user_id=e.user_id "
                 "WHERE g.status!='deleted' ORDER BY g.id, e.votes DESC, e.id")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["giveaway", "entry_id", "name", "votes", "user_id", "username", "created_at"])
    for r in rows:
        w.writerow([_csv_safe(r["giveaway"]), r["id"], _csv_safe(r["name"]), r["votes"],
                    r["user_id"], _csv_safe(r["username"]), r["created_at"]])
    return BufferedInputFile(buf.getvalue().encode("utf-8-sig"), filename="entries.csv")


@dp.callback_query(F.data == "acsv")
async def admin_csv(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    await callback.answer()
    await bot.send_document(callback.message.chat.id, make_csv(), caption=f"{E('csv')} {sc('Entries export')}")


# ---- updates / broadcast -----------------------------------------------------------
@dp.callback_query(F.data == "aupd")
async def admin_update_start(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return
    await state.clear()
    await state.set_state(AdminFlow.update)
    await ask(callback.message.chat.id, f"{E('bell')} {sc('Send the update text.')}\n{sc('Send /cancel to stop.')}")
    await callback.answer()


@dp.message(AdminFlow.update)
async def admin_update_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await tidy(message)
    text = (message.text or "").strip()
    if not text:
        return await notice(message.chat.id, f"{E('warn')} {sc('Send text.')}", ttl=5)
    run("INSERT INTO updates(body, created_at) VALUES(?,?)", (text[:500], now_iso()))
    mark_dirty()
    await state.clear()
    await sweep(message.chat.id)
    await notice(message.chat.id, f"{E('ok')} {sc('Update posted.')}", ttl=6)


@dp.callback_query(F.data == "abc")
async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return
    await state.clear()
    await state.set_state(AdminFlow.broadcast)
    await ask(callback.message.chat.id, f"{E('channel')} {sc('Send the message to broadcast to every user (text, photo ...).')}\n"
                                        f"{sc('Send /cancel to stop.')}")
    await callback.answer()


async def do_broadcast(admin_chat, src_chat, src_msg):
    ok = fail = 0
    for u in q_all("SELECT user_id FROM users WHERE banned=0"):
        try:
            await bot.copy_message(u["user_id"], src_chat, src_msg)
            ok += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await bot.copy_message(u["user_id"], src_chat, src_msg)
                ok += 1
            except Exception:
                fail += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)
    await notice(admin_chat, f"{E('ok')} {sc('Broadcast done')}: {ok} {sc('sent')} · {fail} {sc('failed')}", ttl=20)


@dp.message(AdminFlow.broadcast)
async def admin_broadcast_send(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    await sweep(message.chat.id)
    await notice(message.chat.id, f"{E('wait')} {sc('Broadcasting...')}", ttl=5)
    spawn(do_broadcast(message.chat.id, message.chat.id, message.message_id))


# ---- backup / restore --------------------------------------------------------------
@dp.callback_query(F.data == "abackup")
async def admin_backup(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    await callback.answer("Backing up...")
    await backup_now("manual")
    await notice(callback.message.chat.id, f"{E('ok')} {sc('Backup saved to the storage channel and pinned.')}", ttl=8)


@dp.callback_query(F.data == "arestore")
async def admin_restore_menu(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return
    await state.clear()
    await safe_edit(callback,
                    "\n".join([bq(f"{E('restore')} {sc('Restore backup')}"), "",
                               sc("Current data is saved locally before it is replaced."), "",
                               f"{E('dot')} {sc('Latest = the pinned backup in the storage channel.')}",
                               f"{E('dot')} {sc('Or upload any .db backup file.')}"]),
                    admin_back([btn("Restore Latest (Pinned)", "restore", style="danger", callback_data="arestorel")],
                               [btn("Upload A .db File", "csv", callback_data="arestoref")]))
    await callback.answer()


@dp.callback_query(F.data == "arestorel")
async def admin_restore_latest(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    await callback.answer("Downloading...")
    chat = callback.message.chat.id
    try:
        path = await download_latest_backup()
    except Exception:
        logging.exception("could not read the storage channel")
        return await notice(chat, f"{E('err')} {sc('Could not read the storage channel.')}", ttl=8)
    if not path:
        return await notice(chat, f"{E('warn')} {sc('No pinned backup found in the storage channel.')}", ttl=8)
    try:
        ok = await apply_restore(path)
    finally:
        shutil.rmtree(path.parent, ignore_errors=True)
    await notice(chat, f"{E('ok')} {sc('Database restored successfully.')}" if ok
                 else f"{E('err')} {sc('That backup is not valid.')}", ttl=10)


@dp.callback_query(F.data == "arestoref")
async def admin_restore_file_prompt(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return
    await state.clear()
    await state.set_state(AdminFlow.restore)
    await ask(callback.message.chat.id, f"{E('restore')} {sc('Send the .db backup file to restore.')}\n"
                                        f"{sc('Send /cancel to stop.')}")
    await callback.answer()


@dp.message(AdminFlow.restore)
async def admin_restore_file(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    chat = message.chat.id
    if not message.document:
        await tidy(message)
        return await notice(chat, f"{E('warn')} {sc('Send the .db file as a document.')}", ttl=6)
    tmp = Path(tempfile.mkdtemp()) / "upload.db"
    try:
        await bot.download(message.document, destination=tmp)
        ok = await apply_restore(tmp)
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)
    await state.clear()
    await sweep(chat)
    await tidy(message)
    if ok:
        await log_channel(f"{E('restore')} Database restored by {message.from_user.id}")
    await notice(chat, f"{E('ok')} {sc('Database restored successfully.')}" if ok
                 else f"{E('err')} {sc('That is not a valid backup file.')}", ttl=10)


# ----------------------------------------------------------------------------
# Premium emoji: real start-up test + helper to read emoji IDs
# ----------------------------------------------------------------------------
async def setup_emoji():
    """Checks every custom emoji ID with Telegram and tests whether this bot may show premium
    emoji in message text and on buttons. Anything that does not work is switched off cleanly."""
    global MSG_EMOJI_OK, BTN_ICON_OK
    notes = []
    if not EMOJI_IDS:
        MSG_EMOJI_OK = BTN_ICON_OK = False
        return "Premium emoji: disabled (USE_CUSTOM_EMOJI=0)"
    ids = sorted(set(EMOJI_IDS.values()))
    try:
        valid = set()
        for i in range(0, len(ids), 200):
            for s in await bot.get_custom_emoji_stickers(ids[i:i + 200]):
                valid.add(s.custom_emoji_id)
        for name in [n for n, c in EMOJI_IDS.items() if c not in valid]:
            EMOJI_IDS.pop(name)
            notes.append(f"invalid ID removed: {name}")
    except Exception as e:
        notes.append(f"ID check failed: {e}")
    if not EMOJI_IDS:
        MSG_EMOJI_OK = BTN_ICON_OK = False
        return "Premium emoji: no valid IDs left\n" + "\n".join(notes)
    name, cid = next(iter(EMOJI_IDS.items()))
    MSG_EMOJI_OK = BTN_ICON_OK = False
    try:
        m = await bot.send_message(OWNER_ID, f'<tg-emoji emoji-id="{cid}">{EMOJI.get(name, "•")}</tg-emoji>')
        MSG_EMOJI_OK = any(e.type == "custom_emoji" for e in (m.entities or []))
        await _delete(OWNER_ID, m.message_id)
    except Exception as e:
        notes.append(f"message test failed: {e}")
    if HAS_ICON:
        try:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="test", icon_custom_emoji_id=cid)]])
            m = await bot.send_message(OWNER_ID, "test", reply_markup=kb)
            BTN_ICON_OK = True
            await _delete(OWNER_ID, m.message_id)
        except Exception as e:
            notes.append(f"button test failed: {e}")
    else:
        notes.append("buttons: upgrade aiogram to >= 3.25")
    report = [f"Premium emoji in messages: {'ON' if MSG_EMOJI_OK else 'OFF'}",
              f"Premium emoji on buttons: {'ON' if BTN_ICON_OK else 'OFF'}"] + notes
    if not MSG_EMOJI_OK:
        report.append("Premium emoji in message text needs a bot with a Fragment username; normal emoji are used instead.")
    if not BTN_ICON_OK:
        report.append("Button icons need the bot owner to have Telegram Premium.")
    return "\n".join(report)


def _emoji_ids_in(message: Message):
    ents = (message.entities or []) + (message.caption_entities or [])
    return [e.custom_emoji_id for e in ents if e.type == "custom_emoji"]


def _is_emoji_probe(message: Message):
    return bool(message.from_user and is_admin(message.from_user.id)
                and (message.sticker is not None or _emoji_ids_in(message)))


@dp.message(StateFilter(None), F.func(_is_emoji_probe))
async def emoji_id_helper(message: Message):
    """Admin: send any premium emoji or sticker to the bot and get its ID back."""
    lines = [bq(f"{E('info')} {sc('Emoji / sticker IDs')}"), ""]
    for cid in dict.fromkeys(_emoji_ids_in(message)):
        lines.append(f"{E('dot')} {sc('Emoji ID')}: <code>{cid}</code>")
    if message.sticker and message.sticker.custom_emoji_id:
        lines.append(f"{E('dot')} {sc('Emoji ID')}: <code>{message.sticker.custom_emoji_id}</code>")
    lines += [SEP, sc("Paste the Emoji ID into EMOJI_IDS in the code.")]
    await message.reply("\n".join(lines))


@dp.message(StateFilter(None), F.chat.type == "private")
async def fallback(message: Message):
    """Any other message: keep the chat clean and show the main menu."""
    await tidy(message)
    await go_home(message.chat.id, message.from_user)


# ----------------------------------------------------------------------------
# Run
# ----------------------------------------------------------------------------
async def main():
    global BOT_USERNAME
    keep_alive()
    if not DB_PATH.exists():
        await auto_restore()
    init_db()
    BOT_USERNAME = (await bot.get_me()).username
    spawn(backup_loop())
    await bot.delete_webhook(drop_pending_updates=True)
    report = await setup_emoji()
    print(report)
    await notice(OWNER_ID, f"{E('info')} <code>{esc(report)}</code>", ttl=60)
    logging.info("Bot running as @%s", BOT_USERNAME)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
