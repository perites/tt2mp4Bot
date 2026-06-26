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
from telegram.helpers import escape_markdown

# ─────────────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────────────
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
CACHE_CHANNEL = os.getenv("CACHE_CHANNEL")
PLACEHOLDER_FILE_ID = os.getenv('PLACEHOLDER_FILE_ID')

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
#  STATE
# ─────────────────────────────────────────────────────────────────
pending: dict[str, UserRequestVideo] = {}
video_cache: dict[str, UserRequestVideo] = {}


# ─────────────────────────────────────────────────────────────────
#  Video
# ─────────────────────────────────────────────────────────────────
async def process_video(bot, ur_video: UserRequestVideo, inline_message_id: str) -> UserRequestVideo:
    try:
        log.info(f"Downloading {ur_video.url}")

        loop = asyncio.get_running_loop()

        with tempfile.TemporaryDirectory() as tmpdir:
            # download in thread pool so the event loop stays free
            filepath = await loop.run_in_executor(
                executor,
                lambda: download_video(ur_video, tmpdir)
            )

            with open(filepath, "rb") as f:
                msg = await bot.send_video(
                    chat_id=CACHE_CHANNEL,
                    video=f,
                    supports_streaming=True,
                    read_timeout=180,
                    write_timeout=180,
                )
                ur_video.file_id = msg.video.file_id

        return ur_video

    except Exception as e:
        log.error("process_video error: %s", e, exc_info=True)


def download_video(ur_video: UserRequestVideo, tmpdir: str) -> str:
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

        info = ydl.extract_info(ur_video.url, download=True)
        filepath = ydl.prepare_filename(info)

    base, _ = os.path.splitext(filepath)
    mp4 = base + ".mp4"
    if not os.path.exists(mp4):
        for f in os.listdir(tmpdir):
            mp4 = os.path.join(tmpdir, f)
            break

    return mp4


async def send_final_message(bot, ur_video: UserRequestVideo, inline_message_id: str):
    caption = (f'{escape_markdown(ur_video.title[:200], version=2)}\n'
               f'by {escape_markdown(ur_video.author, version=2)}\n'
               f'[{escape_markdown('(=^･ω･^=)', version=2)}]({ur_video.url})')

    await bot.edit_message_media(
        inline_message_id=inline_message_id,
        media=InputMediaVideo(
            media=ur_video.file_id,
            caption=caption,
            parse_mode=ParseMode.MARKDOWN_V2,
            supports_streaming=True,
        ),
        reply_markup=None,
    )


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

    ur_video = video_cache.get(url)
    if not ur_video:
        ur_video = UserRequestVideo(url)
        await asyncio.get_running_loop().run_in_executor(executor, ur_video.fetch_info)

    if ur_video.too_large:
        await update.inline_query.answer(
            [InlineQueryResultArticle(id='hint', title=f"(×_×)  video too large - {ur_video.size_str}",
                                      input_message_content=InputTextMessageContent(
                                          f"(×_×)  video too large to download: <a href='{ur_video.url}'>link</a> ",
                                          parse_mode='HTML'))],

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
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("(ノ>ω<)ノ starting download…", callback_data="noop")
        ]]),
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
            media=PLACEHOLDER_FILE_ID,
        ),
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("(ノ>ω<)ノ  starting download…", callback_data="noop")
        ]]),
    )

    if not ur_video.file_id:
        ur_video = await process_video(context.bot, ur_video, inline_message_id)

    await send_final_message(context.bot, ur_video, inline_message_id)
    video_cache[ur_video.url] = ur_video


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

    print("Bot running!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
