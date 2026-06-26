import asyncio
import logging
import os
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from uuid import uuid4

import yt_dlp
from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultCachedGif,
    InputMediaVideo,
    InputMediaAnimation,
    Update,
)
from telegram.ext import (
    Application,
    ChosenInlineResultHandler,
    ContextTypes,
    InlineQueryHandler,
)

# ─────────────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────────────
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
CACHE_CHANNEL = os.getenv("CACHE_CHANNEL")
PLACEHOLDER_FILE_ID = os.getenv('PLACEHOLDER_FILE_ID')

TIKTOK_RE = re.compile(
    r"https?://(?:"
    r"(?:www\.|vm\.|vt\.)?tiktok\.com/\S+"  # TikTok
    r"|(?:www\.)?instagram\.com/(?:reel|reels)/\S+"  # Instagram Reels
    r"|(?:www\.)?youtube\.com/shorts/\S+"  # YouTube Shorts
    r"|youtu\.be/\S+"  # YouTube short links
    r")",
    re.IGNORECASE,
)
MAX_BYTES = 50 * 1024 * 1024  # Telegram's 50 MB upload cap

# Button shown while video is processing
LOADING_KEYBOARD = InlineKeyboardMarkup([[
    InlineKeyboardButton("⏳ Downloading video…", callback_data="noop")
]])

executor = ThreadPoolExecutor(max_workers=4)

# ─────────────────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────
#  STATE
# ─────────────────────────────────────────────────────────────────
# result_id  →  url  (set in inline_query, read in chosen_inline_result)
pending: dict[str, str] = {}

# url  →  telegram file_id  (avoid re-downloading the same video)
video_cache: dict[str, dict] = {}


# ─────────────────────────────────────────────────────────────────
#  PROGRESS TRACKING
# ─────────────────────────────────────────────────────────────────
@dataclass
class DownloadProgress:
    total_bytes: int = 0
    downloaded_bytes: int = 0
    speed: float = 0.0  # bytes/sec
    done: bool = False

    @property
    def percent(self) -> int:
        if not self.total_bytes:
            return 0
        return min(int(self.downloaded_bytes / self.total_bytes * 100), 100)

    @property
    def total_mb(self) -> str:
        return f"{self.total_bytes / 1024 / 1024:.1f} MB" if self.total_bytes else "? MB"

    @property
    def speed_str(self) -> str:
        if not self.speed:
            return ""
        if self.speed >= 1024 * 1024:
            return f"{self.speed / 1024 / 1024:.1f} MB/s"
        return f"{self.speed / 1024:.0f} KB/s"


def make_progress_hook(progress: DownloadProgress):
    def hook(d):
        if d["status"] == "downloading":
            progress.total_bytes = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            progress.downloaded_bytes = d.get("downloaded_bytes", 0)
            progress.speed = d.get("speed") or 0.0
        elif d["status"] == "finished":
            progress.done = True

    return hook


def progress_bar(percent: int, width: int = 10) -> str:
    filled = int(percent / 100 * width)
    return "█" * filled + " - " * (width - filled)


async def progress_updater(bot, inline_message_id: str, progress: DownloadProgress):
    """Edits the button text every 4 seconds with live download stats."""
    await asyncio.sleep(1)  # give yt-dlp time to fetch headers + file size

    while not progress.done:
        try:
            pct = progress.percent
            bar = progress_bar(pct)
            parts = [f"(ノ>ω<)ノ  {bar} {pct}%", progress.total_mb]

            await bot.edit_message_reply_markup(
                inline_message_id=inline_message_id,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(" · ".join(parts), callback_data="noop")
                ]]),
            )
        except asyncio.CancelledError:
            break
        except Exception:
            pass  # silently ignore edit failures (rate limit, etc.)

        await asyncio.sleep(1)  # safe for Telegram's rate limit


# ─────────────────────────────────────────────────────────────────
#  DOWNLOAD
# ─────────────────────────────────────────────────────────────────


def get_video_size(url: str) -> int:
    """Returns expected file size in bytes before downloading. 0 if unknown."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    # try exact size first, fall back to estimate
    size = info.get("filesize") or info.get("filesize_approx") or 0

    # if merging video+audio, sum both streams
    if not size and "requested_formats" in info:
        size = sum(
            f.get("filesize") or f.get("filesize_approx") or 0
            for f in info["requested_formats"]
        )

    return size


def download_video(url: str, tmpdir: str, progress: DownloadProgress) -> tuple[str, dict]:
    out_tmpl = os.path.join(tmpdir, "%(id)s.%(ext)s")
    opts = {
        "outtmpl": out_tmpl,
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "progress_hooks": [make_progress_hook(progress)],
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

    return mp4, info


# ─────────────────────────────────────────────────────────────────
#  BACKGROUND TASK — runs AFTER the placeholder is already in chat
# ─────────────────────────────────────────────────────────────────
async def process_video(bot, url: str, inline_message_id: str) -> None:
    try:
        video_size = get_video_size(url)
        if video_size > MAX_BYTES:
            await bot.edit_message_media(
                inline_message_id=inline_message_id,
                media=InputMediaAnimation(
                    media=PLACEHOLDER_FILE_ID,
                    caption=f"╮( ˘ ､ ˘ )╭  Video is too large for Telegram: {int(video_size / (1024 * 1024))} Mb",
                ),
                reply_markup=None,
            )
            return

        # ── 1. Check cache ────────────────────────────────────────
        if url in video_cache:
            log.info("Cache hit → %s", video_cache[url]["file_id"])

        else:
            # ── 2. Download ───────────────────────────────────────
            log.info("Downloading %s", url)
            # with tempfile.TemporaryDirectory() as tmpdir:
            #     filepath, info = download_video(url, tmpdir)
            #
            #     if os.path.getsize(filepath) > MAX_BYTES:
            #         await bot.edit_message_media(
            #             inline_message_id=inline_message_id,
            #             media=InputMedia(
            #                 media_type="gif",
            #                 media=PLACEHOLDER_FILE_ID,
            #                 caption="❌ Video is too large for Telegram (> 50 MB)",
            #             ),
            #             reply_markup=None,
            #         )
            #         return
            #
            #     # ── 3. Upload to cache channel ────────────────────
            #     with open(filepath, "rb") as f:
            #         msg = await bot.send_video(
            #             chat_id=CACHE_CHANNEL,
            #             video=f,
            #             supports_streaming=True,
            #             read_timeout=180,
            #             write_timeout=180,
            #         )
            #
            # file_id = msg.video.file_id
            # video_cache[url] = {
            #     "file_id": file_id,
            #     "author": info.get("uploader") or info.get("creator") or "creator",
            #     "description": info.get("title") or "words",
            # }
            # log.info("Uploaded → file_id: %s", file_id)

            progress = DownloadProgress()
            loop = asyncio.get_event_loop()
            update_task = asyncio.create_task(
                progress_updater(bot, inline_message_id, progress)
            )

            try:
                with tempfile.TemporaryDirectory() as tmpdir:
                    # download in thread pool so the event loop stays free
                    filepath, info = await loop.run_in_executor(
                        executor,
                        lambda: download_video(url, tmpdir, progress),
                    )

                    progress.done = True
                    update_task.cancel()

                    title = info.get("title") or "words"
                    author = info.get("uploader") or info.get("creator") or "creator"

                    # upload to cache channel → get permanent file_id
                    with open(filepath, "rb") as f:
                        msg = await bot.send_video(
                            chat_id=CACHE_CHANNEL,
                            video=f,
                            supports_streaming=True,
                            read_timeout=180,
                            write_timeout=180,
                        )

            except Exception:
                progress.done = True
                update_task.cancel()
                raise

            video_cache[url] = {
                "file_id": msg.video.file_id,
                "description": title,
                "author": author,
            }
            log.info("Cached → %s", msg.video.file_id)

        cached = video_cache[url]
        file_id = cached["file_id"]
        author = cached["author"]
        description = cached["description"]

        # ── 4. Swap placeholder → real video ──────────────────────

        caption = ''
        if description:
            caption += f"{description[:200]}"  # trim long descriptions
        if author:
            caption += f"\nby {author}"

        await bot.edit_message_media(
            inline_message_id=inline_message_id,
            media=InputMediaVideo(
                media=file_id,
                caption=caption,  # ← add this
                supports_streaming=True,
            ),
            reply_markup=None,
        )

        log.info("Message updated ✅")

    except Exception as e:
        log.error("process_video error: %s", e, exc_info=True)


# ─────────────────────────────────────────────────────────────────
#  INLINE QUERY  — return placeholder instantly
# ─────────────────────────────────────────────────────────────────
async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.inline_query.query.strip()
    match = TIKTOK_RE.search(query)

    if not match:
        await update.inline_query.answer(
            [],
            cache_time=0,
        )

    url = match.group(0)
    result_id = str(uuid4())
    pending[result_id] = url  # store so chosen_inline_result can find it

    result = InlineQueryResultCachedGif(
        id=result_id,
        gif_file_id=PLACEHOLDER_FILE_ID,
        title="TikTok Video",
        reply_markup=LOADING_KEYBOARD,
    )

    # cache_time=0 → fresh result every time
    await update.inline_query.answer([result], cache_time=0)


# ─────────────────────────────────────────────────────────────────
#  CHOSEN INLINE RESULT  — fires after user taps send
# ─────────────────────────────────────────────────────────────────
async def chosen_inline_result(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result_id = update.chosen_inline_result.result_id
    inline_message_id = update.chosen_inline_result.inline_message_id

    url = pending.pop(result_id, None)

    if not url or not inline_message_id:
        log.warning("chosen_inline_result: no matching pending job for %s", result_id)
        return

    log.info("Starting background download for message %s", inline_message_id)

    # Fire and forget — does NOT block the bot
    await asyncio.create_task(
        process_video(context.bot, url, inline_message_id)
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

    print("Bot running!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
