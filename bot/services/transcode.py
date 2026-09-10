"""Downscale a downloaded video to a lower resolution with ffmpeg.

Used to serve the synthetic 360p quality: PornHub itself only publishes 480p,
720p and 1080p, so when a user picks 360p we download the 480p stream and
re-encode it to 360p locally. The user only ever sees "360p".

While the encode runs we keep reporting progress on the *same* Telegram
message as the download (see ``state`` below), so from the user's point of
view it is one continuous download.
"""

import asyncio
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

#: Encoder settings. ``veryfast`` keeps the output small; override with the
#: TRANSCODE_PRESET / TRANSCODE_CRF env vars if you want faster/smaller files.
_PRESET = os.getenv("TRANSCODE_PRESET", "veryfast")
_CRF = os.getenv("TRANSCODE_CRF", "23")
#: Cap the frame rate of the downscaled copy. Measured on a 2-vCPU box this
#: made the encode ~2.5x faster (2.2x -> 5.6x realtime) for the same file
#: size, because x264 no longer re-encodes duplicate/surplus frames. 24 fps is
#: plenty for a 360p deliverable. Set to 0 to keep the source frame rate.
_FPS_CAP = int(os.getenv("TRANSCODE_FPS", "24") or 0)
_AUDIO_FALLBACK = ["-c:a", "aac", "-b:a", "96k"]

#: Percent range the encode drives — the download reports the rest, so the
#: counter keeps creeping instead of freezing while ffmpeg works.
_PCT_FROM = 95
_PCT_TO = 99

_OUT_TIME_RE = re.compile(r"^out_time_(?:us|ms)=(\d+)")


class TranscodeError(Exception):
    pass


def ffmpeg_path() -> str | None:
    """Locate the ffmpeg binary (env override first, then PATH)."""
    custom = os.getenv("FFMPEG_PATH")
    if custom and Path(custom).exists():
        return custom
    found = shutil.which("ffmpeg")
    if found:
        return found
    for candidate in ("/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/bin/ffmpeg"):
        if Path(candidate).exists():
            return candidate
    return None


def _build_cmd(ffmpeg: str, src: Path, height: int, out_path: Path, audio: str) -> list[str]:
    cmd = [
        ffmpeg, "-nostdin", "-y", "-loglevel", "error",
        # machine-readable progress on stdout (parsed for the percent)
        "-progress", "pipe:1", "-nostats",
        "-i", str(src),
        # -2 keeps the width even (H.264 requirement) while preserving AR.
        "-vf", f"scale=-2:{height}",
        "-c:v", "libx264", "-preset", _PRESET, "-crf", _CRF,
        "-threads", "0",
    ]
    if _FPS_CAP:
        cmd += ["-r", str(_FPS_CAP)]
    cmd += [
        "-movflags", "+faststart",
    ]
    if audio == "copy":
        cmd += ["-c:a", "copy"]
    else:
        cmd += _AUDIO_FALLBACK
    cmd.append(str(out_path))
    return cmd


def _run_ffmpeg(cmd: list[str], duration: int | None, on_pct) -> None:
    """Run ffmpeg, feeding encode progress to ``on_pct`` (0-100)."""
    timeout = max(900.0, float(duration or 0) * 3.0)
    started = time.monotonic()
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1
    )
    try:
        for line in proc.stdout or []:
            if duration:
                m = _OUT_TIME_RE.match(line.strip())
                if m:
                    # out_time_ms is microseconds despite the name
                    seconds = int(m.group(1)) / 1_000_000
                    on_pct(min(int(seconds * 100 / duration), 100))
            if time.monotonic() - started > timeout:
                proc.kill()
                raise TranscodeError(f"ffmpeg exceeded {timeout:.0f}s")
        stderr = (proc.stderr.read() if proc.stderr else "") or ""
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    if proc.returncode != 0:
        raise TranscodeError(f"ffmpeg exited {proc.returncode}: {stderr[-400:]}")


def _transcode_sync(
    src: Path, height: int, out_path: Path, duration: int | None, on_pct
) -> None:
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        raise TranscodeError("ffmpeg binary not found")

    # Prefer copying the audio track (fast, lossless); some sources need a
    # re-encode, so fall back to AAC once.
    try:
        _run_ffmpeg(_build_cmd(ffmpeg, src, height, out_path, "copy"), duration, on_pct)
        return
    except TranscodeError as exc:
        logger.warning("audio copy failed (%s); re-encoding audio", str(exc)[:200])
    _run_ffmpeg(_build_cmd(ffmpeg, src, height, out_path, "aac"), duration, on_pct)


async def transcode_to_height(
    src: Path,
    height: int,
    out_dir: Path,
    state: dict | None = None,
    duration: int | None = None,
) -> Path:
    """Return the path of ``src`` scaled to ``height`` p (written into out_dir).

    ``state`` (optional) is the shared progress dict the Telegram message
    watches: its pct is advanced across ``_PCT_FROM``..``_PCT_TO`` as the
    encode proceeds. Raises TranscodeError when ffmpeg is unavailable or the
    encode fails.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{src.stem}_{height}p.mp4"

    def on_pct(pct: int) -> None:
        if state is not None:
            span = max(_PCT_TO - _PCT_FROM, 1)
            state["pct"] = min(_PCT_FROM + int(pct * span / 100), _PCT_TO)

    if state is not None:
        state["phase"] = "download"
        state["pct"] = max(state.get("pct", 0), _PCT_FROM - 1)

    started = time.monotonic()
    await asyncio.to_thread(_transcode_sync, src, height, out_path, duration, on_pct)

    if not out_path.exists() or out_path.stat().st_size == 0:
        raise TranscodeError("ffmpeg produced no output")
    logger.info(
        "Transcoded %s -> %s (%s -> %s) in %.0fs",
        src.name, out_path.name,
        _mb(src.stat().st_size), _mb(out_path.stat().st_size),
        time.monotonic() - started,
    )
    return out_path


def _mb(n: int) -> str:
    return f"{n / 1e6:.1f} MB"
