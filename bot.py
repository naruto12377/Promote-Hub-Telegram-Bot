#!/usr/bin/env python3
"""
PromoteHub — Telegram Promotion Marketplace Bot
Simple. Organised. Error-free. Runs on Render free tier.
"""

import asyncio
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
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------
logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger("PromoteHub")

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
def _req(k):
    v = os.environ.get(k, "").strip()
    if not v:
        raise RuntimeError(f"Missing env var: {k}")
    return v

def _intlist(k):
    return [int(x) for x in os.getenv(k, "").split(",") if x.strip().lstrip("-").isdigit()]

def _strlist(k, default):
    return [x.strip().lower() for x in os.getenv(k, default).split(",") if x.strip()]

BOT_TOKEN          = _req("BOT_TOKEN")
BOT_USERNAME       = os.getenv("BOT_USERNAME", "@PromoteHubBot")
PROMO_CHANNEL_ID   = int(_req("PROMOTION_CHANNEL_ID"))
PROMO_CHANNEL_LINK = os.getenv("PROMOTION_CHANNEL_LINK", "")
GROUP_ID           = int(_req("GROUP_ID"))
GROUP_LINK         = os.getenv("GROUP_LINK", "")
DB_CHANNEL_ID      = int(_req("DATABASE_CHANNEL_ID"))
ADMIN_IDS          = _intlist("ADMIN_IDS")
PORT               = int(os.getenv("PORT", "8080"))
POSTS_PER_HOUR     = int(os.getenv("POSTS_PER_HOUR", "2"))
MAX_WARNINGS       = int(os.getenv("MAX_WARNINGS", "3"))
POST_ALLOWED_IN    = os.getenv("POST_ALLOWED_IN", "dm").lower()
BAD_WORDS          = _strlist("BAD_WORDS", "spam,scam,fake,adult,xxx,porn,crypto scam,ponzi,betting,gambling,hack,phishing")
BAD_LINKS          = _strlist("BAD_LINKS", "onlyfans.com,bit.ly/adult")

# ---------------------------------------------------------------------------
# STATE
# ---------------------------------------------------------------------------
db = {
    "post_count": 0,
    "channels": 0, "groups": 0, "services": 0,
    "referrals": 0, "links": 0, "others": 0,
    "banned": [],
    "warnings": {},
}
db_msg_id = None
rate_data = defaultdict(list)
sessions = {}

# ---------------------------------------------------------------------------
# PERSISTENCE
# ---------------------------------------------------------------------------
async def db_save(app):
    global db_msg_id
    text = "#PH_DB\n" + json.dumps(db, ensure_ascii=False)
    try:
        if db_msg_id:
            await app.bot.edit_message_text(chat_id=DB_CHANNEL_ID, message_id=db_msg_id, text=text)
        else:
            msg = await app.bot.send_message(chat_id=DB_CHANNEL_ID, text=text)
            db_msg_id = msg.message_id
            try:
                await app.bot.pin_chat_message(chat_id=DB_CHANNEL_ID, message_id=db_msg_id, disable_notification=True)
            except TelegramError:
                pass
    except TelegramError as e:
        log.error(f"db_save: {e}")

async def db_load(app):
    global db, db_msg_id
    try:
        chat = await app.bot.get_chat(DB_CHANNEL_ID)
        pinned = chat.pinned_message
        if pinned and pinned.text and pinned.text.startswith("#PH_DB"):
            loaded = json.loads(pinned.text.split("\n", 1)[1])
            db.update(loaded)
            db_msg_id = pinned.message_id
            log.info(f"State loaded: posts={db['post_count']} banned={len(db['banned'])}")
        else:
            log.info("No saved state — starting fresh.")
    except Exception as e:
        log.error(f"db_load: {e}")

async def db_log_post(app, num, uid, ptype):
    try:
        await app.bot.send_message(
            chat_id=DB_CHANNEL_ID,
            text=f"#POST id={num} user={uid} type={ptype.lower()} ts={int(time.time())}"
        )
    except TelegramError:
        pass

# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------
def is_banned(uid):   return uid in db["banned"]
def get_warns(uid):   return db["warnings"].get(str(uid), 0)
def is_admin(uid):    return uid in ADMIN_IDS

def has_bad(text):
    t = text.lower()
    return any(w in t for w in BAD_WORDS) or any(l in t for l in BAD_LINKS)

def rate_ok(uid):
    now = time.time()
    clean = [t for t in rate_data[uid] if now - t < 3600]
    rate_data[uid] = clean
    if len(clean) >= POSTS_PER_HOUR:
        return False, 3600 - (now - clean[0])
    return True, 0.0

def fmt_wait(s):
    m, s = divmod(int(s), 60)
    return f"{m}m {s}s" if m else f"{s}s"

def post_allowed_here(chat_type):
    if POST_ALLOWED_IN == "both": return True
    if POST_ALLOWED_IN == "dm":   return chat_type == ChatType.PRIVATE
    if POST_ALLOWED_IN == "group": return chat_type in (ChatType.GROUP, ChatType.SUPERGROUP)
    return False

def extract_tme(text):
    m = re.search(r"(?:t\.me|telegram\.me)/([A-Za-z0-9_]{4,})", text)
    if m and m.group(1).lower() not in ("joinchat","share","addstickers","boost"):
        return m.group(1)
    return None

# ---------------------------------------------------------------------------
# POST TYPE
# ---------------------------------------------------------------------------
TYPE_EMOJI = {"Channel":"📢","Group":"👥","Service":"🛠️","Referral":"🔗","Link":"🌐","Other":"📋"}
STAT_KEY   = {"channel":"channels","group":"groups","service":"services","referral":"referrals","link":"links","other":"others"}

async def detect_type(text, bot):
    if re.search(r"t\.me/\+|t\.me/joinchat", text):
        return "Group"
    username = extract_tme(text)
    if username:
        try:
            chat = await bot.get_chat(f"@{username}")
            if chat.type == ChatType.CHANNEL: return "Channel"
            if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP): return "Group"
        except TelegramError:
            pass
        return "Link"
    if re.search(r"https?://", text):
        return "Link"
    return None

def type_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Channel",  callback_data="type:Channel"),
         InlineKeyboardButton("👥 Group",    callback_data="type:Group")],
        [InlineKeyboardButton("🛠️ Service",  callback_data="type:Service"),
         InlineKeyboardButton("🔗 Referral", callback_data="type:Referral")],
        [InlineKeyboardButton("🌐 Link",     callback_data="type:Link"),
         InlineKeyboardButton("📋 Other",    callback_data="type:Other")],
        [InlineKeyboardButton("❌ Cancel",   callback_data="cancel")],
    ])

def confirm_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Publish",  callback_data="publish"),
         InlineKeyboardButton("✏️ Edit",    callback_data="edit")],
        [InlineKeyboardButton("❌ Cancel",   callback_data="cancel")],
    ])

# ---------------------------------------------------------------------------
# FORCE-JOIN
# ---------------------------------------------------------------------------
async def is_joined(uid, bot):
    for cid in (PROMO_CHANNEL_ID, GROUP_ID):
        try:
            m = await bot.get_chat_member(chat_id=cid, user_id=uid)
            if m.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
                return False
        except TelegramError:
            return False
    return True

def join_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Join Channel", url=PROMO_CHANNEL_LINK)],
        [InlineKeyboardButton("💬 Join Group",   url=GROUP_LINK)],
        [InlineKeyboardButton("✅ I Joined — Verify", callback_data="check_join")],
    ])

# ---------------------------------------------------------------------------
# POST FORMATTING
# ---------------------------------------------------------------------------
def _escape(text):
    """Escape special chars for MarkdownV2."""
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text

def post_text(num, ptype, content):
    e = TYPE_EMOJI.get(ptype, "📋")
    return (
        f"📌 *POST #{num:04d}*\n"
        f"📂 Type: {e} {ptype}\n"
        f"{'─'*22}\n\n"
        f"{content}\n\n"
        f"{'─'*22}\n"
        f"🚀 Promote FREE → {BOT_USERNAME}"
    )

def preview_text(ptype, content):
    next_num = db["post_count"] + 1
    e = TYPE_EMOJI.get(ptype, "📋")
    return (
        f"👁 *Preview — POST #{next_num:04d}*\n"
        f"📂 Type: {e} {ptype}\n"
        f"{'─'*22}\n\n"
        f"{content}\n\n"
        f"{'─'*22}\n"
        f"🚀 Promote FREE → {BOT_USERNAME}"
    )

# ---------------------------------------------------------------------------
# PUBLISH
# ---------------------------------------------------------------------------
async def do_publish(uid, ptype, content, app):
    db["post_count"] += 1
    num = db["post_count"]
    sk  = STAT_KEY.get(ptype.lower(), "others")
    db[sk] += 1
    text = post_text(num, ptype, content)
    try:
        await app.bot.send_message(chat_id=PROMO_CHANNEL_ID, text=text, parse_mode="Markdown")
    except TelegramError:
        plain = re.sub(r"[*_`]", "", text)
        await app.bot.send_message(chat_id=PROMO_CHANNEL_ID, text=plain)
    rate_data[uid].append(time.time())
    await db_log_post(app, num, uid, ptype)
    await db_save(app)
    return num

# ---------------------------------------------------------------------------
# WARNINGS / BAN
# ---------------------------------------------------------------------------
async def add_warning(uid, app):
    count = get_warns(uid) + 1
    db["warnings"][str(uid)] = count
    if count >= MAX_WARNINGS and uid not in db["banned"]:
        db["banned"].append(uid)
        try:
            await app.bot.restrict_chat_member(
                chat_id=GROUP_ID, user_id=uid,
                permissions=ChatPermissions(can_send_messages=False),
            )
        except TelegramError:
            pass
    await db_save(app)
    return count

async def do_ban(uid, app):
    if uid not in db["banned"]: db["banned"].append(uid)
    db["warnings"][str(uid)] = MAX_WARNINGS
    await db_save(app)
    try:
        await app.bot.restrict_chat_member(
            chat_id=GROUP_ID, user_id=uid,
            permissions=ChatPermissions(can_send_messages=False),
        )
    except TelegramError:
        pass

async def do_unban(uid, app):
    if uid in db["banned"]: db["banned"].remove(uid)
    db["warnings"].pop(str(uid), None)
    await db_save(app)
    try:
        await app.bot.restrict_chat_member(
            chat_id=GROUP_ID, user_id=uid,
            permissions=ChatPermissions(
                can_send_messages=True, can_send_media_messages=True,
                can_send_other_messages=True, can_add_web_page_previews=True,
            ),
        )
    except TelegramError:
        pass

# ---------------------------------------------------------------------------
# SAFE SEND WRAPPERS
# ---------------------------------------------------------------------------
async def sreply(update, text, **kw):
    try:
        await update.message.reply_text(text, **kw)
    except TelegramError as e:
        log.warning(f"reply failed: {e}")

async def sedit(query, text, **kw):
    try:
        await query.edit_message_text(text, **kw)
    except TelegramError as e:
        log.warning(f"edit failed: {e}")

# ---------------------------------------------------------------------------
# TEXT BUILDERS
# ---------------------------------------------------------------------------
def home_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Submit Promotion — FREE", callback_data="start_post")],
        [InlineKeyboardButton("📊 Stats", callback_data="stats"),
         InlineKeyboardButton("❓ Help",  callback_data="help")],
        [InlineKeyboardButton("📢 Browse Posts", url=PROMO_CHANNEL_LINK)],
    ])

def stats_msg():
    return (
        f"📊 *PromoteHub Stats*\n\n"
        f"📌 Total Posts : *{db['post_count']}*\n"
        f"📢 Channels   : *{db['channels']}*\n"
        f"👥 Groups     : *{db['groups']}*\n"
        f"🛠️ Services   : *{db['services']}*\n"
        f"🔗 Referrals  : *{db['referrals']}*\n"
        f"🌐 Links      : *{db['links']}*\n"
        f"📋 Others     : *{db['others']}*\n\n"
        f"🚀 Submit yours → {BOT_USERNAME}"
    )

def help_msg():
    return (
        f"❓ *PromoteHub Help*\n\n"
        f"*Commands*\n"
        f"/post — Submit a promotion\n"
        f"/stats — Live stats\n"
        f"/help — This message\n"
        f"/cancel — Cancel current action\n\n"
        f"*Allowed*\n"
        f"✅ Channels, groups, bots\n"
        f"✅ Services, websites, referrals\n\n"
        f"*Rules*\n"
        f"⏳ Max {POSTS_PER_HOUR} post(s) per hour\n"
        f"⚠️ {MAX_WARNINGS} violations = permanent ban\n"
        f"🚫 No spam / adult / scam content\n\n"
        f"📢 Browse → {PROMO_CHANNEL_LINK}"
    )

# ---------------------------------------------------------------------------
# COMMANDS
# ---------------------------------------------------------------------------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    if is_banned(user.id):
        await sreply(update, "🚫 You are banned from PromoteHub.")
        return
    joined = await is_joined(user.id, ctx.bot)
    if not joined:
        await sreply(update,
            "👋 *Welcome to PromoteHub!*\n\n"
            "Please join our channel and group first to use the bot:",
            parse_mode="Markdown", reply_markup=join_kb())
        return
    if ctx.args and ctx.args[0] == "post":
        await _begin_post(update.message, user.id, ctx)
        return
    await update.message.reply_text(
        f"👋 Hey *{user.first_name}!* Welcome to *PromoteHub* 🚀\n\n"
        f"The free Telegram promotion marketplace.\n"
        f"📊 *{db['post_count']}* promotions published!\n\n"
        f"What would you like to do?",
        parse_mode="Markdown", reply_markup=home_kb()
    )

async def cmd_post(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if is_banned(user.id):
        await sreply(update, "🚫 You are banned from PromoteHub.")
        return
    if not post_allowed_here(chat.type):
        if POST_ALLOWED_IN == "dm":
            await sreply(update, "📩 Please submit promotions in private chat:",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📝 Open DM",
                        url=f"https://t.me/{BOT_USERNAME.lstrip('@')}?start=post")
                ]]))
        else:
            await sreply(update, f"📩 Use /post inside our group: {GROUP_LINK}")
        return
    if chat.type == ChatType.PRIVATE:
        joined = await is_joined(user.id, ctx.bot)
        if not joined:
            await sreply(update, "⚠️ Join our channel and group first:",
                reply_markup=join_kb())
            return
    await _begin_post(update.message, user.id, ctx)

async def _begin_post(message, uid, ctx):
    ok, wait = rate_ok(uid)
    if not ok:
        await message.reply_text(
            f"⏳ *Posting limit reached.*\n\n"
            f"You can post *{POSTS_PER_HOUR}* time(s) per hour.\n"
            f"Please wait *{fmt_wait(wait)}*.",
            parse_mode="Markdown")
        return
    sessions[uid] = {"step": "wait_content"}
    await message.reply_text(
        "📝 *Submit Your Promotion*\n\n"
        "Send your promotion message now.\n"
        "Include your description and link.\n\n"
        "_/cancel to abort._",
        parse_mode="Markdown")

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await sreply(update, stats_msg(), parse_mode="Markdown")

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await sreply(update, help_msg(), parse_mode="Markdown")

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid in sessions:
        del sessions[uid]
        await sreply(update, "❌ Cancelled. Use /post to start again.")
    else:
        await sreply(update, "Nothing to cancel.")

# ---------------------------------------------------------------------------
# MESSAGE HANDLER
# ---------------------------------------------------------------------------
async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg  = update.message
    user = update.effective_user
    chat = update.effective_chat
    if not msg or not user or not msg.text:
        return
    text = msg.text.strip()

    # Group moderation
    if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP) and chat.id == GROUP_ID:
        await _moderate(update, ctx, text)
        return

    # DM only from here
    if chat.type != ChatType.PRIVATE:
        return
    if is_banned(user.id):
        await sreply(update, "🚫 You are banned from PromoteHub.")
        return

    session = sessions.get(user.id)
    if not session:
        await sreply(update, "💡 Use /post to submit a promotion.")
        return

    if session["step"] == "wait_content":
        await _recv_content(update, ctx, text)

async def _recv_content(update, ctx, text):
    uid = update.effective_user.id
    if not text:
        await sreply(update, "⚠️ Empty message. Send your promotion:")
        return
    if has_bad(text):
        await _issue_warning(update, ctx, uid)
        return
    sessions[uid]["content"] = text
    ptype = await detect_type(text, ctx.bot)
    if ptype:
        sessions[uid]["ptype"] = ptype
        sessions[uid]["step"]  = "confirm"
        await update.message.reply_text(
            preview_text(ptype, text),
            parse_mode="Markdown",
            reply_markup=confirm_kb(),
        )
    else:
        sessions[uid]["step"] = "wait_type"
        await update.message.reply_text(
            "📂 *Select the type of your promotion:*",
            parse_mode="Markdown",
            reply_markup=type_kb(),
        )

async def _issue_warning(update, ctx, uid):
    count = await add_warning(uid, ctx.application)
    if is_banned(uid):
        sessions.pop(uid, None)
        await sreply(update,
            f"🚫 *Permanently banned* after {MAX_WARNINGS} violations.\n"
            f"Inappropriate content is not allowed.",
            parse_mode="Markdown")
    else:
        left = MAX_WARNINGS - count
        await sreply(update,
            f"⚠️ *Warning {count}/{MAX_WARNINGS}:* Inappropriate content detected.\n"
            f"*{left}* warning(s) left before a permanent ban.\n\n"
            f"Please send appropriate content:",
            parse_mode="Markdown")

async def _moderate(update, ctx, text):
    uid = update.effective_user.id
    if is_admin(uid): return
    if is_banned(uid):
        try: await update.message.delete()
        except TelegramError: pass
        return
    if has_bad(text):
        try: await update.message.delete()
        except TelegramError: pass
        count = await add_warning(uid, ctx.application)
        mention = update.effective_user.mention_html()
        if is_banned(uid):
            try:
                await ctx.bot.send_message(GROUP_ID,
                    f"🚫 {mention} permanently banned after {MAX_WARNINGS} violations.",
                    parse_mode="HTML")
            except TelegramError: pass
        else:
            left = MAX_WARNINGS - count
            try:
                await ctx.bot.send_message(GROUP_ID,
                    f"⚠️ {mention} — message removed. <b>Warning {count}/{MAX_WARNINGS}</b> ({left} left).",
                    parse_mode="HTML")
            except TelegramError: pass

# ---------------------------------------------------------------------------
# CALLBACK HANDLER
# ---------------------------------------------------------------------------
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    uid = q.from_user.id
    d   = q.data
    await q.answer()

    if d == "check_join":
        if await is_joined(uid, ctx.bot):
            await sedit(q,
                f"✅ *Verified!* You're in.\n\n📊 {db['post_count']} promotions published!",
                parse_mode="Markdown", reply_markup=home_kb())
        else:
            await sedit(q,
                "❌ Not joined yet. Please join *both* and try again:",
                parse_mode="Markdown", reply_markup=join_kb())

    elif d == "start_post":
        if is_banned(uid):
            await sedit(q, "🚫 You are banned from PromoteHub.")
            return
        ok, wait = rate_ok(uid)
        if not ok:
            await sedit(q, f"⏳ Wait *{fmt_wait(wait)}* before posting again.", parse_mode="Markdown")
            return
        sessions[uid] = {"step": "wait_content"}
        await sedit(q,
            "📝 *Submit Your Promotion*\n\n"
            "Send your promotion message now.\n"
            "Include your description and link.\n\n"
            "_/cancel to abort._",
            parse_mode="Markdown")

    elif d.startswith("type:"):
        session = sessions.get(uid)
        if not session:
            await sedit(q, "⚠️ Session expired. Use /post to start again.")
            return
        ptype = d[5:]
        session["ptype"] = ptype
        session["step"]  = "confirm"
        await sedit(q, preview_text(ptype, session.get("content", "")),
            parse_mode="Markdown", reply_markup=confirm_kb())

    elif d == "publish":
        session = sessions.get(uid)
        if not session or session.get("step") != "confirm":
            await sedit(q, "⚠️ Session expired. Use /post to start again.")
            return
        await sedit(q, "⏳ Publishing your post...")
        try:
            num = await do_publish(uid, session["ptype"], session["content"], ctx.application)
            sessions.pop(uid, None)
            await sedit(q,
                f"✅ *Post Published — #{num:04d}!*\n\n"
                f"📂 Type: {TYPE_EMOJI.get(session['ptype'],'')} {session['ptype']}\n\n"
                f"🎉 Share the bot to grow the community!\n{BOT_USERNAME}",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📢 View Post", url=PROMO_CHANNEL_LINK)],
                    [InlineKeyboardButton("📝 Post Another", callback_data="start_post")],
                ]))
        except TelegramError as e:
            log.error(f"Publish error uid={uid}: {e}")
            # rollback counters
            db["post_count"] = max(0, db["post_count"] - 1)
            sk = STAT_KEY.get(session["ptype"].lower(), "others")
            db[sk] = max(0, db[sk] - 1)
            await sedit(q, "❌ Failed to publish. Please try again with /post.")

    elif d == "edit":
        if uid in sessions:
            sessions[uid]["step"] = "wait_content"
        await sedit(q, "✏️ Send your updated promotion message:")

    elif d == "cancel":
        sessions.pop(uid, None)
        await sedit(q, "❌ Cancelled. Use /post to start again.")

    elif d == "stats":
        await sedit(q, stats_msg(), parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="home")]]))

    elif d == "help":
        await sedit(q, help_msg(), parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="home")]]))

    elif d == "home":
        await sedit(q,
            f"🏠 *PromoteHub* — {db['post_count']} promotions published!",
            parse_mode="Markdown", reply_markup=home_kb())

# ---------------------------------------------------------------------------
# ADMIN COMMANDS
# ---------------------------------------------------------------------------
def _target(update, ctx):
    if ctx.args:
        try: return int(ctx.args[0])
        except ValueError: return None
    m = update.message
    if m.reply_to_message and m.reply_to_message.from_user:
        return m.reply_to_message.from_user.id
    return None

async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    await sreply(update,
        f"🔧 *Admin Panel*\n\n"
        f"📌 Posts   : {db['post_count']}\n"
        f"🚫 Banned  : {len(db['banned'])}\n"
        f"⚠️ Warned  : {len(db['warnings'])}\n\n"
        "`/ban <id>` — Ban user\n"
        "`/unban <id>` — Unban user\n"
        "`/warn <id>` — Warn user\n"
        "`/broadcast <msg>` — Post to channel\n"
        "`/stats` — Live stats",
        parse_mode="Markdown")

async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    uid = _target(update, ctx)
    if not uid:
        await sreply(update, "Usage: /ban <user_id>"); return
    if uid in ADMIN_IDS:
        await sreply(update, "❌ Cannot ban an admin."); return
    await do_ban(uid, ctx.application)
    await sreply(update, f"✅ User `{uid}` banned.", parse_mode="Markdown")
    try: await ctx.bot.send_message(uid, "🚫 You have been banned from PromoteHub.")
    except TelegramError: pass

async def cmd_unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    uid = _target(update, ctx)
    if not uid:
        await sreply(update, "Usage: /unban <user_id>"); return
    await do_unban(uid, ctx.application)
    await sreply(update, f"✅ User `{uid}` unbanned.", parse_mode="Markdown")
    try: await ctx.bot.send_message(uid, "✅ Unbanned. Use /start to continue.")
    except TelegramError: pass

async def cmd_warn_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    uid = _target(update, ctx)
    if not uid:
        await sreply(update, "Usage: /warn <user_id>"); return
    count = await add_warning(uid, ctx.application)
    status = "banned" if is_banned(uid) else f"{count}/{MAX_WARNINGS}"
    await sreply(update, f"⚠️ User `{uid}` warned — {status}.", parse_mode="Markdown")

async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if not ctx.args:
        await sreply(update, "Usage: /broadcast <message>"); return
    msg = " ".join(ctx.args)
    try:
        await ctx.bot.send_message(PROMO_CHANNEL_ID, f"📢 *Announcement*\n\n{msg}", parse_mode="Markdown")
        await sreply(update, "✅ Broadcast sent.")
    except TelegramError as e:
        await sreply(update, f"❌ Failed: {e}")

# ---------------------------------------------------------------------------
# STARTUP
# ---------------------------------------------------------------------------
async def on_startup(app):
    log.info("PromoteHub starting…")
    await db_load(app)
    await app.bot.set_my_commands([
        BotCommand("start",   "Home menu"),
        BotCommand("post",    "Submit a free promotion"),
        BotCommand("stats",   "Live statistics"),
        BotCommand("help",    "Help & rules"),
        BotCommand("cancel",  "Cancel current action"),
    ])
    log.info("✅ PromoteHub ready!")

# ---------------------------------------------------------------------------
# KEEP-ALIVE
# ---------------------------------------------------------------------------
class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(f"OK | posts={db['post_count']}\n".encode())
    def log_message(self, *_): pass

def start_http():
    threading.Thread(
        target=HTTPServer(("0.0.0.0", PORT), _Health).serve_forever,
        daemon=True
    ).start()
    log.info(f"HTTP keep-alive on port {PORT}")

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def build_app():
    app = Application.builder().token(BOT_TOKEN).post_init(on_startup).build()
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("post",      cmd_post))
    app.add_handler(CommandHandler("stats",     cmd_stats))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("cancel",    cmd_cancel))
    app.add_handler(CommandHandler("admin",     cmd_admin))
    app.add_handler(CommandHandler("ban",       cmd_ban))
    app.add_handler(CommandHandler("unban",     cmd_unban))
    app.add_handler(CommandHandler("warn",      cmd_warn_admin))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    return app

async def main():
    start_http()
    app = build_app()
    async with app:
        await app.start()
        await app.updater.start_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
        log.info("Bot is live.")
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try: loop.add_signal_handler(sig, stop.set)
            except (ValueError, NotImplementedError): pass
        await stop.wait()
        log.info("Shutting down…")
        await app.updater.stop()
        await app.stop()

if __name__ == "__main__":
    asyncio.run(main())
