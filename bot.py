#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════╗
║          PromoteHub — Telegram Promotion Marketplace     ║
║          Full-featured bot with viral growth loops       ║
╚══════════════════════════════════════════════════════════╝

Features:
  • Force-join gate (channel + group)
  • Smart post type auto-detection
  • Per-user hourly rate limits
  • Bad-word / bad-link content filter
  • Warning + auto-ban system
  • Group auto-moderation
  • Telegram-channel-as-database (survives Render sleep)
  • Admin panel (/ban /unban /broadcast /admin)
  • Built-in HTTP keep-alive server (for UptimeRobot)
  • POST_ALLOWED_IN env var: dm | group | both
"""

import os
import re
import json
import time
import logging
import asyncio
import threading
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ChatPermissions,
    BotCommand,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.error import TelegramError, BadRequest
from telegram.constants import ChatType, ChatMemberStatus

# ══════════════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════════════

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("PromoteHub")

# ══════════════════════════════════════════════════════════════════════
# ENVIRONMENT VARIABLES  (all configurable without touching code)
# ══════════════════════════════════════════════════════════════════════

BOT_TOKEN              = os.environ["BOT_TOKEN"]
BOT_USERNAME           = os.getenv("BOT_USERNAME", "@PromoteHubBot")
PROMOTION_CHANNEL_ID   = int(os.environ["PROMOTION_CHANNEL_ID"])
PROMOTION_CHANNEL_LINK = os.getenv("PROMOTION_CHANNEL_LINK", "")
GROUP_ID               = int(os.environ["GROUP_ID"])
GROUP_LINK             = os.getenv("GROUP_LINK", "")
DATABASE_CHANNEL_ID    = int(os.environ["DATABASE_CHANNEL_ID"])

POSTS_PER_HOUR  = int(os.getenv("POSTS_PER_HOUR", "2"))
MAX_WARNINGS    = int(os.getenv("MAX_WARNINGS", "3"))
ADMIN_IDS       = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
KEEP_ALIVE_PORT = int(os.getenv("PORT", "8080"))

BAD_WORDS = [
    w.strip().lower()
    for w in os.getenv("BAD_WORDS", "spam,scam,fake,adult,xxx,porn,crypto scam,ponzi,betting,gambling").split(",")
    if w.strip()
]
BAD_LINKS = [
    l.strip().lower()
    for l in os.getenv("BAD_LINKS", "onlyfans.com,bit.ly/adult").split(",")
    if l.strip()
]

# "dm"    → /post only works in bot DM
# "group" → /post only works in the group (button opens DM for submission)
# "both"  → /post works anywhere
POST_ALLOWED_IN = os.getenv("POST_ALLOWED_IN", "dm").lower()

# ══════════════════════════════════════════════════════════════════════
# IN-MEMORY STATE  (loaded from & saved to DATABASE_CHANNEL on every change)
# ══════════════════════════════════════════════════════════════════════

state: dict = {
    "total_posts": 0,
    "channels":    0,
    "groups":      0,
    "services":    0,
    "referrals":   0,
    "links":       0,
    "others":      0,
    "banned_users":  [],   # list[int]
    "user_warnings": {},   # {"user_id_str": int}
}

state_msg_id: int | None = None          # message_id of the pinned state in DB channel
user_post_times: dict[int, list] = defaultdict(list)   # rate-limiting
user_sessions:  dict[int, dict]  = {}                  # active submission states

# ══════════════════════════════════════════════════════════════════════
# DATABASE CHANNEL HELPERS
# ══════════════════════════════════════════════════════════════════════

async def save_state(app: Application) -> None:
    """Persist full state as JSON into the pinned DB-channel message."""
    global state_msg_id
    text = "#PROMOTEHUB_STATE\n" + json.dumps(state, ensure_ascii=False, indent=2)
    try:
        if state_msg_id:
            await app.bot.edit_message_text(
                chat_id=DATABASE_CHANNEL_ID,
                message_id=state_msg_id,
                text=text,
            )
        else:
            msg = await app.bot.send_message(chat_id=DATABASE_CHANNEL_ID, text=text)
            state_msg_id = msg.message_id
            try:
                await app.bot.pin_chat_message(
                    chat_id=DATABASE_CHANNEL_ID,
                    message_id=state_msg_id,
                    disable_notification=True,
                )
            except TelegramError:
                pass
    except TelegramError as e:
        logger.error(f"save_state failed: {e}")


async def load_state(app: Application) -> None:
    """On startup: restore state from pinned DB-channel message."""
    global state, state_msg_id
    try:
        chat = await app.bot.get_chat(DATABASE_CHANNEL_ID)
        pinned = chat.pinned_message
        if pinned and pinned.text and "#PROMOTEHUB_STATE" in pinned.text:
            raw = pinned.text.split("\n", 1)[1]
            loaded = json.loads(raw)
            state.update(loaded)
            state_msg_id = pinned.message_id
            logger.info(
                f"State restored ✅  posts={state['total_posts']}  "
                f"banned={len(state['banned_users'])}"
            )
        else:
            logger.info("No saved state found — starting fresh.")
    except Exception as e:
        logger.error(f"load_state failed: {e}")


async def log_post_record(app: Application, post_num: int, user_id: int, post_type: str) -> None:
    """Append a lightweight record to DB channel for audit purposes."""
    record = (
        f"#POST\n"
        f"post_id={post_num}\n"
        f"user_id={user_id}\n"
        f"type={post_type.lower()}\n"
        f"timestamp={int(time.time())}"
    )
    try:
        await app.bot.send_message(chat_id=DATABASE_CHANNEL_ID, text=record)
    except TelegramError:
        pass

# ══════════════════════════════════════════════════════════════════════
# UTILITY HELPERS
# ══════════════════════════════════════════════════════════════════════

def is_banned(user_id: int) -> bool:
    return user_id in state["banned_users"]

def get_warnings(user_id: int) -> int:
    return state["user_warnings"].get(str(user_id), 0)

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def contains_bad_content(text: str) -> bool:
    lower = text.lower()
    for word in BAD_WORDS:
        if word in lower:
            return True
    for link in BAD_LINKS:
        if link in lower:
            return True
    return False

def extract_tme_username(text: str) -> str | None:
    """Extract first @username from t.me links."""
    patterns = [
        r"t\.me/([A-Za-z0-9_]{3,})",
        r"telegram\.me/([A-Za-z0-9_]{3,})",
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            username = m.group(1)
            if username.lower() not in ("joinchat", "share", "addstickers"):
                return username
    return None

def check_rate_limit(user_id: int) -> tuple[bool, float]:
    """Returns (allowed, seconds_to_wait)."""
    now = time.time()
    times = [t for t in user_post_times[user_id] if now - t < 3600]
    user_post_times[user_id] = times
    if len(times) >= POSTS_PER_HOUR:
        wait = 3600 - (now - times[0])
        return False, wait
    return True, 0.0

def can_post_in_chat(chat_type: str) -> bool:
    if POST_ALLOWED_IN == "both":
        return True
    if POST_ALLOWED_IN == "dm":
        return chat_type == ChatType.PRIVATE
    if POST_ALLOWED_IN == "group":
        return chat_type in (ChatType.GROUP, ChatType.SUPERGROUP)
    return False

def format_wait(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    if m:
        return f"{m}m {s}s"
    return f"{s}s"

# ══════════════════════════════════════════════════════════════════════
# FORCE-JOIN CHECK
# ══════════════════════════════════════════════════════════════════════

async def is_member_of_both(user_id: int, bot) -> bool:
    for chat_id in (PROMOTION_CHANNEL_ID, GROUP_ID):
        try:
            member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
            if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
                return False
        except TelegramError:
            return False
    return True

def join_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Join Channel", url=PROMOTION_CHANNEL_LINK)],
        [InlineKeyboardButton("💬 Join Group",   url=GROUP_LINK)],
        [InlineKeyboardButton("✅ I've Joined — Verify", callback_data="verify_join")],
    ])

# ══════════════════════════════════════════════════════════════════════
# POST TYPE DETECTION
# ══════════════════════════════════════════════════════════════════════

POST_TYPES    = ["Channel", "Group", "Service", "Referral", "Link", "Other"]
TYPE_EMOJI    = {
    "Channel":  "📢",
    "Group":    "👥",
    "Service":  "🛠",
    "Referral": "🔗",
    "Link":     "🌐",
    "Other":    "📋",
}
TYPE_STAT_KEY = {
    "channel":  "channels",
    "group":    "groups",
    "service":  "services",
    "referral": "referrals",
    "link":     "links",
    "other":    "others",
}

async def detect_type(text: str, bot) -> str | None:
    """
    Try to auto-detect the promotion type.
    Returns a type string or None (user must pick manually).
    """
    username = extract_tme_username(text)
    if username:
        try:
            chat = await bot.get_chat(f"@{username}")
            if chat.type == ChatType.CHANNEL:
                return "Channel"
            elif chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
                return "Group"
        except TelegramError:
            pass
    # Invite links (joinchat / +xxxxx) → likely group
    if re.search(r"t\.me/\+|t\.me/joinchat", text):
        return "Group"
    return None

# ══════════════════════════════════════════════════════════════════════
# PUBLISHING
# ══════════════════════════════════════════════════════════════════════

async def publish_post(
    content: str,
    post_type: str,
    user_id: int,
    app: Application,
) -> int:
    """
    Format and publish a post to the promotion channel.
    Updates stats, records to DB channel, returns post number.
    """
    state["total_posts"] += 1
    num = state["total_posts"]
    stat_key = TYPE_STAT_KEY.get(post_type.lower(), "others")
    state[stat_key] += 1

    num_str   = str(num).zfill(4)
    emoji     = TYPE_EMOJI.get(post_type, "📋")

    post_text = (
        f"📌 *POST #{num_str}*\n"
        f"📂 *Type:* {emoji} {post_type}\n\n"
        f"{content}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🚀 Promote for FREE → {BOT_USERNAME}"
    )

    try:
        await app.bot.send_message(
            chat_id=PROMOTION_CHANNEL_ID,
            text=post_text,
            parse_mode="Markdown",
            disable_web_page_preview=False,
        )
    except TelegramError:
        # Fallback without markdown
        try:
            await app.bot.send_message(
                chat_id=PROMOTION_CHANNEL_ID,
                text=post_text.replace("*", "").replace("_", ""),
            )
        except TelegramError as e:
            # Rollback counters
            state["total_posts"] -= 1
            state[stat_key] -= 1
            raise e

    user_post_times[user_id].append(time.time())
    await log_post_record(app, num, user_id, post_type)
    await save_state(app)
    return num


def build_preview_text(content: str, post_type: str) -> str:
    num_str = str(state["total_posts"] + 1).zfill(4)
    emoji   = TYPE_EMOJI.get(post_type, "📋")
    return (
        f"👁 *Preview — POST #{num_str}*\n\n"
        f"📂 *Type:* {emoji} {post_type}\n\n"
        f"{content}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🚀 Promote for FREE → {BOT_USERNAME}"
    )

# ══════════════════════════════════════════════════════════════════════
# WARNING / BAN HELPERS
# ══════════════════════════════════════════════════════════════════════

async def add_warning(user_id: int, app: Application) -> int:
    w = get_warnings(user_id) + 1
    state["user_warnings"][str(user_id)] = w
    if w >= MAX_WARNINGS and user_id not in state["banned_users"]:
        state["banned_users"].append(user_id)
        try:
            await app.bot.restrict_chat_member(
                chat_id=GROUP_ID,
                user_id=user_id,
                permissions=ChatPermissions(can_send_messages=False),
            )
        except TelegramError:
            pass
    await save_state(app)
    return w


async def do_ban(user_id: int, app: Application) -> None:
    if user_id not in state["banned_users"]:
        state["banned_users"].append(user_id)
    state["user_warnings"][str(user_id)] = MAX_WARNINGS
    await save_state(app)
    try:
        await app.bot.restrict_chat_member(
            chat_id=GROUP_ID,
            user_id=user_id,
            permissions=ChatPermissions(can_send_messages=False),
        )
    except TelegramError:
        pass


async def do_unban(user_id: int, app: Application) -> None:
    if user_id in state["banned_users"]:
        state["banned_users"].remove(user_id)
    state["user_warnings"].pop(str(user_id), None)
    await save_state(app)
    try:
        await app.bot.restrict_chat_member(
            chat_id=GROUP_ID,
            user_id=user_id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
            ),
        )
    except TelegramError:
        pass

# ══════════════════════════════════════════════════════════════════════
# /start
# ══════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat

    if chat.type != ChatType.PRIVATE:
        return

    if is_banned(user.id):
        await update.message.reply_text(
            "🚫 You are banned from PromoteHub.\n"
            "Contact an admin if you believe this is a mistake."
        )
        return

    # Handle deep-link ?start=post
    if ctx.args and ctx.args[0] == "post":
        await _begin_post_flow(update, ctx)
        return

    joined = await is_member_of_both(user.id, ctx.bot)
    if not joined:
        await update.message.reply_text(
            "👋 Welcome to *PromoteHub* — the free Telegram promotion marketplace!\n\n"
            "📌 To use this bot you must join our channel and group first:",
            parse_mode="Markdown",
            reply_markup=join_keyboard(),
        )
        return

    await send_home(update.message, user)


async def send_home(message, user):
    text = (
        f"👋 Hey *{user.first_name}*! Welcome to *PromoteHub* 🚀\n\n"
        f"📣 The #1 *free* promotion marketplace on Telegram!\n\n"
        f"📊 *{state['total_posts']}* promotions published so far!\n\n"
        f"What would you like to do?"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Submit Promotion — FREE", callback_data="begin_post")],
        [
            InlineKeyboardButton("📊 Stats",              callback_data="show_stats"),
            InlineKeyboardButton("❓ Help",               callback_data="show_help"),
        ],
        [InlineKeyboardButton("📢 Browse Promotions", url=PROMOTION_CHANNEL_LINK)],
    ])
    await message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)

# ══════════════════════════════════════════════════════════════════════
# /post  (entry point)
# ══════════════════════════════════════════════════════════════════════

async def cmd_post(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat

    if is_banned(user.id):
        await update.message.reply_text("🚫 You are banned from using this bot.")
        return

    # Enforce POST_ALLOWED_IN setting
    if not can_post_in_chat(chat.type):
        if POST_ALLOWED_IN == "dm":
            deep = f"https://t.me/{BOT_USERNAME.lstrip('@')}?start=post"
            await update.message.reply_text(
                "📩 Promotions are submitted in private — click below:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📝 Submit in DM", url=deep)]
                ]),
            )
        elif POST_ALLOWED_IN == "group":
            await update.message.reply_text(
                f"📩 Please use /post inside our group: {GROUP_LINK}"
            )
        return

    # Force-join (only relevant in DM)
    if chat.type == ChatType.PRIVATE:
        joined = await is_member_of_both(user.id, ctx.bot)
        if not joined:
            await update.message.reply_text(
                "⚠️ You need to join our channel and group first:",
                reply_markup=join_keyboard(),
            )
            return

    await _begin_post_flow(update, ctx)


async def _begin_post_flow(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    allowed, wait = check_rate_limit(user.id)
    if not allowed:
        await update.message.reply_text(
            f"⏳ You've reached your posting limit (*{POSTS_PER_HOUR} posts/hour*).\n"
            f"Please wait *{format_wait(wait)}* before posting again.",
            parse_mode="Markdown",
        )
        return

    user_sessions[user.id] = {"step": "awaiting_content"}
    await update.message.reply_text(
        "📝 *Submit Your Promotion*\n\n"
        "Send your promotion message now.\n"
        "Include your description and link.\n\n"
        "💡 *Tips for maximum reach:*\n"
        "• Write a catchy 1-2 line description\n"
        "• Include your Telegram link\n"
        "• Keep it clean and clear\n\n"
        "Send /cancel to abort.",
        parse_mode="Markdown",
    )

# ══════════════════════════════════════════════════════════════════════
# /stats  /help  /cancel
# ══════════════════════════════════════════════════════════════════════

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(_stats_text(), parse_mode="Markdown")


def _stats_text() -> str:
    return (
        "📊 *PromoteHub Statistics*\n\n"
        f"📌 Total Posts : *{state['total_posts']}*\n"
        f"📢 Channels   : *{state['channels']}*\n"
        f"👥 Groups     : *{state['groups']}*\n"
        f"🛠 Services   : *{state['services']}*\n"
        f"🔗 Referrals  : *{state['referrals']}*\n"
        f"🌐 Links      : *{state['links']}*\n"
        f"📋 Others     : *{state['others']}*\n\n"
        f"🚀 Submit yours → {BOT_USERNAME}"
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(_help_text(), parse_mode="Markdown")


def _help_text() -> str:
    return (
        "❓ *PromoteHub Help*\n\n"
        "*Commands*\n"
        "/start  — Home menu\n"
        "/post   — Submit a free promotion\n"
        "/stats  — View live statistics\n"
        "/help   — Show this message\n"
        "/cancel — Cancel current action\n\n"
        "*What can I promote?*\n"
        "✅ Telegram channels & groups\n"
        "✅ Bots & services\n"
        "✅ Referral & affiliate links\n"
        "✅ Websites & tools\n\n"
        "*Rules*\n"
        "🚫 No spam / scam / adult content\n"
        f"⏳ Max *{POSTS_PER_HOUR}* posts per hour\n"
        f"⚠️  *{MAX_WARNINGS}* violations = permanent ban\n\n"
        f"📢 Browse promotions: {PROMOTION_CHANNEL_LINK}"
    )


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id in user_sessions:
        del user_sessions[update.effective_user.id]
        await update.message.reply_text("❌ Cancelled. Use /post to start again.")
    else:
        await update.message.reply_text("Nothing to cancel.")

# ══════════════════════════════════════════════════════════════════════
# MESSAGE HANDLER  (submission flow + group moderation)
# ══════════════════════════════════════════════════════════════════════

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    chat    = update.effective_chat
    message = update.message
    if not message or not user:
        return

    text = message.text or message.caption or ""

    # ── Group moderation ──────────────────────────────────────────────
    if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP) and chat.id == GROUP_ID:
        await _moderate_group(update, ctx, text)
        return

    # ── DM submission flow ────────────────────────────────────────────
    if chat.type != ChatType.PRIVATE:
        return

    if is_banned(user.id):
        return

    session = user_sessions.get(user.id)
    if not session:
        await update.message.reply_text(
            "💡 Use /post to submit a promotion, or /help for info."
        )
        return

    step = session["step"]

    if step == "awaiting_content":
        await _handle_content_submission(update, ctx, text, session)

    elif step == "awaiting_manual":
        await _handle_manual_resubmit(update, ctx, text, session)


async def _handle_content_submission(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str, session: dict
):
    user = update.effective_user

    if not text.strip():
        await update.message.reply_text("⚠️ Message is empty. Please send your promotion:")
        return

    # Content check
    if contains_bad_content(text):
        await _warn_user(update, ctx, user.id)
        return

    # Auto-detect type
    post_type = await detect_type(text, ctx.bot)

    session["content"] = text

    if post_type:
        session["post_type"] = post_type
        session["step"]      = "confirm"
        await _send_preview(update.message, session)
    else:
        session["step"] = "select_type"
        await update.message.reply_text(
            "📂 *Select Promotion Type*\n\nWhat kind of promotion is this?",
            parse_mode="Markdown",
            reply_markup=_type_keyboard(),
        )


async def _handle_manual_resubmit(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str, session: dict
):
    user = update.effective_user

    if contains_bad_content(text):
        await _warn_user(update, ctx, user.id)
        return

    session["content"] = text
    session["step"]    = "confirm"
    await _send_preview(update.message, session)


def _type_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📢 Channel",  callback_data="type:Channel"),
            InlineKeyboardButton("👥 Group",    callback_data="type:Group"),
        ],
        [
            InlineKeyboardButton("🛠 Service",  callback_data="type:Service"),
            InlineKeyboardButton("🔗 Referral", callback_data="type:Referral"),
        ],
        [
            InlineKeyboardButton("🌐 Link",     callback_data="type:Link"),
            InlineKeyboardButton("📋 Other",    callback_data="type:Other"),
        ],
        [InlineKeyboardButton("❌ Cancel",      callback_data="cancel_post")],
    ])


async def _send_preview(message, session: dict):
    content   = session["content"]
    post_type = session["post_type"]
    preview   = build_preview_text(content, post_type)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Publish Now!", callback_data="confirm_post"),
            InlineKeyboardButton("❌ Cancel",       callback_data="cancel_post"),
        ],
        [InlineKeyboardButton("✏️ Edit Message",   callback_data="edit_post")],
    ])
    await message.reply_text(preview, parse_mode="Markdown", reply_markup=keyboard)


async def _warn_user(update: Update, ctx: ContextTypes.DEFAULT_TYPE, user_id: int):
    w = await add_warning(user_id, ctx.application)
    if is_banned(user_id):
        user_sessions.pop(user_id, None)
        await update.message.reply_text(
            "🚫 *Inappropriate content detected.*\n\n"
            "You have been *permanently banned* from PromoteHub "
            "due to repeated violations.",
            parse_mode="Markdown",
        )
        return
    remaining = MAX_WARNINGS - w
    await update.message.reply_text(
        f"⚠️ *Warning {w}/{MAX_WARNINGS}:* Inappropriate content detected.\n"
        f"You have *{remaining}* warning(s) left before a permanent ban.\n\n"
        f"Please send appropriate content:",
        parse_mode="Markdown",
    )

# ══════════════════════════════════════════════════════════════════════
# GROUP MODERATION
# ══════════════════════════════════════════════════════════════════════

async def _moderate_group(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str):
    user    = update.effective_user
    message = update.message
    if not text or is_admin(user.id):
        return

    if is_banned(user.id):
        try:
            await message.delete()
        except TelegramError:
            pass
        return

    if contains_bad_content(text):
        try:
            await message.delete()
        except TelegramError:
            pass

        w = await add_warning(user.id, ctx.application)

        if is_banned(user.id):
            await ctx.bot.send_message(
                chat_id=GROUP_ID,
                text=(
                    f"🚫 {user.mention_html()} has been *permanently banned* "
                    f"from PromoteHub due to repeated violations."
                ),
                parse_mode="HTML",
            )
            try:
                await ctx.bot.send_message(
                    chat_id=user.id,
                    text="🚫 You have been banned from PromoteHub.",
                )
            except TelegramError:
                pass
        else:
            remaining = MAX_WARNINGS - w
            await ctx.bot.send_message(
                chat_id=GROUP_ID,
                text=(
                    f"⚠️ {user.mention_html()}, your message was removed.\n"
                    f"*Warning {w}/{MAX_WARNINGS}* — {remaining} left before ban."
                ),
                parse_mode="HTML",
            )

# ══════════════════════════════════════════════════════════════════════
# CALLBACK QUERY HANDLER
# ══════════════════════════════════════════════════════════════════════

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user  = query.from_user
    data  = query.data

    await query.answer()

    # ── Verify join ───────────────────────────────────────────────────
    if data == "verify_join":
        joined = await is_member_of_both(user.id, ctx.bot)
        if joined:
            await query.edit_message_text(
                "✅ Verified! You're all set.\n\nSending your home menu..."
            )
            await ctx.bot.send_message(
                chat_id=user.id,
                text=f"🎉 Welcome, *{user.first_name}*!\nUse /post to submit your first promotion.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📝 Submit Promotion — FREE", callback_data="begin_post")]
                ]),
            )
        else:
            await query.edit_message_text(
                "❌ You haven't joined yet!\n\nPlease join *both* and try again:",
                parse_mode="Markdown",
                reply_markup=join_keyboard(),
            )

    # ── Home / menu ───────────────────────────────────────────────────
    elif data in ("begin_post", "start_post"):
        if is_banned(user.id):
            await query.edit_message_text("🚫 You are banned.")
            return
        allowed, wait = check_rate_limit(user.id)
        if not allowed:
            await query.edit_message_text(
                f"⏳ Wait *{format_wait(wait)}* before posting again.",
                parse_mode="Markdown",
            )
            return
        user_sessions[user.id] = {"step": "awaiting_content"}
        await query.edit_message_text(
            "📝 *Submit Your Promotion*\n\n"
            "Send your promotion message now (text + link).\n\n"
            "Send /cancel to abort.",
            parse_mode="Markdown",
        )

    elif data == "show_stats":
        await query.edit_message_text(
            _stats_text(),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back", callback_data="go_home")
            ]]),
        )

    elif data == "show_help":
        await query.edit_message_text(
            _help_text(),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back", callback_data="go_home")
            ]]),
        )

    elif data == "go_home":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📝 Submit Promotion — FREE", callback_data="begin_post")],
            [
                InlineKeyboardButton("📊 Stats", callback_data="show_stats"),
                InlineKeyboardButton("❓ Help",  callback_data="show_help"),
            ],
            [InlineKeyboardButton("📢 Browse Promotions", url=PROMOTION_CHANNEL_LINK)],
        ])
        await query.edit_message_text(
            f"🏠 *PromoteHub Home*\n\n📊 {state['total_posts']} promotions published!\n\nWhat would you like to do?",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )

    # ── Type selection ────────────────────────────────────────────────
    elif data.startswith("type:"):
        post_type = data[5:]
        session   = user_sessions.get(user.id)
        if not session:
            await query.edit_message_text("⚠️ Session expired. Use /post to start again.")
            return

        session["post_type"] = post_type
        session["step"]      = "confirm"

        preview  = build_preview_text(session.get("content", ""), post_type)
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Publish Now!", callback_data="confirm_post"),
                InlineKeyboardButton("❌ Cancel",       callback_data="cancel_post"),
            ],
            [InlineKeyboardButton("✏️ Edit Message",   callback_data="edit_post")],
        ])
        await query.edit_message_text(preview, parse_mode="Markdown", reply_markup=keyboard)

    # ── Confirm publish ───────────────────────────────────────────────
    elif data == "confirm_post":
        session = user_sessions.get(user.id)
        if not session or session.get("step") != "confirm":
            await query.edit_message_text("⚠️ Session expired. Use /post to start again.")
            return

        await query.edit_message_text("⏳ Publishing your post...")

        try:
            num = await publish_post(
                session["content"],
                session["post_type"],
                user.id,
                ctx.application,
            )
            user_sessions.pop(user.id, None)
            num_str = str(num).zfill(4)

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📢 View Live Post", url=PROMOTION_CHANNEL_LINK)],
                [InlineKeyboardButton("📝 Post Another",   callback_data="begin_post")],
            ])
            await query.edit_message_text(
                f"✅ *Post Published!*\n\n"
                f"📌 *POST #{num_str}* is now live!\n"
                f"📂 Type: *{session['post_type']}*\n\n"
                f"🎉 Share the bot to help others promote too!\n"
                f"👇 {BOT_USERNAME}",
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
        except TelegramError as e:
            logger.error(f"Publish failed: {e}")
            await query.edit_message_text(
                "❌ Could not publish. Please try again with /post"
            )

    # ── Cancel / Edit ─────────────────────────────────────────────────
    elif data == "cancel_post":
        user_sessions.pop(user.id, None)
        await query.edit_message_text("❌ Cancelled. Use /post to start again.")

    elif data == "edit_post":
        session = user_sessions.get(user.id)
        if session:
            session["step"] = "awaiting_content"
        await query.edit_message_text(
            "✏️ Send your updated promotion message:"
        )

# ══════════════════════════════════════════════════════════════════════
# ADMIN COMMANDS
# ══════════════════════════════════════════════════════════════════════

async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admin only.")
        return
    text = (
        "🔧 *Admin Panel*\n\n"
        f"📌 Total Posts   : {state['total_posts']}\n"
        f"🚫 Banned Users  : {len(state['banned_users'])}\n"
        f"⚠️  Users Warned  : {len(state['user_warnings'])}\n\n"
        "*Commands:*\n"
        "`/ban <user_id>`         — Ban a user\n"
        "`/unban <user_id>`       — Unban a user\n"
        "`/warn <user_id>`        — Add a warning\n"
        "`/broadcast <message>`  — Broadcast to channel\n"
        "`/stats`                 — Live stats\n"
        "`/admin`                 — This panel"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    target_id = _resolve_target(update, ctx)
    if not target_id:
        await update.message.reply_text("Usage: /ban <user_id>  or reply to a message.")
        return

    if target_id in ADMIN_IDS:
        await update.message.reply_text("❌ Cannot ban an admin.")
        return

    await do_ban(target_id, ctx.application)
    await update.message.reply_text(f"✅ User `{target_id}` banned.", parse_mode="Markdown")
    try:
        await ctx.bot.send_message(chat_id=target_id, text="🚫 You have been banned from PromoteHub by an admin.")
    except TelegramError:
        pass


async def cmd_unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    target_id = _resolve_target(update, ctx)
    if not target_id:
        await update.message.reply_text("Usage: /unban <user_id>")
        return

    await do_unban(target_id, ctx.application)
    await update.message.reply_text(f"✅ User `{target_id}` unbanned.", parse_mode="Markdown")
    try:
        await ctx.bot.send_message(
            chat_id=target_id,
            text="✅ You've been unbanned from PromoteHub. Use /start to continue.",
        )
    except TelegramError:
        pass


async def cmd_warn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    target_id = _resolve_target(update, ctx)
    if not target_id:
        await update.message.reply_text("Usage: /warn <user_id>")
        return

    w = await add_warning(target_id, ctx.application)
    if is_banned(target_id):
        await update.message.reply_text(f"⛔ User `{target_id}` warned and auto-banned (reached max).", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"⚠️ User `{target_id}` warned ({w}/{MAX_WARNINGS}).", parse_mode="Markdown")


async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    if not ctx.args:
        await update.message.reply_text("Usage: /broadcast <your message>")
        return

    msg = " ".join(ctx.args)
    try:
        await ctx.bot.send_message(
            chat_id=PROMOTION_CHANNEL_ID,
            text=f"📢 *Announcement*\n\n{msg}",
            parse_mode="Markdown",
        )
        await update.message.reply_text("✅ Broadcast sent to channel.")
    except TelegramError as e:
        await update.message.reply_text(f"❌ Failed: {e}")


def _resolve_target(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int | None:
    if ctx.args:
        try:
            return int(ctx.args[0])
        except ValueError:
            return None
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        return update.message.reply_to_message.from_user.id
    return None

# ══════════════════════════════════════════════════════════════════════
# KEEP-ALIVE HTTP SERVER  (prevents Render free tier from sleeping)
# ══════════════════════════════════════════════════════════════════════

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        body = (
            f"PromoteHub is running ✅\n"
            f"Posts: {state['total_posts']}\n"
            f"Banned: {len(state['banned_users'])}\n"
        ).encode()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # Suppress HTTP log noise


def start_keep_alive():
    server = HTTPServer(("0.0.0.0", KEEP_ALIVE_PORT), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"Keep-alive HTTP server started on port {KEEP_ALIVE_PORT}")

# ══════════════════════════════════════════════════════════════════════
# STARTUP  &  MAIN
# ══════════════════════════════════════════════════════════════════════

async def on_startup(app: Application):
    logger.info("PromoteHub starting up…")
    await load_state(app)

    commands = [
        BotCommand("start",     "Home menu"),
        BotCommand("post",      "Submit a free promotion"),
        BotCommand("stats",     "Live statistics"),
        BotCommand("help",      "Help & rules"),
        BotCommand("cancel",    "Cancel current action"),
    ]
    await app.bot.set_my_commands(commands)
    logger.info("✅ PromoteHub is ready!")


def main():
    start_keep_alive()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(on_startup)
        .build()
    )

    # Commands
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("post",      cmd_post))
    app.add_handler(CommandHandler("stats",     cmd_stats))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("cancel",    cmd_cancel))
    app.add_handler(CommandHandler("admin",     cmd_admin))
    app.add_handler(CommandHandler("ban",       cmd_ban))
    app.add_handler(CommandHandler("unban",     cmd_unban))
    app.add_handler(CommandHandler("warn",      cmd_warn))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))

    # Callbacks
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Text messages (submission flow + group moderation)
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handle_message,
    ))

    logger.info("Starting polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
