"""Local media selection, video sampling, and filename mentions."""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from dataclasses import dataclass
from pathlib import Path


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".m4v"}
AUDIO_EXTENSIONS = {".wav", ".mp3"}
DIRECT_VIDEO_EXTENSIONS = {".mp4", ".mov"}
MAX_IMAGE_BYTES = 30 * 1024 * 1024
MAX_AUDIO_BYTES = 15 * 1024 * 1024


@dataclass
class MediaAsset:
    path: Path | None
    kind: str  # image, video, or audio
    purpose: str  # competitor, character, scene, first, last, audio, other
    handle: str
    source_url: str = ""
    duration: float | None = None
    selected: bool = True
    source_expires_at: str = ""
    upload_id: str = ""
    upload_file_size: int = 0
    upload_file_mtime_ns: int = 0

    @property
    def filename(self) -> str:
        if self.purpose == "keyframe":
            return self.handle.removeprefix("@{").removesuffix("}")
        if self.path and not self.source_url:
            return self.path.name
        return self.handle.removeprefix("@{").removesuffix("}")


def _run_media_tool(command: list[str], timeout: int = 45) -> subprocess.CompletedProcess:
    command = list(command)
    if getattr(sys, "frozen", False) and Path(command[0]).name.lower() in {"ffmpeg", "ffmpeg.exe", "ffprobe", "ffprobe.exe"}:
        name = Path(command[0]).stem.lower() + ".exe"
        tool = Path(sys._MEIPASS) / "media" / name
        if not tool.is_file():
            raise ValueError("视频处理组件缺失，请重新解压完整的便携版文件夹。")
        command[0] = str(tool)
    # pythonw has no console; without this flag every FFmpeg/FFprobe invocation
    # opens a new console window while inspecting or extracting local media.
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False,
                                stdin=subprocess.DEVNULL, **options)
    except FileNotFoundError as error:
        raise ValueError("未找到视频处理组件，请检查便携版是否完整；源码运行时需配置 FFmpeg。") from error
    except subprocess.TimeoutExpired as error:
        raise ValueError("读取视频超时，请检查文件是否损坏。") from error
    if result.returncode != 0:
        raise ValueError((result.stderr or "无法读取素材文件。")[-500:])
    return result


def media_duration(path: Path) -> float:
    result = _run_media_tool([
        "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)
    ])
    try:
        duration = float(json.loads(result.stdout)["format"]["duration"])
    except (KeyError, ValueError, TypeError) as error:
        raise ValueError("无法识别视频时长。") from error
    if duration <= 0:
        raise ValueError("视频时长无效。")
    return duration


video_duration = media_duration


def video_metadata(path: Path) -> dict:
    result = _run_media_tool([
        "ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height,duration",
        "-of", "json", str(path),
    ])
    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    if not video:
        raise ValueError("文件没有可读取的视频轨道。")
    duration = float(video.get("duration") or data.get("format", {}).get("duration", 0))
    return {"width": int(video.get("width", 0)), "height": int(video.get("height", 0)),
            "duration": duration, "has_audio": any(stream.get("codec_type") == "audio" for stream in streams)}


def image_dimensions(path: Path) -> tuple[int, int]:
    result = _run_media_tool([
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
        "-of", "json", str(path),
    ])
    try:
        stream = json.loads(result.stdout)["streams"][0]
        width, height = int(stream["width"]), int(stream["height"])
    except (KeyError, IndexError, ValueError, TypeError) as error:
        raise ValueError(f"{path.name} 无法读取图片尺寸。") from error
    if not 300 <= width <= 6000 or not 300 <= height <= 6000 or not 0.4 <= width / height <= 2.5:
        raise ValueError(f"{path.name} 尺寸须为 300–6000 像素，宽高比须在 0.4–2.5。")
    return width, height


def add_local_asset(path: str | Path, purpose: str, existing: list[MediaAsset]) -> MediaAsset:
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ValueError("所选文件不存在。")
    suffix = path.suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        kind = "image"
    elif suffix in VIDEO_EXTENSIONS:
        kind = "video"
    elif suffix in AUDIO_EXTENSIONS:
        kind = "audio"
    else:
        raise ValueError("仅支持常见图片、MP4/MOV/WebM 视频或 WAV/MP3 音频。")
    if purpose == "competitor" and kind != "video":
        raise ValueError("视频素材请选择视频文件。")
    if purpose in {"character", "scene"} and kind != "image":
        raise ValueError("角色和场景素材请选择图片文件。")
    if purpose in {"first", "last"} and kind != "image":
        raise ValueError("首帧与尾帧必须是图片。")
    if purpose == "audio" and kind != "audio":
        raise ValueError("参考音频请选择 WAV 或 MP3。")
    duration = media_duration(path) if kind in {"video", "audio"} else None
    if purpose == "competitor" and duration is not None and duration > 30:
        raise ValueError("参考视频不能超过 30 秒。")
    if kind == "image" and path.stat().st_size >= MAX_IMAGE_BYTES:
        raise ValueError("单张图片必须小于 30 MB。")
    if kind == "image":
        image_dimensions(path)
    if kind == "audio":
        if path.stat().st_size > MAX_AUDIO_BYTES:
            raise ValueError("单段参考音频不能超过 15 MB。")
        if duration is None or not 2 <= duration <= 30:
            raise ValueError("单段参考音频时长必须是 2–30 秒。")
    handle = _unique_handle(path.name, existing)
    return MediaAsset(
        path=path, kind=kind, purpose=purpose, handle=handle, duration=duration,
        selected=purpose != "competitor",
    )


def _unique_handle(label: str, existing: list[MediaAsset]) -> str:
    used = {asset.handle for asset in existing}
    number = 2
    handle = f"@{{{label}}}"
    while handle in used:
        handle = f"@{{{label} ({number})}}"
        number += 1
    return handle


def add_url_asset(url: str, kind: str, existing: list[MediaAsset], duration: float | None = None) -> MediaAsset:
    url = url.strip()
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("参考链接必须是服务端可访问的安全网址（以 https:// 开头）。")
    if kind not in {"image", "video", "audio"}:
        raise ValueError("素材类型只能是图片、视频或音频。")
    if kind in {"video", "audio"} and (duration is None or not 2 <= duration <= 30):
        raise ValueError("视频/音频网址需要填写 2–30 秒的素材时长，以便校验总时长。")
    filename = Path(urllib.parse.unquote(parsed.path)).name or {"image": "远程图片", "video": "远程视频", "audio": "远程音频"}[kind]
    return MediaAsset(
        path=None, kind=kind, purpose="audio" if kind == "audio" else "other",
        handle=_unique_handle(filename, existing), source_url=url, duration=duration,
    )


def image_data_url(path: Path) -> str:
    path = Path(path)
    if path.suffix.lower() not in IMAGE_EXTENSIONS:
        raise ValueError("这不是受支持的图片文件。")
    data = path.read_bytes()
    if len(data) >= MAX_IMAGE_BYTES:
        raise ValueError(f"{path.name} 超过 30 MB。")
    media_type, _ = mimetypes.guess_type(path.name)
    media_type = {"image/jpg": "image/jpeg", "image/x-ms-bmp": "image/bmp", "image/tiff": "image/tiff"}.get(
        media_type or "", media_type or "image/png"
    )
    return f"data:{media_type};base64,{base64.b64encode(data).decode('ascii')}"


def audio_data_url(path: Path) -> str:
    path = Path(path)
    if path.suffix.lower() not in AUDIO_EXTENSIONS:
        raise ValueError("参考音频只支持 WAV/MP3。")
    data = path.read_bytes()
    if len(data) > MAX_AUDIO_BYTES:
        raise ValueError(f"{path.name} 超过 15 MB。")
    subtype = "wav" if path.suffix.lower() == ".wav" else "mp3"
    return f"data:audio/{subtype};base64,{base64.b64encode(data).decode('ascii')}"


def compiled_mentions(prompt: str, assets: list[MediaAsset]) -> tuple[str, list[MediaAsset], list[MediaAsset], list[MediaAsset]]:
    """Convert visible filename mentions to Seedance's per-type media indices."""
    selected = [asset for asset in assets if asset.selected]
    images = [asset for asset in selected if asset.kind == "image"]
    videos = [asset for asset in selected if asset.kind == "video"]
    audios = [asset for asset in selected if asset.kind == "audio"]
    mapping = {
        asset.handle: f"@图片{index}" for index, asset in enumerate(images, start=1)
    }
    mapping.update({asset.handle: f"@视频{index}" for index, asset in enumerate(videos, start=1)})
    mapping.update({asset.handle: f"@音频{index}" for index, asset in enumerate(audios, start=1)})
    compiled = prompt
    for handle in sorted(mapping, key=len, reverse=True):
        compiled = compiled.replace(handle, mapping[handle])
    import re

    remaining = re.findall(r"@\{[^}]+\}", compiled)
    if remaining:
        raise ValueError(f"提示词引用了未选中的素材：{', '.join(remaining[:3])}")
    for name, media in (("图片", images), ("视频", videos), ("音频", audios)):
        for match in re.finditer(r"@" + name + r"(\d+)", compiled):
            if not 1 <= int(match.group(1)) <= len(media):
                raise ValueError(f"提示词引用 {match.group(0)}，但只勾选了 {len(media)} 份{name}素材。")
    return compiled, images, videos, audios


def thumbnail(path: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    _run_media_tool([
        "ffmpeg", "-y", "-v", "error", "-i", str(path), "-frames:v", "1",
        "-vf", "scale=160:100:force_original_aspect_ratio=decrease,pad=160:100:(ow-iw)/2:(oh-ih)/2",
        str(destination),
    ])
    return destination


def sample_video_frames(path: Path, *, max_frames: int = 20, generated: bool = False, on_progress=None) -> list[tuple[float, Path]]:
    """Sample a short competitor video into JPEGs for OpenAI vision analysis."""
    duration = video_duration(path)
    if (duration > 30.5 if generated else duration > 30):
        raise ValueError("成片需不超过 30 秒（允许容器时长误差）。" if generated else "参考视频不能超过 30 秒。")
    count = max(2, min(max_frames, int(duration) + 1))
    timestamps = [min(duration - 0.05, index * duration / count) for index in range(count)]
    output_dir = Path(tempfile.mkdtemp(prefix="seedance_frames_"))
    frames: list[tuple[float, Path]] = []
    try:
        for index, timestamp in enumerate(timestamps):
            if on_progress:
                on_progress(f"正在提取{'成片' if generated else '参考视频'}画面：{index + 1}/{count}")
            destination = output_dir / f"frame_{index:02d}.jpg"
            _run_media_tool([
                "ffmpeg", "-y", "-v", "error", "-ss", f"{timestamp:.3f}", "-i", str(path),
                "-frames:v", "1", "-vf", "scale=640:-2:force_original_aspect_ratio=decrease",
                "-q:v", "5", str(destination),
            ])
            frames.append((timestamp, destination))
        return frames
    except Exception:
        shutil.rmtree(output_dir, ignore_errors=True)
        raise


def extract_audio(path: Path, destination: Path) -> Path | None:
    """Return a compact MP3 track, or None when the video has no audio."""
    probe = _run_media_tool([
        "ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "json", str(path)
    ])
    if not json.loads(probe.stdout).get("streams"):
        return None
    _run_media_tool([
        "ffmpeg", "-y", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000",
        "-b:a", "64k", str(destination),
    ])
    return destination
