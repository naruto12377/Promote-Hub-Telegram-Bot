#!/usr/bin/env python3
"""
PromoteHub — Telegram Promotion Marketplace Bot v2
==================================================
Fixes & Features:
  • Webhook (aiohttp) — Render free tier, auto-restarts on request
  • Robust DB persistence — pinned message with recreate-on-fail fallback
  • Session keys: (uid, chat_id) — no cross-user collision in groups
  • Group posting: /post <text>  OR  /post → reply-to-bot-message flow
  • POST_ALLOWED_IN enforced everywhere (dm | group | both)
  • 13 post categories incl. Gaming, Crypto, Business, Anime, Study, Earning
  • Hashtag support — up to 4 per post, validated
  • "👤 Posted by: @username" attribution in every post
  • Session timeout (default 5 min) — stale sessions auto-expire
  • Global error handler — nothing crashes the bot
"""

import asyncio
import html
import json
import logging
import os
import re
import time
from collections import defaultdict
from typing import Optional

from aiohttp import web
from telegram import (
    BotCommand,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("PromoteHub")

# ══════════════════════════════════════════════════════════════════════════════
# CONFIG  — 100% environment variables
# ══════════════════════════════════════════════════════════════════════════════

def _req(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        raise RuntimeError(f"Missing required env var: {key}")
    return val


def _intlist(key: str) -> list:
    return [
        int(x.strip())
        for x in os.getenv(key, "").split(",")
        if x.strip().lstrip("-").isdigit()
    ]


def _strlist(key: str, default: str) -> list:
    return [
        x.strip().lower()
        for x in os.getenv(key, default).split(",")
        if x.strip()
    ]


BOT_TOKEN          = _req("BOT_TOKEN")
BOT_USERNAME       = os.getenv("BOT_USERNAME", "@PromoteHubBot").strip()
PROMO_CHANNEL_ID   = int(_req("PROMOTION_CHANNEL_ID"))
PROMO_CHANNEL_LINK = os.getenv("PROMOTION_CHANNEL_LINK", "").strip()
GROUP_ID           = int(_req("GROUP_ID"))
GROUP_LINK         = os.getenv("GROUP_LINK", "").strip()
DB_CHANNEL_ID      = int(_req("DATABASE_CHANNEL_ID"))
ADMIN_IDS          = _intlist("ADMIN_IDS")
PORT               = int(os.getenv("PORT", "10000"))   # Render default is 10000

# Webhook URL for Render — e.g. https://myapp.onrender.com
# Leave blank to fall back to long-polling (local dev)
WEBHOOK_URL        = os.getenv("WEBHOOK_URL", "").strip()

POSTS_PER_HOUR     = max(1, int(os.getenv("POSTS_PER_HOUR", "2")))
MAX_WARNINGS       = max(1, int(os.getenv("MAX_WARNINGS", "3")))
SESSION_TIMEOUT    = int(os.getenv("SESSION_TIMEOUT_SEC", "300"))  # 5 min

BAD_WORDS = _strlist(
    "BAD_WORDS",
    "spam,scam,fake,adult,xxx,porn,crypto scam,ponzi,betting,gambling,hack,phishing",
)
BAD_LINKS = _strlist("BAD_LINKS", "onlyfans.com,bit.ly/adult")

# POST_ALLOWED_IN: "dm" | "group" | "both"
_pai = os.getenv("POST_ALLOWED_IN", "dm").strip().lower()
POST_ALLOWED_IN = _pai if _pai in ("dm", "group", "both") else "dm"

# ══════════════════════════════════════════════════════════════════════════════
# IN-MEMORY STATE
# ══════════════════════════════════════════════════════════════════════════════

db: dict = {
    "post_count": 0,
    # Category counters
    "channels":  0,
    "groups":    0,
    "gaming":    0,
    "crypto":    0,
    "business":  0,
    "chatting":  0,
    "anime":     0,
    "study":     0,
    "earning":   0,
    "services":  0,
    "referrals": 0,
    "links":     0,
    "others":    0,
    # User management
    "banned":    [],   # list[int]
    "warnings":  {},   # {str(user_id): int}
}
_db_msg_id: Optional[int] = None

# Per-user post timestamps for rate-limiting (resets on restart — acceptable)
_rate: dict = defaultdict(list)

# Active sessions: {(uid, chat_id): dict}
# Fields: step, content, ptype, chat_id, ts, prompt_msg_id (group only)
_sessions: dict = {}

# Global Application reference (set in main)
_app: Optional[Application] = None

# ══════════════════════════════════════════════════════════════════════════════
# DB PERSISTENCE  — Telegram channel as key-value store
# ══════════════════════════════════════════════════════════════════════════════

async def db_save(app: Application) -> None:
    """Persist state to the DB channel. Edit existing message or create new one."""
    global _db_msg_id
    payload = "#PH_DB\n" + json.dumps(db, ensure_ascii=False, separators=(",", ":"))

    try:
        if _db_msg_id:
            try:
                await app.bot.edit_message_text(
                    chat_id=DB_CHANNEL_ID,
                    message_id=_db_msg_id,
                    text=payload,
                )
                return
            except TelegramError as e:
                log.warning(f"db_save edit failed — will recreate: {e}")
                _db_msg_id = None

        # Create a new DB message and pin it
        msg = await app.bot.send_message(chat_id=DB_CHANNEL_ID, text=payload)
        _db_msg_id = msg.message_id
        try:
            await app.bot.pin_chat_message(
                chat_id=DB_CHANNEL_ID,
                message_id=_db_msg_id,
                disable_notification=True,
            )
        except TelegramError as pin_err:
            log.warning(
                f"db_save: could not pin message — check bot has 'Pin Messages' "
                f"permission in DB channel. Error: {pin_err}"
            )
    except TelegramError as e:
        log.error(f"db_save FAILED completely: {e}")


async def db_load(app: Application) -> None:
    """Restore state from the pinned DB channel message on startup."""
    global db, _db_msg_id

    try:
        chat = await app.bot.get_chat(DB_CHANNEL_ID)
        pinned = getattr(chat, "pinned_message", None)

        if pinned and pinned.text and pinned.text.startswith("#PH_DB"):
            raw = pinned.text.split("\n", 1)[1]
            loaded: dict = json.loads(raw)
            # Merge — preserve any new keys added in this version
            for k, v in loaded.items():
                db[k] = v
            _db_msg_id = pinned.message_id
            log.info(
                f"✅ DB restored — posts={db['post_count']}  "
                f"banned={len(db['banned'])}  msg_id={_db_msg_id}"
            )
            return

        if pinned:
            log.info(
                "Pinned message found but not a #PH_DB record — "
                "starting fresh. Make sure only this bot pins in the DB channel."
            )
        else:
            log.info("No pinned message in DB channel — starting fresh.")

    except Exception as e:
        log.error(
            f"db_load failed: {e}\n"
            f"  ► Ensure bot is ADMIN in DB channel with 'Post' & 'Pin Messages' rights."
        )


async def db_log(app: Application, num: int, uid: int, ptype: str, username: str) -> None:
    """Lightweight audit record in DB channel."""
    try:
        await app.bot.send_message(
            chat_id=DB_CHANNEL_ID,
            text=(
                f"#POST id={num} user={uid} "
                f"uname={username or 'N/A'} "
                f"type={ptype.lower()} ts={int(time.time())}"
            ),
        )
    except TelegramError:
        pass

# ══════════════════════════════════════════════════════════════════════════════
# POST TYPES & CATEGORIES
# ══════════════════════════════════════════════════════════════════════════════

TYPE_EMOJI: dict = {
    "Channel":  "📢",
    "Group":    "👥",
    "Gaming":   "🎮",
    "Crypto":   "💰",
    "Business": "💼",
    "Chatting": "💬",
    "Anime":    "🎌",
    "Study":    "📚",
    "Earning":  "💵",
    "Service":  "🛠️",
    "Referral": "🔗",
    "Link":     "🌐",
    "Other":    "📋",
}

STAT_KEY: dict = {
    "channel":  "channels",
    "group":    "groups",
    "gaming":   "gaming",
    "crypto":   "crypto",
    "business": "business",
    "chatting": "chatting",
    "anime":    "anime",
    "study":    "study",
    "earning":  "earning",
    "service":  "services",
    "referral": "referrals",
    "link":     "links",
    "other":    "others",
}

# ══════════════════════════════════════════════════════════════════════════════
# SMALL UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

def h(text) -> str:
    """Escape user-supplied text for HTML parse_mode."""
    return html.escape(str(text))


def is_banned(uid: int) -> bool:
    return uid in db["banned"]


def get_warns(uid: int) -> int:
    return db["warnings"].get(str(uid), 0)


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def has_bad_content(text: str) -> bool:
    lower = text.lower()
    return any(w in lower for w in BAD_WORDS) or any(l in lower for l in BAD_LINKS)


def count_hashtags(text: str) -> int:
    return len(re.findall(r"#\w+", text))


def rate_ok(uid: int):
    """Returns (allowed: bool, seconds_to_wait: float)."""
    now = time.time()
    clean = [t for t in _rate[uid] if now - t < 3600]
    _rate[uid] = clean
    if len(clean) >= POSTS_PER_HOUR:
        return False, 3600.0 - (now - clean[0])
    return True, 0.0


def fmt_wait(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    return f"{m}m {s}s" if m else f"{s}s"


def post_allowed_here(chat_type) -> bool:
    if POST_ALLOWED_IN == "both":
        return True
    if POST_ALLOWED_IN == "dm":
        return chat_type == ChatType.PRIVATE
    if POST_ALLOWED_IN == "group":
        return chat_type in (ChatType.GROUP, ChatType.SUPERGROUP)
    return False


def session_key(uid: int, chat_id: int) -> tuple:
    return (uid, chat_id)


def session_expired(s: dict) -> bool:
    return time.time() - s.get("ts", 0) > SESSION_TIMEOUT


def clean_expired_sessions() -> None:
    expired = [k for k, v in _sessions.items() if session_expired(v)]
    for k in expired:
        del _sessions[k]


def extract_tme_username(text: str) -> Optional[str]:
    m = re.search(r"(?:t\.me|telegram\.me)/([A-Za-z0-9_]{4,})", text)
    if not m:
        return None
    username = m.group(1)
    if username.lower() in ("joinchat", "share", "addstickers", "boost", "s"):
        return None
    return username

# ══════════════════════════════════════════════════════════════════════════════
# AUTO-DETECT POST TYPE
# ══════════════════════════════════════════════════════════════════════════════

async def detect_type(text: str, bot) -> Optional[str]:
    """Returns a type string if auto-detected, else None (user must choose)."""
    # Invite links
    if re.search(r"t\.me/\+|t\.me/joinchat", text, re.IGNORECASE):
        return "Group"

    username = extract_tme_username(text)
    if username:
        try:
            chat = await bot.get_chat(f"@{username}")
            if chat.type == ChatType.CHANNEL:
                return "Channel"
            if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
                return "Group"
        except TelegramError:
            pass
        return "Link"

    if re.search(r"https?://\S+", text):
        return "Link"

    return None

# ══════════════════════════════════════════════════════════════════════════════
# KEYBOARDS
# ══════════════════════════════════════════════════════════════════════════════

def kb_join() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Join Channel", url=PROMO_CHANNEL_LINK)],
        [InlineKeyboardButton("💬 Join Group",   url=GROUP_LINK)],
        [InlineKeyboardButton("✅ I Joined — Verify", callback_data="check_join")],
    ])


def kb_home() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Submit Promotion — FREE", callback_data="start_post")],
        [
            InlineKeyboardButton("📊 Stats",  callback_data="stats"),
            InlineKeyboardButton("❓ Help",   callback_data="help"),
        ],
        [InlineKeyboardButton("📢 Browse Posts", url=PROMO_CHANNEL_LINK)],
    ])


def kb_type() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📢 Channel",  callback_data="type:Channel"),
            InlineKeyboardButton("👥 Group",    callback_data="type:Group"),
        ],
        [
            InlineKeyboardButton("🎮 Gaming",   callback_data="type:Gaming"),
            InlineKeyboardButton("💰 Crypto",   callback_data="type:Crypto"),
        ],
        [
            InlineKeyboardButton("💼 Business", callback_data="type:Business"),
            InlineKeyboardButton("💬 Chatting", callback_data="type:Chatting"),
        ],
        [
            InlineKeyboardButton("🎌 Anime",    callback_data="type:Anime"),
            InlineKeyboardButton("📚 Study",    callback_data="type:Study"),
        ],
        [
            InlineKeyboardButton("💵 Earning",  callback_data="type:Earning"),
            InlineKeyboardButton("🛠️ Service",  callback_data="type:Service"),
        ],
        [
            InlineKeyboardButton("🔗 Referral", callback_data="type:Referral"),
            InlineKeyboardButton("🌐 Link",     callback_data="type:Link"),
        ],
        [InlineKeyboardButton("📋 Other",       callback_data="type:Other")],
        [InlineKeyboardButton("❌ Cancel",       callback_data="cancel")],
    ])


def kb_confirm() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Publish Now", callback_data="publish"),
            InlineKeyboardButton("✏️ Edit",        callback_data="edit"),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ])


def kb_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="home")]])


def kb_after_post() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 View Post",    url=PROMO_CHANNEL_LINK)],
        [InlineKeyboardButton("📝 Post Another", callback_data="start_post")],
    ])


def kb_post_group_open_dm() -> InlineKeyboardMarkup:
    bot_name = BOT_USERNAME.lstrip("@")
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📩 Open DM to Post", url=f"https://t.me/{bot_name}?start=post")
    ]])

# ══════════════════════════════════════════════════════════════════════════════
# MESSAGE TEMPLATES  — all HTML, user content always escaped
# ══════════════════════════════════════════════════════════════════════════════

def tpl_home(first_name: str) -> str:
    return (
        f"👋 Hey <b>{h(first_name)}!</b> Welcome to <b>PromoteHub</b> 🚀\n\n"
        f"The free Telegram promotion marketplace.\n"
        f"📊 <b>{db['post_count']}</b> promotions published so far!\n\n"
        f"What would you like to do?"
    )


def tpl_stats() -> str:
    return (
        f"📊 <b>PromoteHub Stats</b>\n\n"
        f"📌 Total Posts : <b>{db['post_count']}</b>\n\n"
        f"📢 Channels   : <b>{db['channels']}</b>\n"
        f"👥 Groups     : <b>{db['groups']}</b>\n"
        f"🎮 Gaming     : <b>{db['gaming']}</b>\n"
        f"💰 Crypto     : <b>{db['crypto']}</b>\n"
        f"💼 Business   : <b>{db['business']}</b>\n"
        f"💬 Chatting   : <b>{db['chatting']}</b>\n"
        f"🎌 Anime      : <b>{db['anime']}</b>\n"
        f"📚 Study      : <b>{db['study']}</b>\n"
        f"💵 Earning    : <b>{db['earning']}</b>\n"
        f"🛠️ Services   : <b>{db['services']}</b>\n"
        f"🔗 Referrals  : <b>{db['referrals']}</b>\n"
        f"🌐 Links      : <b>{db['links']}</b>\n"
        f"📋 Others     : <b>{db['others']}</b>\n\n"
        f"🚀 Submit yours → {h(BOT_USERNAME)}"
    )


def tpl_help() -> str:
    where = {
        "dm":    "private chat with the bot",
        "group": f"our group ({GROUP_LINK})",
        "both":  "private chat or our group",
    }.get(POST_ALLOWED_IN, "private chat with the bot")
    return (
        f"❓ <b>PromoteHub Help</b>\n\n"
        f"<b>Commands</b>\n"
        f"/post — Submit a promotion\n"
        f"/stats — Live statistics\n"
        f"/help — This message\n"
        f"/cancel — Cancel current action\n\n"
        f"<b>What can I promote?</b>\n"
        f"✅ Telegram channels &amp; groups\n"
        f"✅ Services, bots, websites\n"
        f"✅ Referral &amp; affiliate links\n"
        f"✅ Gaming, Crypto, Business &amp; more!\n\n"
        f"<b>Hashtags</b>\n"
        f"💡 Add up to <b>4 hashtags</b> in your post for better discoverability.\n"
        f"Example: <code>#gaming #crypto #free #earn</code>\n\n"
        f"<b>Rules</b>\n"
        f"⏳ Max <b>{POSTS_PER_HOUR}</b> post(s) per hour\n"
        f"⚠️ <b>{MAX_WARNINGS}</b> violations = permanent ban\n"
        f"🚫 No spam / adult / scam content\n"
        f"📍 Posts must be submitted via {where}\n\n"
        f"📢 Browse posts → {PROMO_CHANNEL_LINK}"
    )


def tpl_submit_prompt() -> str:
    return (
        "📝 <b>Submit Your Promotion</b>\n\n"
        "Send your promotion message below.\n"
        "Include your description and link.\n\n"
        "💡 <b>Hashtag tip:</b> Add up to <b>4 hashtags</b> at the end for better "
        "search ranking &amp; visibility!\n"
        "<i>Example: #gaming #earn #free #telegram</i>\n\n"
        "<i>Use /cancel to abort.</i>"
    )


def tpl_post(
    num: int,
    ptype: str,
    content: str,
    username: Optional[str],
    user_id: int,
) -> str:
    """Plain-text post for the promotion channel. No parse_mode — 100% safe."""
    emoji  = TYPE_EMOJI.get(ptype, "📋")
    div    = "─" * 26
    poster = f"@{username}" if username else f"ID: {user_id}"
    return (
        f"📌 POST #{num:04d}  |  {emoji} {ptype}\n"
        f"{div}\n\n"
        f"{content}\n\n"
        f"{div}\n"
        f"👤 Posted by: {poster}\n"
        f"🚀 Promote FREE → {BOT_USERNAME}"
    )


def tpl_preview(ptype: str, content: str) -> str:
    """Preview shown to user — HTML, user content escaped."""
    num   = db["post_count"] + 1
    emoji = TYPE_EMOJI.get(ptype, "📋")
    return (
        f"👁 <b>Preview — POST #{num:04d}</b>\n"
        f"📂 Type: {emoji} {h(ptype)}\n"
        f"{'─'*26}\n\n"
        f"{h(content)}\n\n"
        f"{'─'*26}\n"
        f"👤 Posted by: <i>your @username</i>\n"
        f"🚀 Promote FREE → {h(BOT_USERNAME)}\n\n"
        f"<i>Looks good? Hit Publish — or Edit to revise.</i>"
    )

# ══════════════════════════════════════════════════════════════════════════════
# FORCE-JOIN CHECK
# ══════════════════════════════════════════════════════════════════════════════

async def is_joined(uid: int, bot) -> bool:
    for cid in (PROMO_CHANNEL_ID, GROUP_ID):
        try:
            member = await bot.get_chat_member(chat_id=cid, user_id=uid)
            if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
                return False
        except TelegramError:
            return False
    return True

# ══════════════════════════════════════════════════════════════════════════════
# PUBLISH
# ══════════════════════════════════════════════════════════════════════════════

async def publish(
    uid: int,
    username: Optional[str],
    ptype: str,
    content: str,
    app: Application,
) -> int:
    db["post_count"] += 1
    num      = db["post_count"]
    stat_key = STAT_KEY.get(ptype.lower(), "others")
    db[stat_key] += 1

    text = tpl_post(num, ptype, content, username, uid)
    try:
        await app.bot.send_message(
            chat_id=PROMO_CHANNEL_ID,
            text=text,
            # NO parse_mode — plain text, handles all characters safely
        )
    except TelegramError as e:
        db["post_count"] -= 1
        db[stat_key]     -= 1
        raise e

    _rate[uid].append(time.time())
    await db_log(app, num, uid, ptype, username or "")
    await db_save(app)
    return num

# ══════════════════════════════════════════════════════════════════════════════
# WARNINGS / BAN / UNBAN
# ══════════════════════════════════════════════════════════════════════════════

async def add_warning(uid: int, app: Application) -> int:
    count = get_warns(uid) + 1
    db["warnings"][str(uid)] = count
    if count >= MAX_WARNINGS and uid not in db["banned"]:
        db["banned"].append(uid)
        try:
            await app.bot.restrict_chat_member(
                chat_id=GROUP_ID,
                user_id=uid,
                permissions=ChatPermissions(can_send_messages=False),
            )
        except TelegramError:
            pass
    await db_save(app)
    return count


async def do_ban(uid: int, app: Application) -> None:
    if uid not in db["banned"]:
        db["banned"].append(uid)
    db["warnings"][str(uid)] = MAX_WARNINGS
    await db_save(app)
    try:
        await app.bot.restrict_chat_member(
            chat_id=GROUP_ID,
            user_id=uid,
            permissions=ChatPermissions(can_send_messages=False),
        )
    except TelegramError:
        pass


async def do_unban(uid: int, app: Application) -> None:
    if uid in db["banned"]:
        db["banned"].remove(uid)
    db["warnings"].pop(str(uid), None)
    await db_save(app)
    try:
        await app.bot.restrict_chat_member(
            chat_id=GROUP_ID,
            user_id=uid,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
            ),
        )
    except TelegramError:
        pass

# ══════════════════════════════════════════════════════════════════════════════
# SAFE SEND HELPERS
# ══════════════════════════════════════════════════════════════════════════════

async def safe_reply(message, text: str, **kwargs) -> Optional[object]:
    """Reply to a message. Returns the sent Message or None on error."""
    try:
        return await message.reply_text(text, **kwargs)
    except TelegramError as e:
        log.warning(f"safe_reply failed: {e}")
        return None


async def safe_edit(query, text: str, **kwargs) -> None:
    try:
        await query.edit_message_text(text, **kwargs)
    except TelegramError as e:
        log.warning(f"safe_edit failed: {e}")


async def notify_user(bot, uid: int, text: str, **kwargs) -> None:
    try:
        await bot.send_message(chat_id=uid, text=text, **kwargs)
    except (Forbidden, BadRequest, TelegramError) as e:
        log.debug(f"notify uid={uid} failed: {e}")

# ══════════════════════════════════════════════════════════════════════════════
# POST SUBMISSION FLOW
# ══════════════════════════════════════════════════════════════════════════════

async def begin_post(message, uid: int, chat_id: int, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Initiate a post submission session (DM or group)."""
    clean_expired_sessions()

    ok, wait = rate_ok(uid)
    if not ok:
        await safe_reply(
            message,
            f"⏳ <b>Posting limit reached.</b>\n\n"
            f"You can post <b>{POSTS_PER_HOUR}</b> time(s) per hour.\n"
            f"Please wait <b>{fmt_wait(wait)}</b> and try again.",
            parse_mode="HTML",
        )
        return

    is_group = message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)

    if is_group:
        # In groups: ask user to reply to the bot's message (prevents session mixing)
        prompt_msg = await safe_reply(
            message,
            tpl_submit_prompt() + "\n\n📌 <b>Reply to THIS message with your content.</b>",
            parse_mode="HTML",
        )
        if prompt_msg:
            _sessions[session_key(uid, chat_id)] = {
                "step":          "wait_content",
                "prompt_msg_id": prompt_msg.message_id,
                "chat_id":       chat_id,
                "ts":            time.time(),
            }
    else:
        # DM: any next message from user counts
        await safe_reply(message, tpl_submit_prompt(), parse_mode="HTML")
        _sessions[session_key(uid, chat_id)] = {
            "step":    "wait_content",
            "chat_id": chat_id,
            "ts":      time.time(),
        }


async def process_content(
    message,
    uid: int,
    chat_id: int,
    text: str,
    ctx: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Validate content then show type picker or auto-detected preview."""
    sk = session_key(uid, chat_id)
    session = _sessions.get(sk)

    if not text.strip():
        await safe_reply(message, "⚠️ Empty message. Please send your promotion text.", parse_mode="HTML")
        return

    # Hashtag count check
    htags = count_hashtags(text)
    if htags > 4:
        await safe_reply(
            message,
            f"⚠️ You used <b>{htags} hashtags</b>. Maximum allowed is <b>4</b>.\n"
            f"Please trim your hashtags and try again.",
            parse_mode="HTML",
        )
        return

    if has_bad_content(text):
        await _issue_warning(message, uid, ctx)
        return

    if session:
        session["content"] = text
        session["ts"]      = time.time()

    ptype = await detect_type(text, ctx.bot)

    if ptype:
        if session:
            session["ptype"] = ptype
            session["step"]  = "confirm"
        await safe_reply(
            message,
            tpl_preview(ptype, text),
            parse_mode="HTML",
            reply_markup=kb_confirm(),
        )
    else:
        if session:
            session["step"] = "wait_type"
        await safe_reply(
            message,
            "📂 <b>Select your promotion category:</b>",
            parse_mode="HTML",
            reply_markup=kb_type(),
        )


async def _issue_warning(
    message,
    uid: int,
    ctx: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Warn user for bad content. Auto-ban at MAX_WARNINGS."""
    # Remove their session — they need to restart cleanly
    for key in list(_sessions.keys()):
        if key[0] == uid:
            del _sessions[key]

    count = await add_warning(uid, ctx.application)

    if is_banned(uid):
        await safe_reply(
            message,
            f"🚫 <b>Permanently banned</b> after {MAX_WARNINGS} violations.\n"
            f"Inappropriate content is not tolerated.",
            parse_mode="HTML",
        )
    else:
        left = MAX_WARNINGS - count
        await safe_reply(
            message,
            f"⚠️ <b>Warning {count}/{MAX_WARNINGS}:</b> Inappropriate content detected.\n"
            f"<b>{left}</b> warning(s) remaining before a permanent ban.\n\n"
            f"Please send appropriate content:",
            parse_mode="HTML",
        )

# ══════════════════════════════════════════════════════════════════════════════
# COMMAND: /start
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat

    if chat.type != ChatType.PRIVATE:
        return  # /start is DM-only

    if is_banned(user.id):
        await safe_reply(update.message, "🚫 You are banned from PromoteHub.", parse_mode="HTML")
        return

    # Deep-link: /start post
    if ctx.args and ctx.args[0] == "post":
        joined = await is_joined(user.id, ctx.bot)
        if not joined:
            await safe_reply(
                update.message,
                "⚠️ Please join our channel and group first:",
                parse_mode="HTML",
                reply_markup=kb_join(),
            )
            return
        await begin_post(update.message, user.id, chat.id, ctx)
        return

    joined = await is_joined(user.id, ctx.bot)
    if not joined:
        await safe_reply(
            update.message,
            "👋 <b>Welcome to PromoteHub!</b>\n\nPlease join our channel and group first:",
            parse_mode="HTML",
            reply_markup=kb_join(),
        )
        return

    await update.message.reply_text(
        tpl_home(user.first_name),
        parse_mode="HTML",
        reply_markup=kb_home(),
    )

# ══════════════════════════════════════════════════════════════════════════════
# COMMAND: /post
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_post(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    msg  = update.message

    if not user or not msg:
        return

    if is_banned(user.id):
        await safe_reply(msg, "🚫 You are banned from PromoteHub.", parse_mode="HTML")
        return

    # Enforce POST_ALLOWED_IN
    if not post_allowed_here(chat.type):
        if POST_ALLOWED_IN == "dm":
            await safe_reply(
                msg,
                "📩 <b>Promotions must be submitted via private chat.</b>",
                parse_mode="HTML",
                reply_markup=kb_post_group_open_dm(),
            )
        elif POST_ALLOWED_IN == "group":
            await safe_reply(
                msg,
                f"📩 Please use <b>/post</b> inside our group: {GROUP_LINK}",
                parse_mode="HTML",
            )
        return

    # Force-join check (DM only — group members are clearly in the group)
    if chat.type == ChatType.PRIVATE:
        joined = await is_joined(user.id, ctx.bot)
        if not joined:
            await safe_reply(
                msg,
                "⚠️ You must join our channel and group first:",
                parse_mode="HTML",
                reply_markup=kb_join(),
            )
            return

    # In groups: /post <content> → process inline without waiting for reply
    is_group = chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)
    if is_group and ctx.args:
        inline_text = " ".join(ctx.args)
        clean_expired_sessions()

        ok, wait = rate_ok(user.id)
        if not ok:
            await safe_reply(
                msg,
                f"⏳ Please wait <b>{fmt_wait(wait)}</b> before posting again.",
                parse_mode="HTML",
            )
            return

        _sessions[session_key(user.id, chat.id)] = {
            "step":    "wait_content",
            "chat_id": chat.id,
            "ts":      time.time(),
        }
        await process_content(msg, user.id, chat.id, inline_text, ctx)
        return

    # Default: begin interactive flow
    await begin_post(msg, user.id, chat.id, ctx)

# ══════════════════════════════════════════════════════════════════════════════
# COMMANDS: /stats  /help  /cancel
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await safe_reply(update.message, tpl_stats(), parse_mode="HTML")


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await safe_reply(update.message, tpl_help(), parse_mode="HTML")


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid  = update.effective_user.id
    cid  = update.effective_chat.id
    sk   = session_key(uid, cid)
    if sk in _sessions:
        del _sessions[sk]
        await safe_reply(update.message, "❌ Cancelled. Use /post to start again.", parse_mode="HTML")
    else:
        await safe_reply(update.message, "Nothing to cancel.", parse_mode="HTML")

# ══════════════════════════════════════════════════════════════════════════════
# MESSAGE HANDLER  — DM submission flow + group post replies + moderation
# ══════════════════════════════════════════════════════════════════════════════

async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg  = update.message
    user = update.effective_user
    chat = update.effective_chat

    if not msg or not user or not msg.text:
        return

    text     = msg.text.strip()
    uid      = user.id
    chat_id  = chat.id
    sk       = session_key(uid, chat_id)
    is_group = chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)

    # ── Group handling ────────────────────────────────────────────────────────
    if is_group and chat_id == GROUP_ID:
        session = _sessions.get(sk)

        # Check if this is a reply to our submission prompt
        if (
            session
            and not session_expired(session)
            and session.get("step") == "wait_content"
            and session.get("prompt_msg_id")
            and msg.reply_to_message
            and msg.reply_to_message.message_id == session["prompt_msg_id"]
        ):
            await process_content(msg, uid, chat_id, text, ctx)
            return

        # Otherwise: group moderation
        await _moderate_group(update, ctx, text)
        return

    # ── DM only below ─────────────────────────────────────────────────────────
    if chat.type != ChatType.PRIVATE:
        return

    if is_banned(uid):
        await safe_reply(msg, "🚫 You are banned from PromoteHub.", parse_mode="HTML")
        return

    session = _sessions.get(sk)
    if not session:
        await safe_reply(msg, "💡 Use /post to submit a promotion.", parse_mode="HTML")
        return

    if session_expired(session):
        del _sessions[sk]
        await safe_reply(msg, "⌛ Session expired. Please use /post to start again.", parse_mode="HTML")
        return

    if session["step"] == "wait_content":
        await process_content(msg, uid, chat_id, text, ctx)
    # wait_type and confirm steps are handled via inline buttons in on_callback


async def _moderate_group(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str
) -> None:
    uid = update.effective_user.id
    if is_admin(uid):
        return

    if is_banned(uid):
        try:
            await update.message.delete()
        except TelegramError:
            pass
        return

    if has_bad_content(text):
        try:
            await update.message.delete()
        except TelegramError:
            pass

        count    = await add_warning(uid, ctx.application)
        mention  = update.effective_user.mention_html()

        try:
            if is_banned(uid):
                await ctx.bot.send_message(
                    chat_id=GROUP_ID,
                    text=(
                        f"🚫 {mention} has been <b>permanently banned</b> "
                        f"after {MAX_WARNINGS} violations."
                    ),
                    parse_mode="HTML",
                )
            else:
                left = MAX_WARNINGS - count
                await ctx.bot.send_message(
                    chat_id=GROUP_ID,
                    text=(
                        f"⚠️ {mention} — message removed.\n"
                        f"<b>Warning {count}/{MAX_WARNINGS}</b> ({left} remaining before ban)."
                    ),
                    parse_mode="HTML",
                )
        except TelegramError:
            pass

# ══════════════════════════════════════════════════════════════════════════════
# CALLBACK HANDLER
# ══════════════════════════════════════════════════════════════════════════════

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q       = update.callback_query
    uid     = q.from_user.id
    chat_id = q.message.chat.id
    d       = q.data
    sk      = session_key(uid, chat_id)

    try:
        await q.answer()
    except TelegramError:
        pass

    # ── Verify join ───────────────────────────────────────────────────────────
    if d == "check_join":
        joined = await is_joined(uid, ctx.bot)
        if joined:
            await safe_edit(
                q,
                f"✅ <b>Verified!</b> You're all set.\n\n"
                f"📊 <b>{db['post_count']}</b> promotions published!",
                parse_mode="HTML",
                reply_markup=kb_home(),
            )
        else:
            await safe_edit(
                q,
                "❌ You haven't joined yet.\n\nPlease join <b>both</b> and try again:",
                parse_mode="HTML",
                reply_markup=kb_join(),
            )

    # ── Home ──────────────────────────────────────────────────────────────────
    elif d == "home":
        await safe_edit(
            q,
            f"🏠 <b>PromoteHub</b> — {db['post_count']} promotions published!",
            parse_mode="HTML",
            reply_markup=kb_home(),
        )

    # ── Stats ─────────────────────────────────────────────────────────────────
    elif d == "stats":
        await safe_edit(q, tpl_stats(), parse_mode="HTML", reply_markup=kb_back())

    # ── Help ──────────────────────────────────────────────────────────────────
    elif d == "help":
        await safe_edit(q, tpl_help(), parse_mode="HTML", reply_markup=kb_back())

    # ── Start post (from home menu) ───────────────────────────────────────────
    elif d == "start_post":
        if is_banned(uid):
            await safe_edit(q, "🚫 You are banned from PromoteHub.", parse_mode="HTML")
            return

        # Enforce POST_ALLOWED_IN from callback context (should always be DM from home)
        if not post_allowed_here(q.message.chat.type):
            await safe_edit(
                q,
                "📩 Promotions can only be submitted via private chat.",
                parse_mode="HTML",
            )
            return

        ok, wait = rate_ok(uid)
        if not ok:
            await safe_edit(
                q,
                f"⏳ <b>Posting limit reached.</b>\n\nWait <b>{fmt_wait(wait)}</b> and try again.",
                parse_mode="HTML",
            )
            return

        clean_expired_sessions()
        _sessions[sk] = {"step": "wait_content", "chat_id": chat_id, "ts": time.time()}
        await safe_edit(q, tpl_submit_prompt(), parse_mode="HTML")

    # ── Type selection ────────────────────────────────────────────────────────
    elif d.startswith("type:"):
        session = _sessions.get(sk)
        if not session or session_expired(session):
            _sessions.pop(sk, None)
            await safe_edit(q, "⚠️ Session expired. Please use /post to start again.", parse_mode="HTML")
            return

        ptype = d[5:]
        if ptype not in TYPE_EMOJI:
            await safe_edit(q, "⚠️ Unknown type. Please use /post to start again.", parse_mode="HTML")
            return

        session["ptype"] = ptype
        session["step"]  = "confirm"
        session["ts"]    = time.time()

        content = session.get("content", "")
        await safe_edit(
            q,
            tpl_preview(ptype, content),
            parse_mode="HTML",
            reply_markup=kb_confirm(),
        )

    # ── Publish ───────────────────────────────────────────────────────────────
    elif d == "publish":
        session = _sessions.get(sk)
        if not session or session.get("step") != "confirm" or session_expired(session):
            _sessions.pop(sk, None)
            await safe_edit(q, "⚠️ Session expired. Please use /post to start again.", parse_mode="HTML")
            return

        await safe_edit(q, "⏳ Publishing your post...", parse_mode="HTML")

        try:
            username = q.from_user.username
            num = await publish(
                uid,
                username,
                session["ptype"],
                session["content"],
                ctx.application,
            )
            _sessions.pop(sk, None)
            emoji = TYPE_EMOJI.get(session["ptype"], "📋")
            await safe_edit(
                q,
                f"✅ <b>Published!</b>\n\n"
                f"📌 <b>POST #{num:04d}</b> is now live in the channel!\n"
                f"📂 Type: {emoji} {h(session['ptype'])}\n\n"
                f"🎉 Share the bot to help others promote!\n{h(BOT_USERNAME)}",
                parse_mode="HTML",
                reply_markup=kb_after_post(),
            )
        except TelegramError as e:
            log.error(f"Publish failed uid={uid}: {e}")
            await safe_edit(
                q,
                "❌ <b>Publish failed.</b>\n\nPlease try again with /post.",
                parse_mode="HTML",
            )

    # ── Edit content ──────────────────────────────────────────────────────────
    elif d == "edit":
        session = _sessions.get(sk)
        if not session:
            _sessions[sk] = {"step": "wait_content", "chat_id": chat_id, "ts": time.time()}
        else:
            session["step"] = "wait_content"
            session["ts"]   = time.time()
        await safe_edit(
            q,
            tpl_submit_prompt() + "\n\n✏️ <b>Send your updated promotion message:</b>",
            parse_mode="HTML",
        )

    # ── Cancel ────────────────────────────────────────────────────────────────
    elif d == "cancel":
        _sessions.pop(sk, None)
        await safe_edit(q, "❌ Cancelled. Use /post to start again.", parse_mode="HTML")

# ══════════════════════════════════════════════════════════════════════════════
# ADMIN COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

def _get_target(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    if ctx.args:
        try:
            return int(ctx.args[0])
        except ValueError:
            return None
    m = update.message
    if m and m.reply_to_message and m.reply_to_message.from_user:
        return m.reply_to_message.from_user.id
    return None


async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    await safe_reply(
        update.message,
        f"🔧 <b>Admin Panel</b>\n\n"
        f"📌 Posts   : {db['post_count']}\n"
        f"🚫 Banned  : {len(db['banned'])}\n"
        f"⚠️ Warned  : {len([v for v in db['warnings'].values() if v > 0])}\n\n"
        f"<code>/ban &lt;id&gt;</code>         — Ban a user\n"
        f"<code>/unban &lt;id&gt;</code>       — Unban a user\n"
        f"<code>/warn &lt;id&gt;</code>        — Add a warning\n"
        f"<code>/broadcast &lt;msg&gt;</code>  — Post to channel\n"
        f"<code>/stats</code>              — Live stats\n"
        f"<code>/dbcheck</code>            — DB health check",
        parse_mode="HTML",
    )


async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    uid = _get_target(update, ctx)
    if not uid:
        await safe_reply(update.message, "Usage: /ban &lt;user_id&gt;  or reply to a message.", parse_mode="HTML")
        return
    if uid in ADMIN_IDS:
        await safe_reply(update.message, "❌ Cannot ban an admin.", parse_mode="HTML")
        return
    await do_ban(uid, ctx.application)
    await safe_reply(update.message, f"✅ User <code>{uid}</code> banned.", parse_mode="HTML")
    await notify_user(ctx.bot, uid, "🚫 You have been banned from PromoteHub.")


async def cmd_unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    uid = _get_target(update, ctx)
    if not uid:
        await safe_reply(update.message, "Usage: /unban &lt;user_id&gt;", parse_mode="HTML")
        return
    await do_unban(uid, ctx.application)
    await safe_reply(update.message, f"✅ User <code>{uid}</code> unbanned.", parse_mode="HTML")
    await notify_user(ctx.bot, uid, "✅ You have been unbanned from PromoteHub. Use /start to continue.")


async def cmd_warn_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    uid = _get_target(update, ctx)
    if not uid:
        await safe_reply(update.message, "Usage: /warn &lt;user_id&gt;", parse_mode="HTML")
        return
    count = await add_warning(uid, ctx.application)
    if is_banned(uid):
        await safe_reply(
            update.message,
            f"⛔ User <code>{uid}</code> auto-banned (reached {MAX_WARNINGS} warnings).",
            parse_mode="HTML",
        )
    else:
        await safe_reply(
            update.message,
            f"⚠️ User <code>{uid}</code> warned ({count}/{MAX_WARNINGS}).",
            parse_mode="HTML",
        )


async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args:
        await safe_reply(update.message, "Usage: /broadcast &lt;message&gt;", parse_mode="HTML")
        return
    msg_text = " ".join(ctx.args)
    try:
        await ctx.bot.send_message(
            chat_id=PROMO_CHANNEL_ID,
            text=f"📢 <b>Announcement</b>\n\n{msg_text}",
            parse_mode="HTML",
        )
        await safe_reply(update.message, "✅ Broadcast sent.", parse_mode="HTML")
    except TelegramError as e:
        await safe_reply(update.message, f"❌ Failed: {h(str(e))}", parse_mode="HTML")


async def cmd_dbcheck(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: verify DB channel connectivity and state."""
    if not is_admin(update.effective_user.id):
        return
    status_lines = [
        f"🗄️ <b>DB Channel Health Check</b>",
        f"Channel ID : <code>{DB_CHANNEL_ID}</code>",
        f"Stored msg_id : <code>{_db_msg_id}</code>",
        f"Current posts : <code>{db['post_count']}</code>",
        f"Banned users : <code>{len(db['banned'])}</code>",
    ]
    try:
        chat = await ctx.bot.get_chat(DB_CHANNEL_ID)
        status_lines.append(f"Channel access : ✅ <code>{h(str(chat.title or chat.id))}</code>")
        pinned = getattr(chat, "pinned_message", None)
        if pinned and pinned.text and pinned.text.startswith("#PH_DB"):
            status_lines.append(f"Pinned DB msg : ✅ ID <code>{pinned.message_id}</code>")
        elif pinned:
            status_lines.append(f"Pinned msg : ⚠️ Not a DB record (ID <code>{pinned.message_id}</code>)")
        else:
            status_lines.append("Pinned msg : ❌ None found — DB won't survive restart!")
    except TelegramError as e:
        status_lines.append(f"Channel access : ❌ {h(str(e))}")
    await safe_reply(update.message, "\n".join(status_lines), parse_mode="HTML")

# ══════════════════════════════════════════════════════════════════════════════
# GLOBAL ERROR HANDLER
# ══════════════════════════════════════════════════════════════════════════════

async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.error(f"Unhandled exception: {ctx.error}", exc_info=ctx.error)
    if isinstance(update, Update):
        if update.message:
            try:
                await update.message.reply_text(
                    "⚠️ Something went wrong. Please try again or use /cancel.",
                    parse_mode="HTML",
                )
            except TelegramError:
                pass
        elif update.callback_query:
            try:
                await update.callback_query.answer(
                    "⚠️ Something went wrong. Please try again.",
                    show_alert=True,
                )
            except TelegramError:
                pass

# ══════════════════════════════════════════════════════════════════════════════
# STARTUP
# ══════════════════════════════════════════════════════════════════════════════

async def on_startup(app: Application) -> None:
    log.info("PromoteHub v2 starting up…")
    await db_load(app)
    await app.bot.set_my_commands([
        BotCommand("start",     "Home menu"),
        BotCommand("post",      "Submit a free promotion"),
        BotCommand("stats",     "Live statistics"),
        BotCommand("help",      "Help & rules"),
        BotCommand("cancel",    "Cancel current action"),
    ])
    mode = "WEBHOOK" if WEBHOOK_URL else "POLLING"
    log.info(
        f"✅ PromoteHub ready! "
        f"mode={mode}  "
        f"POST_ALLOWED_IN={POST_ALLOWED_IN}  "
        f"posts={db['post_count']}"
    )

# ══════════════════════════════════════════════════════════════════════════════
# BUILD APPLICATION
# ══════════════════════════════════════════════════════════════════════════════

def build_app() -> Application:
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(on_startup)
        .build()
    )

    # User commands
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("post",      cmd_post))
    app.add_handler(CommandHandler("stats",     cmd_stats))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("cancel",    cmd_cancel))

    # Admin commands
    app.add_handler(CommandHandler("admin",     cmd_admin))
    app.add_handler(CommandHandler("ban",       cmd_ban))
    app.add_handler(CommandHandler("unban",     cmd_unban))
    app.add_handler(CommandHandler("warn",      cmd_warn_admin))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("dbcheck",   cmd_dbcheck))

    # Callbacks and messages
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    app.add_error_handler(error_handler)
    return app

# ══════════════════════════════════════════════════════════════════════════════
# AIOHTTP WEB SERVER  — handles both webhook and health checks on single PORT
# ══════════════════════════════════════════════════════════════════════════════

async def webhook_handler(request: web.Request) -> web.Response:
    """Receive Telegram updates via webhook."""
    global _app
    try:
        data   = await request.json()
        update = Update.de_json(data, _app.bot)
        await _app.process_update(update)
    except Exception as e:
        log.error(f"Webhook processing error: {e}", exc_info=True)
    return web.Response(status=200)


async def health_handler(request: web.Request) -> web.Response:
    """Health check endpoint — Render/UptimeRobot can ping this."""
    body = (
        f"PromoteHub OK\n"
        f"posts={db['post_count']}\n"
        f"banned={len(db['banned'])}\n"
        f"sessions={len(_sessions)}\n"
    )
    return web.Response(text=body, content_type="text/plain")


def make_web_app() -> web.Application:
    web_app = web.Application()
    web_app.router.add_get("/",        health_handler)
    web_app.router.add_get("/health",  health_handler)
    web_app.router.add_post(f"/{BOT_TOKEN}", webhook_handler)
    return web_app

# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

async def main() -> None:
    global _app
    _app = build_app()

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 1 — Bind the HTTP port IMMEDIATELY (before ANY Telegram API calls)
    #
    # Render scans for an open port right after the process starts.
    # If we do ANY network I/O first (Telegram API, db_load, set_webhook…)
    # Render may time out and print "No open ports detected" → deploy fails.
    #
    # The HTTP server serves:
    #   GET  /        → health check (Render uptime monitor)
    #   GET  /health  → health check
    #   POST /{TOKEN} → Telegram webhook updates
    # ══════════════════════════════════════════════════════════════════════════
    web_application = make_web_app()
    runner = web.AppRunner(web_application)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info(f"✅ HTTP server bound to 0.0.0.0:{PORT}  ← Render detects this immediately")

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 2 — Start the Telegram application (db_load, set_my_commands, etc.)
    # ══════════════════════════════════════════════════════════════════════════
    async with _app:
        await _app.start()  # triggers on_startup → db_load, set_my_commands

        if WEBHOOK_URL:
            # ── WEBHOOK MODE (Render / any public HTTPS host) ─────────────────
            webhook_full_url = f"{WEBHOOK_URL.rstrip('/')}/{BOT_TOKEN}"

            # Delete any stale webhook from a previous deploy/instance first
            try:
                await _app.bot.delete_webhook(drop_pending_updates=True)
                log.info("Cleared any previous webhook.")
            except TelegramError as e:
                log.warning(f"Could not clear old webhook (non-fatal): {e}")

            await _app.bot.set_webhook(
                url=webhook_full_url,
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,
            )
            log.info(f"✅ Webhook set → {webhook_full_url}")
            log.info("Bot is LIVE via webhook. Waiting for updates…")

            try:
                await asyncio.Event().wait()  # run forever
            except (KeyboardInterrupt, SystemExit):
                log.info("Shutdown signal received.")
            finally:
                log.info("Shutting down webhook bot…")
                try:
                    await _app.bot.delete_webhook()
                except TelegramError:
                    pass

        else:
            # ── POLLING MODE (local dev — WEBHOOK_URL not set) ────────────────
            #
            # Even in polling mode we keep the HTTP server running so Render
            # never complains about "no open ports". The /health endpoint
            # answers normally; the webhook route just won't receive anything.
            #
            # Before polling we MUST delete any registered webhook, otherwise
            # Telegram returns 409 Conflict on every getUpdates call.
            log.info("WEBHOOK_URL not set — starting polling mode.")
            try:
                await _app.bot.delete_webhook(drop_pending_updates=True)
                log.info("Cleared any registered webhook (required before polling).")
            except TelegramError as e:
                log.warning(f"Could not clear webhook (non-fatal): {e}")

            await _app.updater.start_polling(
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,
            )
            log.info("✅ Bot is polling. HTTP health server still running on port {PORT}.")

            try:
                await asyncio.Event().wait()  # run forever
            except (KeyboardInterrupt, SystemExit):
                log.info("Shutdown signal received.")
            finally:
                log.info("Shutting down polling bot…")
                await _app.updater.stop()

        await _app.stop()

    await runner.cleanup()
    log.info("Goodbye.")


if __name__ == "__main__":
    asyncio.run(main())
