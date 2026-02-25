#!/usr/bin/env python3
"""
PromoteHub — Telegram Promotion Marketplace Bot
================================================
- HTML parse_mode throughout (no Markdown parse errors)
- html.escape() on ALL user-provided content
- Global error handler catches everything
- Clean 3-step post flow: content → type → preview → publish
- Telegram channel as database (JSON pinned message)
- Render free tier compatible (asyncio.run + keep-alive HTTP)
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
from telegram.error import BadRequest, Forbidden, NetworkError, TelegramError
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
# CONFIG  — purely from environment variables
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

# ═══════════════════════════════════════════════════════════════════════════════
# IN-MEMORY STATE  (persisted to DB_CHANNEL after every mutation)
# ═══════════════════════════════════════════════════════════════════════════════

db: dict = {
    "post_count": 0,
    "channels":   0,
    "groups":     0,
    "services":   0,
    "referrals":  0,
    "links":      0,
    "others":     0,
    "banned":     [],   # list[int]
    "warnings":   {},   # {str(user_id): int}
}
_db_msg_id: Optional[int] = None

# Per-user post timestamps for rate-limiting (not persisted — resets on restart, fine)
_rate: dict = defaultdict(list)

# Active submission sessions: {user_id: {"step": str, "content": str, "ptype": str}}
_sessions: dict = {}

# ═══════════════════════════════════════════════════════════════════════════════
# PERSISTENCE  — Telegram channel as database
# ═══════════════════════════════════════════════════════════════════════════════

async def db_save(app: Application) -> None:
    """Overwrite the pinned state message in the DB channel."""
    global _db_msg_id
    payload = "#PH_DB\n" + json.dumps(db, ensure_ascii=False, separators=(",", ":"))
    try:
        if _db_msg_id:
            await app.bot.edit_message_text(
                chat_id=DB_CHANNEL_ID,
                message_id=_db_msg_id,
                text=payload,
            )
        else:
            msg = await app.bot.send_message(chat_id=DB_CHANNEL_ID, text=payload)
            _db_msg_id = msg.message_id
            try:
                await app.bot.pin_chat_message(
                    chat_id=DB_CHANNEL_ID,
                    message_id=_db_msg_id,
                    disable_notification=True,
                )
            except TelegramError:
                pass  # pin failure is non-critical
    except TelegramError as e:
        log.error(f"db_save failed: {e}")


async def db_load(app: Application) -> None:
    """Restore state from the pinned DB channel message on startup."""
    global db, _db_msg_id
    try:
        chat = await app.bot.get_chat(DB_CHANNEL_ID)
        pinned = chat.pinned_message
        if pinned and pinned.text and pinned.text.startswith("#PH_DB"):
            raw = pinned.text.split("\n", 1)[1]
            loaded = json.loads(raw)
            # Merge carefully — keep all keys even if new keys were added
            for k, v in loaded.items():
                db[k] = v
            _db_msg_id = pinned.message_id
            log.info(
                f"State restored ✅  posts={db['post_count']}  "
                f"banned={len(db['banned'])}"
            )
        else:
            log.info("No saved state found — starting fresh.")
    except Exception as e:
        log.error(f"db_load failed: {e}")


async def db_log(app: Application, num: int, uid: int, ptype: str) -> None:
    """Append a lightweight audit record to the DB channel."""
    try:
        await app.bot.send_message(
            chat_id=DB_CHANNEL_ID,
            text=f"#POST id={num} user={uid} type={ptype.lower()} ts={int(time.time())}",
        )
    except TelegramError:
        pass  # audit log failure is non-critical

# ═══════════════════════════════════════════════════════════════════════════════
# SMALL UTILITIES
# ═══════════════════════════════════════════════════════════════════════════════

def is_banned(uid: int) -> bool:
    return uid in db["banned"]

def get_warns(uid: int) -> int:
    return db["warnings"].get(str(uid), 0)

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

def has_bad_content(text: str) -> bool:
    lower = text.lower()
    return any(w in lower for w in BAD_WORDS) or any(l in lower for l in BAD_LINKS)

def rate_ok(uid: int):
    """Returns (allowed: bool, seconds_to_wait: float)."""
    now   = time.time()
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
    # Skip non-resolvable paths
    if username.lower() in ("joinchat", "share", "addstickers", "boost", "s"):
        return None
    return username

# ═══════════════════════════════════════════════════════════════════════════════
# POST TYPES
# ═══════════════════════════════════════════════════════════════════════════════

TYPE_EMOJI = {
    "Channel":  "📢",
    "Group":    "👥",
    "Service":  "🛠️",
    "Referral": "🔗",
    "Link":     "🌐",
    "Other":    "📋",
}
STAT_KEY = {
    "channel":  "channels",
    "group":    "groups",
    "service":  "services",
    "referral": "referrals",
    "link":     "links",
    "other":    "others",
}

async def detect_type(text: str, bot) -> Optional[str]:
    """
    Auto-detect post type.
    Returns type string or None (user must choose manually).
    """
    # Invite links (t.me/+ or joinchat) → Group
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
        # t.me link but couldn't resolve → treat as Link
        return "Link"

    # Other URLs → Link
    if re.search(r"https?://\S+", text):
        return "Link"

    # Pure text → ask user
    return None

# ═══════════════════════════════════════════════════════════════════════════════
# KEYBOARDS
# ═══════════════════════════════════════════════════════════════════════════════

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
            InlineKeyboardButton("📊 Stats",       callback_data="stats"),
            InlineKeyboardButton("❓ Help",         callback_data="help"),
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
            InlineKeyboardButton("🛠️ Service",  callback_data="type:Service"),
            InlineKeyboardButton("🔗 Referral", callback_data="type:Referral"),
        ],
        [
            InlineKeyboardButton("🌐 Link",     callback_data="type:Link"),
            InlineKeyboardButton("📋 Other",    callback_data="type:Other"),
        ],
        [InlineKeyboardButton("❌ Cancel",      callback_data="cancel")],
    ])

def kb_confirm() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Publish Now", callback_data="publish"),
            InlineKeyboardButton("✏️ Edit",        callback_data="edit"),
        ],
        [InlineKeyboardButton("❌ Cancel",         callback_data="cancel")],
    ])

def kb_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Back", callback_data="home")]
    ])

def kb_after_post() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 View Post",     url=PROMO_CHANNEL_LINK)],
        [InlineKeyboardButton("📝 Post Another",  callback_data="start_post")],
    ])

# ═══════════════════════════════════════════════════════════════════════════════
# MESSAGE TEMPLATES  — all HTML, user content always escaped
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
        f"📢 Channels   : <b>{db['channels']}</b>\n"
        f"👥 Groups     : <b>{db['groups']}</b>\n"
        f"🛠️ Services   : <b>{db['services']}</b>\n"
        f"🔗 Referrals  : <b>{db['referrals']}</b>\n"
        f"🌐 Links      : <b>{db['links']}</b>\n"
        f"📋 Others     : <b>{db['others']}</b>\n\n"
        f"🚀 Submit yours → {h(BOT_USERNAME)}"
    )

def tpl_help() -> str:
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
        f"✅ Referral &amp; affiliate links\n\n"
        f"<b>Rules</b>\n"
        f"⏳ Max <b>{POSTS_PER_HOUR}</b> post(s) per hour\n"
        f"⚠️ <b>{MAX_WARNINGS}</b> violations = permanent ban\n"
        f"🚫 No spam / adult / scam content\n\n"
        f"📢 Browse posts → {PROMO_CHANNEL_LINK}"
    )

def tpl_post(num: int, ptype: str, content: str) -> str:
    """Format a post for the promotion channel. Content is raw user text — no HTML escaping
    here because we send this WITHOUT parse_mode to avoid any formatting errors."""
    emoji = TYPE_EMOJI.get(ptype, "📋")
    divider = "─" * 24
    return (
        f"📌 POST #{num:04d}\n"
        f"📂 Type: {emoji} {ptype}\n"
        f"{divider}\n\n"
        f"{content}\n\n"
        f"{divider}\n"
        f"🚀 Promote FREE → {BOT_USERNAME}"
    )

def tpl_preview(ptype: str, content: str) -> str:
    """Preview shown to user in DM — use HTML, escape user content."""
    next_num = db["post_count"] + 1
    emoji    = TYPE_EMOJI.get(ptype, "📋")
    return (
        f"👁 <b>Preview — POST #{next_num:04d}</b>\n"
        f"📂 Type: {emoji} {ptype}\n"
        f"{'─'*24}\n\n"
        f"{h(content)}\n\n"
        f"{'─'*24}\n"
        f"🚀 Promote FREE → {h(BOT_USERNAME)}\n\n"
        f"<i>Does this look good? Hit Publish or Edit.</i>"
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

async def publish(uid: int, ptype: str, content: str, app: Application) -> int:
    """
    Publish post to promotion channel.
    Updates in-memory counters, saves to DB, logs audit record.
    Raises TelegramError if sending fails (caller must handle and rollback).
    """
    db["post_count"] += 1
    num      = db["post_count"]
    stat_key = STAT_KEY.get(ptype.lower(), "others")
    db[stat_key] += 1

    # Send WITHOUT parse_mode — user content may contain any characters
    text = tpl_post(num, ptype, content)
    try:
        await app.bot.send_message(
            chat_id=PROMO_CHANNEL_ID,
            text=text,
            # No parse_mode — plain text, 100% safe with any user content
        )
    except TelegramError as e:
        # Rollback counters before re-raising
        db["post_count"] -= 1
        db[stat_key]     -= 1
        raise e

    # Record rate-limit timestamp
    _rate[uid].append(time.time())

    # Persist and audit (non-critical — don't raise on failure)
    await db_log(app, num, uid, ptype)
    await db_save(app)
    return num

# ═══════════════════════════════════════════════════════════════════════════════
# WARNINGS / BAN / UNBAN
# ═══════════════════════════════════════════════════════════════════════════════

async def add_warning(uid: int, app: Application) -> int:
    """Increment warning count. Auto-bans at MAX_WARNINGS. Returns new count."""
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
# SAFE SEND WRAPPERS  — never crash on Telegram API errors
# ═══════════════════════════════════════════════════════════════════════════════

async def send(update: Update, text: str, **kwargs) -> None:
    """Safe reply — logs but never raises."""
    try:
        await update.message.reply_text(text, **kwargs)
    except TelegramError as e:
        log.warning(f"send() failed: {e}")

async def edit(query, text: str, **kwargs) -> None:
    """Safe edit — logs but never raises."""
    try:
        await query.edit_message_text(text, **kwargs)
    except TelegramError as e:
        log.warning(f"edit() failed: {e}")

async def notify(bot, uid: int, text: str, **kwargs) -> None:
    """Send a DM to a user — silently skip if blocked/not started."""
    try:
        await bot.send_message(chat_id=uid, text=text, **kwargs)
    except (Forbidden, BadRequest, TelegramError) as e:
        log.debug(f"notify uid={uid} failed: {e}")

# ═══════════════════════════════════════════════════════════════════════════════
# POST SUBMISSION FLOW HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

async def begin_post(message, uid: int, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Rate-limit check then start the submission session."""
    ok, wait = rate_ok(uid)
    if not ok:
        await message.reply_text(
            f"⏳ <b>Posting limit reached.</b>\n\n"
            f"You can post <b>{POSTS_PER_HOUR}</b> time(s) per hour.\n"
            f"Please wait <b>{fmt_wait(wait)}</b> and try again.",
            parse_mode="HTML",
        )
        return
    _sessions[uid] = {"step": "wait_content"}
    await message.reply_text(
        "📝 <b>Submit Your Promotion</b>\n\n"
        "Send your promotion message now.\n"
        "Include your description and link.\n\n"
        "<i>Send /cancel to abort.</i>",
        parse_mode="HTML",
    )

async def process_content(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Handle user's promotion content — validate, detect type, show preview or type picker."""
    uid = update.effective_user.id

    if not text.strip():
        await send(update, "⚠️ Empty message. Please send your promotion text:", parse_mode="HTML")
        return

    if has_bad_content(text):
        await issue_warning(update, ctx, uid)
        return

    _sessions[uid]["content"] = text

    ptype = await detect_type(text, ctx.bot)
    if ptype:
        _sessions[uid]["ptype"] = ptype
        _sessions[uid]["step"]  = "confirm"
        await update.message.reply_text(
            tpl_preview(ptype, text),
            parse_mode="HTML",
            reply_markup=kb_confirm(),
        )
    else:
        _sessions[uid]["step"] = "wait_type"
        await update.message.reply_text(
            "📂 <b>Select your promotion type:</b>",
            parse_mode="HTML",
            reply_markup=kb_type(),
        )

async def issue_warning(update: Update, ctx: ContextTypes.DEFAULT_TYPE, uid: int) -> None:
    """Warn user for bad content. Auto-ban at MAX_WARNINGS."""
    count = await add_warning(uid, ctx.application)
    if is_banned(uid):
        _sessions.pop(uid, None)
        await send(
            update,
            f"🚫 <b>Permanently banned</b> after {MAX_WARNINGS} violations.\n"
            f"Inappropriate content is not tolerated.",
            parse_mode="HTML",
        )
    else:
        left = MAX_WARNINGS - count
        await send(
            update,
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

    # /start is DM only
    if chat.type != ChatType.PRIVATE:
        return

    if is_banned(user.id):
        await send(update, "🚫 You are banned from PromoteHub.", parse_mode="HTML")
        return

    # Deep-link: /start post
    if ctx.args and ctx.args[0] == "post":
        joined = await is_joined(user.id, ctx.bot)
        if not joined:
            await send(update,
                "⚠️ Please join our channel and group first:",
                parse_mode="HTML", reply_markup=kb_join())
            return
        await begin_post(update.message, user.id, ctx)
        return

    # Force-join check
    joined = await is_joined(user.id, ctx.bot)
    if not joined:
        await send(update,
            "👋 <b>Welcome to PromoteHub!</b>\n\n"
            "Please join our channel and group first to use the bot:",
            parse_mode="HTML", reply_markup=kb_join())
        return

    await update.message.reply_text(
        tpl_home(user.first_name),
        parse_mode="HTML",
        reply_markup=kb_home(),
    )

# ═══════════════════════════════════════════════════════════════════════════════
# /post
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_post(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat

    if is_banned(user.id):
        await send(update, "🚫 You are banned from PromoteHub.", parse_mode="HTML")
        return

    # Enforce POST_ALLOWED_IN setting
    if not post_allowed_here(chat.type):
        if POST_ALLOWED_IN == "dm":
            bot_name = BOT_USERNAME.lstrip("@")
            await send(update,
                "📩 <b>Please submit promotions in private chat with the bot:</b>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📝 Open DM",
                        url=f"https://t.me/{bot_name}?start=post")
                ]]))
        elif POST_ALLOWED_IN == "group":
            await send(update,
                f"📩 Please use /post inside our group: {GROUP_LINK}",
                parse_mode="HTML")
        return

    # Force-join check (only needed in DM — group members are already "in")
    if chat.type == ChatType.PRIVATE:
        joined = await is_joined(user.id, ctx.bot)
        if not joined:
            await send(update,
                "⚠️ You must join our channel and group first:",
                parse_mode="HTML", reply_markup=kb_join())
            return

    await begin_post(update.message, user.id, ctx)

# ═══════════════════════════════════════════════════════════════════════════════
# /stats  /help  /cancel
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await send(update, tpl_stats(), parse_mode="HTML")

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await send(update, tpl_help(), parse_mode="HTML")

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if uid in _sessions:
        del _sessions[uid]
        await send(update, "❌ Cancelled. Use /post to start again.", parse_mode="HTML")
    else:
        await send(update, "Nothing to cancel.", parse_mode="HTML")

# ═══════════════════════════════════════════════════════════════════════════════
# MESSAGE HANDLER  — DM submission flow + group moderation
# ═══════════════════════════════════════════════════════════════════════════════

async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg  = update.message
    user = update.effective_user
    chat = update.effective_chat

    if not msg or not user or not msg.text:
        return

    text = msg.text.strip()

    # ── Group moderation ──────────────────────────────────────────────
    if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP) and chat.id == GROUP_ID:
        await moderate_group(update, ctx, text)
        return

    # ── DM only below ─────────────────────────────────────────────────
    if chat.type != ChatType.PRIVATE:
        return

    if is_banned(user.id):
        await send(update, "🚫 You are banned from PromoteHub.", parse_mode="HTML")
        return

    session = _sessions.get(user.id)
    if not session:
        await send(update, "💡 Use /post to submit a promotion.", parse_mode="HTML")
        return

    if session["step"] == "wait_content":
        await process_content(update, ctx, text)
    # wait_type step is handled entirely by inline button callbacks


async def moderate_group(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str
) -> None:
    """Delete bad messages from the group and warn/ban the sender."""
    uid = update.effective_user.id

    # Admins are immune
    if is_admin(uid):
        return

    # Silently delete messages from already-banned users
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
    q   = update.callback_query
    uid = q.from_user.id
    d   = q.data

    # Always answer the callback — prevents "loading" spinner in Telegram
    try:
        await q.answer()
    except TelegramError:
        pass

    # ── Verify join ───────────────────────────────────────────────────
    if d == "check_join":
        joined = await is_joined(uid, ctx.bot)
        if joined:
            await edit(q,
                f"✅ <b>Verified!</b> You're all set.\n\n"
                f"📊 <b>{db['post_count']}</b> promotions published!",
                parse_mode="HTML", reply_markup=kb_home())
        else:
            await edit(q,
                "❌ You haven't joined yet.\n\nPlease join <b>both</b> and try again:",
                parse_mode="HTML", reply_markup=kb_join())

    # ── Home ─────────────────────────────────────────────────────────
    elif d == "home":
        await edit(q,
            f"🏠 <b>PromoteHub</b> — {db['post_count']} promotions published!",
            parse_mode="HTML", reply_markup=kb_home())

    # ── Stats ─────────────────────────────────────────────────────────
    elif d == "stats":
        await edit(q, tpl_stats(), parse_mode="HTML", reply_markup=kb_back())

    # ── Help ──────────────────────────────────────────────────────────
    elif d == "help":
        await edit(q, tpl_help(), parse_mode="HTML", reply_markup=kb_back())

    # ── Start post (from home menu button) ────────────────────────────
    elif d == "start_post":
        if is_banned(uid):
            await edit(q, "🚫 You are banned from PromoteHub.", parse_mode="HTML")
            return
        ok, wait = rate_ok(uid)
        if not ok:
            await edit(q,
                f"⏳ <b>Posting limit reached.</b>\n\n"
                f"Please wait <b>{fmt_wait(wait)}</b> and try again.",
                parse_mode="HTML")
            return
        _sessions[uid] = {"step": "wait_content"}
        await edit(q,
            "📝 <b>Submit Your Promotion</b>\n\n"
            "Send your promotion message now.\n"
            "Include your description and link.\n\n"
            "<i>Send /cancel to abort.</i>",
            parse_mode="HTML")

    # ── Type selection (after user sends content) ─────────────────────
    elif d.startswith("type:"):
        session = _sessions.get(uid)
        if not session:
            await edit(q, "⚠️ Session expired. Please use /post to start again.", parse_mode="HTML")
            return
        ptype = d[5:]
        if ptype not in TYPE_EMOJI:
            await edit(q, "⚠️ Unknown type. Please use /post to start again.", parse_mode="HTML")
            return
        session["ptype"] = ptype
        session["step"]  = "confirm"
        content = session.get("content", "")
        await edit(q,
            tpl_preview(ptype, content),
            parse_mode="HTML",
            reply_markup=kb_confirm())

    # ── Publish ───────────────────────────────────────────────────────
    elif d == "publish":
        session = _sessions.get(uid)
        if not session or session.get("step") != "confirm":
            await edit(q, "⚠️ Session expired. Please use /post to start again.", parse_mode="HTML")
            return

        # Show immediate feedback
        await edit(q, "⏳ Publishing your post...", parse_mode="HTML")

        try:
            num = await publish(uid, session["ptype"], session["content"], ctx.application)
            _sessions.pop(uid, None)
            emoji = TYPE_EMOJI.get(session["ptype"], "📋")
            await edit(q,
                f"✅ <b>Post Published!</b>\n\n"
                f"📌 <b>POST #{num:04d}</b> is now live!\n"
                f"📂 Type: {emoji} {h(session['ptype'])}\n\n"
                f"🎉 Share the bot to help others promote too!\n"
                f"{h(BOT_USERNAME)}",
                parse_mode="HTML",
                reply_markup=kb_after_post())
        except TelegramError as e:
            log.error(f"Publish failed for uid={uid}: {e}")
            await edit(q,
                "❌ <b>Failed to publish.</b>\n\n"
                "Please try again with /post.",
                parse_mode="HTML")

    # ── Edit (go back to resend content) ─────────────────────────────
    elif d == "edit":
        if uid in _sessions:
            _sessions[uid]["step"] = "wait_content"
        else:
            _sessions[uid] = {"step": "wait_content"}
        await edit(q, "✏️ Send your updated promotion message:", parse_mode="HTML")

    # ── Cancel ────────────────────────────────────────────────────────
    elif d == "cancel":
        _sessions.pop(uid, None)
        await edit(q, "❌ Cancelled. Use /post to start again.", parse_mode="HTML")

# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════

def _get_target(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    """Extract target user ID from command args or replied-to message."""
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
    await send(update,
        f"🔧 <b>Admin Panel</b>\n\n"
        f"📌 Posts   : {db['post_count']}\n"
        f"🚫 Banned  : {len(db['banned'])}\n"
        f"⚠️ Warned  : {len(db['warnings'])}\n\n"
        f"<code>/ban &lt;id&gt;</code>          Ban a user\n"
        f"<code>/unban &lt;id&gt;</code>        Unban a user\n"
        f"<code>/warn &lt;id&gt;</code>         Add a warning\n"
        f"<code>/broadcast &lt;msg&gt;</code>   Post to channel\n"
        f"<code>/stats</code>               Live stats",
        parse_mode="HTML")

async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    uid = _get_target(update, ctx)
    if not uid:
        await send(update, "Usage: /ban &lt;user_id&gt;  or reply to a message.", parse_mode="HTML")
        return
    if uid in ADMIN_IDS:
        await send(update, "❌ Cannot ban an admin.", parse_mode="HTML")
        return
    await do_ban(uid, ctx.application)
    await send(update, f"✅ User <code>{uid}</code> banned.", parse_mode="HTML")
    await notify(ctx.bot, uid, "🚫 You have been banned from PromoteHub by an admin.")

async def cmd_unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    uid = _get_target(update, ctx)
    if not uid:
        await send(update, "Usage: /unban &lt;user_id&gt;", parse_mode="HTML")
        return
    await do_unban(uid, ctx.application)
    await send(update, f"✅ User <code>{uid}</code> unbanned.", parse_mode="HTML")
    await notify(ctx.bot, uid, "✅ You have been unbanned from PromoteHub. Use /start to continue.")

async def cmd_warn_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    uid = _get_target(update, ctx)
    if not uid:
        await send(update, "Usage: /warn &lt;user_id&gt;", parse_mode="HTML")
        return
    count = await add_warning(uid, ctx.application)
    if is_banned(uid):
        await send(update, f"⛔ User <code>{uid}</code> auto-banned (reached {MAX_WARNINGS} warnings).", parse_mode="HTML")
    else:
        await send(update, f"⚠️ User <code>{uid}</code> warned ({count}/{MAX_WARNINGS}).", parse_mode="HTML")

async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args:
        await send(update, "Usage: /broadcast &lt;message&gt;", parse_mode="HTML")
        return
    msg = " ".join(ctx.args)
    try:
        await ctx.bot.send_message(
            chat_id=PROMO_CHANNEL_ID,
            text=f"📢 Announcement\n\n{msg}",
            # No parse_mode — admin message may contain any characters
        )
        await send(update, "✅ Broadcast sent to channel.", parse_mode="HTML")
    except TelegramError as e:
        await send(update, f"❌ Failed: {h(str(e))}", parse_mode="HTML")

# ═══════════════════════════════════════════════════════════════════════════════
# GLOBAL ERROR HANDLER  — catches ALL unhandled exceptions, logs them, never crashes
# ═══════════════════════════════════════════════════════════════════════════════

async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.error(f"Unhandled exception: {ctx.error}", exc_info=ctx.error)
    # If we have a message context, tell the user something went wrong
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

# ═══════════════════════════════════════════════════════════════════════════════
# STARTUP  — load DB, set commands
# ═══════════════════════════════════════════════════════════════════════════════

async def on_startup(app: Application) -> None:
    log.info("PromoteHub starting up…")
    await db_load(app)
    await app.bot.set_my_commands([
        BotCommand("start",     "Home menu"),
        BotCommand("post",      "Submit a free promotion"),
        BotCommand("stats",     "Live statistics"),
        BotCommand("help",      "Help & rules"),
        BotCommand("cancel",    "Cancel current action"),
    ])
    log.info(
        f"✅ PromoteHub ready! "
        f"posts={db['post_count']} "
        f"POST_ALLOWED_IN={POST_ALLOWED_IN}"
    )

# ═══════════════════════════════════════════════════════════════════════════════
# KEEP-ALIVE HTTP SERVER  — UptimeRobot pings this to prevent Render sleep
# ═══════════════════════════════════════════════════════════════════════════════

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = (
            f"PromoteHub OK\n"
            f"posts={db['post_count']}\n"
            f"banned={len(db['banned'])}\n"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass  # suppress HTTP log noise


def start_http_server() -> None:
    server = HTTPServer(("0.0.0.0", PORT), _HealthHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    log.info(f"Keep-alive HTTP server started on port {PORT}")

# ═══════════════════════════════════════════════════════════════════════════════
# BUILD APP
# ═══════════════════════════════════════════════════════════════════════════════

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

    # Callbacks & messages
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    # Global error handler — catches everything that slips through
    app.add_error_handler(error_handler)

    return app

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN  — asyncio.run compatible with Python 3.10–3.14
# ═══════════════════════════════════════════════════════════════════════════════

async def main() -> None:
    start_http_server()
    app = build_app()

    async with app:
        await app.start()
        await app.updater.start_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
        log.info("Bot is live and polling.")

        # Wait for shutdown signal
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (ValueError, NotImplementedError):
                pass  # Windows doesn't support add_signal_handler

        await stop.wait()
        log.info("Shutdown signal received — stopping gracefully.")
        await app.updater.stop()
        await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
