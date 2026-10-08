"""
A2T Telegram client helpers.
Low-level Telegram API wrapper: send text/file/photo, Markdown->HTML converter,
keyboard builder, message splitting. Preserves A2T's rich formatting support
including tables, LaTeX stripping, and multi-level fallback sending.
"""

import os
import re
import html as html_module

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from helpers.errors import format_error
from helpers.print_style import PrintStyle

_UNSET = object()  # sentinel: "not provided" (lets Bot default apply)

MAX_MESSAGE_LENGTH: int = 4096


# ---------------------------------------------------------------------------
#  Thread fallback helper
# ---------------------------------------------------------------------------

async def _send_with_thread_fallback(bot_method, *args, message_thread_id: int | None = None, **kwargs):
    """Call a bot send method, retrying without message_thread_id if the thread is not found.

    Telegram returns 'thread not found' for deleted topics or when a thread_id
    is invalid.  This wrapper retries the call without message_thread_id so the
    message still lands in the chat.
    """
    try:
        if message_thread_id is not None:
            return await bot_method(*args, message_thread_id=message_thread_id, **kwargs)
        return await bot_method(*args, **kwargs)
    except TelegramBadRequest as e:
        if message_thread_id is not None and "thread" in str(e).lower() and "not found" in str(e).lower():
            PrintStyle.warning(f"A2T: thread {message_thread_id} not found, retrying without it")
            return await bot_method(*args, **kwargs)
        raise


# ---------------------------------------------------------------------------
#  Text messages
# ---------------------------------------------------------------------------

async def _send_chunk_with_fallback(
    bot: Bot,
    chat_id: int,
    chunk: str,
    reply_to_message_id: int | None,
    message_thread_id: int | None,
    pm_kwargs: dict,
    reply_markup=None,
) -> int | None:
    """Send one chunk; if formatting is rejected, retry as plain text.

    Guarantees delivery: on TelegramBadRequest (e.g. invalid HTML entities
    produced by the Markdown->HTML converter) the chunk is sanitized with
    strip_html_tags() and resent with parse_mode=None, preserving any
    reply_markup. Never retries with an empty payload.
    """
    try:
        msg = await _send_with_thread_fallback(
            bot.send_message,
            chat_id=chat_id,
            text=chunk,
            reply_to_message_id=reply_to_message_id,
            message_thread_id=message_thread_id,
            reply_markup=reply_markup,
            **pm_kwargs,
        )
        return msg.message_id
    except TelegramBadRequest as e:
        PrintStyle.warning(
            f"A2T: formatted send rejected, retrying as plain text: {format_error(e)}"
        )
        plain = strip_html_tags(chunk)
        if not plain.strip():
            plain = "(mensagem com formatação não suportada)"
        msg = await _send_with_thread_fallback(
            bot.send_message,
            chat_id=chat_id,
            text=plain,
            reply_to_message_id=reply_to_message_id,
            message_thread_id=message_thread_id,
            reply_markup=reply_markup,
            parse_mode=None,
        )
        return msg.message_id


async def send_text(
    bot: Bot,
    chat_id: int,
    message_thread_id: int | None = None,
    text: str = "",
    reply_to_message_id: int | None = None,
    parse_mode: object = _UNSET,
) -> int | None:
    """Send text message, splitting if too long. Returns last message_id or None on error.

    parse_mode behaviour:
      - _UNSET (default): omitted from send_message -> Bot's DefaultBotProperties applies.
      - None: explicitly no formatting.
      - "HTML"/"Markdown"/etc.: that specific mode.

    Each chunk falls back automatically to plain text when formatting is
    rejected by Telegram, so the message is always delivered.
    """
    try:
        chunks = split_message(text, MAX_MESSAGE_LENGTH)
        last_msg_id = None
        pm_kwargs: dict = {} if parse_mode is _UNSET else {"parse_mode": parse_mode}
        for chunk in chunks:
            last_msg_id = await _send_chunk_with_fallback(
                bot,
                chat_id,
                chunk,
                reply_to_message_id,
                message_thread_id,
                pm_kwargs,
            )
        return last_msg_id
    except Exception as e:
        PrintStyle.error(f"A2T send_text failed: {format_error(e)}")
        return None


async def send_text_with_keyboard(
    bot: Bot,
    chat_id: int,
    message_thread_id: int | None = None,
    text: str = "",
    buttons: list[list[dict]] = [],
    reply_to_message_id: int | None = None,
    parse_mode: object = _UNSET,
) -> int | None:
    """Send text with inline keyboard buttons.

    Falls back automatically to plain text (keyboard preserved) when
    formatting is rejected by Telegram, so the message is always delivered.
    """
    try:
        keyboard = build_inline_keyboard(buttons)
        pm_kwargs: dict = {} if parse_mode is _UNSET else {"parse_mode": parse_mode}
        chunks = split_message(text, MAX_MESSAGE_LENGTH)
        last_msg_id = None
        for chunk in chunks:
            last_msg_id = await _send_chunk_with_fallback(
                bot,
                chat_id,
                chunk,
                reply_to_message_id,
                message_thread_id,
                pm_kwargs,
                reply_markup=keyboard,
            )
        return last_msg_id
    except Exception as e:
        PrintStyle.error(f"A2T send_text_with_keyboard failed: {format_error(e)}")
        return None


# ---------------------------------------------------------------------------
#  Files and images
# ---------------------------------------------------------------------------

async def send_file(
    bot: Bot,
    chat_id: int,
    message_thread_id: int | None = None,
    file_path: str = "",
    caption: str = "",
    reply_to_message_id: int | None = None,
) -> int | None:
    """Send a file from local path. Returns message_id or None on error."""
    try:
        if not os.path.isfile(file_path):
            PrintStyle.error(f"A2T: file not found: {file_path}")
            return None
        input_file = FSInputFile(file_path)
        msg = await _send_with_thread_fallback(
            bot.send_document,
            chat_id=chat_id,
            document=input_file,
            caption=caption[:1024] if caption else None,
            reply_to_message_id=reply_to_message_id,
            message_thread_id=message_thread_id,
        )
        return msg.message_id
    except Exception as e:
        PrintStyle.error(f"A2T send_file failed: {format_error(e)}")
        return None


async def send_photo(
    bot: Bot,
    chat_id: int,
    message_thread_id: int | None = None,
    photo_path: str = "",
    caption: str = "",
    reply_to_message_id: int | None = None,
) -> int | None:
    """Send a photo from local path. Returns message_id or None on error."""
    try:
        if not os.path.isfile(photo_path):
            PrintStyle.error(f"A2T: photo not found: {photo_path}")
            return None
        input_file = FSInputFile(photo_path)
        msg = await _send_with_thread_fallback(
            bot.send_photo,
            chat_id=chat_id,
            photo=input_file,
            caption=caption[:1024] if caption else None,
            reply_to_message_id=reply_to_message_id,
            message_thread_id=message_thread_id,
        )
        return msg.message_id
    except Exception as e:
        PrintStyle.error(f"A2T send_photo failed: {format_error(e)}")
        return None


# ---------------------------------------------------------------------------
#  Inline keyboards
# ---------------------------------------------------------------------------

def build_inline_keyboard(
    buttons: list[list[dict]],
) -> InlineKeyboardMarkup:
    """Build inline keyboard from a list of rows.
    Each row is a list of dicts with keys: text, callback_data or url.
    """
    rows = []
    for row in buttons:
        row_buttons = []
        for btn in row:
            if "url" in btn:
                row_buttons.append(InlineKeyboardButton(
                    text=btn["text"], url=btn["url"],
                ))
            else:
                row_buttons.append(InlineKeyboardButton(
                    text=btn["text"],
                    callback_data=btn.get("callback_data", btn["text"]),
                ))
        rows.append(row_buttons)
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
#  Typing indicator
# ---------------------------------------------------------------------------

async def send_typing(bot: Bot, chat_id: int, message_thread_id: int | None = None):
    """Send 'typing...' action to chat, optionally in a specific topic."""
    try:
        kwargs = {"chat_id": chat_id, "action": "typing"}
        if message_thread_id is not None:
            kwargs["message_thread_id"] = message_thread_id
        try:
            await bot.send_chat_action(**kwargs)
        except TelegramBadRequest as e:
            # message_thread_id is only supported for forum supergroups in
            # sendChatAction.  Private chats with reply-thread mode reject it.
            # Retry without it so typing still works in private chat threads.
            if message_thread_id is not None and "thread" in str(e).lower():
                PrintStyle.debug(f"A2T: send_typing with thread_id failed, retrying without: {e}")
                await bot.send_chat_action(chat_id=chat_id, action="typing")
            else:
                raise
    except Exception as e:
        from helpers.print_style import PrintStyle
        PrintStyle.debug(f"A2T: send_typing error: {e}")


# ---------------------------------------------------------------------------
#  File download
# ---------------------------------------------------------------------------

async def download_file(
    bot: Bot,
    file_id: str,
    destination: str,
) -> str | None:
    """Download a file by file_id to destination path. Returns path or None on error."""
    try:
        file = await bot.get_file(file_id)
        if not file.file_path:
            return None
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        await bot.download_file(file.file_path, destination)
        return destination
    except Exception as e:
        PrintStyle.error(f"A2T download failed: {format_error(e)}")
        return None


# ---------------------------------------------------------------------------
#  Message splitting
# ---------------------------------------------------------------------------

def split_message(text: str, limit: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Split text into chunks that fit Telegram's message length limit.

    HTML-aware: never splits inside a tag and reopens/closes open tags
    at chunk boundaries so every chunk is valid HTML.
    """
    if len(text) <= limit:
        return [text]

    # HTML tag tracking for safe splitting
    TAG_RE = re.compile(r"<(/?)(\w+)[^>]*>")

    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break

        # Find a safe split position (newline preferred, then space)
        split_pos = text.rfind("\n", 0, limit)
        if split_pos == -1 or split_pos < limit // 2:
            split_pos = text.rfind(" ", 0, limit)
        if split_pos == -1:
            split_pos = limit

        # Walk back to avoid splitting inside a tag
        # If we're inside <...>, find the end of the tag and split after it
        lt_pos = text.rfind("<", 0, split_pos)
        gt_pos = text.rfind(">", 0, split_pos)
        if lt_pos > gt_pos:
            # We're inside a tag — find where it ends
            tag_end = text.find(">", split_pos)
            if tag_end != -1 and tag_end < limit + 200:
                split_pos = tag_end + 1
            else:
                # Can't find tag end; split at newline before the tag instead
                split_pos = text.rfind("\n", 0, lt_pos)
                if split_pos == -1:
                    split_pos = lt_pos

        chunk = text[:split_pos]
        # Preserve all whitespace — only skip a single newline at the split point
        # so we don't merge paragraphs but also don't lose meaningful blank lines.
        remaining = text[split_pos:]
        if remaining.startswith("\n"):
            remaining = remaining[1:]
        text = remaining

        # Determine which HTML tags are still open at the end of this chunk
        open_tags: list[str] = []
        for m in TAG_RE.finditer(chunk):
            tag_name = m.group(2).lower()
            if m.group(1) == "/":  # closing tag
                if open_tags and open_tags[-1] == tag_name:
                    open_tags.pop()
            else:
                # Don't track self-closing / void tags
                if tag_name not in ("br", "hr", "img", "input", "meta", "link"):
                    open_tags.append(tag_name)

        # Close open tags at end of chunk
        for tag in reversed(open_tags):
            chunk += f"</{tag}>"

        chunks.append(chunk)

        # Reopen closed tags at start of next text
        for tag in open_tags:
            text = f"<{tag}>" + text

    return chunks


# ---------------------------------------------------------------------------
#  HTML helpers
# ---------------------------------------------------------------------------

def strip_html_tags(text: str) -> str:
    """Remove HTML tags from text, preserving content."""
    clean = re.sub(r"<[^>]+>", "", text)
    return html_module.unescape(clean)


_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


def is_image_file(path: str) -> bool:
    _, ext = os.path.splitext(path.lower())
    return ext in _IMAGE_EXTENSIONS


# ---------------------------------------------------------------------------
#  Markdown -> Telegram HTML conversion (A2T's rich converter)
#  Supports: code blocks, inline code, tables (as <pre>), bold, italic,
#  strikethrough, links, images, headings, horizontal rules, LaTeX stripping.
# ---------------------------------------------------------------------------

def md_to_telegram_html(text: str) -> str:
    """Convert Markdown to Telegram-compatible HTML.
    This is A2T's enhanced converter that supports tables (rendered as
    monospace <pre> blocks), LaTeX stripping, and image indicators.
    """
    code_blocks: list[str] = []
    inline_codes: list[str] = []
    table_blocks: list[str] = []

    # -- Step 1: Stash code blocks and inline code --

    def save_code_block(m):
        code_blocks.append(m.group(2))
        return f"\x00CODEBLOCK{len(code_blocks) - 1}\x00"

    def save_inline_code(m):
        inline_codes.append(m.group(1))
        return f"\x00INLINECODE{len(inline_codes) - 1}\x00"

    text = re.sub(r"```(\w*)?\n?(.*?)```", save_code_block, text, flags=re.DOTALL)
    text = re.sub(r"`([^`]+)`", save_inline_code, text)

    # -- Step 2: Convert tables to monospace blocks --

    def convert_table(m):
        table_text = m.group(0)
        tlines = table_text.strip().split("\n")
        rows = []
        for tl in tlines:
            tl = tl.strip()
            if not tl.startswith("|"):
                continue
            if re.match(r"^\|[\s\-:|]+\|$", tl):
                continue
            cells = [c.strip() for c in tl.split("|")[1:-1]]
            rows.append(cells)
        if not rows:
            return table_text
        num_cols = max(len(r) for r in rows)
        col_widths = [0] * num_cols
        for row in rows:
            for i, cell in enumerate(row):
                if i < num_cols:
                    col_widths[i] = max(col_widths[i], len(cell))
        fmt_lines = []
        for ri, row in enumerate(rows):
            parts = []
            for i in range(num_cols):
                cell = row[i] if i < len(row) else ""
                parts.append(cell.ljust(col_widths[i]))
            fmt_lines.append(" | ".join(parts))
            if ri == 0:
                sep_parts = ["-" * w for w in col_widths]
                fmt_lines.append("-+-".join(sep_parts))
        result = "\n".join(fmt_lines)
        table_blocks.append(result)
        return f"\x00TABLEBLOCK{len(table_blocks) - 1}\x00"

    text = re.sub(r"(?:^\|.+\|$\n?)+", convert_table, text, flags=re.MULTILINE)

    # -- Step 3: Escape HTML entities --

    text = html_module.escape(text)

    # -- Step 4: Inline formatting --

    # Headings -> bold
    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)
    # Bold+italic
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"<b><i>\1</i></b>", text)
    text = re.sub(r"___(.+?)___", r"<b><i>\1</i></b>", text)
    # Bold
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)
    # Italic - asterisk form (always safe)
    text = re.sub(r"\*(.+?)\*", r"<i>\1</i>", text)
    # Italic - underscore form: only when surrounded by whitespace.
    # This prevents matching underscores inside identifiers like
    # "reorgchk_create.sh" or "variable_name".
    text = re.sub(r"(?<![\w\\])_([^_\s][^_]*?[^_\s])_(?![\w\\])", r"<i>\1</i>", text)
    # Strikethrough
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)
    # Links
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    # Image references
    text = re.sub(r"!\[([^\]]*)\]\(img:///([^)]+)\)", r"[image: \1 \2]", text)
    text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"[image: \1 \2]", text)
    # Horizontal rules
    text = re.sub(r"^[-*_]{3,}$", "---", text, flags=re.MULTILINE)
    # LaTeX stripping
    text = re.sub(r"<latex>(.*?)</latex>", r"\1", text)

    # -- Step 5: Restore stashed blocks (reverse order to avoid
    #            substring collisions: INLINECODE1 vs INLINECODE10) --

    for i in range(len(inline_codes) - 1, -1, -1):
        escaped_code = html_module.escape(inline_codes[i])
        text = text.replace(f"\x00INLINECODE{i}\x00", f"<code>{escaped_code}</code>")

    for i in range(len(code_blocks) - 1, -1, -1):
        escaped_block = html_module.escape(code_blocks[i])
        text = text.replace(f"\x00CODEBLOCK{i}\x00", f"<pre>{escaped_block}</pre>")

    for i in range(len(table_blocks) - 1, -1, -1):
        escaped_table = html_module.escape(table_blocks[i])
        text = text.replace(f"\x00TABLEBLOCK{i}\x00", f"<pre>{escaped_table}</pre>")

    return text
