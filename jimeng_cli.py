#!/usr/bin/env python3
"""Headless 即梦 / Seedance 视频生成，供 agent 或命令行直接调用。

复用桌面版 Seedance Studio 的客户端模块，以及它用 Windows DPAPI 加密保存的
接口密钥（api_key.bin）。不复制、不打印、不落盘明文密钥。

提交成功后写入桌面版同一个 history.sqlite3，历史与用量页刷新即可看到；
--data-dir 可指向便携版 data 目录，--no-history 可关闭写入。

示例：
    python jimeng_cli.py --list-models
    python jimeng_cli.py -p "小猫在窗台伸懒腰" --mode text --duration 5 --ratio 16:9
    python jimeng_cli.py -p "参考这张图做一个镜头推进" --image D:\\pics\\a.png --duration 5
    python jimeng_cli.py --task-id cgt-xxxx --out-dir D:\\Test\\out
    python jimeng_cli.py -p "..." --data-dir "D:\\portable\\data"

stdout 只输出一行 JSON 结果；进度与提示写到 stderr。
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import sys
import time
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app_paths import DEFAULT_OUTPUT_DIR, APP_DIR  # noqa: E402
from secure_store import load_key  # noqa: E402
from seedance_client import (  # noqa: E402
    DEFAULT_BASE_URL,
    DEFAULT_MODEL_CATALOG_URL,
    SeedanceClient,
    SeedanceError,
    SubmissionUncertain,
    build_payload,
    download_video,
)
from seedance_models import DEFAULT_VIDEO_MODEL, MODE_NAMES  # noqa: E402
from billing_cny import billing_details, pricing_snapshot  # noqa: E402
from history_store import HistoryStore  # noqa: E402
from media_assets import video_metadata  # noqa: E402

TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "expired"}
IMAGE_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".bmp": "image/bmp", ".tif": "image/tiff",
    ".tiff": "image/tiff", ".gif": "image/gif",
}
AUDIO_MIME = {".wav": "audio/wav", ".mp3": "audio/mpeg"}


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def fail(message: str, *, code: int = 1, extra: dict | None = None) -> "None":
    payload = {"ok": False, "error": message}
    if extra:
        payload.update(extra)
    print(json.dumps(payload, ensure_ascii=False))
    raise SystemExit(code)


def _settings(data_dir=None) -> dict:
    """GUI 里保存的接口地址优先，缺省回落到模块默认值。"""
    path = (Path(data_dir).expanduser() if data_dir else APP_DIR) / "state.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _data_home(data_dir=None) -> Path:
    return Path(data_dir).expanduser() if data_dir else APP_DIR


def _rates(state: dict) -> dict:
    value = state.get("cny_rates")
    return value if isinstance(value, dict) else {}


def _display(value):
    """dry-run 输出时折叠 base64，避免整屏都是素材内容。"""
    if isinstance(value, str) and value.startswith("data:") and ";base64," in value:
        head, _, body = value.partition(";base64,")
        return f"{head};base64,<{len(body)} chars>"
    if isinstance(value, dict):
        return {key: _display(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_display(item) for item in value]
    return value


def _source(value: str, table: dict[str, str], label: str) -> str:
    path = Path(value)
    if not path.is_file():
        return value  # 交给客户端按 https 网址校验
    mime = table.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0]
    if not mime:
        fail(f"{label}格式无法识别：{path.name}")
    try:
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError as error:
        fail(f"{label}读取失败：{error}")
    return f"data:{mime};base64,{encoded}"


def _client(state: dict, data_dir=None) -> SeedanceClient:
    key = load_key(data_dir)
    if not key:
        fail(f"{_data_home(data_dir)} 中没有保存接口密钥。请先打开 Seedance Studio "
             "填写一次，或用 --data-dir 指向对应数据目录；也可以联系吴仕敏索取。")
    base_url = str(state.get("base_url") or DEFAULT_BASE_URL)
    catalog = str(state.get("model_catalog_url") or DEFAULT_MODEL_CATALOG_URL)
    allow_http = bool(state.get("allow_http_for"))
    try:
        return SeedanceClient(base_url, key, allow_http=allow_http, model_catalog_url=catalog)
    except ValueError as error:
        fail(str(error))


def _payload(args, state: dict) -> dict:
    mode = args.mode
    images = [_source(item, IMAGE_MIME, "参考图片") for item in args.image or []]
    videos = list(args.video or [])
    audios = [_source(item, AUDIO_MIME, "参考音频") for item in args.audio or []]
    first_frame = _source(args.first_frame, IMAGE_MIME, "首帧图片") if args.first_frame else ""
    last_frame = _source(args.last_frame, IMAGE_MIME, "尾帧图片") if args.last_frame else ""
    if mode == "reference" and not (args.prompt or images or videos or audios):
        fail("全模态参考模式至少需要提示词或一份参考素材。")
    if mode in ("first_frame", "first_last_frame") and not first_frame:
        fail("首帧/首尾帧模式需要 --first-frame。")
    try:
        return build_payload(
            prompt=args.prompt or "",
            duration=args.duration,
            resolution=args.resolution,
            ratio=args.ratio,
            image_sources=images,
            video_sources=videos,
            audio_sources=audios,
            mode=mode,
            task_type=args.task_type,
            first_frame=first_frame,
            last_frame=last_frame,
            generate_audio=not args.no_audio,
            watermark=args.watermark,
            output_format=args.output_format,
            return_last_frame=args.return_last_frame,
            model=args.model,
            model_profile=state.get("generation_profile", ""),
        )
    except ValueError as error:
        fail(str(error))


def _mode(args) -> str:
    """Infer the generation mode when resuming a task without asset arguments."""
    if args.first_frame and args.last_frame:
        return 'first_last_frame'
    if args.first_frame:
        return 'first_frame'
    if args.image or args.video or args.audio:
        return 'reference'
    return args.mode


def _resume_payload(task: dict, args) -> dict:
    """Best-effort request record for a task submitted outside this process."""
    prompt = (args.prompt or '').strip()
    content = [{'type': 'text', 'text': prompt}] if prompt else []
    duration = task.get('duration')
    if not isinstance(duration, int) or isinstance(duration, bool):
        duration = args.duration
    resolution = task.get('resolution')
    if resolution not in ('480p', '720p', '1080p', '4k'):
        resolution = args.resolution
    ratio = task.get('ratio')
    if not isinstance(ratio, str) or not ratio:
        ratio = args.ratio
    return {'model': str(task.get('model') or args.model), 'content': content,
            'duration': duration, 'resolution': resolution, 'ratio': ratio}


def _history(args):
    if args.no_history:
        return None
    path = _data_home(args.data_dir) / 'history.sqlite3'
    if args.data_dir and not path.is_file():
        log(f'数据目录中没有 history.sqlite3，将新建：{path}')
    try:
        return HistoryStore(path)
    except Exception as error:
        log(f'历史与用量无法写入（{path}）：{error}')
        return None


def _record_submission(store, task_id, payload, args, state):
    if not store:
        return
    try:
        store.add(task_id, payload, _mode(args), original_prompt=args.prompt or '',
                  template={'generation_model': args.model,
                            'generation_profile': state.get('generation_profile', '')},
                  initial_status='submitted',
                  billing_rates=pricing_snapshot(_rates(state)))
    except Exception as error:
        log(f'历史与用量写入失败（任务 {task_id}）：{error}')


def _record_result(store, task, args, state, path=None):
    if not store:
        return
    task_id = str(task.get('id', ''))
    if not task_id:
        return
    try:
        if store.get(task_id) is None:
            _record_submission(store, task_id, _resume_payload(task, args), args, state)
        store.update(task)
        if path:
            store.set_local_path(task_id, Path(path))
            try:
                info = video_metadata(Path(path))
            except Exception:
                info = {}
            if info:
                store.set_media_info(task_id, info)
    except Exception as error:
        log(f'历史与用量写入失败（任务 {task_id}）：{error}')


def _begin_call(store, payload, prompt):
    if not store:
        return None
    try:
        return store.begin_call('create', {'payload': payload, 'original_prompt': prompt})
    except Exception as error:
        log(f'提交记录无法写入：{error}')
        return None


def _finish_call(store, call_id, *, response=None, error='', uncertain=False):
    if not store or not call_id:
        return
    try:
        store.finish_call(call_id, response=response, error=error, uncertain=uncertain)
    except Exception as write_error:
        log(f'提交记录更新失败：{write_error}')


def _wait(client: SeedanceClient, task_id: str, timeout: int, interval: int) -> dict:
    started = time.monotonic()
    last_status = ""
    errors = 0
    while True:
        try:
            task = client.get_task(task_id)
            errors = 0
        except SeedanceError as error:
            errors += 1
            if errors >= 3:
                fail(f"查询任务失败：{error}", extra={"task_id": task_id})
            log(f"查询失败，{interval}s 后重试：{error}")
            time.sleep(interval)
            continue
        status = str(task.get("status", ""))
        if status != last_status:
            log(f"状态：{status or '未知'}")
            last_status = status
        if status in TERMINAL_STATUSES:
            return task
        if time.monotonic() - started > timeout:
            fail(f"等待超时（{timeout}s），任务仍在 {status or '未知'} 状态。", extra={"task_id": task_id})
        time.sleep(interval)


def _save(task: dict, out_dir: Path) -> str:
    url = str((task.get("content") or {}).get("video_url") or "")
    if not url:
        fail("任务成功但没有返回视频地址。", extra={"task_id": task.get("id", "")})
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = f"jimeng-{stamp}-{str(task.get('id', 'task'))[:8]}{Path(url.split('?')[0]).suffix or '.mp4'}"
    destination = out_dir / name
    try:
        download_video(url, destination)
    except SeedanceError as error:
        fail(f"下载失败：{error}", extra={"task_id": task.get("id", ""), "video_url": url})
    return str(destination)


def main() -> None:
    parser = argparse.ArgumentParser(description="Headless 即梦 / Seedance 视频生成")
    parser.add_argument("-p", "--prompt", default="", help="提示词")
    parser.add_argument("--prompt-file", help="从 UTF-8 文本文件读取提示词（推荐含中文时使用）")
    parser.add_argument("--mode", choices=list(MODE_NAMES), default="text", help="生成方式（默认纯文本）")
    parser.add_argument("--duration", type=int, default=5, help="时长秒数，-1 表示自适应（默认 5）")
    parser.add_argument("--resolution", choices=("480p", "720p", "1080p"), default="720p")
    parser.add_argument("--ratio", default="16:9", help="画面比例，如 16:9 / 9:16 / 1:1 / adaptive")
    parser.add_argument("--model", default=DEFAULT_VIDEO_MODEL, help=f"模型 ID（默认 {DEFAULT_VIDEO_MODEL}）")
    parser.add_argument("--image", action="append", help="参考图片，可重复；支持本地路径或 https 网址")
    parser.add_argument("--video", action="append", help="参考视频 https 网址，可重复")
    parser.add_argument("--audio", action="append", help="参考音频，可重复；支持本地路径或 https 网址")
    parser.add_argument("--first-frame", help="首帧图片，本地路径或 https 网址")
    parser.add_argument("--last-frame", help="尾帧图片，本地路径或 https 网址")
    parser.add_argument("--task-type", choices=("reference", "edit", "extend"), default="reference")
    parser.add_argument("--output-format", choices=("mp4", "mov"), default="mp4")
    parser.add_argument("--no-audio", action="store_true", help="不生成声音")
    parser.add_argument("--watermark", action="store_true", help="生成水印")
    parser.add_argument("--return-last-frame", action="store_true", help="要求返回尾帧")
    parser.add_argument("--out-dir", help="成片目录；默认使用桌面版设置中的保存目录")
    parser.add_argument("--data-dir", help="桌面版数据目录（state.json、api_key.bin、history.sqlite3）；默认本机开发数据目录")
    parser.add_argument("--no-history", action="store_true", help="不写入桌面版历史与用量")
    parser.add_argument("--timeout", type=int, default=1800, help="最长等待秒数（默认 1800）")
    parser.add_argument("--interval", type=int, default=5, help="轮询间隔秒数（默认 5）")
    parser.add_argument("--task-id", help="续查已有任务")
    parser.add_argument("--dry-run", action="store_true", help="只打印请求体，不提交")
    parser.add_argument("--list-models", action="store_true", help="列出中转站可用模型")
    args = parser.parse_args()

    if args.prompt_file:
        try:
            args.prompt = Path(args.prompt_file).read_text(encoding="utf-8").strip()
        except OSError as error:
            fail(f"提示词文件读取失败：{error}")

    state = _settings(args.data_dir)
    client = _client(state, args.data_dir)
    out_dir = (Path(args.out_dir).expanduser() if args.out_dir
               else Path(str(state.get("download_dir") or DEFAULT_OUTPUT_DIR)).expanduser())

    if args.list_models:
        try:
            models = client.list_models()
        except SeedanceError as error:
            fail(f"获取模型列表失败：{error}")
        print(json.dumps({"ok": True, "base_url": client.base_url, "models": models}, ensure_ascii=False))
        return

    if args.dry_run:
        if args.task_id:
            fail("--dry-run 不能与 --task-id 同时使用。", code=2)
        payload = _payload(args, state)
        print(json.dumps({"ok": True, "dry_run": True, "base_url": client.base_url,
                          "payload": _display(payload)}, ensure_ascii=False))
        return

    store = _history(args)

    task_id = args.task_id
    if not task_id:
        payload = _payload(args, state)
        log(f"提交任务：{payload['model']} / {args.duration}s / {args.resolution} / {args.ratio}")
        call_id = _begin_call(store, payload, args.prompt or "")
        try:
            task_id = client.create_task(payload)
        except SubmissionUncertain as error:
            _finish_call(store, call_id, error=str(error), uncertain=True)
            fail(str(error))
        except SeedanceError as error:
            _finish_call(store, call_id, error=str(error))
            fail(str(error))
        except Exception as error:
            _finish_call(store, call_id, error=str(error))
            fail(f"提交失败：{error}")
        _finish_call(store, call_id, response={"id": task_id})
        _record_submission(store, task_id, payload, args, state)
        log(f"任务编号：{task_id}")

    task = _wait(client, task_id, args.timeout, args.interval)
    status = str(task.get("status", ""))
    _record_result(store, task, args, state)
    if status != "succeeded":
        detail = task.get("error") or task.get("message") or ""
        fail(f"任务结束状态为 {status}。{detail}".strip(), extra={"task_id": task_id, "status": status})

    path = _save(task, out_dir)
    _record_result(store, task, args, state, path=path)
    if store:
        log(f"历史与用量已写入：{store.path}")
    usage = task.get("usage") if isinstance(task.get("usage"), dict) else {}
    record = store.get(task_id) if store else None
    if record is None:
        record = {
            "model": str(task.get("model") or args.model),
            "status": status,
            "request_json": _resume_payload(task, args),
            "billing_rates_json": pricing_snapshot(_rates(state)),
            "completion_tokens": usage.get("completion_tokens"),
            "actual_duration": task.get("duration"),
            "actual_resolution": task.get("resolution"),
        }
    cost = billing_details(record, _rates(state))
    print(json.dumps({
        "ok": True,
        "task_id": task_id,
        "status": status,
        "file": path,
        "video_url": (task.get("content") or {}).get("video_url", ""),
        "model": task.get("model", ""),
        "resolution": task.get("resolution", ""),
        "duration": task.get("duration", ""),
        "ratio": task.get("ratio", ""),
        "usage": usage,
        "estimated_cost_cny": float(cost["amount"]) if cost["amount"] is not None else None,
        "estimated_cost_usd": float(cost["usd"]) if cost["usd"] is not None else None,
        "cost_source": cost["source"] if cost["amount"] is not None else cost["reason"],
        "history_file": str(store.path) if store else "",
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
