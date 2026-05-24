#!/usr/bin/env python3
"""
All-in-One Telegram Bot
━━━━━━━━━━━━━━━━━━━━━━
① Keyword Filter (Movie/Cartoon Search)
   • User types keyword → bot shows ALL matches as inline buttons in ONE message
   • User clicks a button → bot sends photo + caption + download button privately
   • Not found → sends Sinhala not-found message

② File Share (Admin only)
   • Admin sends file → permanent share link
   • Single & bulk mode
   • Users must join channel, file deleted from PM after N hours
"""

import asyncio
import secrets
import logging
from datetime import datetime, timedelta

from motor.motor_asyncio import AsyncIOMotorClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatMemberUpdated
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ChatMemberHandler, ConversationHandler, filters, ContextTypes
)

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════

BOT_TOKEN          = "8720377838:AAFBfWJJFsaqA58idhX5SUO3EbLea2j9Kik"
BOT_USERNAME       = "sdc_lk_bot"

# ── Multiple Admins — add as many IDs as you want ─────────────
ADMIN_IDS = [
    6046457212,   # Admin 1
    # 987654321,  # Admin 2
    # 111111111,  # Admin 3
]

# ── Multiple Must-Join Channels ───────────────────────────────
# Users must be a member of ALL channels listed here to download files.
# Add @username or -100xxxxxxxxxx (private channel ID)
CHANNEL_IDS = [
    "@testsira",
    # "@yourchannel2",
    # -1001234567890,
]

REFRESH_CHANNEL    = -1002286319100
MONGODB_URI        = "mongodb+srv://pasindubhagya733:QN8odFqc2mz0Z7K4@cluster0.lhimv.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"
DB_NAME            = "RenameTg"
DELETE_AFTER_HOURS = 10
BULK_TIMEOUT       = 60

NOT_FOUND_MSG  = "සාමාවෙන්න, ඔයා හොයන Cartoon එකේ සිංහල Dubbed version එක අපි ළඟ දැනට නැහැ."
WELCOME_MSG    = "👋 ආයුබෝවන්! ඔයාට ඕන Movie/Cartoon එකේ නම type කරන්න, මම ඒක හොයලා දෙන්නම්! 😊🎬"

# ═══════════════════════════════════════════════════════════════
#  MONGODB
# ═══════════════════════════════════════════════════════════════

mongo_client = AsyncIOMotorClient(MONGODB_URI)
db           = mongo_client[DB_NAME]
col_files    = db["files"]
col_batches  = db["batches"]
col_pending  = db["pending_deletes"]
col_filters  = db["filters"]

async def ensure_indexes():
    await col_files.create_index([("token", 1)], unique=True)
    await col_batches.create_index([("token", 1)], unique=True)
    await col_pending.create_index([("delete_at", 1)])
    await col_filters.create_index([("keywords", 1)])
    log.info("✅ MongoDB indexes ready")

# ═══════════════════════════════════════════════════════════════
#  SHARED HELPERS
# ═══════════════════════════════════════════════════════════════

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

def make_token() -> str:
    return secrets.token_hex(16)

def delete_at() -> datetime:
    return datetime.utcnow() + timedelta(hours=DELETE_AFTER_HOURS)

def seconds_until(dt: datetime) -> float:
    return max(0.0, (dt - datetime.utcnow()).total_seconds())

def extract_file_info(msg) -> dict | None:
    if msg.document:
        return dict(file_id=msg.document.file_id,
                    file_unique_id=msg.document.file_unique_id,
                    file_name=msg.document.file_name or "file",
                    file_type="document")
    if msg.photo:
        p = msg.photo[-1]
        return dict(file_id=p.file_id, file_unique_id=p.file_unique_id,
                    file_name="photo.jpg", file_type="photo")
    if msg.video:
        return dict(file_id=msg.video.file_id,
                    file_unique_id=msg.video.file_unique_id,
                    file_name=msg.video.file_name or "video.mp4", file_type="video")
    if msg.audio:
        return dict(file_id=msg.audio.file_id,
                    file_unique_id=msg.audio.file_unique_id,
                    file_name=msg.audio.file_name or "audio.mp3", file_type="audio")
    if msg.voice:
        return dict(file_id=msg.voice.file_id,
                    file_unique_id=msg.voice.file_unique_id,
                    file_name="voice.ogg", file_type="voice")
    if msg.video_note:
        return dict(file_id=msg.video_note.file_id,
                    file_unique_id=msg.video_note.file_unique_id,
                    file_name="video_note.mp4", file_type="video_note")
    return None

async def send_one_file(bot, chat_id: int, record: dict):
    fid, ftype = record["file_id"], record["file_type"]
    cap   = record.get("caption")
    extra = {"caption": cap} if cap else {}
    if ftype == "photo":      return await bot.send_photo(chat_id, fid, **extra)
    if ftype == "video":      return await bot.send_video(chat_id, fid, **extra)
    if ftype == "audio":      return await bot.send_audio(chat_id, fid, **extra)
    if ftype == "voice":      return await bot.send_voice(chat_id, fid, **extra)
    if ftype == "video_note": return await bot.send_video_note(chat_id, fid)
    return await bot.send_document(chat_id, fid, **extra)

def build_keyboard(btn_rows: list) -> InlineKeyboardMarkup | None:
    if not btn_rows:
        return None
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(b["text"], url=b["url"]) for b in row]
        for row in btn_rows
    ])

def parse_buttons(text: str) -> list:
    rows = []
    for line in text.strip().splitlines():
        row = []
        for part in line.split("|"):
            part = part.strip()
            if " - " in part:
                label, url = part.split(" - ", 1)
                row.append({"text": label.strip(), "url": url.strip()})
        if row:
            rows.append(row)
    return rows

# ═══════════════════════════════════════════════════════════════
#  KEYWORD FILTER — search logic
# ═══════════════════════════════════════════════════════════════

async def find_all_matching_filters(text: str) -> list:
    """
    Return ALL filters that match user search text.
    Partial match both ways: 'open season' matches 'open season 2' and vice versa.
    """
    text_lower = text.lower().strip()
    if not text_lower:
        return []

    matched  = []
    seen_ids = set()

    async for f in col_filters.find({}):
        fid = str(f["_id"])
        if fid in seen_ids:
            continue
        for kw in f.get("keywords", []):
            kw_lower = kw.strip().lower()
            if kw_lower in text_lower or text_lower in kw_lower:
                matched.append(f)
                seen_ids.add(fid)
                break

    return matched

# ═══════════════════════════════════════════════════════════════
#  CALLBACK: user clicks a search result button
# ═══════════════════════════════════════════════════════════════

async def handle_filter_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    callback_data format: "filter:<user_id>:<filter_id>"
    - Only the original requester can click the button
    - Other users get a popup alert (no new message)
    - After click: edit original message to remove buttons, send result to requester only
    """
    from bson import ObjectId

    query  = update.callback_query
    clicker_id = query.from_user.id
    data   = query.data  # "filter:<user_id>:<filter_id>"

    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "filter":
        await query.answer("❌ Invalid.", show_alert=True)
        return

    requester_id = int(parts[1])
    filter_id    = parts[2]

    # ── Only the requester can click ──────────────────────────
    if clicker_id != requester_id:
        await query.answer(
            "🚫 මේ ඔයාගේ search result නෙමෙයි! ඔයාගේම keyword type කරන්න.",
            show_alert=True
        )
        return

    # ── Fetch filter ──────────────────────────────────────────
    try:
        oid = ObjectId(filter_id)
    except Exception:
        await query.answer("❌ Invalid filter.", show_alert=True)
        return

    f = await col_filters.find_one({"_id": oid})
    if not f:
        await query.answer("❌ Not found.", show_alert=True)
        return

    await query.answer()  # dismiss loading spinner

    # ── Edit original message: remove buttons, keep text ──────
    try:
        name = f.get("display_name") or f["keywords"][0].title()
        await query.edit_message_text(
            f"✅ <b>{name}</b>",
            parse_mode="HTML",
        )
    except Exception:
        pass  # message too old or already edited — ignore

    # ── Send result in the GROUP (reply to requester) ────────
    kb    = build_keyboard(f.get("buttons", []))
    photo = f.get("photo_id")
    cap   = f.get("caption")
    chat_id = query.message.chat_id

    try:
        if photo:
            await ctx.bot.send_photo(
                chat_id, photo,
                caption=cap, parse_mode="HTML", reply_markup=kb
            )
        elif cap:
            await ctx.bot.send_message(
                chat_id, cap,
                parse_mode="HTML", reply_markup=kb
            )
        elif kb:
            await ctx.bot.send_message(chat_id, "Here you go:", reply_markup=kb)
    except Exception as e:
        log.error("filter result send failed: %s", e)

# ═══════════════════════════════════════════════════════════════
#  FILE SHARE — file_id refresh
# ═══════════════════════════════════════════════════════════════

async def refresh_record(bot, record: dict, col) -> str | None:
    try:
        msg  = await send_one_file(bot, REFRESH_CHANNEL, record)
        info = extract_file_info(msg)
        if info:
            await col.update_one({"_id": record["_id"]}, {"$set": {"file_id": info["file_id"]}})
        try:
            await bot.delete_message(REFRESH_CHANNEL, msg.message_id)
        except Exception:
            pass
        return info["file_id"] if info else None
    except Exception as e:
        log.warning("refresh_record failed: %s", e)
        return None

async def refresh_all_on_startup(bot):
    count = 0
    async for r in col_files.find({}):
        await refresh_record(bot, r, col_files)
        count += 1
    async for batch in col_batches.find({}):
        new_files = []
        for f in batch.get("files", []):
            msg = None
            try:
                msg  = await send_one_file(bot, REFRESH_CHANNEL, f)
                info = extract_file_info(msg)
                if info:
                    f["file_id"] = info["file_id"]
                    count += 1
            except Exception as e:
                log.warning("batch refresh failed: %s", e)
            if msg:
                try:
                    await bot.delete_message(REFRESH_CHANNEL, msg.message_id)
                except Exception:
                    pass
            new_files.append(f)
        await col_batches.update_one({"_id": batch["_id"]}, {"$set": {"files": new_files}})
    if count:
        log.info("Startup: refreshed %d file_id(s)", count)

# ═══════════════════════════════════════════════════════════════
#  FILE SHARE — PM delete scheduler
# ═══════════════════════════════════════════════════════════════

async def schedule_delete(bot, chat_id, message_ids, notice_id, del_at):
    await col_pending.insert_one({
        "chat_id": chat_id, "message_ids": message_ids,
        "notice_msg_id": notice_id, "delete_at": del_at,
    })
    delay = seconds_until(del_at)
    asyncio.get_event_loop().call_later(delay, lambda: asyncio.create_task(
        do_delete(bot, chat_id, message_ids, notice_id)
    ))

async def do_delete(bot, chat_id, message_ids, notice_id):
    for mid in message_ids + [notice_id]:
        try:
            await bot.delete_message(chat_id, mid)
        except Exception:
            pass
    await col_pending.delete_one({"chat_id": chat_id, "message_ids": message_ids})

async def restore_pending_deletes(bot):
    count = 0
    async for row in col_pending.find({}):
        delay = seconds_until(row["delete_at"])
        mids  = row.get("message_ids") or [row.get("message_id")]
        asyncio.get_event_loop().call_later(delay, lambda r=row, m=mids: asyncio.create_task(
            do_delete(bot, r["chat_id"], m, r["notice_msg_id"])
        ))
        count += 1
    if count:
        log.info("Startup: rescheduled %d deletion(s)", count)

# ═══════════════════════════════════════════════════════════════
#  FILE SHARE — channel gate
# ═══════════════════════════════════════════════════════════════

async def check_membership(bot, user_id: int, channel) -> bool:
    """
    Check if user is a member of a single channel.
    Returns True if member, False if not or if check fails.
    Bot MUST be admin in the channel for this to work.
    """
    try:
        m = await bot.get_chat_member(channel, user_id)
        log.debug("check_membership %s in %s: %s", user_id, channel, m.status)
        return m.status in ("member", "administrator", "creator", "restricted")
        # Note: "restricted" means they ARE in the group but with limited permissions
        # "kicked" / "left" / "banned" means NOT a member
    except Exception as e:
        log.warning("check_membership failed for %s in %s: %s", user_id, channel, e)
        # If bot is not admin or channel not found, skip this channel check
        return True


async def is_member(bot, user_id: int) -> bool:
    """User must be a member of ALL channels in CHANNEL_IDS."""
    for channel in CHANNEL_IDS:
        if not await check_membership(bot, user_id, channel):
            return False
    return True


async def get_not_joined_channels(bot, user_id: int) -> list:
    """Return list of (channel, title) tuples the user has NOT joined yet."""
    not_joined = []
    for channel in CHANNEL_IDS:
        try:
            m = await bot.get_chat_member(channel, user_id)
            if m.status not in ("member", "administrator", "creator", "restricted"):
                # Get channel title for nice button text
                try:
                    chat = await bot.get_chat(channel)
                    title = chat.title or str(channel)
                except Exception:
                    title = str(channel)
                not_joined.append((channel, title))
        except Exception as e:
            log.warning("get_not_joined_channels failed for %s: %s", channel, e)
            # Could not check — skip (bot might not be admin)
    return not_joined

# ═══════════════════════════════════════════════════════════════
#  FILE SHARE — bulk session
# ═══════════════════════════════════════════════════════════════

bulk_sessions: dict[int, dict] = {}

async def bulk_finalize(bot, admin_id: int, chat_id: int):
    await asyncio.sleep(BULK_TIMEOUT)
    session = bulk_sessions.pop(admin_id, None)
    if not session or not session["files"]:
        return
    file_list = session["files"]
    token     = make_token()
    await col_batches.insert_one({
        "token": token, "files": file_list, "uploaded_by": admin_id,
        "uploaded_at": datetime.utcnow(), "batch_size": len(file_list),
    })
    link  = f"https://t.me/{BOT_USERNAME}?start=batch_{token}"
    kb    = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Open Link", url=link)]])
    names = "\n".join(f"  {i+1}. {f['file_name']}" for i, f in enumerate(file_list))
    await bot.send_message(
        chat_id,
        f"✅ <b>Bulk batch saved! ({len(file_list)} files)</b>\n\n"
        f"<b>Files:</b>\n{names}\n\n"
        f"🔗 <b>Share Link:</b>\n<code>{link}</code>\n\n"
        f"⏰ Files deleted from user PM after <b>{DELETE_AFTER_HOURS}h</b>",
        parse_mode="HTML", reply_markup=kb,
    )

# ═══════════════════════════════════════════════════════════════
#  WIZARD STATES
# ═══════════════════════════════════════════════════════════════

fw_wizard: dict[int, dict] = {}
(FW_KW, FW_IMG, FW_TEXT, FW_BTNS, FW_CONFIRM) = range(5)

async def fw_cmd_addfilter(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Admins only.")
        return ConversationHandler.END
    fw_wizard[update.effective_user.id] = {"data": {}}
    await update.message.reply_html(
        "🔧 <b>Add Filter — Step 1/4</b>\n\n"
        "Send <b>keyword(s)</b> separated by commas.\n"
        "Use the display title as users would search:\n"
        "<code>See Spot Run, see spot run 2001</code>\n\n"
        "/cancel to abort."
    )
    return FW_KW

async def fw_step_kw(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    kws  = [k.strip().lower() for k in update.message.text.split(",") if k.strip()]
    if not kws:
        await update.message.reply_text("❌ Send at least one keyword.")
        return FW_KW
    # Store first keyword as display name (title case)
    fw_wizard[uid]["data"]["keywords"]     = kws
    fw_wizard[uid]["data"]["display_name"] = update.message.text.split(",")[0].strip()
    await update.message.reply_html(
        f"✅ Keywords: <code>{', '.join(kws)}</code>\n\n"
        "🔧 <b>Step 2/4 — Image</b>\n\nSend a photo or type <code>skip</code>."
    )
    return FW_IMG

async def fw_step_img_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    fw_wizard[update.effective_user.id]["data"]["photo_id"] = update.message.photo[-1].file_id
    await update.message.reply_html(
        "✅ Image saved!\n\n"
        "🔧 <b>Step 3/4 — Caption</b>\n\n"
        "Send caption text (HTML ok) or <code>skip</code>.\n\n"
        "<b>Example:</b>\n"
        "<code>🎬 See Spot Run 2001\n⭐ Rating: 7/10\n🗣 Sinhala Dubbed</code>"
    )
    return FW_TEXT

async def fw_step_img_skip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.text.strip().lower() != "skip":
        await update.message.reply_text("Send a photo or type 'skip'.")
        return FW_IMG
    fw_wizard[update.effective_user.id]["data"]["photo_id"] = None
    await update.message.reply_html(
        "⏭️ No image.\n\n🔧 <b>Step 3/4 — Caption</b>\n\nSend text or <code>skip</code>."
    )
    return FW_TEXT

async def fw_step_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    text = update.message.text.strip()
    fw_wizard[uid]["data"]["caption"] = None if text.lower() == "skip" else text
    await update.message.reply_html(
        "✅ Caption saved!\n\n"
        "🔧 <b>Step 4/4 — Download Button</b>\n\n"
        "This button appears when user clicks the search result.\n\n"
        "<b>Format:</b>\n"
        "<code>📥 Download - https://t.me/yourbot?start=TOKEN</code>\n\n"
        "Multiple buttons:\n"
        "<code>▶️ Watch - https://link.com | 📥 Download - https://dl.com</code>\n\n"
        "Or <code>skip</code> for no buttons."
    )
    return FW_BTNS

async def fw_step_btns(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid      = update.effective_user.id
    text     = update.message.text.strip()
    btn_rows = [] if text.lower() == "skip" else parse_buttons(text)
    if text.lower() != "skip" and not btn_rows:
        await update.message.reply_text(
            "❌ Invalid format.\nUse: Label - https://url.com\nOr 'skip'."
        )
        return FW_BTNS
    fw_wizard[uid]["data"]["buttons"] = btn_rows
    data = fw_wizard[uid]["data"]
    kws  = ", ".join(data["keywords"])
    await update.message.reply_html(
        f"👀 <b>Preview</b>\n\n"
        f"📌 <b>Display name:</b> {data['display_name']}\n"
        f"🔑 <b>Keywords:</b> <code>{kws}</code>\n"
        f"🖼️ <b>Image:</b> {'Yes' if data.get('photo_id') else 'No'}\n"
        f"📝 <b>Caption:</b> {'Yes' if data.get('caption') else 'No'}\n"
        f"🔘 <b>Buttons:</b> {len(btn_rows)} row(s)\n\n"
        "Type <b>save</b> to confirm or <b>cancel</b> to abort."
    )
    if data.get("photo_id"):
        try:
            await update.message.reply_photo(
                data["photo_id"], caption=data.get("caption"),
                parse_mode="HTML", reply_markup=build_keyboard(btn_rows)
            )
        except Exception:
            pass
    return FW_CONFIRM

async def fw_step_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    text = update.message.text.strip().lower()
    if text == "save":
        data     = fw_wizard.pop(uid, {}).get("data", {})
        existing = await col_filters.find_one({"keywords": {"$in": data["keywords"]}})
        if existing:
            await col_filters.update_one({"_id": existing["_id"]}, {"$set": {
                "keywords":     data["keywords"],
                "display_name": data.get("display_name", data["keywords"][0]),
                "photo_id":     data.get("photo_id"),
                "caption":      data.get("caption"),
                "buttons":      data.get("buttons", []),
                "updated_at":   datetime.utcnow(),
            }})
            await update.message.reply_text("✅ Filter updated!")
        else:
            await col_filters.insert_one({
                "keywords":     data["keywords"],
                "display_name": data.get("display_name", data["keywords"][0]),
                "photo_id":     data.get("photo_id"),
                "caption":      data.get("caption"),
                "buttons":      data.get("buttons", []),
                "created_by":   uid,
                "created_at":   datetime.utcnow(),
            })
            await update.message.reply_text("✅ Filter saved!")
        return ConversationHandler.END
    elif text in ("cancel", "/cancel"):
        fw_wizard.pop(uid, None)
        await update.message.reply_text("❌ Cancelled.")
        return ConversationHandler.END
    await update.message.reply_text("Type 'save' to confirm or 'cancel' to abort.")
    return FW_CONFIRM

async def fw_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    fw_wizard.pop(update.effective_user.id, None)
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END

# ═══════════════════════════════════════════════════════════════
#  COMMANDS
# ═══════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Only handles file share deep-links. Silent for plain /start."""
    payload = ctx.args[0] if ctx.args else None
    if not payload:
        return  # silent — no message

    user = update.effective_user

    # Channel gate — check all channels
    not_joined = await get_not_joined_channels(ctx.bot, user.id)
    if not_joined:
        buttons = []
        for i, (ch, title) in enumerate(not_joined):
            if str(ch).startswith("@"):
                ch_link = f"https://t.me/{ch.lstrip('@')}"
            else:
                # Private channel: convert -100xxxxxxxxxx → https://t.me/c/xxxxxxxxxx
                ch_link = f"https://t.me/c/{str(ch).replace('-100', '')}"
            buttons.append([InlineKeyboardButton(f"📢 {title}", url=ch_link)])
        buttons.append([InlineKeyboardButton(
            "✅ I Joined All — Try Again",
            url=f"https://t.me/{BOT_USERNAME}?start={payload}"
        )])
        kb = InlineKeyboardMarkup(buttons)
        await update.message.reply_html(
            f"🔒 <b>Access Denied</b>\n\nYou must join <b>all {len(not_joined)}</b> channel(s) below first:",
            reply_markup=kb,
        )
        return

    # Bulk batch
    if payload.startswith("batch_"):
        token = payload[6:]
        batch = await col_batches.find_one({"token": token})
        if not batch:
            await update.message.reply_text("❌ Batch not found.")
            return
        await update.message.reply_html(f"📦 <b>Sending {batch['batch_size']} file(s)…</b>")
        sent_ids = []
        for f in batch["files"]:
            try:
                s = await send_one_file(ctx.bot, update.effective_chat.id, f)
                sent_ids.append(s.message_id)
            except Exception as e:
                if "400" in str(e) or "file_id" in str(e).lower():
                    try:
                        tmp  = await send_one_file(ctx.bot, REFRESH_CHANNEL, f)
                        info = extract_file_info(tmp)
                        if info:
                            f["file_id"] = info["file_id"]
                            s = await send_one_file(ctx.bot, update.effective_chat.id, f)
                            sent_ids.append(s.message_id)
                        try:
                            await ctx.bot.delete_message(REFRESH_CHANNEL, tmp.message_id)
                        except Exception:
                            pass
                    except Exception:
                        pass
        if sent_ids:
            nm = await update.message.reply_html(
                f"⚠️ <b>These {len(sent_ids)} file(s) will be deleted in {DELETE_AFTER_HOURS}h.</b> Save them!"
            )
            await schedule_delete(ctx.bot, update.effective_chat.id, sent_ids, nm.message_id, delete_at())
        return

    # Single file
    record = await col_files.find_one({"token": payload})
    if not record:
        await update.message.reply_text("❌ File not found.")
        return
    sent = None
    try:
        sent = await send_one_file(ctx.bot, update.effective_chat.id, record)
    except Exception as e:
        if "400" in str(e) or "file_id" in str(e).lower() or "wrong file" in str(e).lower():
            wm = await update.message.reply_text("🔄 Refreshing…")
            new_id = await refresh_record(ctx.bot, record, col_files)
            try:
                await wm.delete()
            except Exception:
                pass
            if new_id:
                record["file_id"] = new_id
                try:
                    sent = await send_one_file(ctx.bot, update.effective_chat.id, record)
                except Exception:
                    await update.message.reply_text("❌ Could not retrieve the file.")
                    return
            else:
                await update.message.reply_text("❌ Could not retrieve the file.")
                return
        else:
            raise
    nm = await update.message.reply_html(
        f"⚠️ <b>This file will be deleted in {DELETE_AFTER_HOURS}h.</b> Save it!"
    )
    await schedule_delete(ctx.bot, update.effective_chat.id, [sent.message_id], nm.message_id, delete_at())


async def cmd_ahelp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_html(
        "<b>🤖 Admin Help</b>\n\n"
        "<b>━━ 🔍 Filter (Movie Search) ━━</b>\n"
        "/addfilter — add keyword filter (wizard)\n"
        "/filters — list all filters\n"
        "/delfilter &lt;keyword&gt; — delete a filter\n\n"
        "<b>━━ 📁 File Share ━━</b>\n"
        "📤 Send any file → permanent share link\n"
        "/batch — start bulk session\n"
        "/endbatch — finish bulk &amp; get link\n"
        "/cancelbatch — abort bulk\n"
        "/myfiles — list all files &amp; batches\n"
        "/delfile &lt;token&gt; — delete file/batch\n"
        "/refreshfiles — refresh all file_ids\n\n"
        "<b>━━ General ━━</b>\n"
        f"/stats — statistics\n\n"
        f"⏰ PM files deleted after {DELETE_AFTER_HOURS}h · "
        f"⏱ Bulk auto-saves after {BULK_TIMEOUT}s\n\n"
        f"<b>━━ Admins & Channels ━━</b>\n"
        f"/myadmins — list all admins\n"
        f"/mychannels — list all must-join channels"
    )


async def cmd_batch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        return
    if user.id in bulk_sessions:
        n = len(bulk_sessions[user.id]["files"])
        await update.message.reply_html(f"⚠️ Active bulk session with <b>{n}</b> file(s). /endbatch to finish.")
        return
    bulk_sessions[user.id] = {"files": [], "task": None}
    await update.message.reply_html(
        f"📦 <b>Bulk mode ON!</b>\n\nSend files one by one.\n"
        f"• Auto-saves after <b>{BULK_TIMEOUT}s</b>\n"
        f"• /endbatch — finish now\n• /cancelbatch — abort"
    )

async def cmd_endbatch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        return
    session = bulk_sessions.get(user.id)
    if not session:
        await update.message.reply_text("❌ No active bulk session.")
        return
    n = len(session["files"])
    if n == 0:
        bulk_sessions.pop(user.id, None)
        await update.message.reply_text("❌ No files collected.")
        return
    if session.get("task"):
        session["task"].cancel()
    file_list = session["files"]
    bulk_sessions.pop(user.id, None)
    token = make_token()
    await col_batches.insert_one({
        "token": token, "files": file_list, "uploaded_by": user.id,
        "uploaded_at": datetime.utcnow(), "batch_size": len(file_list),
    })
    link  = f"https://t.me/{BOT_USERNAME}?start=batch_{token}"
    kb    = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Open Link", url=link)]])
    names = "\n".join(f"  {i+1}. {f['file_name']}" for i, f in enumerate(file_list))
    await update.message.reply_html(
        f"✅ <b>Batch saved! ({n} files)</b>\n\n<b>Files:</b>\n{names}\n\n"
        f"🔗 <b>Share Link:</b>\n<code>{link}</code>",
        reply_markup=kb,
    )

async def cmd_cancelbatch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        return
    session = bulk_sessions.pop(user.id, None)
    if not session:
        await update.message.reply_text("❌ No active bulk session.")
        return
    if session.get("task"):
        session["task"].cancel()
    await update.message.reply_text(f"🗑️ Cancelled. {len(session['files'])} file(s) discarded.")

async def cmd_myfiles(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    lines = []
    async for r in col_files.find({}, sort=[("uploaded_at", -1)], limit=15):
        lines.append(
            f"📄 {r['file_name']}\n"
            f"  <code>https://t.me/{BOT_USERNAME}?start={r['token']}</code>"
        )
    async for b in col_batches.find({}, sort=[("uploaded_at", -1)], limit=10):
        names = ", ".join(f["file_name"] for f in b["files"][:3])
        if b["batch_size"] > 3:
            names += f" +{b['batch_size']-3} more"
        lines.append(
            f"📦 Batch ({b['batch_size']}): {names}\n"
            f"  <code>https://t.me/{BOT_USERNAME}?start=batch_{b['token']}</code>"
        )
    if not lines:
        await update.message.reply_text("📭 No files found.")
        return
    await update.message.reply_html(f"📁 <b>Files ({len(lines)})</b>\n\n" + "\n\n".join(lines))

async def cmd_delfile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /delfile <token>")
        return
    token  = ctx.args[0]
    result = await col_batches.delete_one({"token": token[6:]}) \
             if token.startswith("batch_") else await col_files.delete_one({"token": token})
    await update.message.reply_text("🗑️ Deleted." if result.deleted_count else "❌ Not found.")

async def cmd_refreshfiles(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("🔄 Refreshing…")
    await refresh_all_on_startup(ctx.bot)
    await update.message.reply_text("✅ Done.")

async def cmd_filters(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    lines = []
    async for f in col_filters.find({}, sort=[("created_at", -1)]):
        kws  = ", ".join(f["keywords"])
        btns = sum(len(r) for r in f.get("buttons", []))
        lines.append(
            f"📌 <b>{f.get('display_name', kws)}</b>\n"
            f"  🔑 <code>{kws}</code>\n"
            f"  🖼 {'Yes' if f.get('photo_id') else 'No'} · "
            f"📝 {'Yes' if f.get('caption') else 'No'} · "
            f"🔘 {btns} btn(s)"
        )
    if not lines:
        await update.message.reply_text("📭 No filters. Use /addfilter.")
        return
    await update.message.reply_html(f"📋 <b>Filters ({len(lines)})</b>\n\n" + "\n\n".join(lines))

async def cmd_delfilter(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_html("Usage: /delfilter &lt;keyword&gt;")
        return
    kw     = " ".join(ctx.args).lower().strip()
    result = await col_filters.delete_one({"keywords": kw})
    await update.message.reply_text(
        f"🗑️ Deleted '{kw}'." if result.deleted_count else f"❌ No filter for '{kw}'."
    )

async def cmd_myadmins(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    lines = []
    for uid in ADMIN_IDS:
        try:
            user = await ctx.bot.get_chat(uid)
            name = user.full_name
            username = f"@{user.username}" if user.username else "no username"
            lines.append(f"• <b>{name}</b> ({username}) — <code>{uid}</code>")
        except Exception:
            lines.append(f"• <code>{uid}</code>")
    await update.message.reply_html(
        f"👮 <b>Admins ({len(ADMIN_IDS)})</b>\n\n" + "\n".join(lines) +
        "\n\n<i>Edit ADMIN_IDS in bot.py to add/remove admins.</i>"
    )


async def cmd_mychannels(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    lines = []
    for ch in CHANNEL_IDS:
        try:
            chat = await ctx.bot.get_chat(ch)
            members = await ctx.bot.get_chat_member_count(ch)
            lines.append(f"• <b>{chat.title}</b> — {ch} — 👥 {members}")
        except Exception:
            lines.append(f"• {ch} — (could not fetch info)")
    await update.message.reply_html(
        f"📢 <b>Must-Join Channels ({len(CHANNEL_IDS)})</b>\n\n" + "\n".join(lines) +
        "\n\n<i>Edit CHANNEL_IDS in bot.py to add/remove channels.</i>"
    )


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    s  = await col_files.count_documents({})
    b  = await col_batches.count_documents({})
    p  = await col_pending.count_documents({})
    f  = await col_filters.count_documents({})
    await update.message.reply_html(
        f"📊 <b>Stats</b>\n\n"
        f"🔑 Filters: <b>{f}</b>\n"
        f"📄 Files: <b>{s}</b> · 📦 Batches: <b>{b}</b>\n"
        f"⏳ Pending deletes: <b>{p}</b>"
    )

# ═══════════════════════════════════════════════════════════════
#  WELCOME
# ═══════════════════════════════════════════════════════════════

async def handle_new_member(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    result: ChatMemberUpdated = update.chat_member

    # Only send in groups/supergroups — NOT in channels
    chat_type = result.chat.type
    if chat_type not in ("group", "supergroup"):
        return

    if result.new_chat_member.status not in ("member", "restricted"):
        return
    if result.old_chat_member.status in ("member", "administrator", "creator", "restricted"):
        return

    # Skip bots
    if result.new_chat_member.user.is_bot:
        return

    try:
        await ctx.bot.send_message(
            result.chat.id,
            WELCOME_MSG,
            parse_mode="HTML",
        )
    except Exception as e:
        log.warning("Welcome msg failed: %s", e)

# ═══════════════════════════════════════════════════════════════
#  MESSAGE HANDLERS
# ═══════════════════════════════════════════════════════════════

MEDIA_FILTER = (
    filters.Document.ALL | filters.PHOTO | filters.VIDEO
    | filters.AUDIO | filters.VOICE | filters.VIDEO_NOTE
)

async def handle_media(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin sends file → save for sharing. Skip if inside wizard."""
    user = update.effective_user
    if not is_admin(user.id):
        return
    if user.id in fw_wizard:
        return  # wizard handles it

    info = extract_file_info(update.message)
    if not info:
        return
    info["caption"] = update.message.caption or None

    # Bulk mode
    if user.id in bulk_sessions:
        session = bulk_sessions[user.id]
        if session.get("task"):
            session["task"].cancel()
        session["files"].append(info)
        n    = len(session["files"])
        task = asyncio.create_task(bulk_finalize(ctx.bot, user.id, update.effective_chat.id))
        session["task"] = task
        await update.message.reply_html(
            f"📦 <b>File {n} added:</b> {info['file_name']}\n"
            f"Send more or /endbatch to finish.\n"
            f"<i>Auto-saves in {BULK_TIMEOUT}s.</i>"
        )
        return

    # Single mode
    token = make_token()
    await col_files.insert_one({
        "token": token, "file_id": info["file_id"],
        "file_unique_id": info["file_unique_id"],
        "file_name": info["file_name"], "file_type": info["file_type"],
        "caption": info["caption"], "uploaded_by": user.id,
        "uploaded_at": datetime.utcnow(),
    })
    link = f"https://t.me/{BOT_USERNAME}?start={token}"
    kb   = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Open Link", url=link)]])
    await update.message.reply_html(
        f"✅ <b>File stored!</b>\n\n"
        f"📎 <b>Name:</b> {info['file_name']}\n"
        f"🔗 <b>Share Link:</b>\n<code>{link}</code>\n\n"
        f"⏰ Deleted from user PM after <b>{DELETE_AFTER_HOURS}h</b>\n"
        f"💡 Use /batch for multiple files.",
        reply_markup=kb,
    )


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Keyword filter search — runs in group 1.
    Only works in groups/supergroups — NOT in bot PM.
    Finds ALL matching filters → shows them as inline buttons in ONE message.
    User clicks a button → handle_filter_callback sends the full result in group.
    """
    uid       = update.effective_user.id
    chat_type = update.effective_chat.type

    # ── Only allow search in groups, not in bot PM ────────────
    if chat_type == "private":
        return

    if uid in fw_wizard:
        return

    text = (update.message.text or "").strip()
    if not text or text.startswith("/"):
        return

    matches = await find_all_matching_filters(text)

    if not matches:
        await update.message.reply_text(NOT_FOUND_MSG)
        return

    # Build one message with all results as inline buttons
    # callback_data includes the requester user_id so only they can click
    uid     = update.effective_user.id
    buttons = []
    for f in matches:
        name    = f.get("display_name") or f["keywords"][0].title()
        fid_str = str(f["_id"])
        buttons.append([InlineKeyboardButton(name, callback_data=f"filter:{uid}:{fid_str}")])

    kb = InlineKeyboardMarkup(buttons)

    await update.message.reply_html(
        f"🔍 <b>{len(matches)} result(s) found</b>\n\n"
        f"👆 <b>{update.effective_user.first_name}</b>, click to get the file:",
        reply_markup=kb,
    )

# ═══════════════════════════════════════════════════════════════
#  STARTUP
# ═══════════════════════════════════════════════════════════════

async def post_init(app: Application):
    await ensure_indexes()
    await refresh_all_on_startup(app.bot)
    await restore_pending_deletes(app.bot)

# ═══════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════

def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # ── Group 0: wizard (must catch photos before handle_media) ──
    addfilter_conv = ConversationHandler(
        entry_points=[CommandHandler("addfilter", fw_cmd_addfilter)],
        states={
            FW_KW:      [MessageHandler(filters.TEXT & ~filters.COMMAND, fw_step_kw)],
            FW_IMG:     [
                MessageHandler(filters.PHOTO, fw_step_img_photo),
                MessageHandler(filters.TEXT & ~filters.COMMAND, fw_step_img_skip),
            ],
            FW_TEXT:    [MessageHandler(filters.TEXT & ~filters.COMMAND, fw_step_text)],
            FW_BTNS:    [MessageHandler(filters.TEXT & ~filters.COMMAND, fw_step_btns)],
            FW_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, fw_step_confirm)],
        },
        fallbacks=[CommandHandler("cancel", fw_cancel)],
        allow_reentry=True,
        per_user=True,
        per_chat=True,
    )
    app.add_handler(addfilter_conv, group=0)

    # ── Group 0: commands ──────────────────────────────────────
    app.add_handler(CommandHandler("start",        cmd_start),        group=0)
    app.add_handler(CommandHandler("ahelp",        cmd_ahelp),        group=0)
    app.add_handler(CommandHandler("batch",        cmd_batch),        group=0)
    app.add_handler(CommandHandler("endbatch",     cmd_endbatch),     group=0)
    app.add_handler(CommandHandler("cancelbatch",  cmd_cancelbatch),  group=0)
    app.add_handler(CommandHandler("myfiles",      cmd_myfiles),      group=0)
    app.add_handler(CommandHandler("delfile",      cmd_delfile),      group=0)
    app.add_handler(CommandHandler("refreshfiles", cmd_refreshfiles), group=0)
    app.add_handler(CommandHandler("filters",      cmd_filters),      group=0)
    app.add_handler(CommandHandler("delfilter",    cmd_delfilter),    group=0)
    app.add_handler(CommandHandler("stats",        cmd_stats),        group=0)
    app.add_handler(CommandHandler("myadmins",     cmd_myadmins),     group=0)
    app.add_handler(CommandHandler("mychannels",   cmd_mychannels),   group=0)

    # ── Group 0: welcome ──────────────────────────────────────
    app.add_handler(
        ChatMemberHandler(handle_new_member, ChatMemberHandler.CHAT_MEMBER),
        group=0
    )

    # ── Group 0: callback query (button clicks) ───────────────
    app.add_handler(CallbackQueryHandler(handle_filter_callback, pattern=r"^filter:"), group=0)

    # ── Group 1: media & text (after wizard so photos go to wizard first) ──
    app.add_handler(MessageHandler(MEDIA_FILTER,                    handle_media), group=1)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text),  group=1)

    log.info("🚀 Bot @%s starting…", BOT_USERNAME)
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
