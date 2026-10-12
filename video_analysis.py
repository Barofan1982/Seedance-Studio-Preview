"""Persistent one-second reference frames and the provenance of AI analysis."""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict
from pathlib import Path

from media_assets import MediaAsset, _run_media_tool, _unique_handle


def analysis_assets(assets):
    return [a for a in assets if a.purpose != "keyframe"]


def reference_videos(assets):
    return [a for a in assets if a.kind == "video" and (a.selected or a.purpose == "competitor")]


def analysis_signature(assets):
    values = []
    for asset in analysis_assets(assets):
        data = {**asdict(asset), "path": str(asset.path) if asset.path else ""}
        for key in ('source_expires_at','upload_id','upload_file_size','upload_file_mtime_ns'):
            data.pop(key, None)
        if asset.path and asset.path.is_file():
            stat = asset.path.stat()
            data["file_version"] = [stat.st_size, stat.st_mtime_ns]
        values.append(data)
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def needs_video_analysis(state, assets):
    if not state.get('auto_analyze', False) or state.get("mode") != "全模态参考" or not reference_videos(assets):
        return False
    draft = state.get("prompt_draft") or {}
    return (draft.get("analysis_signature") != analysis_signature(assets)
            or not draft.get("frame_count")
            or not state.get("frame_catalog")
            or any(not Path(f["path"]).is_file() for f in state["frame_catalog"]))


def extract_reference_frames(asset: MediaAsset, directory: Path, on_progress=None):
    """Read a local video (preferred) or stream its URL; keep a frame at every second and the ending."""
    report = on_progress or (lambda _message: None)
    if asset.path and asset.path.is_file():
        source = str(asset.path)
        version = f"{source}:{asset.path.stat().st_size}:{asset.path.stat().st_mtime_ns}"
        network_options = []
    elif asset.source_url:
        source = asset.source_url
        version = source
        network_options = ["-user_agent", "Mozilla/5.0 SeedanceStudio", "-rw_timeout", "20000000"]
    else:
        raise ValueError(f"{asset.filename} 的本地文件已移动，请在素材菜单中关联本地视频。")
    cache = directory / hashlib.sha256((version + ":one-second-v4").encode()).hexdigest()[:20]
    metadata = cache / "frames.json"
    if metadata.is_file():
        saved = json.loads(metadata.read_text(encoding="utf-8"))
        if saved.get("frames") and all((cache / f["file"]).is_file() for f in saved["frames"]):
            report(f"已读取 {asset.filename} 的 {len(saved['frames'])} 张抽帧")
            return saved["duration"], [(f["timestamp"], cache / f["file"]) for f in saved["frames"]]
    report(f"正在读取 {asset.filename} 的视频信息…")
    probe = _run_media_tool(["ffprobe", "-v", "error", *network_options, "-show_entries",
                             "format=duration:stream=codec_type,width,height", "-of", "json", source])
    data = json.loads(probe.stdout)
    duration = float(data["format"]["duration"])
    video = next((s for s in data["streams"] if s.get("codec_type") == "video"), None)
    if not video or not 0 < duration <= 30:
        raise ValueError("参考视频须有视频画面，且时长不超过 30 秒。")
    width, height = int(video["width"]), int(video["height"])
    if not .4 <= width / height <= 2.5:
        raise ValueError("参考视频宽高比须在 0.4–2.5 之间。")
    # Keep keyframes within the image API dimensions; analysis copies remain readable.
    scale = min(1280 / max(width, height), max(1, 300 / min(width, height)))
    out_w, out_h = max(300, round(width * scale / 2) * 2), max(300, round(height * scale / 2) * 2)
    cache.mkdir(parents=True, exist_ok=True)
    report(f"正在对 {asset.filename} 每秒抽帧（约 {math.ceil(duration)} 张）…")
    _run_media_tool(["ffmpeg", "-y", "-v", "error", *network_options, "-i", source,
                     "-vf", f"fps=1:start_time=0:round=up:eof_action=pass,scale={out_w}:{out_h}", "-frames:v", str(math.ceil(duration)),
                     "-q:v", "4", "-start_number", "0", str(cache / "second-%03d.jpg")], timeout=90)
    frames = [(float(i), cache / f"second-{i:03d}.jpg") for i in range(math.ceil(duration))
              if (cache / f"second-{i:03d}.jpg").is_file()]
    if not frames:
        raise ValueError("未抽取到视频画面，请检查视频是否可读取。")
    tail = max(0, duration - .08)
    if tail - frames[-1][0] > .15:
        end = cache / "ending.jpg"
        _run_media_tool(["ffmpeg", "-y", "-v", "error", *network_options, "-ss", f"{tail:.3f}",
                         "-i", source, "-frames:v", "1", "-vf", f"scale={out_w}:{out_h}", "-q:v", "4", str(end)])
        if end.is_file():
            frames.append((tail, end))
    metadata.write_text(json.dumps({"duration": duration, "frames": [
        {"timestamp": timestamp, "file": path.name} for timestamp, path in frames
    ]}, ensure_ascii=False, indent=2), encoding="utf-8")
    return duration, frames


def frame_catalog(videos, assets, directory, on_progress=None):
    catalog = []
    names = list(analysis_assets(assets))
    for video in videos:
        duration, frames = extract_reference_frames(video, directory, on_progress)
        video.duration = duration
        stem = Path(video.filename).stem[:35]
        for timestamp, path in frames:
            handle = _unique_handle(f"{stem}-{timestamp:05.2f}s.jpg", names)
            candidate = MediaAsset(path, "image", "keyframe", handle, selected=False)
            names.append(candidate)
            catalog.append({"source_handle": video.handle, "timestamp": timestamp, "path": str(path), "handle": handle})
    return catalog


def choose_keyframes(catalog, recommended, image_capacity=30):
    available = {f["handle"]: f for f in catalog}
    selected = list(dict.fromkeys(h for h in recommended if h in available))[:min(4, image_capacity)]
    if not selected and catalog and image_capacity:
        for source in dict.fromkeys(f["source_handle"] for f in catalog):
            frames = [f for f in catalog if f["source_handle"] == source]
            for index in (0, len(frames) // 2, len(frames) - 1):
                if frames[index]["handle"] not in selected:
                    selected.append(frames[index]["handle"])
        selected = selected[:min(4, image_capacity)]
    return selected


def keyframe_assets(catalog, selected):
    return [MediaAsset(Path(f["path"]), "image", "keyframe", f["handle"]) for f in catalog if f["handle"] in selected]
