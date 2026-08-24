"""
A2T Handler - Central message routing, context lifecycle, user auth,
attachment download, reply sending, typing indicator, and all A2T commands.

Preserves the full A2T command set:
  /start, /help, /clear, /status, /id, /stop, /resume, /nudge,
  /context

Uses the A0 plugin system's AgentContext for per-user sessions and
direct Python API calls for control commands (no HTTP/CSRF needed).
"""

import base64
import json
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager, suppress

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import (
    Message as TgMessage,
    CallbackQuery,
)

from agent import AgentContext, UserMessage
from helpers import plugins, files, projects
from helpers import message_queue as mq
from helpers.notification import NotificationManager, NotificationType, NotificationPriority
from helpers.persist_chat import save_tmp_chat
from helpers.print_style import PrintStyle
from helpers.errors import format_error
from initialize import initialize_agent

from usr.plugins.a2t.helpers import telegram_client as tc
from usr.plugins.a2t.helpers.bot_manager import get_bot, stop_all_bots
from usr.plugins.a2t.helpers.constants import (
    PLUGIN_NAME,
    DOWNLOAD_FOLDER,
    STATE_FILE,
    CTX_TG_BOT,
    CTX_TG_BOT_CFG,
    CTX_TG_CHAT_ID,
    CTX_TG_USER_ID,
    CTX_TG_USERNAME,
    CTX_TG_TYPING_STOP,
    CTX_TG_REPLY_TO,
    CTX_TG_ATTACHMENTS,
    CTX_TG_KEYBOARD,
    CTX_TG_PROJECT,
    CTX_TG_THREAD_ID,
    CTX_TG_API_THREAD_ID,
)


# ---------------------------------------------------------------------------
#  State persistence
# ---------------------------------------------------------------------------

_chat_map_lock = threading.Lock()


def _load_state() -> dict:
    path = files.get_abs_path(STATE_FILE)
    if os.path.isfile(path):
        try:
            return json.loads(files.read_file(path))
        except Exception:
            return {}
    return {}


def _save_state(state: dict):
    path = files.get_abs_path(STATE_FILE)
    files.make_dirs(path)
    files.write_file(path, json.dumps(state, indent=2))


def _message_thread_id(message: TgMessage | None) -> int:
    """Extract message_thread_id for internal mapping keys.

    In forum groups the General topic has thread_id=1, but Telegram may
    omit message_thread_id on some messages sent from General.  We
    normalise to 1 so mapping keys are always consistent.
    """
    if not message:
        return 0
    tid = int(getattr(message, "message_thread_id", None) or 0)
    if tid:
        return tid
    # No thread_id on the message -- if the chat is a forum group,
    # this is the General topic (thread_id=1).
    chat = getattr(message, "chat", None)
    if chat and getattr(chat, "is_forum", False):
        return 1
    return 0


def _api_thread_id(message: TgMessage | None) -> int | None:
    """Return the message_thread_id for Telegram API calls.

    Forum groups always have topics.  Private chats and regular groups
    may also have message_thread_id when threaded/reply-thread mode is
    enabled.  We pass the thread_id through whenever it is present so
    replies land in the correct thread.
    """
    if not message:
        return None
    tid = int(getattr(message, "message_thread_id", None) or 0)
    chat = getattr(message, "chat", None)
    is_forum = chat and getattr(chat, "is_forum", False)
    if is_forum:
        # Forum group: return the thread id (General defaults to 1)
        return tid if tid else 1
    # Non-forum chat: pass thread_id if present (threaded private chats)
    return tid if tid else None


def _map_key(bot_name: str, user_id: int, chat_id: int, thread_id: int = 0) -> str:
    return f"{bot_name}:{user_id}:{chat_id}:{thread_id}"


# ---------------------------------------------------------------------------
#  Direct A0 Python API helpers (no HTTP/CSRF needed - we run inside A0)
# ---------------------------------------------------------------------------

def _a0_pause_context(ctx: AgentContext, paused: bool):
    """Pause or unpause an agent context directly."""
    ctx.paused = paused


def _a0_nudge_context(ctx: AgentContext):
    """Nudge (reset process chain) for a stuck agent context."""
    ctx.reset_process()


def _a0_get_context_window(ctx: AgentContext) -> dict:
    """Get context window info directly from the agent's stored data."""
    try:
        agent = ctx.streaming_agent or ctx.agent0
        window = agent.get_data(agent.DATA_NAME_CTX_WINDOW)
        if window and isinstance(window, dict):
            return {
                "tokens": window.get("tokens", 0),
                "content": window.get("text", ""),
            }
        # No window yet (fresh context, agent hasn't processed any message)
        return {"tokens": 0, "content": "(no context window yet)"}
    except Exception as e:
        PrintStyle.error(f"A2T: failed to read context window: {e}")
        return {"tokens": 0, "content": ""}





# ---------------------------------------------------------------------------
#  Attachment cleanup
# ---------------------------------------------------------------------------

def cleanup_old_attachments():
    """Remove downloaded attachment files older than per-bot max age."""
    config = plugins.get_plugin_config(PLUGIN_NAME) or {}
    bots_cfg = config.get("bots") or []
    total_removed = 0
    upload_dir = files.get_abs_path(DOWNLOAD_FOLDER)
    if not os.path.isdir(upload_dir):
        return
    for bot_cfg in bots_cfg:
        bot_name = bot_cfg.get("name", "")
        if not bot_name:
            continue
        max_age_hours = bot_cfg.get("attachment_max_age_hours", 0)
        if not max_age_hours or max_age_hours <= 0:
            continue
        prefix = f"a2t_{bot_name}_"
        cutoff = time.time() - max_age_hours * 3600
        for name in os.listdir(upload_dir):
            if not name.startswith(prefix):
                continue
            path = os.path.join(upload_dir, name)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    total_removed += 1
            except OSError:
                pass
    if total_removed:
        PrintStyle.info(f"A2T: cleaned up {total_removed} old attachment(s)")


# ---------------------------------------------------------------------------
#  Access control
# ---------------------------------------------------------------------------

def _is_allowed(bot_cfg: dict, user_id: int, username: str | None, chat_id: int) -> bool:
    """Check if user/chat is authorized. Empty lists = allow all."""
    # Check chat whitelist
    allowed_chats = bot_cfg.get("allowed_chats") or []
    if allowed_chats:
        if str(chat_id) not in [str(c).strip() for c in allowed_chats]:
            return False

    # Check user whitelist
    allowed_users = bot_cfg.get("allowed_users") or []
    if not allowed_users:
        return True
    for entry in allowed_users:
        entry_str = str(entry).strip()
        if entry_str.startswith("@"):
            if username and f"@{username}" == entry_str:
                return True
        else:
            try:
                if int(entry_str) == user_id:
                    return True
            except ValueError:
                if username and entry_str.lower() == username.lower():
                    return True
    PrintStyle.warning(f"A2T: blocked unauthorized user {user_id} in chat {chat_id}")
    return False


def _get_project(bot_cfg: dict, user_id: int, thread_id: int = 0) -> str:
    topic_projects = bot_cfg.get("topic_projects") or {}
    project = topic_projects.get(str(thread_id), "") if thread_id else ""
    if not project:
        user_projects = bot_cfg.get("user_projects") or {}
        project = user_projects.get(str(user_id), "")
    if not project:
        project = bot_cfg.get("default_project", "")
    return project


# ---------------------------------------------------------------------------
#  A2T Help Text
# ---------------------------------------------------------------------------

HELP_TEXT = (
    "<b>A2T</b>\n"
    "<i>Telegram connector for Agent Zero</i>\n\n"
    "Send me any message and I'll forward it to Agent Zero.\n"
    "Supported: text, photos, documents/files.\n\n"
    "<b>Commands:</b>\n"
    "/start -- Start the bot\n"
    "/help -- Show this message\n"
    "/clear -- Start a new conversation\n"
    "/status -- Show connection status\n"
    "/id -- Show your User/Chat ID\n"
    "/stop -- Pause the agent (stop current work)\n"
    "/resume -- Resume a paused agent\n"
    "/nudge -- Kick the agent when stuck\n"
    "/context -- Show context window info"
)


# ---------------------------------------------------------------------------
#  Command handlers
# ---------------------------------------------------------------------------

async def handle_start(message: TgMessage, bot_name: str, bot_cfg: dict):
    """Handle /start command."""
    user = message.from_user
    if not user:
        return
    if not _is_allowed(bot_cfg, user.id, user.username, message.chat.id):
        await message.reply("You are not authorized to use this bot.")
        return

    instance = get_bot(bot_name)
    if not instance:
        return

    await _send_with_temp_bot(
        instance.bot.token,
        message.chat.id,
        HELP_TEXT,
        message_thread_id=_api_thread_id(message),
    )
    await _get_or_create_context(bot_name, bot_cfg, message)


async def handle_help(message: TgMessage, bot_name: str, bot_cfg: dict):
    """Handle /help command."""
    user = message.from_user
    if not user:
        return
    if not _is_allowed(bot_cfg, user.id, user.username, message.chat.id):
        return

    instance = get_bot(bot_name)
    if not instance:
        return

    await _send_with_temp_bot(
        instance.bot.token,
        message.chat.id,
        HELP_TEXT,
        message_thread_id=_api_thread_id(message),
    )


async def handle_clear(message: TgMessage, bot_name: str, bot_cfg: dict):
    """Handle /clear command -- reset user's chat context."""
    user = message.from_user
    if not user:
        return
    if not _is_allowed(bot_cfg, user.id, user.username, message.chat.id):
        return

    key = _map_key(bot_name, user.id, message.chat.id, _message_thread_id(message))
    with _chat_map_lock:
        state = _load_state()
        ctx_id = state.get("chats", {}).get(key)
        if ctx_id:
            ctx = AgentContext.get(ctx_id)
            if ctx:
                ctx.reset()
                PrintStyle.info(f"A2T ({bot_name}): cleared chat for user {user.id}")

    instance = get_bot(bot_name)
    if instance:
        await _send_with_temp_bot(
            instance.bot.token, message.chat.id,
            "Chat cleared. Send a new message to start fresh.",
            message_thread_id=_api_thread_id(message),
            parse_mode=None,
        )

    if bot_cfg.get("notify_messages", False):
        username_str = _safe_username(user)
        NotificationManager.send_notification(
            type=NotificationType.INFO,
            priority=NotificationPriority.NORMAL,
            title="A2T: chat cleared",
            message=f"{username_str} cleared their chat via /clear",
            display_time=5,
            group="a2t",
        )


async def handle_status(message: TgMessage, bot_name: str, bot_cfg: dict):
    """Handle /status command -- show connection info."""
    user = message.from_user
    if not user:
        return
    if not _is_allowed(bot_cfg, user.id, user.username, message.chat.id):
        return

    key = _map_key(bot_name, user.id, message.chat.id, _message_thread_id(message))
    state = _load_state()
    ctx_id = state.get("chats", {}).get(key, "(none)")
    max_file = bot_cfg.get("max_file_size_mb", 20)
    timeout = bot_cfg.get("a0_timeout", 300)
    mode = bot_cfg.get("mode", "polling")

    # Get project info
    project = "(default)"
    with _chat_map_lock:
        chats = state.get("chats", {})
        cid = chats.get(key)
        if cid:
            ctx = AgentContext.get(cid)
            if ctx:
                project = ctx.data.get(CTX_TG_PROJECT, "(default)")

    instance = get_bot(bot_name)
    if instance:
        await _send_with_temp_bot(
            instance.bot.token, message.chat.id,
            f"<b>A2T Status</b>\n"
            f"- Bot: <code>{bot_name}</code>\n"
            f"- Mode: <code>{mode}</code>\n"
            f"- Context: <code>{ctx_id}</code>\n"
            f"- Project: <code>{project}</code>\n"
            f"- Timeout: {timeout}s\n"
            f"- Max file: {max_file} MB",
            message_thread_id=_api_thread_id(message),
        )


async def handle_id(message: TgMessage, bot_name: str, bot_cfg: dict):
    """Handle /id command -- show user/chat IDs."""
    user = message.from_user
    if not user:
        return
    if not _is_allowed(bot_cfg, user.id, user.username, message.chat.id):
        return

    instance = get_bot(bot_name)
    if instance:
        await _send_with_temp_bot(
            instance.bot.token, message.chat.id,
            f"<b>Your IDs</b>\n"
            f"- User ID: <code>{user.id}</code>\n"
            f"- Chat ID: <code>{message.chat.id}</code>\n"
            f"- Username: @{user.username or '(none)'}",
            message_thread_id=_api_thread_id(message),
        )


async def handle_stop(message: TgMessage, bot_name: str, bot_cfg: dict):
    """Handle /stop command -- pause the agent."""
    user = message.from_user
    if not user:
        return
    if not _is_allowed(bot_cfg, user.id, user.username, message.chat.id):
        return

    ctx = _get_existing_context(bot_name, user.id, message.chat.id, _message_thread_id(message))
    if not ctx:
        await _reply_no_context(bot_name, message)
        return

    try:
        _a0_pause_context(ctx, True)
        instance = get_bot(bot_name)
        if instance:
            await _send_with_temp_bot(
                instance.bot.token, message.chat.id,
                "<b>Agent paused.</b>\n\n"
                "The agent has been stopped mid-work.\n"
                "Use /resume to continue or /clear to start fresh.",
                message_thread_id=_api_thread_id(message),
            )
        PrintStyle.info(f"A2T ({bot_name}): agent paused for user {user.id}")
    except Exception as e:
        PrintStyle.error(f"A2T: failed to pause agent: {e}")
        instance = get_bot(bot_name)
        if instance:
            await _send_with_temp_bot(
                instance.bot.token, message.chat.id,
                f"Failed to stop agent: {str(e)[:300]}",
                parse_mode=None,
                message_thread_id=_api_thread_id(message),
            )


async def handle_resume(message: TgMessage, bot_name: str, bot_cfg: dict):
    """Handle /resume command -- resume a paused agent."""
    user = message.from_user
    if not user:
        return
    if not _is_allowed(bot_cfg, user.id, user.username, message.chat.id):
        return

    ctx = _get_existing_context(bot_name, user.id, message.chat.id, _message_thread_id(message))
    if not ctx:
        await _reply_no_context(bot_name, message)
        return

    try:
        _a0_pause_context(ctx, False)
        instance = get_bot(bot_name)
        if instance:
            await _send_with_temp_bot(
                instance.bot.token, message.chat.id,
                "<b>Agent resumed.</b> Send your next message.",
                message_thread_id=_api_thread_id(message),
            )
        PrintStyle.info(f"A2T ({bot_name}): agent resumed for user {user.id}")
    except Exception as e:
        PrintStyle.error(f"A2T: failed to resume agent: {e}")
        instance = get_bot(bot_name)
        if instance:
            await _send_with_temp_bot(
                instance.bot.token, message.chat.id,
                f"Failed to resume agent: {str(e)[:300]}",
                parse_mode=None,
                message_thread_id=_api_thread_id(message),
            )


async def handle_nudge(message: TgMessage, bot_name: str, bot_cfg: dict):
    """Handle /nudge command -- kick stuck agent."""
    user = message.from_user
    if not user:
        return
    if not _is_allowed(bot_cfg, user.id, user.username, message.chat.id):
        return

    ctx = _get_existing_context(bot_name, user.id, message.chat.id, _message_thread_id(message))
    if not ctx:
        await _reply_no_context(bot_name, message)
        return

    try:
        _a0_nudge_context(ctx)
        msg = "Agent process chain reset."
        instance = get_bot(bot_name)
        if instance:
            await _send_with_temp_bot(
                instance.bot.token, message.chat.id,
                f"<b>Agent nudged!</b>\n{msg}",
                message_thread_id=_api_thread_id(message),
            )
        PrintStyle.info(f"A2T ({bot_name}): agent nudged for user {user.id}")
    except Exception as e:
        PrintStyle.error(f"A2T: failed to nudge agent: {e}")
        instance = get_bot(bot_name)
        if instance:
            await _send_with_temp_bot(
                instance.bot.token, message.chat.id,
                f"Failed to nudge agent: {str(e)[:300]}",
                parse_mode=None,
                message_thread_id=_api_thread_id(message),
            )


async def handle_context(message: TgMessage, bot_name: str, bot_cfg: dict):
    """Handle /context command -- show context window info."""
    user = message.from_user
    if not user:
        return
    if not _is_allowed(bot_cfg, user.id, user.username, message.chat.id):
        return

    ctx = _get_existing_context(bot_name, user.id, message.chat.id, _message_thread_id(message))
    if not ctx:
        await _reply_no_context(bot_name, message)
        return

    try:
        data = _a0_get_context_window(ctx)
        tokens_used = data.get("tokens", 0)
        content_len = len(data.get("content", ""))
        tokens_fmt = f"{tokens_used:,}"
        instance = get_bot(bot_name)
        if instance:
            await _send_with_temp_bot(
                instance.bot.token, message.chat.id,
                f"<b>Context Window</b>\n\n"
                f"- Tokens: <code>{tokens_fmt}</code>\n"
                f"- Content length: <code>{content_len:,}</code> chars\n"
                f"- Context ID: <code>{ctx.id[:16]}...</code>",
                message_thread_id=_api_thread_id(message),
            )
    except Exception as e:
        PrintStyle.error(f"A2T: failed to get context info: {e}")
        instance = get_bot(bot_name)
        if instance:
            await _send_with_temp_bot(
                instance.bot.token, message.chat.id,
                f"Failed to get context info: {str(e)[:300]}",
                parse_mode=None,
                message_thread_id=_api_thread_id(message),
            )


# ---------------------------------------------------------------------------
#  Callback query handler
# ---------------------------------------------------------------------------

async def handle_callback_query(query: CallbackQuery, bot_name: str, bot_cfg: dict):
    """Handle inline keyboard button presses."""
    user = query.from_user
    if not user or not query.message:
        return

    if not _is_allowed(bot_cfg, user.id, user.username, query.message.chat.id):
        await query.answer("Not authorized.")
        return

    await query.answer()

    cb_data = query.data or ""
    # Treat unknown callback data as a user message (keyboard buttons from agent)
    text = cb_data
    if not text:
        return
    context = await _get_or_create_context_from_user(
        bot_name, bot_cfg, user.id, user.username, query.message.chat.id, _message_thread_id(query.message),
        api_thread_id=_api_thread_id(query.message),
    )
    if not context:
        return

    # Start typing indicator (same pattern as handle_message)
    instance = get_bot(bot_name)
    typing_stop = None
    if instance:
        api_tid = _api_thread_id(query.message)
        is_forum = getattr(query.message.chat, "is_forum", False)
        typing_thread_id = api_tid if is_forum else None
        typing_stop = _start_typing(instance.bot.token, query.message.chat.id, thread_id=typing_thread_id)
        # Safety: stop any typing stop event already on the context
        old_stop = context.data.get(CTX_TG_TYPING_STOP)
        if old_stop:
            old_stop.set()
        context.data[CTX_TG_TYPING_STOP] = typing_stop

    agent = context.agent0
    user_msg = agent.read_prompt(
        "fw.a2t.user_message.md",
        sender=_format_user(user),
        body=f"[Button pressed: {text}]",
    )
    msg_id = str(uuid.uuid4())
    mq.log_user_message(context, user_msg, [], message_id=msg_id, source=" (a2t)")
    context.communicate(UserMessage(message=user_msg, id=msg_id))
    save_tmp_chat(context)

    if typing_stop:
        typing_stop.set()



# ---------------------------------------------------------------------------
#  Welcome handler
# ---------------------------------------------------------------------------

async def handle_new_members(message: TgMessage, bot_name: str, bot_cfg: dict):
    """Send welcome message when new members join a group."""
    if not bot_cfg.get("welcome_enabled", False):
        return

    new_members = message.new_chat_members or []
    if not new_members:
        return

    instance = get_bot(bot_name)
    if not instance:
        return

    template = bot_cfg.get("welcome_message", "").strip()
    if not template:
        template = "Welcome, {name}!"

    for member in new_members:
        if member.is_bot:
            continue
        name = member.full_name or member.first_name or str(member.id)
        text = template.replace("{name}", name)
        await _send_with_temp_bot(instance.bot.token, message.chat.id, text, parse_mode=None, message_thread_id=_api_thread_id(message))


# ---------------------------------------------------------------------------
#  Message handler (text, photos, documents)
# ---------------------------------------------------------------------------

async def handle_message(message: TgMessage, bot_name: str, bot_cfg: dict):
    """Handle incoming user message."""
    user = message.from_user
    if not user:
        return
    if not _is_allowed(bot_cfg, user.id, user.username, message.chat.id):
        return

    instance = get_bot(bot_name)
    if not instance:
        return

    # Check file size for documents
    max_file_bytes = bot_cfg.get("max_file_size_mb", 20) * 1024 * 1024
    if message.document and message.document.file_size and message.document.file_size > max_file_bytes:
        max_mb = bot_cfg.get("max_file_size_mb", 20)
        await _send_with_temp_bot(
            instance.bot.token, message.chat.id,
            f"File too large (max {max_mb} MB).",
            parse_mode=None,
            message_thread_id=_api_thread_id(message),
        )
        return

    # Stop any existing typing indicator from a previous message
    _stop_typing_for_context(bot_name, message)

    # Start persistent typing indicator
    api_tid = _api_thread_id(message)
    # Typing indicator: only pass thread_id for forum groups. In private
    # chats Telegram accepts message_thread_id on sendChatAction but does
    # not display the typing indicator visually inside the reply thread.
    # Sending without thread_id shows typing at the chat level, which is
    # the only place Telegram shows it for non-forum conversations.
    is_forum = getattr(message.chat, "is_forum", False)
    typing_thread_id = api_tid if is_forum else None
    typing_stop = _start_typing(instance.bot.token, message.chat.id, thread_id=typing_thread_id)

    context = await _get_or_create_context(bot_name, bot_cfg, message)
    if not context:
        typing_stop.set()
        await _send_with_temp_bot(
            instance.bot.token, message.chat.id,
            "Failed to create chat session.",
            parse_mode=None,
            message_thread_id=api_tid,
        )
        return

    # Safety: stop any typing stop event already on the context (e.g. rapid re-send)
    old_stop = context.data.get(CTX_TG_TYPING_STOP)
    if old_stop:
        old_stop.set()

    context.data[CTX_TG_TYPING_STOP] = typing_stop

    # Reply-to tracking for group chats
    reply_to_id = None
    if message.chat.type != "private" and instance.bot_info:
        if (message.reply_to_message
                and message.reply_to_message.from_user
                and message.reply_to_message.from_user.id == instance.bot_info.id):
            reply_to_id = message.message_id
    context.data[CTX_TG_REPLY_TO] = reply_to_id

    text = _extract_message_content(message)

    async with _temp_bot(instance.bot.token) as dl_bot:
        attachments = await _download_attachments(dl_bot, message, bot_name=bot_name)

    # Transcribe voice/audio messages using Whisper STT (native runtime, model in memory)
    if message.voice or message.audio or message.video_note:
        for attr, label in [("voice", "Voice message"), ("audio", "Audio"), ("video_note", "Video note")]:
            obj = getattr(message, attr, None)
            if not obj:
                continue
            # Find the downloaded file for this attachment type
            tg_prefix = f"a2t_{bot_name}_" if bot_name else "a2t_"
            download_dir = files.get_abs_path(DOWNLOAD_FOLDER)
            transcribed = False
            for att_path in (attachments or []):
                # att_path is dockerized; resolve to absolute path for reading
                abs_att = files.get_abs_path(att_path) if not os.path.isabs(att_path) else att_path
                if os.path.isfile(abs_att):
                    transcript = await _transcribe_audio_file(att_path)
                    if transcript:
                        marker = f"[{label} -- see attachment]"
                        text = text.replace(marker, f"[{label} transcribed]: {transcript}")
                        transcribed = True
                        PrintStyle.info(f"A2T: {label} transcribed via Whisper ({len(transcript)} chars)")
                        break
            if not transcribed and f"[{label} -- see attachment]" in text:
                PrintStyle.info(f"A2T: {label} could not be transcribed (Whisper unavailable or failed)")

    agent = context.agent0
    user_msg = agent.read_prompt(
        "fw.a2t.user_message.md",
        sender=_format_user(user),
        body=text,
    )

    msg_id = str(uuid.uuid4())
    mq.log_user_message(context, user_msg, attachments, message_id=msg_id, source=" (a2t)")
    context.communicate(UserMessage(
        message=user_msg,
        attachments=attachments,
        id=msg_id,
    ))

    save_tmp_chat(context)

    if bot_cfg.get("notify_messages", False):
        username_str = _safe_username(user)
        preview = (text[:80] + "...") if len(text) > 80 else text
        NotificationManager.send_notification(
            type=NotificationType.INFO,
            priority=NotificationPriority.HIGH,
            title="A2T: new message",
            message=f"From {username_str}: {preview}",
            display_time=10,
            group="a2t",
        )


# ---------------------------------------------------------------------------
#  Context management
# ---------------------------------------------------------------------------

def _get_existing_context(bot_name: str, user_id: int, chat_id: int, thread_id: int = 0) -> AgentContext | None:
    """Get existing context without creating a new one."""
    key = _map_key(bot_name, user_id, chat_id, thread_id)
    with _chat_map_lock:
        state = _load_state()
        ctx_id = state.get("chats", {}).get(key)
        if ctx_id:
            return AgentContext.get(ctx_id)
    return None


async def _reply_no_context(bot_name: str, message: TgMessage):
    """Reply that there's no active context."""
    instance = get_bot(bot_name)
    if instance:
        await _send_with_temp_bot(
            instance.bot.token, message.chat.id,
            "No active conversation. Send a message first.",
            parse_mode=None,
            message_thread_id=_api_thread_id(message),
        )


async def _get_or_create_context(
    bot_name: str,
    bot_cfg: dict,
    message: TgMessage,
) -> AgentContext | None:
    user = message.from_user
    if not user:
        return None
    return await _get_or_create_context_from_user(
        bot_name, bot_cfg, user.id, user.username, message.chat.id, _message_thread_id(message),
        api_thread_id=_api_thread_id(message),
    )


async def _get_or_create_context_from_user(
    bot_name: str,
    bot_cfg: dict,
    user_id: int,
    username: str | None,
    chat_id: int,
    thread_id: int = 0,
    api_thread_id: int | None = None,
) -> AgentContext | None:
    key = _map_key(bot_name, user_id, chat_id, thread_id)

    with _chat_map_lock:
        state = _load_state()
        chats = state.setdefault("chats", {})
        ctx_id = chats.get(key)

        # Migration: forum General topic key changed from :0 to :1.
        # If the new key is not found but a legacy :0 key exists, migrate it.
        if not ctx_id and thread_id == 1:
            legacy_key = _map_key(bot_name, user_id, chat_id, 0)
            legacy_ctx_id = chats.get(legacy_key)
            if legacy_ctx_id:
                ctx_id = legacy_ctx_id
                chats.pop(legacy_key, None)
                chats[key] = ctx_id
                # Migrate user_projects key too
                up = state.get("user_projects", {})
                if legacy_key in up:
                    up[key] = up.pop(legacy_key)
                _save_state(state)
                PrintStyle.info(f"A2T: migrated General topic key {legacy_key} -> {key}")

        if ctx_id:
            ctx = AgentContext.get(ctx_id)
            if ctx:
                # Update API thread id on existing context (may be missing on old sessions)
                if api_thread_id is not None and ctx.data.get(CTX_TG_API_THREAD_ID) is None:
                    ctx.data[CTX_TG_API_THREAD_ID] = api_thread_id
                return ctx
            chats.pop(key, None)

        try:
            config = initialize_agent()
            display_name = f"@{username}" if username else str(user_id)
            # Avoid partial §secret() masking if username overlaps a secret value
            if username:
                try:
                    from helpers.secrets import get_secrets_manager
                    if "§§secret(" in get_secrets_manager(None).mask_values(username):
                        display_name = f"id:{user_id}"
                except Exception:
                    pass
            ctx = AgentContext(config, name=f"A2T: {display_name}" + (f" [topic {thread_id}]" if thread_id else ""))

            ctx.data[CTX_TG_BOT] = bot_name
            ctx.data[CTX_TG_BOT_CFG] = bot_cfg
            ctx.data[CTX_TG_CHAT_ID] = chat_id
            ctx.data[CTX_TG_USER_ID] = user_id
            ctx.data[CTX_TG_USERNAME] = username or ""
            ctx.data[CTX_TG_THREAD_ID] = thread_id
            ctx.data[CTX_TG_API_THREAD_ID] = api_thread_id

            # Check persisted project from state first, then bot config
            project = _load_state().get("user_projects", {}).get(key, "")
            if not project:
                project = _get_project(bot_cfg, user_id, thread_id)
            if project:
                ctx.data[CTX_TG_PROJECT] = project
                projects.activate_project(ctx.id, project)

            # Try to inherit model override from sibling context
            _inherit_model_override(ctx)

            chats[key] = ctx.id
            _save_state(state)

            PrintStyle.success(
                f"A2T ({bot_name}): new chat {ctx.id} for user {display_name}"
            )
            return ctx

        except Exception as e:
            PrintStyle.error(f"A2T: failed to create context: {format_error(e)}")
            return None


# ---------------------------------------------------------------------------
#  Message content extraction
# ---------------------------------------------------------------------------

def _extract_message_content(message: TgMessage) -> str:
    parts = []

    if message.text:
        parts.append(message.text)
    elif message.caption:
        parts.append(message.caption)

    if message.location:
        loc = message.location
        parts.append(f"[Location: {loc.latitude}, {loc.longitude}]")

    if message.contact:
        c = message.contact
        parts.append(f"[Contact: {c.first_name} {c.last_name or ''} phone={c.phone_number}]")

    if message.sticker:
        parts.append(f"[Sticker: {message.sticker.emoji or ''}]")

    for attr, label in [("voice", "Voice message"), ("video_note", "Video note")]:
        if getattr(message, attr, None):
            parts.append(f"[{label} -- see attachment]")

    if message.animation:
        parts.append("[Animation/GIF -- see attachment]")
    if message.poll:
        poll = message.poll
        parts.append(f"[Poll: {poll.question}]")
        for opt in poll.options:
            parts.append(f"  - {opt.text}")
    if message.dice:
        parts.append(f"[Dice: {message.dice.emoji} = {message.dice.value}]")
    if message.game:
        parts.append(f"[Game: {message.game.title}]")
    if message.venue:
        v = message.venue
        parts.append(f"[Venue: {v.title} at {v.location.latitude}, {v.location.longitude}]")

    return "\n".join(parts) if parts else "[No text content]"


async def _transcribe_audio_file(file_path: str) -> str | None:
    """Transcribe audio file using Whisper STT runtime (model in memory).

    Returns transcribed text or None if Whisper is unavailable.
    """
    try:
        from plugins._whisper_stt.helpers import runtime as whisper_runtime
        if not whisper_runtime.is_globally_enabled():
            return None

        abs_path = files.get_abs_path(file_path)
        if not os.path.isfile(abs_path):
            return None

        with open(abs_path, "rb") as f:
            audio_b64 = base64.b64encode(f.read()).decode("utf-8")

        result = await whisper_runtime.transcribe(audio_b64)
        text = str(result.get("text") or "").strip()
        return text if text else None
    except Exception as e:
        PrintStyle.error(f"A2T: Whisper transcription failed: {format_error(e)}")
        return None


async def _download_attachments(bot, message: TgMessage, bot_name: str = "") -> list[str]:
    """Download photos, documents, audio, voice, video from message."""
    paths: list[str] = []
    tg_prefix = f"a2t_{bot_name}_" if bot_name else "a2t_"
    download_dir = files.get_abs_path(DOWNLOAD_FOLDER)
    os.makedirs(download_dir, exist_ok=True)
    download_dir_ref = files.get_abs_path_dockerized(DOWNLOAD_FOLDER)

    async def _dl(file_id: str, filename: str) -> str | None:
        safe_name = f"{tg_prefix}{uuid.uuid4().hex[:8]}_{filename}"
        dest = os.path.join(download_dir, safe_name)
        result = await tc.download_file(bot, file_id, dest)
        if result:
            return os.path.join(download_dir_ref, safe_name)
        return None

    if message.photo:
        photo = message.photo[-1]
        path = await _dl(photo.file_id, f"photo_{photo.file_unique_id}.jpg")
        if path:
            paths.append(path)

    _types = [
        ("document", "file", None),
        ("audio", "audio", ".mp3"),
        ("voice", "voice", ".ogg"),
        ("video", "video", ".mp4"),
        ("video_note", "videonote", ".mp4"),
    ]
    for attr, prefix, ext in _types:
        obj = getattr(message, attr, None)
        if not obj:
            continue
        raw_name = getattr(obj, "file_name", None) or f"{prefix}_{obj.file_unique_id}{ext or ''}"
        # Sanitize: strip path components to prevent directory traversal
        fname = os.path.basename(raw_name).replace("..", "_")
        if not fname:
            fname = f"{prefix}_{obj.file_unique_id}{ext or ''}"
        path = await _dl(obj.file_id, fname)
        if path:
            paths.append(path)

    return paths


# ---------------------------------------------------------------------------
#  Reply sending (called from process_chain_end extension)
# ---------------------------------------------------------------------------

async def send_telegram_reply(
    context: AgentContext,
    response_text: str,
    attachments: list[str] | None = None,
    keyboard: list[list[dict]] | None = None,
) -> str | None:
    """Send reply to Telegram user. Returns error string or None on success."""
    bot_name = context.data.get(CTX_TG_BOT)
    if not bot_name:
        return "No A2T bot configured on context"

    instance = get_bot(bot_name)
    if not instance:
        return f"Bot '{bot_name}' not running"

    chat_id = context.data.get(CTX_TG_CHAT_ID)
    if not chat_id:
        return "No chat_id on context"

    reply_to = context.data.get(CTX_TG_REPLY_TO)
    thread_id = context.data.get(CTX_TG_API_THREAD_ID)

    try:
        async with _temp_bot(instance.bot.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML)) as reply_bot:
            if attachments:
                for path in attachments:
                    local_path = files.fix_dev_path(path)
                    if tc.is_image_file(local_path):
                        await tc.send_photo(reply_bot, chat_id, message_thread_id=thread_id, photo_path=local_path, reply_to_message_id=reply_to)
                    else:
                        await tc.send_file(reply_bot, chat_id, message_thread_id=thread_id, file_path=local_path, reply_to_message_id=reply_to)

            if response_text:
                html_text = tc.md_to_telegram_html(response_text)
                if keyboard:
                    await tc.send_text_with_keyboard(reply_bot, chat_id, message_thread_id=thread_id, text=html_text, buttons=keyboard, reply_to_message_id=reply_to)
                else:
                    await tc.send_text(reply_bot, chat_id, message_thread_id=thread_id, text=html_text, reply_to_message_id=reply_to)

        return None

    except Exception as e:
        error = format_error(e)
        PrintStyle.error(f"A2T reply failed: {error}")
        return error


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _temp_bot(token: str, **kwargs):
    """Create a temporary Bot, yield it, and ensure the session is closed."""
    bot = Bot(token=token, **kwargs)
    try:
        yield bot
    finally:
        with suppress(Exception):
            await bot.session.close()


async def _send_with_temp_bot(
    token: str,
    chat_id: int,
    text: str,
    message_thread_id: int | None = None,
    parse_mode: str | None = "HTML",
):
    """Send text using a temporary Bot to avoid cross-event-loop session issues."""
    async with _temp_bot(token) as bot:
        await tc.send_text(
            bot,
            chat_id,
            message_thread_id=message_thread_id,
            text=text,
            parse_mode=parse_mode,
        )


def _stop_typing_for_context(bot_name: str, message: TgMessage):
    """Stop any active typing indicator on an existing context for this chat/thread."""
    ctx = _get_existing_context(bot_name, message.from_user.id, message.chat.id, _message_thread_id(message))
    if not ctx:
        return
    old_stop = ctx.data.pop(CTX_TG_TYPING_STOP, None)
    if old_stop:
        old_stop.set()


def _start_typing(token: str, chat_id: int, thread_id: int | None = None) -> threading.Event:
    """Spawn a daemon thread that sends typing every 4s. Returns a stop Event.

    The thread automatically stops after MAX_TYPING_SECONDS to prevent
    persistent 'typing...' indicators if the stop event is never set
    (e.g. context reset, agent crash, lost reference).
    """
    stop = threading.Event()
    max_typing_seconds = 300  # 5 minutes safety timeout

    def _run():
        import asyncio

        async def _loop():
            async with _temp_bot(token) as bot:
                deadline = asyncio.get_event_loop().time() + max_typing_seconds
                PrintStyle.debug(f"A2T: typing loop started, chat_id={chat_id}, thread_id={thread_id}")
                while not stop.is_set():
                    if asyncio.get_event_loop().time() > deadline:
                        PrintStyle.info("A2T: typing indicator timed out, stopping")
                        return
                    await tc.send_typing(bot, chat_id, message_thread_id=thread_id)
                    for _ in range(8):
                        if stop.is_set():
                            return
                        await asyncio.sleep(0.5)

        try:
            PrintStyle.debug(f"A2T: starting typing thread for chat_id={chat_id}, thread_id={thread_id}")
            asyncio.run(_loop())
            PrintStyle.debug(f"A2T: typing thread ended for chat_id={chat_id}, thread_id={thread_id}")
        except Exception:
            import traceback
            PrintStyle.error(f"A2T: typing thread crashed: {traceback.format_exc()}")
            pass

    threading.Thread(target=_run, daemon=True).start()
    return stop


def _safe_username(user) -> str:
    """Return @username, or fall back to id:NNN if username overlaps a secret value."""
    if not user.username:
        return str(user.id)
    try:
        from helpers.secrets import get_secrets_manager
        masked = get_secrets_manager(None).mask_values(user.username)
        if "§§secret(" in masked:
            return f"id:{user.id}"
    except Exception:
        pass
    return f"@{user.username}"

def _format_user(user) -> str:
    name = user.first_name or ""
    if user.last_name:
        name += f" {user.last_name}"
    if user.username:
        name += f" ({_safe_username(user)})"
    return name.strip() or str(user.id)


def _inherit_model_override(ctx: AgentContext):
    """Copy chat_model_override from the most recent sibling context in the same project."""
    project = ctx.get_data("project")
    if not project:
        return
    try:
        from plugins._model_config.helpers.model_config import is_chat_override_allowed
        if not is_chat_override_allowed(ctx.agent0):
            return
    except Exception:
        return
    source = max(
        (c for c in AgentContext.all()
         if c.id != ctx.id and c.get_data("project") == project and c.get_data("chat_model_override")),
        key=lambda c: c.last_message,
        default=None,
    )
    if source:
        ctx.set_data("chat_model_override", source.get_data("chat_model_override"))


# ---------------------------------------------------------------------------
#  Graceful shutdown
# ---------------------------------------------------------------------------

async def shutdown():
    """Stop all A2T bots for graceful shutdown."""
    await stop_all_bots()
