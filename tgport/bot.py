import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from functools import wraps

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import config
from .claude import Error, Result, SessionNotFoundError, TextDelta, ToolUse, stream_claude
from .session import SessionManager

logger = logging.getLogger(__name__)

session_manager = SessionManager()
_chat_locks: dict[int, asyncio.Lock] = {}

MAX_MSG_LEN = 4000
MAX_MESSAGES_PER_RESPONSE = 10


def _get_lock(chat_id: int) -> asyncio.Lock:
    if chat_id not in _chat_locks:
        _chat_locks[chat_id] = asyncio.Lock()
    return _chat_locks[chat_id]


def restricted(func):
    """Allow only configured user IDs."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or user.id not in config.ALLOWED_USER_IDS:
            logger.warning("Unauthorized access attempt from user %s", user.id if user else "unknown")
            return
        return await func(update, context)
    return wrapper


async def _edit_message(msg, text: str):
    """Edit message with HTML, falling back to plain text."""
    if not text.strip():
        return
    try:
        await msg.edit_text(text, parse_mode=ParseMode.HTML)
    except Exception:
        try:
            await msg.edit_text(text)
        except Exception as e:
            logger.debug("Failed to edit message: %s", e)


@restricted
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Claude bot ready. Send any message to interact.\n"
        "/new - start a new conversation"
    )


@restricted
async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    session_manager.reset(chat_id)
    await update.message.reply_text("New conversation started.")


@restricted
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    prompt = update.message.text
    if not prompt:
        return

    lock = _get_lock(chat_id)
    if lock.locked():
        await update.message.reply_text("Please wait for the current response to finish.")
        return

    async with lock:
        await _process_message(update, chat_id, prompt)


ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".txt", ".md", ".pdf"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
DOC_EXTENSIONS = {".txt", ".md", ".pdf"}


def _get_download_dir(ext: str) -> str:
    if ext in DOC_EXTENSIONS:
        return os.path.expanduser("~/workspace/docs/downloads")
    return config.DOWNLOAD_DIR


@restricted
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    caption = update.message.caption or "この画像を確認してください。"

    lock = _get_lock(chat_id)
    if lock.locked():
        await update.message.reply_text("Please wait for the current response to finish.")
        return

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)

    download_dir = _get_download_dir(".jpg")
    os.makedirs(download_dir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"{ts}_{photo.file_unique_id}.jpg"
    filepath = os.path.join(download_dir, filename)
    await file.download_to_drive(filepath)

    prompt = f"{caption}\n\n[画像ファイル: {filepath}]"
    logger.info("Photo saved: %s", filepath)

    async with lock:
        await _process_message(update, chat_id, prompt)


@restricted
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    caption = update.message.caption or "このファイルを確認してください。"
    doc = update.message.document

    # Check file extension
    original_name = doc.file_name or ""
    ext = os.path.splitext(original_name)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
        await update.message.reply_text(f"非対応のファイル形式です。対応: {allowed}")
        return

    lock = _get_lock(chat_id)
    if lock.locked():
        await update.message.reply_text("Please wait for the current response to finish.")
        return

    file = await context.bot.get_file(doc.file_id)

    download_dir = _get_download_dir(ext)
    os.makedirs(download_dir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"{ts}_{original_name}" if original_name else f"{ts}_{doc.file_unique_id}{ext}"
    filepath = os.path.join(download_dir, filename)
    await file.download_to_drive(filepath)

    label = "画像ファイル" if ext in IMAGE_EXTENSIONS else "ファイル"
    prompt = f"{caption}\n\n[{label}: {filepath}]"
    logger.info("Document saved: %s", filepath)

    async with lock:
        await _process_message(update, chat_id, prompt)


async def _process_message(update: Update, chat_id: int, prompt: str, retry: bool = False):
    session_id, is_new = session_manager.get_or_create(chat_id)
    if retry:
        is_new = True

    bot_msg = await update.message.reply_text("...")
    accumulated = ""
    last_edit = 0.0
    msg_count = 1

    try:
        async for event in stream_claude(prompt, session_id, is_new):
            if isinstance(event, TextDelta):
                accumulated += event.text

                now = time.monotonic()
                if now - last_edit >= config.EDIT_INTERVAL:
                    display = accumulated[:MAX_MSG_LEN]
                    await _edit_message(bot_msg, display)
                    last_edit = now

                    # Split if too long
                    if len(accumulated) > MAX_MSG_LEN and msg_count < MAX_MESSAGES_PER_RESPONSE:
                        await _edit_message(bot_msg, accumulated[:MAX_MSG_LEN])
                        accumulated = accumulated[MAX_MSG_LEN:]
                        bot_msg = await update.message.reply_text("...")
                        msg_count += 1

            elif isinstance(event, ToolUse):
                tool_indicator = f"\n<i>[{event.tool}]</i>"
                accumulated += tool_indicator
                now = time.monotonic()
                if now - last_edit >= config.EDIT_INTERVAL:
                    await _edit_message(bot_msg, accumulated[:MAX_MSG_LEN])
                    last_edit = now

            elif isinstance(event, Result):
                # Handle session-not-found errors from CLI
                if event.is_error and any("no conversation found" in e.lower() for e in event.errors):
                    raise SessionNotFoundError("; ".join(event.errors))

                if event.is_error:
                    error_msg = "; ".join(event.errors) if event.errors else event.text or "Unknown error"
                    await _edit_message(bot_msg, f"Error: {error_msg}")
                    return

                # Use result text as final output if we have it
                if event.text and not accumulated.strip(".\n "):
                    accumulated = event.text

                # Split remaining text into messages
                while len(accumulated) > MAX_MSG_LEN and msg_count < MAX_MESSAGES_PER_RESPONSE:
                    await _edit_message(bot_msg, accumulated[:MAX_MSG_LEN])
                    accumulated = accumulated[MAX_MSG_LEN:]
                    bot_msg = await update.message.reply_text("...")
                    msg_count += 1

                footer = f"\n\n<i>${event.cost_usd:.4f}</i>"
                final = (accumulated + footer).strip()
                await _edit_message(bot_msg, final)

            elif isinstance(event, Error):
                await _edit_message(bot_msg, f"Error: {event.message}")

    except SessionNotFoundError:
        if not retry:
            await _edit_message(bot_msg, "Session not found, starting new...")
            await _process_message(update, chat_id, prompt, retry=True)
        else:
            await _edit_message(bot_msg, "Error: Failed to start Claude session.")
    except RuntimeError as e:
        logger.error("Claude CLI error: %s", e)
        await _edit_message(bot_msg, f"Error: {e}")


def run():
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )

    app = Application.builder().token(config.BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    logger.info("Bot started. Allowed users: %s", config.ALLOWED_USER_IDS)
    app.run_polling()
