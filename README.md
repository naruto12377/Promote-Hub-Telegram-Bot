# 🚀 PromoteHub — Telegram Promotion Marketplace Bot

> The free, viral, self-growing Telegram promotion bot.
> Deploy in 15 minutes. Zero database needed.

---

## ✨ Features

| Feature | Details |
|---|---|
| 📢 **Promotion Channel** | Auto-formatted posts with numbering |
| 💬 **Discussion Group** | Linked group with auto-moderation |
| 🤖 **Smart Bot** | Full submission flow with type detection |
| 🗄 **Telegram-as-DB** | No external database needed |
| 🔄 **Render Recovery** | Survives free-tier sleep cycles |
| 🛡 **Content Filter** | Bad words + bad links auto-blocked |
| ⚠️ **Warning System** | 3 strikes → permanent ban |
| 🚦 **Rate Limiting** | Configurable posts-per-hour per user |
| 👑 **Admin Panel** | Ban/unban/warn/broadcast commands |
| 🔗 **Force Join** | Users must join channel + group to use bot |
| 📈 **Live Stats** | Real-time post statistics |
| 🌐 **Viral Footer** | Every post promotes the bot automatically |

---

## 🛠 Setup Guide

### Step 1 — Create your 4 Telegram entities

1. **Promotion Channel** — Public channel where posts appear
   - Add your bot as **Admin** (can post messages)
2. **Discussion Group** — Public group linked to the channel
   - Add your bot as **Admin** (can delete messages, restrict users)
3. **Database Channel** — **Private** channel, only you and the bot
   - Add your bot as **Admin** (can post & edit messages, pin messages)
4. **The Bot** — Create via [@BotFather](https://t.me/BotFather)
   - `/newbot` → follow prompts → copy the token

### Step 2 — Get the Chat IDs

Forward any message from your channel/group to [@userinfobot](https://t.me/userinfobot).
It will show the chat ID (negative number like `-1001234567890`).

### Step 3 — Deploy to Render

1. Push this folder to a GitHub repository
2. Go to [render.com](https://render.com) → **New Web Service**
3. Connect your GitHub repo
4. Set **Start Command**: `python bot.py`
5. Add all environment variables (see below)

### Step 4 — Set Environment Variables on Render

| Variable | Value | Required |
|---|---|---|
| `BOT_TOKEN` | Token from @BotFather | ✅ |
| `BOT_USERNAME` | `@YourBotUsername` | ✅ |
| `PROMOTION_CHANNEL_ID` | `-100xxxxxxxxxx` | ✅ |
| `PROMOTION_CHANNEL_LINK` | `https://t.me/YourChannel` | ✅ |
| `GROUP_ID` | `-100xxxxxxxxxx` | ✅ |
| `GROUP_LINK` | `https://t.me/YourGroup` | ✅ |
| `DATABASE_CHANNEL_ID` | `-100xxxxxxxxxx` | ✅ |
| `ADMIN_IDS` | `123456789` (your user ID) | ✅ |
| `POSTS_PER_HOUR` | `2` | Default: 2 |
| `MAX_WARNINGS` | `3` | Default: 3 |
| `POST_ALLOWED_IN` | `dm` or `group` or `both` | Default: `dm` |
| `BAD_WORDS` | `spam,scam,fake,...` | Default set |
| `BAD_LINKS` | `onlyfans.com,...` | Default set |

### Step 5 — Keep the bot alive (free tier)

Render free tier sleeps after 15 minutes of inactivity.

1. Go to [uptimerobot.com](https://uptimerobot.com) — free account
2. Add a new **HTTP** monitor
3. URL: `https://your-render-app.onrender.com`
4. Interval: **5 minutes**

This pings your bot every 5 minutes → it never sleeps!

---

## 🎛 POST_ALLOWED_IN Options

| Value | Behaviour |
|---|---|
| `dm` | Users must DM the bot to submit a promotion (cleanest, recommended) |
| `group` | Users type `/post` in the group → bot sends them a DM button |
| `both` | `/post` works anywhere |

---

## 👑 Admin Commands

| Command | Action |
|---|---|
| `/admin` | Open admin panel with stats |
| `/ban <user_id>` | Ban a user (or reply to their message) |
| `/unban <user_id>` | Unban a user |
| `/warn <user_id>` | Add a warning to a user |
| `/broadcast <message>` | Post announcement to promotion channel |
| `/stats` | View live stats |

---

## 📊 Post Types

The bot auto-detects the type when a Telegram link is provided:

- **Channel** → Detected from `t.me/@username` (channel)
- **Group** → Detected from `t.me/@username` (group) or invite links
- **Service** / **Referral** / **Link** / **Other** → User selects via buttons

---

## 🔄 Data Persistence (Render Sleep Recovery)

All state is stored as a **pinned JSON message** in the private database channel.

When Render wakes the bot up after sleep:
1. Bot reads the pinned message in DATABASE_CHANNEL
2. Restores: post count, type stats, banned users, warning counts
3. Resumes as if nothing happened

**Zero data loss**, even without a paid database.

---

## 📁 File Structure

```
promotehub/
├── bot.py           ← Main bot (everything in one file)
├── requirements.txt ← Dependencies
├── render.yaml      ← Render deploy config
├── .env.example     ← Environment variables template
└── README.md        ← This file
```

---

## 🌱 Growth Strategy (Built-In Viral Loops)

Every published post contains:
```
🚀 Promote for FREE → @YourBotUsername
```

When users:
1. Share a post → new people see the footer → become new users
2. Forward posts → footer spreads to other groups/channels
3. Join the group → see other people promoting → want to promote too

This creates a **self-growing flywheel** with zero paid ads.

---

## 📜 License

MIT — free to use, modify, and deploy.
