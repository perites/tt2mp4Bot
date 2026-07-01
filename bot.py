from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from uuid import uuid4, UUID

import yt_dlp
from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    InputMediaVideo,
    InputMediaAnimation,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ChosenInlineResultHandler,
    ContextTypes,
    InlineQueryHandler,
)
from telegram.ext import MessageHandler, filters
from telegram.helpers import escape_markdown

load_dotenv()
from error_handler import error_handler

# ─────────────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────────────

BOT_TOKEN = os.getenv("BOT_TOKEN")
CACHE_CHANNEL = os.getenv("CACHE_CHANNEL")
PLACEHOLDER_FILE_LINK = os.getenv('PLACEHOLDER_FILE_LINK')

URL_RE = re.compile(
    r"https?://(?:"
    r"(?:www\.|vm\.|vt\.)?tiktok\.com/\S+"  # TikTok
    r"|(?:www\.)?instagram\.com/(?:reel|reels)/\S+"  # Instagram Reels
    r"|(?:www\.)?youtube\.com/shorts/\S+"  # YouTube Shorts
    r"|youtu\.be/\S+"  # YouTube short links
    r")",
    re.IGNORECASE,
)
MAX_BYTES = 50 * 1024 * 1024  # Telegram's 50 MB upload cap

executor = ThreadPoolExecutor(max_workers=4)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)


@dataclass
class UserRequestVideo:
    url: str
    _id: UUID = field(default_factory=uuid4)
    file_id: str = ''
    size_bytes: int = 0
    size_str: str = ''
    too_large: bool = True
    title: str = ''
    author: str = ''
    thumbnail_url: str = ''

    @property
    def id(self) -> str:
        return str(self._id)

    def messages(self, name, category) -> str | InlineKeyboardMarkup:
        messages = {
            'download': '(ノ>ω<)ノ  starting download…',
            'too_large': f'(×_×)  video too large - {self.size_str}'
        }

        match category:
            case _ if category is str:
                return messages[name]
            case _ if category is InlineKeyboardMarkup:
                return InlineKeyboardMarkup([[
                    InlineKeyboardButton(messages[name], callback_data="noop", )
                ]])

    @property
    def final_video(self) -> InputMediaVideo:
        return InputMediaVideo(
            media=self.file_id,
            caption=self.caption,
            parse_mode=ParseMode.MARKDOWN_V2,
            supports_streaming=True,
        )

    @property
    def caption(self) -> str:
        return (f'{escape_markdown(self.title[:200], version=2)}\n'
                f'by {escape_markdown(self.author, version=2)}\n'
                f'[{escape_markdown('(=^･ω･^=)', version=2)}]({self.url})')

    @classmethod
    async def find(cls, url) -> UserRequestVideo:
        ur_video = video_cache.get(url)
        if not ur_video:
            ur_video = UserRequestVideo(url)
            await asyncio.get_running_loop().run_in_executor(executor, ur_video.fetch_info)
            video_cache[url] = ur_video

        return ur_video

    def fetch_info(self):
        """Fetches metadata only — no download."""
        log.info("FETCHING NEW INFO")
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(self.url, download=False)
            self.size_bytes = self.get_size(info)
            self.size_str = f"{self.size_bytes / 1024 / 1024:.1f} MB"
            self.too_large = self.size_bytes > MAX_BYTES
            self.title = self.get_title(info)
            self.author = info.get("uploader") or info.get("creator") or "creator"
            self.thumbnail_url = info.get("thumbnail")

    @staticmethod
    def get_size(info) -> int:
        size_bytes = info.get("filesize") or info.get("filesize_approx") or 0
        if not size_bytes and "requested_formats" in info:
            size_bytes = sum(
                f.get("filesize") or f.get("filesize_approx") or 0
                for f in info["requested_formats"]
            )

        return size_bytes

    def get_title(self, info) -> str:
        if 'instagram' in self.url:
            return info.get("description") or "words"

        return info.get("title") or "words"

    # ─────────────────────────────────────────────────────────────────
    #  Video
    # ─────────────────────────────────────────────────────────────────
    async def process_video(self, bot) -> None:
        try:
            log.info(f"Processing {self.url}")

            loop = asyncio.get_running_loop()

            with tempfile.TemporaryDirectory() as tmpdir:
                # download in thread pool so the event loop stays free
                filepath = await loop.run_in_executor(
                    executor,
                    lambda: self.download_video(self.url, tmpdir)
                )

                with open(filepath, "rb") as f:
                    msg = await bot.send_video(
                        chat_id=CACHE_CHANNEL,
                        video=f,
                        supports_streaming=True,
                        read_timeout=180,
                        write_timeout=180,
                    )
                    self.file_id = msg.video.file_id

        except Exception as e:
            log.error("process_video error: %s", e, exc_info=True)

    @staticmethod
    def download_video(url: str, tmpdir: str) -> str:
        out_tmpl = os.path.join(tmpdir, "%(id)s.%(ext)s")
        opts = {
            "outtmpl": out_tmpl,
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "merge_output_format": "mp4",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
        }

        with yt_dlp.YoutubeDL(opts) as ydl:

            info = ydl.extract_info(url, download=True)
            filepath = ydl.prepare_filename(info)

        base, _ = os.path.splitext(filepath)
        mp4 = base + ".mp4"
        if not os.path.exists(mp4):
            for f in os.listdir(tmpdir):
                mp4 = os.path.join(tmpdir, f)
                break

        return mp4


# ─────────────────────────────────────────────────────────────────
#  STATE
# ─────────────────────────────────────────────────────────────────
pending: dict[str, UserRequestVideo] = {}
video_cache: dict[str, UserRequestVideo] = {}


async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.inline_query.query.strip()
    match = URL_RE.search(query)

    if not match:
        await update.inline_query.answer(
            [],
            cache_time=0,
        )
        return

    url = match.group(0)

    ur_video = await UserRequestVideo.find(url)

    if ur_video.too_large:
        await update.inline_query.answer(
            [InlineQueryResultArticle(id='hint', title=f"{ur_video.messages('too_large', str)}",
                                      input_message_content=InputTextMessageContent(
                                          f"{ur_video.messages('too_large', str)}: [link]({ur_video.url})",
                                          parse_mode=ParseMode.MARKDOWN_V2))],

            cache_time=0,
        )
        return

    pending[ur_video.id] = ur_video

    result = InlineQueryResultArticle(
        id=ur_video.id,
        title=f"{ur_video.title[:60]}",
        description=f"by {ur_video.author}  •  size {ur_video.size_str}",
        thumbnail_url=ur_video.thumbnail_url,
        input_message_content=InputTextMessageContent(f"will download {ur_video.size_str}…"),
        reply_markup=ur_video.messages('download', InlineKeyboardMarkup),
    )

    await update.inline_query.answer([result], cache_time=0)


async def chosen_inline_result(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ur_video_id = update.chosen_inline_result.result_id
    inline_message_id = update.chosen_inline_result.inline_message_id

    ur_video = pending.pop(ur_video_id, None)
    if not ur_video.url or not inline_message_id:
        return

    await context.bot.edit_message_media(
        inline_message_id=inline_message_id,
        media=InputMediaAnimation(
            media=PLACEHOLDER_FILE_LINK,
        ),
        reply_markup=ur_video.messages('download', InlineKeyboardMarkup),
    )

    if not ur_video.file_id:
        await ur_video.process_video(context.bot)

    await context.bot.edit_message_media(
        inline_message_id=inline_message_id,
        media=ur_video.final_video,
        reply_markup=None,
    )


async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    match = URL_RE.search(text)
    if not match:
        return

    url = match.group(0)
    ur_video = await UserRequestVideo.find(url)

    response = await update.message.reply_animation(
        animation=PLACEHOLDER_FILE_LINK,
        reply_markup=ur_video.messages('download', InlineKeyboardMarkup)
    )

    if ur_video.too_large:
        await response.edit_reply_markup(reply_markup=ur_video.messages('too_large', InlineKeyboardMarkup))
        return

    if not ur_video.file_id:
        await ur_video.process_video(context.bot)

    await response.edit_media(
        media=ur_video.final_video,
        reply_markup=None,
    )


# ─────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────
def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(InlineQueryHandler(inline_query))
    app.add_handler(ChosenInlineResultHandler(chosen_inline_result))
    app.add_handler(MessageHandler(
        filters.TEXT & filters.ChatType.GROUPS & ~filters.COMMAND,
        handle_group_message,
    ))
    app.add_error_handler(error_handler)

    print("Bot running!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
