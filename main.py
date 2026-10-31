import os
import shutil
import tempfile
import asyncio
import subprocess
from typing import List, Optional
from fastapi import FastAPI, BackgroundTasks, HTTPException
from pydantic import BaseModel
import httpx

app = FastAPI(title="Media Processing Worker")

BOT_TOKEN = os.getenv("BOT_TOKEN")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ----------------- Models -----------------
class ScreenshotJob(BaseModel):
    url: str
    chat_id: int
    count: int = 10
    duration: Optional[float] = None
    timestamps: Optional[List[float]] = None

class TrimRange(BaseModel):
    start: str  # e.g., "00:00:10" or "10"
    duration: float  # seconds

class TrimJob(BaseModel):
    url: str
    chat_id: int
    ranges: List[TrimRange]


# ----------------- Helpers -----------------
def format_seconds(secs: float) -> str:
    h = int(secs // 3600)
    m = int((secs % 3600) // 60)
    s = int(secs % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


async def send_media_group(chat_id: int, photo_paths: List[str]):
    """Sends up to 10 photos in a single Telegram album."""
    if not photo_paths:
        return

    # Telegram media groups take chunks of up to 10
    for chunk_start in range(0, len(photo_paths), 10):
        chunk = photo_paths[chunk_start : chunk_start + 10]
        media = []
        files = {}

        for i, path in enumerate(chunk):
            attach_name = f"photo_{i}"
            media.append({"type": "photo", "media": f"attach://{attach_name}"})
            files[attach_name] = open(path, "rb")

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                res = await client.post(
                    f"{TELEGRAM_API}/sendMediaGroup",
                    data={"chat_id": chat_id, "media": str(media).replace("'", '"')},
                    files=files,
                )
                if res.status_code != 200:
                    print("sendMediaGroup error:", res.text)
        finally:
            for f in files.values():
                f.close()


async def send_video_clip(chat_id: int, video_path: str, caption: str):
    async with httpx.AsyncClient(timeout=120.0) as client:
        with open(video_path, "rb") as f:
            await client.post(
                f"{TELEGRAM_API}/sendVideo",
                data={"chat_id": chat_id, "caption": caption},
                files={"video": f},
            )


# ----------------- Tasks -----------------
def execute_screenshots(job: ScreenshotJob):
    work_dir = tempfile.mkdtemp()
    try:
        # Determine timestamps
        if job.timestamps and len(job.timestamps) > 0:
            secs = job.timestamps
        elif job.duration and job.duration > 0:
            interval = job.duration / (job.count + 1)
            secs = [interval * (i + 1) for i in range(job.count)]
        else:
            # Fallback 10-sec increments
            secs = [10.0 * (i + 1) for i in range(job.count)]

        secs = secs[:10]  # Cap at 10 for single album
        output_files = []

        # Build single multi-input FFmpeg command
        cmd = ["ffmpeg", "-y"]
        for s in secs:
            cmd.extend(["-ss", format_seconds(s), "-i", job.url])

        for i in range(len(secs)):
            out_file = os.path.join(work_dir, f"thumb_{i:02d}.jpg")
            output_files.append(out_file)
            cmd.extend(["-map", f"{i}:v:0", "-vframes", "1", "-an", "-sn", out_file])

        print(f"Running screenshots for chat {job.chat_id} with {len(secs)} points...")
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Filter generated files
        valid_photos = [p for p in output_files if os.path.exists(p) and os.path.getsize(p) > 0]

        if valid_photos:
            asyncio.run(send_media_group(job.chat_id, valid_photos))
        else:
            print("No screenshots generated.")

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def execute_trims(job: TrimJob):
    work_dir = tempfile.mkdtemp()
    try:
        for idx, item in enumerate(job.ranges):
            out_clip = os.path.join(work_dir, f"clip_{idx+1}.mp4")

            # Stream copy (-c copy): extracts clip without re-encoding in ~0.5s
            cmd = [
                "ffmpeg", "-y",
                "-ss", str(item.start),
                "-i", job.url,
                "-t", str(item.duration),
                "-c", "copy",
                "-avoid_negative_ts", "make_zero",
                out_clip
            ]

            print(f"Trimming clip {idx+1} from {item.start} for {item.duration}s...")
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            if os.path.exists(out_clip) and os.path.getsize(out_clip) > 0:
                caption = f"✂️ Clip {idx+1}: [{item.start} + {item.duration}s]"
                asyncio.run(send_video_clip(job.chat_id, out_clip, caption))

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# ----------------- Routes -----------------
@app.get("/")
@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/screenshots")
def create_screenshots(job: ScreenshotJob, bg: BackgroundTasks):
    if not BOT_TOKEN:
        raise HTTPException(500, "BOT_TOKEN environment variable not set")
    bg.add_task(execute_screenshots, job)
    return {"status": "queued", "count": job.count}


@app.post("/trim")
def create_trims(job: TrimJob, bg: BackgroundTasks):
    if not BOT_TOKEN:
        raise HTTPException(500, "BOT_TOKEN environment variable not set")
    bg.add_task(execute_trims, job)
    return {"status": "queued", "clips": len(job.ranges)}
