import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pyrogram import Client, filters
from pyrogram.handlers import MessageHandler
from pyrogram.types import Message

import static_ffmpeg
static_ffmpeg.add_paths()  # ffmpeg/ffprobe na thakle download kore PATH-e add kore dey

INSERT_AT_SECONDS = 6 * 60
MAX_TELEGRAM_SEND_MB = 1900

@dataclass
class UserSession:
    main_message: Message | None = None
    waiting_for: str = "main"

sessions: dict[int, UserSession] = {}

def run_ffmpeg(args: list[str]) -> None:
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr[-4000:])

def ffprobe_duration(path: Path) -> float:
    p = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    if p.returncode != 0:
        raise RuntimeError(p.stderr[-2000:])
    return float(p.stdout.strip())

def video_has_audio(path: Path) -> bool:
    p = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    return p.returncode == 0 and bool(p.stdout.strip())

def normalize_video(source: Path, target: Path) -> None:
    args = ["ffmpeg", "-y", "-i", str(source)]
    if not video_has_audio(source):
        duration = ffprobe_duration(source)
        args += ["-f", "lavfi", "-t", f"{duration:.3f}", "-i",
                 "anullsrc=channel_layout=stereo:sample_rate=48000"]

    args += [
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,fps=30,format=yuv420p",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-ar", "48000", "-ac", "2",
        "-shortest", "-movflags", "+faststart", str(target)
    ]
    run_ffmpeg(args)

def trim_video(source: Path, start: float, duration: float | None, target: Path) -> None:
    args = ["ffmpeg", "-y"]
    if start > 0:
        args += ["-ss", f"{start:.3f}"]
    args += ["-i", str(source)]
    if duration is not None:
        args += ["-t", f"{duration:.3f}"]
    args += ["-c", "copy", "-avoid_negative_ts", "make_zero", str(target)]
    run_ffmpeg(args)

def concat_videos(parts: list[Path], target: Path) -> None:
    list_file = target.with_suffix(".txt")
    list_file.write_text(
        "".join(f"file '{part.as_posix()}'\n" for part in parts),
        encoding="utf-8"
    )
    run_ffmpeg([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(list_file), "-c", "copy",
        "-movflags", "+faststart", str(target)
    ])

def insert_promo_video(main_video: Path, promo_video: Path, output_video: Path) -> None:
    work_dir = output_video.parent
    normalized_main = work_dir / "main_normalized.mp4"
    normalized_promo = work_dir / "promo_normalized.mp4"
    first_part = work_dir / "main_first.mp4"
    second_part = work_dir / "main_second.mp4"

    normalize_video(main_video, normalized_main)
    normalize_video(promo_video, normalized_promo)

    main_duration = ffprobe_duration(normalized_main)
    insert_at = min(INSERT_AT_SECONDS, main_duration)

    parts = []
    if insert_at > 0:
        trim_video(normalized_main, 0, insert_at, first_part)
        parts.append(first_part)

    parts.append(normalized_promo)

    if insert_at < main_duration:
        trim_video(normalized_main, insert_at, None, second_part)
        parts.append(second_part)

    concat_videos(parts, output_video)

def is_video_message(message: Message) -> bool:
    if message.video:
        return True
    return bool(message.document and message.document.mime_type and message.document.mime_type.startswith("video/"))

async def start_handler(client: Client, message: Message) -> None:
    sessions[message.from_user.id] = UserSession()
    await message.reply_text("Main video ta forward/send korun. Ami receive korle promo video chaibo.")

async def cancel_handler(client: Client, message: Message) -> None:
    sessions.pop(message.from_user.id, None)
    await message.reply_text("Cancel kora hoyeche. Notun kore start korte /start din.")

async def video_handler(client: Client, message: Message) -> None:
    user_id = message.from_user.id
    session = sessions.setdefault(user_id, UserSession())

    if not is_video_message(message):
        await message.reply_text("Please video file pathan.")
        return

    if session.waiting_for == "main":
        session.main_message = message
        session.waiting_for = "promo"
        await message.reply_text("Main video peyechi. Ebar promo video ta dao.")
        return

    if not session.main_message:
        sessions[user_id] = UserSession(main_message=message, waiting_for="promo")
        await message.reply_text("Main video peyechi. Ebar promo video ta dao.")
        return

    await message.reply_text("Promo video peyechi. Ekhon 6 minute mark-e add kortesi.")
    temp_dir = Path(tempfile.mkdtemp(prefix=f"promo_bot_{user_id}_"))

    try:
        main_path = temp_dir / "main_input.mp4"
        promo_path = temp_dir / "promo_input.mp4"
        output_path = temp_dir / "final_video.mp4"

        await session.main_message.download(file_name=str(main_path))
        await message.download(file_name=str(promo_path))

        await asyncio.to_thread(insert_promo_video, main_path, promo_path, output_path)

        size_mb = output_path.stat().st_size / (1024 * 1024)
        if size_mb > MAX_TELEGRAM_SEND_MB:
            await message.reply_text(f"Final video ready, but size {size_mb:.0f} MB. Video ta aro choto kore try korun.")
            return

        await message.reply_video(str(output_path), caption="Done. Promo video add hoye geche.")
    except FileNotFoundError:
        await message.reply_text("FFmpeg/FFprobe paoa jay nai.")
    except Exception as exc:
        logging.exception("Video processing failed")
        await message.reply_text(f"Video process korte problem hoyeche: {exc}")
    finally:
        sessions.pop(user_id, None)
        shutil.rmtree(temp_dir, ignore_errors=True)

async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    bot_token = os.getenv("BOT_TOKEN")
    api_id = os.getenv("API_ID")
    api_hash = os.getenv("API_HASH")

    if not bot_token or not api_id or not api_hash:
        raise RuntimeError("BOT_TOKEN, API_ID, API_HASH set korun.")

    app = Client(
        "promo_video_bot",
        api_id=int(api_id),
        api_hash=api_hash,
        bot_token=bot_token,
        in_memory=True,
    )

    app.add_handler(MessageHandler(start_handler, filters.command("start") & filters.private))
    app.add_handler(MessageHandler(cancel_handler, filters.command("cancel") & filters.private))
    app.add_handler(MessageHandler(video_handler, (filters.video | filters.document) & filters.private))

    await app.start()
    me = await app.get_me()
    print(f"Bot is running with Pyrogram: @{me.username}")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
