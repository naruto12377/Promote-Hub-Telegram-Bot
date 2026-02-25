"""
PromoteHub — Telegram Promotion Marketplace Bot
================================================
- HTML parse_mode throughout (no Markdown parse errors)
- html.escape() on ALL user-provided content
- Global error handler catches everything
- Clean 3-step post flow: content → type → preview → publish
- Telegram channel as database (JSON message, searched on restart)
- Render free tier compatible (webhook + keep-alive HTTP)
- Group posting support with session isolation
- "Posted by: @username" in posts
- Extended category types
- Hashtag tips and 4-hashtag limit
"""

import asyncio
import html
import json
import logging
import os
import re
import signal
import threading
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional

from telegram import (
    BotCommand,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import BadRequest, Forbidden, NetworkError, TelegramError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ═══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("PromoteHub")

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG — purely from environment variables
# ═══════════════════════════════════════════════════════════════════════════════

def _req(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        raise RuntimeError(f"Missing required environment variable: {key}")
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
PORT               = int(os.getenv("PORT", "8080"))
POSTS_PER_HOUR     = max(1, int(os.getenv("POSTS_PER_HOUR", "2")))
MAX_WARNINGS       = max(1, int(os.getenv("MAX_WARNINGS", "3")))
BAD_WORDS          = _strlist("BAD_WORDS",
    "spam,scam,fake,adult,xxx,porn,crypto scam,ponzi,betting,gambling,hack,phishing")
BAD_LINKS          = _strlist("BAD_LINKS", "onlyfans.com,bit.ly/adult")

# POST_ALLOWED_IN: "dm" | "group" | "both"
POST_ALLOWED_IN = os.getenv("POST_ALLOWED_IN", "dm").strip().lower()
if POST_ALLOWED_IN not in ("dm", "group", "both"):
    POST_ALLOWED_IN = "dm"

# WEBHOOK_URL: if set, bot uses webhooks instead of polling
# Example: https://your-app.onrender.com
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()

# ═══════════════════════════════════════════════════════════════════════════════
# IN-MEMORY STATE (persisted to DB_CHANNEL after every mutation)
# ═══════════════════════════════════════════════════════════════════════════════

db: dict = {
    "post_count": 0,
    "channels":   0,
    "groups":     0,
    "services":   0,
    "referrals":  0,
    "links":      0,
    "others":     0,
    "gaming":     0,
    "crypto":     0,
    "business":   0,
    "chatting":   0,
    "anime":      0,
    "study":      0,
    "earning":    0,
    "banned":     [],
    "warnings":   {},
}
_db_msg_id: Optional[int] = None

# Per-user post timestamps for rate-limiting
_rate: dict = defaultdict(list)

# Active submission sessions: {user_id: {"step": str, "content": str, "ptype": str, "username": str}}
_sessions: dict = {}

# ═══════════════════════════════════════════════════════════════════════════════
# PERSISTENCE — Telegram channel as database
# ═══════════════════════════════════════════════════════════════════════════════

DB_TAG = "#PH_DB_V2"

async def db_save(app: Application) -> None:
    """Save state to DB channel. Always try edit first, then send new if needed."""
    global _db_msg_id
    payload = DB_TAG + "\n" + json.dumps(db, ensure_ascii=False, separators=(",", ":"))
    try:
        if _db_msg_id:
            try:
                await app.bot.edit_message_text(
                    chat_id=DB_CHANNEL_ID,
                    message_id=_db_msg_id,
                    text=payload,
                )
                return
            except BadRequest as e:
                if "message is not modified" in str(e).lower():
                    return
                log.warning(f"db_save edit failed: {e}, will send new message")
                _db_msg_id = None
            except TelegramError as e:
                log.warning(f"db_save edit failed: {e}, will send new message")
                _db_msg_id = None

        # Send new message
        msg = await app.bot.send_message(chat_id=DB_CHANNEL_ID, text=payload)
        _db_msg_id = msg.message_id
        try:
            await app.bot.pin_chat_message(
                chat_id=DB_CHANNEL_ID,
                message_id=_db_msg_id,
                disable_notification=True,
            )
        except TelegramError:
            pass
    except TelegramError as e:
        log.error(f"db_save failed completely: {e}")


async def db_load(app: Application) -> None:
    """
    Restore state from DB channel on startup.
    Strategy:
    1. Try pinned message first
    2. If not found, search recent messages for DB_TAG
    """
    global db, _db_msg_id

    def _parse_db_text(text: str) -> Optional[dict]:
        if text and DB_TAG in text:
            try:
                raw = text.split("\n", 1)[1]
                return json.loads(raw)
            except (IndexError, json.JSONDecodeError) as e:
                log.warning(f"Failed to parse DB text: {e}")
        # Also try old tag for backward compatibility
        if text and text.startswith("#PH_DB"):
            try:
                raw = text.split("\n", 1)[1]
                return json.loads(raw)
            except (IndexError, json.JSONDecodeError):
                pass
        return None

    try:
        # Strategy 1: Check pinned message
        chat = await app.bot.get_chat(DB_CHANNEL_ID)
        if chat.pinned_message and chat.pinned_message.text:
            loaded = _parse_db_text(chat.pinned_message.text)
            if loaded:
                for k, v in loaded.items():
                    db[k] = v
                _db_msg_id = chat.pinned_message.message_id
                log.info(
                    f"State restored from pinned message ✅ posts={db['post_count']} "
                    f"banned={len(db['banned'])}"
                )
                return

        # Strategy 2: Search recent messages (last 20)
        log.info("Pinned message not found or invalid, searching recent messages...")
        # We need to get recent messages from the channel
        # Use getUpdates won't work for channels, so we try a different approach
        # Send a temporary message and check messages before it
        temp_msg = await app.bot.send_message(
            chat_id=DB_CHANNEL_ID, text="#PH_SEARCH_TEMP"
        )
        temp_id = temp_msg.message_id

        # Search backward from temp message
        found = False
        for offset in range(1, 50):
            check_id = temp_id - offset
            if check_id <= 0:
                break
            try:
                # Try to forward the message to get its content
                fwd = await app.bot.forward_message(
                    chat_id=DB_CHANNEL_ID,
                    from_chat_id=DB_CHANNEL_ID,
                    message_id=check_id,
                )
                if fwd.text:
                    loaded = _parse_db_text(fwd.text)
                    if loaded:
                        for k, v in loaded.items():
                            db[k] = v
                        _db_msg_id = check_id
                        log.info(
                            f"State restored from message {check_id} ✅ "
                            f"posts={db['post_count']} banned={len(db['banned'])}"
                        )
                        found = True
                        # Delete the forwarded copy
                        try:
                            await app.bot.delete_message(
                                chat_id=DB_CHANNEL_ID, message_id=fwd.message_id
                            )
                        except TelegramError:
                            pass
                        break
                    else:
                        # Delete forwarded non-DB message
                        try:
                            await app.bot.delete_message(
                                chat_id=DB_CHANNEL_ID, message_id=fwd.message_id
                            )
                        except TelegramError:
                            pass
            except TelegramError:
                continue

        # Clean up temp message
        try:
            await app.bot.delete_message(
                chat_id=DB_CHANNEL_ID, message_id=temp_id
            )
        except TelegramError:
            pass

        if not found:
            log.info("No saved state found — starting fresh.")
    except Exception as e:
        log.error(f"db_load failed: {e}")


async def db_log(app: Application, num: int, uid: int, uname: str, ptype: str) -> None:
    """Append a lightweight audit record to the DB channel."""
    try:
        await app.bot.send_message(
            chat_id=DB_CHANNEL_ID,
            text=(
                f"#POST id={num} user={uid} username=@{uname} "
                f"type={ptype.lower()} ts={int(time.time())}"
            ),
        )
    except TelegramError:
        pass

# ═══════════════════════════════════════════════════════════════════════════════
# SMALL UTILITIES
# ═══════════════════════════════════════════════════════════════════════════════

def is_banned(uid: int) -> bool:
    return uid in db.get("banned", [])

def get_warns(uid: int) -> int:
    return db.get("warnings", {}).get(str(uid), 0)

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

def has_bad_content(text: str) -> bool:
    lower = text.lower()
    return any(w in lower for w in BAD_WORDS) or any(l in lower for l in BAD_LINKS)

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

def post_allowed_here(chat_type: str) -> bool:
    if POST_ALLOWED_IN == "both":
        return True
    if POST_ALLOWED_IN == "dm":
        return chat_type == ChatType.PRIVATE
    if POST_ALLOWED_IN == "group":
        return chat_type in (ChatType.GROUP, ChatType.SUPERGROUP)
    return False

def h(text: str) -> str:
    """Escape user-provided text for safe use in HTML parse_mode messages."""
    return html.escape(str(text))

def extract_tme_username(text: str) -> Optional[str]:
    """Return the first resolvable @username from a t.me link."""
    m = re.search(r"(?:t\.me|telegram\.me)/([A-Za-z0-9_]{4,})", text)
    if not m:
        return None
    username = m.group(1)
    if username.lower() in ("joinchat", "share", "addstickers", "boost", "s"):
        return None
    return username

def count_hashtags(text: str) -> int:
    """Count hashtags in text."""
    return len(re.findall(r"#\w+", text))

def get_username_display(user) -> str:
    """Get @username or first_name for display."""
    if user.username:
        return f"@{user.username}"
    return user.first_name or "Anonymous"

# ═══════════════════════════════════════════════════════════════════════════════
# POST TYPES — Extended categories
# ═══════════════════════════════════════════════════════════════════════════════

TYPE_EMOJI = {
    "Channel":  "📢",
    "Group":    "👥",
    "Service":  "🛠️",
    "Referral": "🔗",
    "Link":     "🌐",
    "Gaming":   "🎮",
    "Crypto":   "💰",
    "Business": "💼",
    "Chatting": "💬",
    "Anime":    "🎌",
    "Study":    "📚",
    "Earning":  "💵",
    "Other":    "📋",
}

STAT_KEY = {
    "channel":  "channels",
    "group":    "groups",
    "service":  "services",
    "referral": "referrals",
    "link":     "links",
    "gaming":   "gaming",
    "crypto":   "crypto",
    "business": "business",
    "chatting": "chatting",
    "anime":    "anime",
    "study":    "study",
    "earning":  "earning",
    "other":    "others",
}

async def detect_type(text: str, bot) -> Optional[str]:
    """Auto-detect post type. Returns type string or None."""
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

# ═══════════════════════════════════════════════════════════════════════════════
# KEYBOARDS
# ═══════════════════════════════════════════════════════════════════════════════

def kb_join() -> InlineKeyboardMarkup:
    buttons = []
    if PROMO_CHANNEL_LINK:
        buttons.append([InlineKeyboardButton("📢 Join Channel", url=PROMO_CHANNEL_LINK)])
    if GROUP_LINK:
        buttons.append([InlineKeyboardButton("💬 Join Group", url=GROUP_LINK)])
    buttons.append([InlineKeyboardButton("✅ I Joined — Verify", callback_data="check_join")])
    return InlineKeyboardMarkup(buttons)

def kb_home() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("📝 Submit Promotion — FREE", callback_data="start_post")],
        [
            InlineKeyboardButton("📊 Stats", callback_data="stats"),
            InlineKeyboardButton("❓ Help", callback_data="help"),
        ],
    ]
    if PROMO_CHANNEL_LINK:
        buttons.append([InlineKeyboardButton("📢 Browse Posts", url=PROMO_CHANNEL_LINK)])
    return InlineKeyboardMarkup(buttons)

def kb_type() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📢 Channel",  callback_data="type:Channel"),
            InlineKeyboardButton("👥 Group",     callback_data="type:Group"),
        ],
        [
            InlineKeyboardButton("🛠️ Service",  callback_data="type:Service"),
            InlineKeyboardButton("🔗 Referral",  callback_data="type:Referral"),
        ],
        [
            InlineKeyboardButton("🎮 Gaming",    callback_data="type:Gaming"),
            InlineKeyboardButton("💰 Crypto",    callback_data="type:Crypto"),
        ],
        [
            InlineKeyboardButton("💼 Business",  callback_data="type:Business"),
            InlineKeyboardButton("💬 Chatting",  callback_data="type:Chatting"),
        ],
        [
            InlineKeyboardButton("🎌 Anime",     callback_data="type:Anime"),
            InlineKeyboardButton("📚 Study",     callback_data="type:Study"),
        ],
        [
            InlineKeyboardButton("💵 Earning",   callback_data="type:Earning"),
            InlineKeyboardButton("🌐 Link",      callback_data="type:Link"),
        ],
        [
            InlineKeyboardButton("📋 Other",     callback_data="type:Other"),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ])

def kb_confirm() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Publish Now", callback_data="publish"),
            InlineKeyboardButton("✏️ Edit", callback_data="edit"),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ])

def kb_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Back", callback_data="home")]
    ])

def kb_after_post() -> InlineKeyboardMarkup:
    buttons = []
    if PROMO_CHANNEL_LINK:
        buttons.append([InlineKeyboardButton("📢 View Post", url=PROMO_CHANNEL_LINK)])
    buttons.append([InlineKeyboardButton("📝 Post Another", callback_data="start_post")])
    return InlineKeyboardMarkup(buttons)

# ═══════════════════════════════════════════════════════════════════════════════
# MESSAGE TEMPLATES — all HTML, user content always escaped
# ═══════════════════════════════════════════════════════════════════════════════

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
        f"📌 Total Posts : <b>{db['post_count']}</b>\n"
        f"{'─'*24}\n"
        f"📢 Channels   : <b>{db.get('channels', 0)}</b>\n"
        f"👥 Groups     : <b>{db.get('groups', 0)}</b>\n"
        f"🛠️ Services   : <b>{db.get('services', 0)}</b>\n"
        f"🔗 Referrals  : <b>{db.get('referrals', 0)}</b>\n"
        f"🎮 Gaming     : <b>{db.get('gaming', 0)}</b>\n"
        f"💰 Crypto     : <b>{db.get('crypto', 0)}</b>\n"
        f"💼 Business   : <b>{db.get('business', 0)}</b>\n"
        f"💬 Chatting   : <b>{db.get('chatting', 0)}</b>\n"
        f"🎌 Anime      : <b>{db.get('anime', 0)}</b>\n"
        f"📚 Study      : <b>{db.get('study', 0)}</b>\n"
        f"💵 Earning    : <b>{db.get('earning', 0)}</b>\n"
        f"🌐 Links      : <b>{db.get('links', 0)}</b>\n"
        f"📋 Others     : <b>{db.get('others', 0)}</b>\n\n"
        f"🚀 Submit yours → {h(BOT_USERNAME)}"
    )

def tpl_help() -> str:
    where = "DM"
    if POST_ALLOWED_IN == "group":
        where = "our group"
    elif POST_ALLOWED_IN == "both":
        where = "DM or group"
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
        f"✅ Gaming, Crypto, Anime communities\n"
        f"✅ Study groups, Earning platforms\n\n"
        f"<b>Where to post?</b>\n"
        f"📩 Post in: <b>{where}</b>\n\n"
        f"<b>Rules</b>\n"
        f"⏳ Max <b>{POSTS_PER_HOUR}</b> post(s) per hour\n"
        f"⚠️ <b>{MAX_WARNINGS}</b> violations = permanent ban\n"
        f"🚫 No spam / adult / scam content\n"
        f"#️⃣ Max <b>4 hashtags</b> per post\n\n"
        f"<b>💡 Tip:</b> Add hashtags to your post for better\n"
        f"search results and ranking!\n"
        f"Example: #gaming #community #fun #telegram\n\n"
        f"📢 Browse posts → {PROMO_CHANNEL_LINK or BOT_USERNAME}"
    )

def tpl_post(num: int, ptype: str, content: str, username_display: str) -> str:
    """Format a post for the promotion channel.
    Sent WITHOUT parse_mode — plain text, 100% safe."""
    emoji = TYPE_EMOJI.get(ptype, "📋")
    divider = "─" * 28
    return (
        f"📌 POST #{num:04d}\n"
        f"📂 Type: {emoji} {ptype}\n"
        f"👤 Posted by: {username_display}\n"
        f"{divider}\n\n"
        f"{content}\n\n"
        f"{divider}\n"
        f"🚀 Promote FREE → {BOT_USERNAME}\n"
        f"💡 Add hashtags for better search!"
    )

def tpl_preview(ptype: str, content: str, username_display: str) -> str:
    """Preview shown to user — use HTML, escape user content."""
    next_num = db["post_count"] + 1
    emoji = TYPE_EMOJI.get(ptype, "📋")
    return (
        f"👁 <b>Preview — POST #{next_num:04d}</b>\n"
        f"📂 Type: {emoji} {ptype}\n"
        f"👤 Posted by: {h(username_display)}\n"
        f"{'─'*28}\n\n"
        f"{h(content)}\n\n"
        f"{'─'*28}\n"
        f"🚀 Promote FREE → {h(BOT_USERNAME)}\n\n"
        f"<i>Does this look good? Hit Publish or Edit.</i>"
    )

def tpl_send_content_prompt() -> str:
    return (
        "📝 <b>Submit Your Promotion</b>\n\n"
        "Send your promotion message now.\n"
        "Include your description and link.\n\n"
        "<b>💡 Tips for better visibility:</b>\n"
        "• Add up to <b>4 hashtags</b> for search &amp; ranking\n"
        "• Example: #gaming #community #fun #telegram\n"
        "• Write a clear description\n"
        "• Include your invite link\n\n"
        "<i>Send /cancel to abort.</i>"
    )

# ═══════════════════════════════════════════════════════════════════════════════
# FORCE-JOIN
# ═══════════════════════════════════════════════════════════════════════════════

async def is_joined(uid: int, bot) -> bool:
    for cid in (PROMO_CHANNEL_ID, GROUP_ID):
        try:
            member = await bot.get_chat_member(chat_id=cid, user_id=uid)
            if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
                return False
        except TelegramError:
            return False
    return True

# ═══════════════════════════════════════════════════════════════════════════════
# PUBLISH
# ═══════════════════════════════════════════════════════════════════════════════

async def do_publish(
    uid: int, ptype: str, content: str,
    username_display: str, app: Application
) -> int:
    """
    Publish post to promotion channel.
    Updates counters, saves DB, logs audit.
    """
    db["post_count"] += 1
    num = db["post_count"]
    stat_key = STAT_KEY.get(ptype.lower(), "others")
    # Ensure key exists
    if stat_key not in db:
        db[stat_key] = 0
    db[stat_key] += 1

    text = tpl_post(num, ptype, content, username_display)
    try:
        await app.bot.send_message(
            chat_id=PROMO_CHANNEL_ID,
            text=text,
            # No parse_mode — plain text, 100% safe
        )
    except TelegramError as e:
        # Rollback
        db["post_count"] -= 1
        db[stat_key] -= 1
        raise e

    _rate[uid].append(time.time())

    uname = username_display.lstrip("@")
    await db_log(app, num, uid, uname, ptype)
    await db_save(app)
    return num

# ═══════════════════════════════════════════════════════════════════════════════
# WARNINGS / BAN / UNBAN
# ═══════════════════════════════════════════════════════════════════════════════

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

# ═══════════════════════════════════════════════════════════════════════════════
# SAFE SEND WRAPPERS
# ═══════════════════════════════════════════════════════════════════════════════

async def safe_reply(message, text: str, **kwargs) -> None:
    """Safe reply — logs but never raises."""
    try:
        await message.reply_text(text, **kwargs)
    except TelegramError as e:
        log.warning(f"safe_reply() failed: {e}")

async def safe_edit(query, text: str, **kwargs) -> None:
    """Safe edit — logs but never raises."""
    try:
        await query.edit_message_text(text, **kwargs)
    except BadRequest as e:
        if "message is not modified" not in str(e).lower():
            log.warning(f"safe_edit() failed: {e}")
    except TelegramError as e:
        log.warning(f"safe_edit() failed: {e}")

async def safe_send(bot, chat_id: int, text: str, **kwargs) -> None:
    """Send a message to a chat — silently skip on failure."""
    try:
        await bot.send_message(chat_id=chat_id, text=text, **kwargs)
    except (Forbidden, BadRequest, TelegramError) as e:
        log.debug(f"safe_send chat_id={chat_id} failed: {e}")

async def notify(bot, uid: int, text: str, **kwargs) -> None:
    """Send a DM to a user — silently skip if blocked."""
    await safe_send(bot, uid, text, **kwargs)

# ═══════════════════════════════════════════════════════════════════════════════
# POST SUBMISSION FLOW HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

async def begin_post(message, user, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Rate-limit check then start the submission session."""
    uid = user.id
    ok, wait = rate_ok(uid)
    if not ok:
        await safe_reply(message,
            f"⏳ <b>Posting limit reached.</b>\n\n"
            f"You can post <b>{POSTS_PER_HOUR}</b> time(s) per hour.\n"
            f"Please wait <b>{fmt_wait(wait)}</b> and try again.",
            parse_mode="HTML",
        )
        return

    _sessions[uid] = {
        "step": "wait_content",
        "username": get_username_display(user),
        "chat_type": message.chat.type,
    }
    await safe_reply(message, tpl_send_content_prompt(), parse_mode="HTML")


async def process_content(
    message, user, ctx: ContextTypes.DEFAULT_TYPE, text: str
) -> None:
    """Handle user's promotion content."""
    uid = user.id

    if not text.strip():
        await safe_reply(message,
            "⚠️ Empty message. Please send your promotion text:",
            parse_mode="HTML")
        return

    if has_bad_content(text):
        await issue_warning_msg(message, ctx, uid)
        return

    # Check hashtag limit
    htag_count = count_hashtags(text)
    if htag_count > 4:
        await safe_reply(message,
            f"⚠️ <b>Too many hashtags!</b>\n\n"
            f"You used <b>{htag_count}</b> hashtags. Maximum is <b>4</b>.\n"
            f"Please resend with 4 or fewer hashtags.",
            parse_mode="HTML")
        return

    session = _sessions.get(uid)
    if not session:
        return
    session["content"] = text

    ptype = await detect_type(text, ctx.bot)
    if ptype:
        session["ptype"] = ptype
        session["step"] = "confirm"
        username_display = session.get("username", get_username_display(user))
        await safe_reply(message,
            tpl_preview(ptype, text, username_display),
            parse_mode="HTML",
            reply_markup=kb_confirm(),
        )
    else:
        session["step"] = "wait_type"
        await safe_reply(message,
            "📂 <b>Select your promotion type:</b>",
            parse_mode="HTML",
            reply_markup=kb_type(),
        )


async def issue_warning_msg(message, ctx: ContextTypes.DEFAULT_TYPE, uid: int) -> None:
    """Warn user for bad content."""
    count = await add_warning(uid, ctx.application)
    if is_banned(uid):
        _sessions.pop(uid, None)
        await safe_reply(message,
            f"🚫 <b>Permanently banned</b> after {MAX_WARNINGS} violations.\n"
            f"Inappropriate content is not tolerated.",
            parse_mode="HTML",
        )
    else:
        left = MAX_WARNINGS - count
        await safe_reply(message,
            f"⚠️ <b>Warning {count}/{MAX_WARNINGS}:</b> Inappropriate content detected.\n"
            f"<b>{left}</b> warning(s) remaining before a permanent ban.\n\n"
            f"Please send appropriate content:",
            parse_mode="HTML",
        )

# ═══════════════════════════════════════════════════════════════════════════════
# /start
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat

    if not user or not update.message:
        return

    if chat.type != ChatType.PRIVATE:
        return

    if is_banned(user.id):
        await safe_reply(update.message,
            "🚫 You are banned from PromoteHub.", parse_mode="HTML")
        return

    # Deep-link: /start post
    if ctx.args and ctx.args[0] == "post":
        if not post_allowed_here(ChatType.PRIVATE):
            await safe_reply(update.message,
                f"📩 Posting is only allowed in the group.\n"
                f"Please use /post in: {GROUP_LINK or 'our group'}",
                parse_mode="HTML")
            return
        joined = await is_joined(user.id, ctx.bot)
        if not joined:
            await safe_reply(update.message,
                "⚠️ Please join our channel and group first:",
                parse_mode="HTML", reply_markup=kb_join())
            return
        await begin_post(update.message, user, ctx)
        return

    joined = await is_joined(user.id, ctx.bot)
    if not joined:
        await safe_reply(update.message,
            "👋 <b>Welcome to PromoteHub!</b>\n\n"
            "Please join our channel and group first to use the bot:",
            parse_mode="HTML", reply_markup=kb_join())
        return

    await safe_reply(update.message,
        tpl_home(user.first_name),
        parse_mode="HTML",
        reply_markup=kb_home(),
    )

# ═══════════════════════════════════════════════════════════════════════════════
# /post — works in DM and group based on POST_ALLOWED_IN
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_post(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    msg = update.message

    if not user or not msg:
        return

    if is_banned(user.id):
        await safe_reply(msg, "🚫 You are banned from PromoteHub.", parse_mode="HTML")
        return

    # Enforce POST_ALLOWED_IN
    if not post_allowed_here(chat.type):
        if POST_ALLOWED_IN == "dm":
            bot_name = BOT_USERNAME.lstrip("@")
            await safe_reply(msg,
                "📩 <b>Please submit promotions in private chat with the bot:</b>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📝 Open DM",
                        url=f"https://t.me/{bot_name}?start=post")
                ]]))
        elif POST_ALLOWED_IN == "group":
            await safe_reply(msg,
                f"📩 Please use /post inside our group: {GROUP_LINK or 'our group'}",
                parse_mode="HTML")
        return

    # Force-join check
    joined = await is_joined(user.id, ctx.bot)
    if not joined:
        await safe_reply(msg,
            "⚠️ You must join our channel and group first:",
            parse_mode="HTML", reply_markup=kb_join())
        return

    # Check if /post has inline content: /post <message>
    raw_text = msg.text or ""
    # Remove the /post command itself
    inline_content = ""
    match = re.match(r"^/post(?:@\w+)?\s+(.+)", raw_text, re.DOTALL)
    if match:
        inline_content = match.group(1).strip()

    if inline_content:
        # Direct content provided with /post command
        _sessions[user.id] = {
            "step": "wait_content",
            "username": get_username_display(user),
            "chat_type": chat.type,
        }
        await process_content(msg, user, ctx, inline_content)
    else:
        # No inline content — start interactive flow
        await begin_post(msg, user, ctx)

# ═══════════════════════════════════════════════════════════════════════════════
# /stats /help /cancel
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await safe_reply(update.message, tpl_stats(), parse_mode="HTML")

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await safe_reply(update.message, tpl_help(), parse_mode="HTML")

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    uid = update.effective_user.id
    if uid in _sessions:
        del _sessions[uid]
        await safe_reply(update.message,
            "❌ Cancelled. Use /post to start again.", parse_mode="HTML")
    else:
        await safe_reply(update.message, "Nothing to cancel.", parse_mode="HTML")

# ═══════════════════════════════════════════════════════════════════════════════
# MESSAGE HANDLER — DM/group submission flow + group moderation
# ═══════════════════════════════════════════════════════════════════════════════

async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    user = update.effective_user
    chat = update.effective_chat

    if not msg or not user or not msg.text:
        return

    text = msg.text.strip()
    uid = user.id

    # ── Group messages ────────────────────────────────────────────────
    if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        # First check if user has an active session (group posting mode)
        session = _sessions.get(uid)
        if session and session.get("chat_type") in (ChatType.GROUP, ChatType.SUPERGROUP):
            if session["step"] == "wait_content":
                await process_content(msg, user, ctx, text)
                return
            # Other steps (wait_type, confirm) are handled by callbacks
            # Don't fall through to moderation for session users

        # Group moderation for non-session messages
        if chat.id == GROUP_ID:
            await moderate_group(update, ctx, text)
        return

    # ── DM only below ─────────────────────────────────────────────────
    if chat.type != ChatType.PRIVATE:
        return

    if is_banned(uid):
        await safe_reply(msg, "🚫 You are banned from PromoteHub.", parse_mode="HTML")
        return

    session = _sessions.get(uid)
    if not session:
        await safe_reply(msg, "💡 Use /post to submit a promotion.", parse_mode="HTML")
        return

    if session["step"] == "wait_content":
        await process_content(msg, user, ctx, text)
    # wait_type and confirm steps are handled by callback buttons


async def moderate_group(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str
) -> None:
    """Delete bad messages from the group and warn/ban the sender."""
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

        count = await add_warning(uid, ctx.application)
        mention = update.effective_user.mention_html()

        if is_banned(uid):
            try:
                await ctx.bot.send_message(
                    chat_id=GROUP_ID,
                    text=(
                        f"🚫 {mention} has been <b>permanently banned</b> "
                        f"after {MAX_WARNINGS} violations."
                    ),
                    parse_mode="HTML",
                )
            except TelegramError:
                pass
        else:
            left = MAX_WARNINGS - count
            try:
                await ctx.bot.send_message(
                    chat_id=GROUP_ID,
                    text=(
                        f"⚠️ {mention} — message removed.\n"
                        f"<b>Warning {count}/{MAX_WARNINGS}</b> "
                        f"({left} left before ban)."
                    ),
                    parse_mode="HTML",
                )
            except TelegramError:
                pass

# ═══════════════════════════════════════════════════════════════════════════════
# CALLBACK HANDLER
# ═══════════════════════════════════════════════════════════════════════════════

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not q.from_user:
        return

    uid = q.from_user.id
    d = q.data or ""

    try:
        await q.answer()
    except TelegramError:
        pass

    # ── Verify join ───────────────────────────────────────────────────
    if d == "check_join":
        joined = await is_joined(uid, ctx.bot)
        if joined:
            await safe_edit(q,
                f"✅ <b>Verified!</b> You're all set.\n\n"
                f"📊 <b>{db['post_count']}</b> promotions published!",
                parse_mode="HTML", reply_markup=kb_home())
        else:
            await safe_edit(q,
                "❌ You haven't joined yet.\n\nPlease join <b>both</b> and try again:",
                parse_mode="HTML", reply_markup=kb_join())

    # ── Home ─────────────────────────────────────────────────────────
    elif d == "home":
        _sessions.pop(uid, None)
        await safe_edit(q,
            f"🏠 <b>PromoteHub</b> — {db['post_count']} promotions published!",
            parse_mode="HTML", reply_markup=kb_home())

    # ── Stats ─────────────────────────────────────────────────────────
    elif d == "stats":
        await safe_edit(q, tpl_stats(), parse_mode="HTML", reply_markup=kb_back())

    # ── Help ──────────────────────────────────────────────────────────
    elif d == "help":
        await safe_edit(q, tpl_help(), parse_mode="HTML", reply_markup=kb_back())

    # ── Start post (from home menu button) ────────────────────────────
    elif d == "start_post":
        if is_banned(uid):
            await safe_edit(q, "🚫 You are banned from PromoteHub.", parse_mode="HTML")
            return

        # Check if posting is allowed in DM (callback comes from DM always for inline buttons)
        if not post_allowed_here(ChatType.PRIVATE):
            await safe_edit(q,
                f"📩 Posting is only allowed in the group.\n"
                f"Please use /post in: {GROUP_LINK or 'our group'}",
                parse_mode="HTML")
            return

        ok, wait = rate_ok(uid)
        if not ok:
            await safe_edit(q,
                f"⏳ <b>Posting limit reached.</b>\n\n"
                f"Please wait <b>{fmt_wait(wait)}</b> and try again.",
                parse_mode="HTML")
            return

        _sessions[uid] = {
            "step": "wait_content",
            "username": get_username_display(q.from_user),
            "chat_type": ChatType.PRIVATE,
        }
        await safe_edit(q, tpl_send_content_prompt(), parse_mode="HTML")

    # ── Type selection ────────────────────────────────────────────────
    elif d.startswith("type:"):
        session = _sessions.get(uid)
        if not session:
            await safe_edit(q,
                "⚠️ Session expired. Please use /post to start again.",
                parse_mode="HTML")
            return
        ptype = d[5:]
        if ptype not in TYPE_EMOJI:
            await safe_edit(q,
                "⚠️ Unknown type. Please use /post to start again.",
                parse_mode="HTML")
            return
        session["ptype"] = ptype
        session["step"] = "confirm"
        content = session.get("content", "")
        username_display = session.get("username", get_username_display(q.from_user))
        await safe_edit(q,
            tpl_preview(ptype, content, username_display),
            parse_mode="HTML",
            reply_markup=kb_confirm())

    # ── Publish ───────────────────────────────────────────────────────
    elif d == "publish":
        session = _sessions.get(uid)
        if not session or session.get("step") != "confirm":
            await safe_edit(q,
                "⚠️ Session expired. Please use /post to start again.",
                parse_mode="HTML")
            return

        await safe_edit(q, "⏳ Publishing your post...", parse_mode="HTML")

        ptype = session.get("ptype", "Other")
        content = session.get("content", "")
        username_display = session.get("username", get_username_display(q.from_user))

        try:
            num = await do_publish(
                uid, ptype, content, username_display, ctx.application
            )
            _sessions.pop(uid, None)
            emoji = TYPE_EMOJI.get(ptype, "📋")
            await safe_edit(q,
                f"✅ <b>Post Published!</b>\n\n"
                f"📌 <b>POST #{num:04d}</b> is now live!\n"
                f"📂 Type: {emoji} {h(ptype)}\n"
                f"👤 Posted by: {h(username_display)}\n\n"
                f"🎉 Share the bot to help others promote too!\n"
                f"{h(BOT_USERNAME)}",
                parse_mode="HTML",
                reply_markup=kb_after_post())
        except TelegramError as e:
            log.error(f"Publish failed for uid={uid}: {e}")
            _sessions.pop(uid, None)
            await safe_edit(q,
                "❌ <b>Failed to publish.</b>\n\n"
                "Please try again with /post.",
                parse_mode="HTML")

    # ── Edit ─────────────────────────────────────────────────────
    elif d == "edit":
        session = _sessions.get(uid)
        if session:
            session["step"] = "wait_content"
        else:
            _sessions[uid] = {
                "step": "wait_content",
                "username": get_username_display(q.from_user),
                "chat_type": ChatType.PRIVATE,
            }
        await safe_edit(q,
            "✏️ Send your updated promotion message:\n\n"
            "<b>💡 Remember:</b> Add up to 4 hashtags for better visibility!",
            parse_mode="HTML")

    # ── Cancel ────────────────────────────────────────────────────────
    elif d == "cancel":
        _sessions.pop(uid, None)
        await safe_edit(q,
            "❌ Cancelled. Use /post to start again.",
            parse_mode="HTML")

# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════

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
    if not update.message or not update.effective_user:
        return
    if not is_admin(update.effective_user.id):
        return
    await safe_reply(update.message,
        f"🔧 <b>Admin Panel</b>\n\n"
        f"📌 Posts   : {db['post_count']}\n"
        f"🚫 Banned  : {len(db.get('banned', []))}\n"
        f"⚠️ Warned  : {len(db.get('warnings', {}))}\n"
        f"📩 Post Mode: {POST_ALLOWED_IN}\n\n"
        f"<code>/ban &lt;id&gt;</code>          Ban a user\n"
        f"<code>/unban &lt;id&gt;</code>        Unban a user\n"
        f"<code>/warn &lt;id&gt;</code>         Add a warning\n"
        f"<code>/broadcast &lt;msg&gt;</code>   Post to channel\n"
        f"<code>/stats</code>               Live stats",
        parse_mode="HTML")

async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    if not is_admin(update.effective_user.id):
        return
    uid = _get_target(update, ctx)
    if not uid:
        await safe_reply(update.message,
            "Usage: /ban &lt;user_id&gt; or reply to a message.",
            parse_mode="HTML")
        return
    if uid in ADMIN_IDS:
        await safe_reply(update.message, "❌ Cannot ban an admin.", parse_mode="HTML")
        return
    await do_ban(uid, ctx.application)
    _sessions.pop(uid, None)
    await safe_reply(update.message,
        f"✅ User <code>{uid}</code> banned.", parse_mode="HTML")
    await notify(ctx.bot, uid,
        "🚫 You have been banned from PromoteHub by an admin.")

async def cmd_unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    if not is_admin(update.effective_user.id):
        return
    uid = _get_target(update, ctx)
    if not uid:
        await safe_reply(update.message,
            "Usage: /unban &lt;user_id&gt;", parse_mode="HTML")
        return
    await do_unban(uid, ctx.application)
    await safe_reply(update.message,
        f"✅ User <code>{uid}</code> unbanned.", parse_mode="HTML")
    await notify(ctx.bot, uid,
        "✅ You have been unbanned from PromoteHub. Use /start to continue.")

async def cmd_warn_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    if not is_admin(update.effective_user.id):
        return
    uid = _get_target(update, ctx)
    if not uid:
        await safe_reply(update.message,
            "Usage: /warn &lt;user_id&gt;", parse_mode="HTML")
        return
    count = await add_warning(uid, ctx.application)
    if is_banned(uid):
        _sessions.pop(uid, None)
        await safe_reply(update.message,
            f"⛔ User <code>{uid}</code> auto-banned "
            f"(reached {MAX_WARNINGS} warnings).",
            parse_mode="HTML")
    else:
        await safe_reply(update.message,
            f"⚠️ User <code>{uid}</code> warned ({count}/{MAX_WARNINGS}).",
            parse_mode="HTML")

async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args:
        await safe_reply(update.message,
            "Usage: /broadcast &lt;message&gt;", parse_mode="HTML")
        return
    msg_text = " ".join(ctx.args)
    try:
        await ctx.bot.send_message(
            chat_id=PROMO_CHANNEL_ID,
            text=f"📢 Announcement\n\n{msg_text}",
        )
        await safe_reply(update.message,
            "✅ Broadcast sent to channel.", parse_mode="HTML")
    except TelegramError as e:
        await safe_reply(update.message,
            f"❌ Failed: {h(str(e))}", parse_mode="HTML")

# ═══════════════════════════════════════════════════════════════════════════════
# GLOBAL ERROR HANDLER
# ═══════════════════════════════════════════════════════════════════════════════

async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    # Ignore network timeouts — they're normal
    if isinstance(ctx.error, (NetworkError, TimedOut)):
        log.warning(f"Network error (will retry): {ctx.error}")
        return

    log.error(f"Unhandled exception: {ctx.error}", exc_info=ctx.error)

    if isinstance(update, Update):
        try:
            if update.callback_query:
                await update.callback_query.answer(
                    "⚠️ Something went wrong. Please try again.",
                    show_alert=True,
                )
            elif update.message:
                await update.message.reply_text(
                    "⚠️ Something went wrong. Please try again or use /cancel.",
                    parse_mode="HTML",
                )
        except TelegramError:
            pass

# ═══════════════════════════════════════════════════════════════════════════════
# STARTUP — load DB, set commands
# ═══════════════════════════════════════════════════════════════════════════════

async def on_startup(app: Application) -> None:
    log.info("PromoteHub starting up…")
    await db_load(app)

    try:
        await app.bot.set_my_commands([
            BotCommand("start", "Home menu"),
            BotCommand("post", "Submit a free promotion"),
            BotCommand("stats", "Live statistics"),
            BotCommand("help", "Help & rules"),
            BotCommand("cancel", "Cancel current action"),
        ])
    except TelegramError as e:
        log.warning(f"Failed to set commands: {e}")

    log.info(
        f"✅ PromoteHub ready! "
        f"posts={db['post_count']} "
        f"POST_ALLOWED_IN={POST_ALLOWED_IN} "
        f"mode={'webhook' if WEBHOOK_URL else 'polling'}"
    )

# ═══════════════════════════════════════════════════════════════════════════════
# KEEP-ALIVE HTTP SERVER — for Render health checks + UptimeRobot
# ═══════════════════════════════════════════════════════════════════════════════

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = (
            f"PromoteHub OK\n"
            f"posts={db['post_count']}\n"
            f"banned={len(db.get('banned', []))}\n"
            f"mode={'webhook' if WEBHOOK_URL else 'polling'}\n"
            f"uptime=alive\n"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *_):
        pass


def start_http_server() -> None:
    server = HTTPServer(("0.0.0.0", PORT), _HealthHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    log.info(f"Keep-alive HTTP server started on port {PORT}")

# ═══════════════════════════════════════════════════════════════════════════════
# BUILD APP
# ═══════════════════════════════════════════════════════════════════════════════

def build_app() -> Application:
    builder = Application.builder().token(BOT_TOKEN).post_init(on_startup)

    # Connection pool settings for stability
    builder.connect_timeout(30.0)
    builder.read_timeout(30.0)
    builder.write_timeout(30.0)
