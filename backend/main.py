from pathlib import Path
import json
import subprocess
import re
import os
import shutil
import tempfile
import zipfile
import threading
import time
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
from urllib.parse import quote, unquote, urlparse, quote_plus
from urllib.parse import parse_qs
from urllib.request import Request, urlopen
import yt_dlp
from fastapi import FastAPI, HTTPException, Query, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

BASE = Path(__file__).resolve().parent.parent
STORAGE = BASE / "storage"
VIDEOS, AUDIO, SUBTITLES, THUMBNAILS = [STORAGE / x for x in ("videos","audio","subtitles","thumbnails")]
DATA = BASE / "data"
HISTORY = DATA / "history.json"
ACCOUNTS = DATA / "accounts"
OPEN_CONNECTORS: dict[str, threading.Thread] = {}
TRANSLATE_JOBS: dict[str, dict] = {}
TRANSLATE_JOB_LOCK = threading.Lock()
TRANSLATE_JOB_EXECUTOR = ThreadPoolExecutor(max_workers=2)
SOCIAL_SEARCH_CACHE: dict[tuple, tuple[float, dict]] = {}
SOCIAL_SEARCH_CACHE_TTL_SEC = 120
SOCIAL_SEARCH_CACHE_MAX = 200
SOCIAL_SEARCH_CACHE_LOCK = threading.Lock()
SOCIAL_QUERY_VARIANTS_MAX = 2
SOCIAL_HTTP_TIMEOUT_SEC = 4
SOCIAL_PROXY_TIMEOUT_SEC = 5
SOCIAL_ENABLE_SLOW_FALLBACK = os.environ.get("SOCIAL_ENABLE_SLOW_FALLBACK", "0") == "1"
STRICT_NO_SERVER_PERSIST = os.environ.get("STRICT_NO_SERVER_PERSIST", "1") == "1"

for p in (VIDEOS, AUDIO, SUBTITLES, THUMBNAILS, DATA):
    p.mkdir(parents=True, exist_ok=True)
ACCOUNTS.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Video Researcher API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173", "http://127.0.0.1:5173",
        "http://localhost:5174", "http://127.0.0.1:5174",
        "https://jayjung1369-gif.github.io",
    ],
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"]
)

def history_read():
    if not HISTORY.exists():
        return []
    try:
        return json.loads(HISTORY.read_text(encoding="utf-8"))
    except Exception:
        return []

def history_write(items):
    # Privacy mode: do not persist user activity server-side.
    return


def _cleanup_path(path: Path):
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink(missing_ok=True)
    except Exception:
        pass


def _mk_temp_dir(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


def _file_download_response(file_path: Path, cleanup_root: Path):
    encoded = quote(file_path.name, safe='')
    return FileResponse(
        file_path,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded}"},
        background=BackgroundTask(_cleanup_path, cleanup_root),
    )

def n(v):
    try:
        return int(v or 0)
    except Exception:
        return 0

def normalize(e):
    raw_views = e.get("view_count")
    raw_likes = e.get("like_count")
    return {
        "id": e.get("id"),
        "platform": "youtube",
        "title": e.get("title") or "",
        "url": e.get("webpage_url") or (f"https://www.youtube.com/watch?v={e.get('id')}" if e.get("id") else ""),
        "thumbnail": e.get("thumbnail") or "",
        "channel": e.get("channel") or e.get("uploader") or "",
        "channel_id": e.get("channel_id") or e.get("uploader_id") or "",
        "upload_date": e.get("upload_date") or "",
        "duration": n(e.get("duration")),
        # Preserve None so filters can skip videos with unknown metadata
        "view_count": n(raw_views) if raw_views is not None else None,
        "like_count": n(raw_likes) if raw_likes is not None else None,
        "description": e.get("description") or "",
        "has_subtitles": bool(e.get("subtitles") or e.get("automatic_captions")),
    }

def is_youtube_url(q: str) -> bool:
    q_low = q.lower().strip()
    return "youtube.com/watch" in q_low or "youtu.be/" in q_low or "youtube.com/shorts/" in q_low

def is_channel_query(q: str) -> bool:
    """Detect @handle or youtube.com/channel / youtube.com/@handle URLs."""
    q_low = q.lower().strip()
    return (
        q.strip().startswith("@")
        or "youtube.com/@" in q_low
        or "youtube.com/channel/" in q_low
        or "youtube.com/c/" in q_low
        or "youtube.com/user/" in q_low
    )

def get_detail(url: str):
    """Full metadata fetch for a single video (used only for direct URL lookup)."""
    opts = _yt_dlp_opts(
        skip_download=True,
        ignoreerrors=True,
        socket_timeout=15,
        retries=2,
    )
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception:
        return None

def detect_platform(url: str) -> str:
    u = url.lower()
    if "instagram.com" in u:
        return "instagram"
    if "xiaohongshu.com" in u or "xhslink.com" in u or "xhs.link" in u:
        return "xiaohongshu"
    if "youtube.com" in u or "youtu.be" in u:
        return "youtube"
    return "other"


def _yt_dlp_opts(**overrides):
    """Shared yt-dlp defaults tuned for cloud hosts (e.g. Render)."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "windowsfilenames": True,
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 5,
        "extractor_retries": 3,
        "force_ipv4": True,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            )
        },
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web", "ios"],
            }
        },
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(opts.get(key), dict):
            merged = dict(opts[key])
            merged.update(value)
            opts[key] = merged
        else:
            opts[key] = value
    return opts


def _download_video_with_fallback(url: str, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    # Try high quality merge first, then progressive fallback for environments
    # where a specific stream combo intermittently fails.
    format_candidates = [
        "bv*+ba/b",
        "b[ext=mp4]/b",
    ]
    last_error = None

    for fmt in format_candidates:
        opts = _yt_dlp_opts(
            outtmpl=str(out_dir / "%(id)s_%(title).160s.%(ext)s"),
            format=fmt,
            merge_output_format="mp4",
        )
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                path = Path(ydl.prepare_filename(info))
                if path.suffix != ".mp4" and path.with_suffix(".mp4").exists():
                    path = path.with_suffix(".mp4")
                if not path.exists():
                    matches = sorted(out_dir.glob(f"{info.get('id','')}*"), key=lambda p: p.stat().st_mtime, reverse=True)
                    if matches:
                        path = matches[0]
                return info, path
        except Exception as exc:
            last_error = exc
            continue

    raise last_error or RuntimeError("다운로드 실패")


TRANSLATE_LANG_LABELS = {
    "ko": "Korean",
    "en": "English",
    "ja": "Japanese",
    "zh-cn": "Chinese (Simplified)",
    "zh-tw": "Chinese (Traditional)",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
}

TRANSLATE_MEMORY_LANG_CODES = {
    "zh-cn": "zh-CN",
    "zh-tw": "zh-TW",
}

TRANSLATE_QUALITY_PRESETS = {
    "fast": {
        "whisper_model": "tiny",
        "ffmpeg_preset": "veryfast",
        "ffmpeg_crf": "24",
        "audio_bitrate": "160k",
    },
    "balanced": {
        "whisper_model": "base",
        "ffmpeg_preset": "medium",
        "ffmpeg_crf": "20",
        "audio_bitrate": "192k",
    },
    "high": {
        "whisper_model": "small",
        "ffmpeg_preset": "slow",
        "ffmpeg_crf": "18",
        "audio_bitrate": "224k",
    },
}


def _format_srt_time(sec: float) -> str:
    total_ms = max(0, int(round(sec * 1000)))
    h = total_ms // 3600000
    total_ms %= 3600000
    m = total_ms // 60000
    total_ms %= 60000
    s = total_ms // 1000
    ms = total_ms % 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _write_srt(segments: list[dict], out_path: Path):
    lines = []
    for idx, seg in enumerate(segments, start=1):
        start = _format_srt_time(float(seg.get("start", 0)))
        end = _format_srt_time(float(seg.get("end", 0)))
        text = re.sub(r"\s+", " ", str(seg.get("text", "")).strip())
        if not text:
            continue
        lines.append(str(idx))
        lines.append(f"{start} --> {end}")
        lines.append(text)
        lines.append("")
    out_path.write_text("\n".join(lines), encoding="utf-8")


def _translate_via_mymemory(text: str, source_lang: str, target_lang: str) -> str:
    source_code = (source_lang or "auto").lower()
    target_code = (TRANSLATE_MEMORY_LANG_CODES.get(target_lang.lower()) or target_lang.lower()).strip()
    if not source_code or source_code in {"auto", "unknown"}:
        raise RuntimeError("MyMemory 번역은 source 언어가 필요합니다.")

    params = quote_plus(text)
    url = (
        "https://api.mymemory.translated.net/get"
        f"?q={params}&langpair={quote_plus(source_code)}|{quote_plus(target_code)}"
    )
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=15) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    translated = (payload.get("responseData") or {}).get("translatedText") or ""
    if not translated:
        raise RuntimeError("MyMemory 번역 결과가 비어 있습니다.")
    return translated.strip()


def _translate_via_deep_translator(text: str, target_lang: str) -> str:
    from deep_translator import GoogleTranslator

    normalized_target = target_lang.lower()
    translator = GoogleTranslator(source="auto", target=normalized_target)
    translated = translator.translate(text)
    if not translated:
        raise RuntimeError("deep_translator 번역 결과가 비어 있습니다.")
    return str(translated).strip()


def _translate_via_google_translate_py(text: str, target_lang: str) -> str:
    from google_translate_py import Translator

    translated = Translator().translate(text=text, source="auto", target=target_lang.lower())
    if not translated:
        raise RuntimeError("google_translate_py 번역 결과가 비어 있습니다.")
    return str(translated).strip()


def _looks_unchanged(original: str, translated: str) -> bool:
    normalized_original = re.sub(r"\s+", " ", original).strip()
    normalized_translated = re.sub(r"\s+", " ", translated).strip()
    return bool(normalized_original) and normalized_original == normalized_translated


def _translate_text(text: str, source_lang: str, target_lang: str) -> str:
    last_error: Exception | None = None
    providers = (
        lambda: _translate_via_mymemory(text, source_lang, target_lang),
        lambda: _translate_via_deep_translator(text, target_lang),
        lambda: _translate_via_google_translate_py(text, target_lang),
    )

    for translate_fn in providers:
        try:
            translated = translate_fn()
            if translated and not _looks_unchanged(text, translated):
                return translated
            last_error = RuntimeError("번역 결과가 원문과 같아서 다른 번역기를 시도합니다.")
        except Exception as exc:
            last_error = exc

    raise RuntimeError(f"번역 실패: {last_error}") from last_error


def _translate_segments(
    segments: list[dict],
    source_lang: str,
    target_lang: str,
    job_id: str | None = None,
) -> list[dict]:
    """
    Translate text segments to target language using multiple providers.
    If every provider fails, raise an error instead of silently returning the source text.
    """
    normalized_target = target_lang.lower()
    if normalized_target not in TRANSLATE_LANG_LABELS:
        raise RuntimeError(f"지원하지 않는 타겟 언어: {target_lang}")

    out = []
    translated_cache: dict[str, str] = {}
    update_every = max(1, len(segments) // 20)

    for i, seg in enumerate(segments):
        raw = re.sub(r"\s+", " ", str(seg.get("text", "")).strip())
        if not raw:
            out.append({**seg, "text": ""})
            continue

        if raw in translated_cache:
            translated = translated_cache[raw]
        else:
            translated = _translate_text(raw, source_lang, normalized_target)
            translated_cache[raw] = translated

        out.append({**seg, "text": translated})

        if job_id and ((i + 1) % update_every == 0 or i + 1 == len(segments)):
            _translate_job_update(
                job_id,
                stage="translate",
                message=f"자막 번역 중입니다. ({i + 1}/{len(segments)})",
            )

    
    return out


def _burn_subtitles_to_video(input_video: Path, subtitle_file: Path, output_video: Path, quality_profile: dict):
    # Escape for ffmpeg subtitles filter on Windows.
    sub = str(subtitle_file).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    subtitle_filter = f"subtitles='{sub}'"

    style = quality_profile.get("subtitle_style")
    if style:
        force_style = style.replace("'", "")
        subtitle_filter = f"{subtitle_filter}:force_style='{force_style}'"

    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_video),
        "-vf", subtitle_filter,
        "-c:v", "libx264",
        "-preset", quality_profile["ffmpeg_preset"],
        "-crf", quality_profile["ffmpeg_crf"],
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", quality_profile["audio_bitrate"],
        "-movflags", "+faststart",
        str(output_video),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError("FFmpeg가 설치되어 있지 않거나 PATH에 없습니다.") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(exc.stderr[-2500:] if exc.stderr else "자막 합성 실패") from exc

    if (not output_video.exists()) or output_video.stat().st_size < 100 * 1024:
        raise RuntimeError("자막 합성 결과 영상이 비정상적으로 작습니다. 작업이 중간에 종료됐을 수 있습니다.")


def _transcribe_video_to_segments(input_video: Path, whisper_model: str) -> tuple[str, list[dict], str]:
    audio_wav = AUDIO / f"vt_audio_{uuid4().hex}.wav"
    extract_cmd = [
        "ffmpeg", "-y",
        "-i", str(input_video),
        "-vn", "-ac", "1", "-ar", "16000",
        str(audio_wav),
    ]
    try:
        subprocess.run(extract_cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError("FFmpeg가 설치되어 있지 않거나 PATH에 없습니다.") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(exc.stderr[-2500:] if exc.stderr else "오디오 추출 실패") from exc

    try:
        import whisper
    except Exception as exc:
        raise RuntimeError(
            "Whisper가 설치되어 있지 않습니다. backend/requirements.txt에 openai-whisper를 추가하고 설치하세요."
        ) from exc

    model_name = os.environ.get("VIDEO_TRANSLATOR_WHISPER_MODEL", whisper_model)
    try:
        model = whisper.load_model(model_name)
        result = model.transcribe(
            str(audio_wav),
            task="transcribe",
            fp16=False,
            temperature=0,
            best_of=5,
            beam_size=5,
            condition_on_previous_text=True,
        )
    except Exception as exc:
        raise RuntimeError(f"음성 인식 실패: {exc}") from exc
    finally:
        try:
            audio_wav.unlink(missing_ok=True)
        except Exception:
            pass

    segments = []
    for seg in result.get("segments", []) or []:
        txt = re.sub(r"\s+", " ", str(seg.get("text", "")).strip())
        if not txt:
            continue
        segments.append({
            "start": float(seg.get("start", 0.0)),
            "end": float(seg.get("end", 0.0)),
            "text": txt,
        })

    if not segments:
        raise RuntimeError("음성 인식 결과가 없습니다.")

    full_text = "\n".join(seg["text"] for seg in segments)
    source_lang = (result.get("language") or "auto").lower()
    return source_lang, segments, full_text


def _subtitle_force_style(font_family: str, font_size: int, position: str, margin_v: int) -> str:
    font = (font_family or "Malgun Gothic").replace(",", " ").strip()
    if not font:
        font = "Malgun Gothic"

    align_map = {
        "bottom": 2,
        "middle": 5,
        "top": 8,
    }
    alignment = align_map.get((position or "bottom").lower(), 2)
    size = max(12, min(int(font_size), 80))
    margin = max(0, min(int(margin_v), 200))

    return (
        f"FontName={font},"
        f"FontSize={size},"
        f"Alignment={alignment},"
        f"MarginV={margin},"
        "Outline=2,Shadow=0,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000"
    )


def _translate_job_update(job_id: str, **updates):
    with TRANSLATE_JOB_LOCK:
        job = TRANSLATE_JOBS.setdefault(job_id, {})
        job.setdefault("job_id", job_id)
        job.update(updates)


def _translate_job_snapshot(job_id: str) -> dict | None:
    with TRANSLATE_JOB_LOCK:
        job = TRANSLATE_JOBS.get(job_id)
        return dict(job) if job else None


def _run_translate_job(
    job_id: str,
    input_video: Path,
    out_video_dir: Path,
    out_sub_dir: Path,
    target_lang: str,
    quality_key: str,
    quality_profile: dict,
    mode_key: str,
):
    total_start = time.perf_counter()
    timings: dict[str, float] = {}
    try:
        _translate_job_update(job_id, status="running", stage="transcribe", message="음성 인식 중입니다.")

        stage_start = time.perf_counter()
        source_lang, src_segments, transcript_text = _transcribe_video_to_segments(
            input_video,
            quality_profile["whisper_model"],
        )
        timings["transcribe_sec"] = round(time.perf_counter() - stage_start, 3)

        src_srt = out_sub_dir / f"{job_id}_source.srt"
        dst_srt = out_sub_dir / f"{job_id}_{target_lang.lower()}.srt"
        transcript_txt = out_sub_dir / f"{job_id}_transcript.txt"
        output_video = out_video_dir / (
            f"{job_id}_{target_lang.lower()}_subbed.mp4" if mode_key == "translate" else f"{job_id}_source_subbed.mp4"
        )

        _write_srt(src_segments, src_srt)
        translated_segments = None
        if mode_key == "translate":
            _translate_job_update(job_id, stage="translate", message="자막을 번역 중입니다.")
            stage_start = time.perf_counter()
            translated_segments = _translate_segments(src_segments, source_lang, target_lang, job_id=job_id)
            _write_srt(translated_segments, dst_srt)
            timings["translate_sec"] = round(time.perf_counter() - stage_start, 3)
        else:
            timings["translate_sec"] = 0.0

        transcript_txt.write_text(transcript_text, encoding="utf-8")

        _translate_job_update(job_id, stage="burn", message="자막을 영상에 입히는 중입니다.")
        subtitle_for_burn = dst_srt if mode_key == "translate" else src_srt
        stage_start = time.perf_counter()
        _burn_subtitles_to_video(input_video, subtitle_for_burn, output_video, quality_profile)
        timings["burn_sec"] = round(time.perf_counter() - stage_start, 3)
        timings["total_sec"] = round(time.perf_counter() - total_start, 3)

        translated_srt_rel = str(dst_srt.relative_to(BASE)) if mode_key == "translate" else ""
        translated_srt_url = f"/api/files/subtitle/{quote(dst_srt.name, safe='')}" if mode_key == "translate" else ""
        result = {
            "ok": True,
            "job_id": job_id,
            "mode": mode_key,
            "source_lang": source_lang,
            "target_lang": target_lang.lower(),
            "target_lang_label": TRANSLATE_LANG_LABELS.get(target_lang.lower(), target_lang.lower()),
            "quality": quality_key,
            "whisper_model": quality_profile["whisper_model"],
            "video_file": str(output_video.relative_to(BASE)),
            "source_srt_file": str(src_srt.relative_to(BASE)),
            "translated_srt_file": translated_srt_rel,
            "transcript_file": str(transcript_txt.relative_to(BASE)),
            "video_url": f"/api/files/video/{quote(output_video.name, safe='')}",
            "source_srt_url": f"/api/files/subtitle/{quote(src_srt.name, safe='')}",
            "translated_srt_url": translated_srt_url,
            "transcript_url": f"/api/files/subtitle/{quote(transcript_txt.name, safe='')}",
            "timings": timings,
        }
        _translate_job_update(job_id, status="completed", stage="done", message="완료되었습니다.", timings=timings, result=result)
    except Exception as exc:
        timings["total_sec"] = round(time.perf_counter() - total_start, 3)
        _translate_job_update(job_id, status="failed", stage="failed", message=str(exc), error=str(exc), timings=timings)


def _translate_job_response(job: dict) -> dict:
    payload = {
        "ok": True,
        "job_id": job.get("job_id"),
        "status": job.get("status", "queued"),
        "stage": job.get("stage", "queued"),
        "message": job.get("message", ""),
        "timings": job.get("timings") or {},
    }
    if job.get("status") == "completed" and job.get("result"):
        payload.update(job["result"])
    elif job.get("status") == "failed":
        payload["detail"] = job.get("error") or job.get("message") or "작업 실패"
    return payload

def _platform_profile_dir(platform: str) -> Path:
    return ACCOUNTS / platform

def _platform_login_url(platform: str) -> str:
    if platform == "instagram":
        return "https://www.instagram.com/accounts/login/"
    if platform == "xiaohongshu":
        return "https://www.xiaohongshu.com/"
    return "https://www.google.com/"

def _profile_has_data(profile_dir: Path) -> bool:
    try:
        return profile_dir.exists() and any(profile_dir.iterdir())
    except Exception:
        return False

def _profile_has_valid_session(platform: str) -> bool:
    profile_dir = _platform_profile_dir(platform)
    if not _profile_has_data(profile_dir):
        return False

    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return False

    if platform == "instagram":
        target = "https://www.instagram.com/explore/search/keyword/?q=ai"
    else:
        target = "https://www.xiaohongshu.com/search_result?keyword=ai"

    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=True,
            )
            page = context.new_page()
            page.set_default_timeout(8000)
            page.goto(target, wait_until="domcontentloaded")
            page.wait_for_timeout(1200)
            current_url = page.url.lower()
            body = (page.text_content("body") or "").lower()
            try:
                context.close()
            except Exception:
                pass
    except Exception:
        return False

    if platform == "instagram":
        if "accounts/login" in current_url or "__coig_login=1" in current_url or "/popular/?next=" in current_url:
            return False
        if "log in" in body and "sign up" in body:
            return False
        return True

    if platform == "xiaohongshu":
        if "login" in current_url or "登录" in body:
            return False
        return True

    return False

def _open_account_browser(platform: str) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return

    profile_dir = _platform_profile_dir(platform)
    profile_dir.mkdir(parents=True, exist_ok=True)

    target = _platform_login_url(platform)

    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=False,
            )
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(target, wait_until="domcontentloaded")
            except Exception:
                pass

            while True:
                try:
                    if context.pages:
                        context.pages[0].wait_for_timeout(1000)
                    else:
                        break
                except Exception:
                    break

            try:
                context.close()
            except Exception:
                pass
    finally:
        OPEN_CONNECTORS.pop(platform, None)


def _social_domains(platform: str) -> tuple[str, ...]:
    if platform == "instagram":
        return ("instagram.com",)
    if platform == "xiaohongshu":
        return ("xiaohongshu.com", "xhslink.com", "xhs.link")
    return ()

def _build_social_query(platform: str, keyword: str) -> str:
    if platform == "instagram":
        return f"instagram {keyword}"
    if platform == "xiaohongshu":
        return f"xiaohongshu {keyword}"
    return keyword

def _social_query_variants(platform: str, keyword: str) -> list[str]:
    base = keyword.strip()
    if not base:
        return []
    if platform == "instagram":
        return [
            base,
            f"instagram {base}",
            f"site:instagram.com {base}",
            f"site:instagram.com {base} reel",
            f"site:instagram.com {base} video",
            f"site:instagram.com {base} shorts",
        ]
    if platform == "xiaohongshu":
        return [
            base,
            f"xiaohongshu {base}",
            f"site:xiaohongshu.com {base}",
            f"site:xiaohongshu.com {base} explore",
            f"site:xiaohongshu.com {base} discovery",
        ]
    return [base]

def _collect_social_links(platform: str, query: str, max_count: int) -> list[str]:
    domains = _social_domains(platform)
    seen = set()
    out = []
    links = []
    targets = [
        f"https://html.duckduckgo.com/html/?q={quote_plus(query)}",
        f"https://duckduckgo.com/html/?q={quote_plus(query)}",
    ]

    for target in targets:
        req = Request(
            target,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            },
        )
        try:
            with urlopen(req, timeout=SOCIAL_HTTP_TIMEOUT_SEC) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
            for enc in re.findall(r"uddg=([^&\"']+)", html):
                try:
                    links.append(unquote(enc))
                except Exception:
                    continue
            links.extend(re.findall(r'href="(https?://[^\"#]+)"', html))
        except Exception:
            continue

    try:
        proxy_target = f"https://r.jina.ai/http://html.duckduckgo.com/html/?q={quote_plus(query)}"
        proxy_req = Request(proxy_target, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(proxy_req, timeout=SOCIAL_PROXY_TIMEOUT_SEC) as resp:
            text = resp.read().decode("utf-8", errors="ignore")
        links.extend(re.findall(r'https?://[^\s\)\]\"\']+', text))
    except Exception:
        pass

    for u in links:
        u = u.strip()
        if not u.startswith("http"):
            continue
        host = (urlparse(u).netloc or "").lower()
        if not any(d in host for d in domains):
            continue
        low = u.lower()
        if "duckduckgo.com" in low or "bing.com/ck/a" in low:
            continue
        key = u.split("?", 1)[0]
        if key in seen:
            continue
        seen.add(key)
        out.append(u)
        if len(out) >= max_count:
            break

    return out

def _social_result_from_url(platform: str, url: str, keyword: str) -> dict:
    parsed = urlparse(url)
    path_parts = [part for part in parsed.path.split("/") if part]
    title = keyword.strip() or parsed.netloc or url
    if path_parts:
        if platform == "instagram" and path_parts[0] in {"reel", "p"} and len(path_parts) > 1:
            kind = "Reel" if path_parts[0] == "reel" else "Post"
            title = f"{keyword.strip()} - Instagram {kind}"
        elif platform == "instagram" and path_parts[0] == "popular" and len(path_parts) > 1:
            title = unquote(path_parts[1]).replace("-", " ")
        elif platform == "xiaohongshu" and len(path_parts) > 1:
            title = f"{keyword.strip()} - Xiaohongshu"
        else:
            title = unquote(path_parts[-1]).replace("-", " ")
    return {
        "id": parsed.path.strip("/").replace("/", "_") or parsed.netloc,
        "platform": platform,
        "title": title,
        "url": url,
        "thumbnail": "",
        "channel": parsed.netloc,
        "upload_date": "",
        "duration": 0,
        "view_count": None,
        "like_count": None,
        "description": url,
        "has_subtitles": None,
    }

def _social_url_priority(platform: str, url: str) -> int:
    low = url.lower()
    if platform == "instagram":
        if "/reel/" in low:
            return 0
        if "/p/" in low:
            return 1
        if "/popular/" in low and "__coig_login=1" not in low and "next=" not in low:
            return 4
        return 99
    if platform == "xiaohongshu":
        if "/explore/" in low or "discovery/item" in low:
            return 0
        if "/search_result" in low:
            return 5
        return 99
    return 99

def _is_login_redirect_result(platform: str, url: str) -> bool:
    low = url.lower()
    if platform == "instagram":
        return "/popular/" in low and ("__coig_login=1" in low or "next=" in low)
    return False

def _social_search_page_item(platform: str, keyword: str) -> dict:
    if platform == "instagram":
        url = f"https://www.instagram.com/explore/search/keyword/?q={quote_plus(keyword)}"
        title = f"Instagram 검색 결과: {keyword}"
        description = "인스타그램 자체 검색 결과 페이지를 엽니다."
    else:
        url = f"https://www.xiaohongshu.com/search_result?keyword={quote_plus(keyword)}"
        title = f"Xiaohongshu 검색 결과: {keyword}"
        description = "샤오홍수 자체 검색 결과 페이지를 엽니다."
    return {
        "id": f"{platform}-search-{re.sub(r'[^0-9A-Za-z가-힣]+', '-', keyword).strip('-') or 'query'}",
        "platform": platform,
        "title": title,
        "url": url,
        "thumbnail": "",
        "channel": platform,
        "upload_date": "",
        "duration": 0,
        "view_count": None,
        "like_count": None,
        "description": description,
        "has_subtitles": None,
    }

def _platform_label(platform: str) -> str:
    if platform == "instagram":
        return "Instagram"
    if platform == "xiaohongshu":
        return "Xiaohongshu"
    return platform


def _social_cache_get(key: tuple):
    now = time.time()
    with SOCIAL_SEARCH_CACHE_LOCK:
        row = SOCIAL_SEARCH_CACHE.get(key)
        if not row:
            return None
        expires_at, payload = row
        if expires_at <= now:
            SOCIAL_SEARCH_CACHE.pop(key, None)
            return None
        return payload


def _social_cache_set(key: tuple, payload: dict):
    now = time.time()
    with SOCIAL_SEARCH_CACHE_LOCK:
        if len(SOCIAL_SEARCH_CACHE) >= SOCIAL_SEARCH_CACHE_MAX:
            # Drop expired entries first; if still large, evict oldest by expiry.
            expired = [k for k, (exp, _) in SOCIAL_SEARCH_CACHE.items() if exp <= now]
            for k in expired:
                SOCIAL_SEARCH_CACHE.pop(k, None)
            if len(SOCIAL_SEARCH_CACHE) >= SOCIAL_SEARCH_CACHE_MAX:
                oldest_key = min(SOCIAL_SEARCH_CACHE, key=lambda k: SOCIAL_SEARCH_CACHE[k][0])
                SOCIAL_SEARCH_CACHE.pop(oldest_key, None)
        SOCIAL_SEARCH_CACHE[key] = (now + SOCIAL_SEARCH_CACHE_TTL_SEC, payload)

def _extract_platform_native_items(platform: str, keyword: str, max_count: int) -> list[dict]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return []

    profile_dir = _platform_profile_dir(platform)
    headed = os.environ.get("SOCIAL_PLAYWRIGHT_HEADED", "0") == "1"

    if platform == "instagram":
        target = f"https://www.instagram.com/explore/search/keyword/?q={quote_plus(keyword)}"
        domain = "instagram.com"
        tokens = (("/reel/", 0), ("/p/", 0), ("/popular/", 1))
    elif platform == "xiaohongshu":
        target = f"https://www.xiaohongshu.com/search_result?keyword={quote_plus(keyword)}"
        domain = "xiaohongshu.com"
        tokens = (("/explore/", 0), ("discovery/item", 0), ("/search_result", 1))
    else:
        return []

    seen = set()
    out = []

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=not headed,
        )
        page = context.new_page()
        page.set_default_timeout(9000)

        try:
            page.goto(target, wait_until="domcontentloaded")
            page.wait_for_timeout(900)
            for _ in range(2):
                page.mouse.wheel(0, 1600)
                page.wait_for_timeout(450)
            cards = page.eval_on_selector_all(
                "a[href]",
                """
                els => els.map((el) => {
                  const href = el.href || '';
                  const container = el.closest('article, section, li, div') || el.parentElement || el;
                  const rawText = (el.innerText || el.getAttribute('aria-label') || container.innerText || '').replace(/\s+/g, ' ').trim();
                  const img = el.querySelector('img') || (container && container.querySelector ? container.querySelector('img') : null);
                  const thumb = img && img.src ? img.src : '';
                  return { href, text: rawText, thumbnail: thumb };
                })
                """,
            )
        except Exception:
            try:
                context.close()
            except Exception:
                pass
            return []

        try:
            context.close()
        except Exception:
            pass

    for row in cards:
        href = ((row or {}).get("href") or "").strip()
        if not href.startswith("http"):
            continue
        host = (urlparse(href).netloc or "").lower()
        if domain not in host:
            continue

        low = href.lower()
        priority = None
        for token, token_priority in tokens:
            if token in low:
                priority = token_priority
                break
        if priority is None:
            continue

        key = href.split("?", 1)[0]
        if key in seen:
            continue
        seen.add(key)

        raw_text = re.sub(r"\s+", " ", ((row or {}).get("text") or "").strip())
        title = raw_text[:110].strip(" -") if raw_text else ""
        if not title:
            title = _social_result_from_url(platform, href, keyword)["title"]
        description = raw_text[:220] if raw_text and raw_text != title else ""

        out.append({
            "id": key.strip("/").replace("/", "_") or f"{platform}-{len(out)}",
            "platform": platform,
            "title": title,
            "url": href,
            "thumbnail": ((row or {}).get("thumbnail") or "").strip(),
            "channel": _platform_label(platform),
            "upload_date": "",
            "duration": 0,
            "view_count": None,
            "like_count": None,
            "description": description,
            "has_subtitles": None,
            "_priority": priority,
        })

    out.sort(key=lambda item: (item.get("_priority", 99), item.get("title", "")))
    trimmed = []
    for item in out:
        item.pop("_priority", None)
        trimmed.append(item)
        if len(trimmed) >= max_count:
            break
    return trimmed

def _extract_web_urls(platform: str, keyword: str, max_count: int) -> list[str]:
    serp_key = os.environ.get("SERPAPI_KEY", "").strip()
    if serp_key:
        try:
            domains = _social_domains(platform)
            q = _build_social_query(platform, keyword)
            api_url = (
                "https://serpapi.com/search.json?"
                f"engine=google&q={quote_plus(q)}&num={max_count}&api_key={quote_plus(serp_key)}"
            )
            req = Request(api_url, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="ignore"))

            links = []
            for row in payload.get("organic_results", []) or []:
                link = (row or {}).get("link")
                if link:
                    links.append(link)

            seen = set()
            out = []
            for u in links:
                host = (urlparse(u).netloc or "").lower()
                if not any(d in host for d in domains):
                    continue
                key = u.split("?", 1)[0]
                if key in seen:
                    continue
                seen.add(key)
                out.append(u)
                if len(out) >= max_count:
                    break
            if out:
                return out
        except Exception:
            # Fall back to free-source scraping when API request fails.
            pass

    out = []
    for q in _social_query_variants(platform, keyword)[:SOCIAL_QUERY_VARIANTS_MAX]:
        out.extend(_collect_social_links(platform, q, max_count - len(out)))
        if len(out) >= max_count:
            return out[:max_count]

    if out:
        seen = set()
        deduped = []
        for u in out:
            key = u.split("?", 1)[0]
            if key in seen:
                continue
            seen.add(key)
            deduped.append(u)
            if len(deduped) >= max_count:
                break
        if deduped:
            return deduped

    if not SOCIAL_ENABLE_SLOW_FALLBACK:
        return []

    # Optional slow fallbacks when quality is preferred over speed.
    out = _extract_web_urls_browser(platform, keyword, max_count)
    if out:
        return out

    out = _extract_platform_native_urls(platform, keyword, max_count)
    return out


def _extract_web_urls_browser(platform: str, keyword: str, max_count: int) -> list[str]:
    domains = _social_domains(platform)
    if not domains:
        return []

    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return []

    seen = set()
    out = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_default_timeout(5000)

        variants = _social_query_variants(platform, keyword)[:SOCIAL_QUERY_VARIANTS_MAX]
        for q in variants:
            engines = [
                f"https://r.jina.ai/http://html.duckduckgo.com/html/?q={quote_plus(q)}",
                f"https://html.duckduckgo.com/html/?q={quote_plus(q)}",
                f"https://www.bing.com/search?q={quote_plus(q)}",
            ]

            for url in engines:
                try:
                    page.goto(url, wait_until="domcontentloaded")
                    page.wait_for_timeout(250)
                    if "r.jina.ai" in url:
                        raw_text = page.text_content("body") or ""
                        hrefs = re.findall(r'https?://[^\s\)\]\"\']+', raw_text)
                    else:
                        hrefs = page.eval_on_selector_all(
                            "a[href]",
                            "els => els.map(e => e.href).filter(Boolean)",
                        )
                except Exception:
                    continue

                for raw in hrefs:
                    u = (raw or "").strip()
                    if not u.startswith("http"):
                        continue

                    parsed = urlparse(u)
                    host = (parsed.netloc or "").lower()

                    # DuckDuckGo redirect extraction
                    if "duckduckgo.com" in host and parsed.path.startswith("/l/"):
                        target = parse_qs(parsed.query).get("uddg", [""])[0]
                        if target:
                            u = unquote(target)
                            parsed = urlparse(u)
                            host = (parsed.netloc or "").lower()

                    if not any(d in host for d in domains):
                        continue

                    low = u.lower()
                    if platform == "instagram" and not ("/p/" in low or "/reel/" in low):
                        continue
                    if platform == "xiaohongshu" and not ("/explore/" in low or "discovery/item" in low or "/search_result" in low):
                        continue

                    key = u.split("?", 1)[0]
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(u)
                    if len(out) >= max_count:
                        try:
                            browser.close()
                        except Exception:
                            pass
                        return out

        try:
            browser.close()
        except Exception:
            pass

    return out


def _extract_platform_native_urls(platform: str, keyword: str, max_count: int) -> list[str]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return []

    profile_dir = _platform_profile_dir(platform)
    headed = os.environ.get("SOCIAL_PLAYWRIGHT_HEADED", "0") == "1"

    if platform == "instagram":
        target = f"https://www.instagram.com/explore/search/keyword/?q={quote_plus(keyword)}"
        wanted = ("/p/", "/reel/")
        domain = "instagram.com"
    elif platform == "xiaohongshu":
        target = f"https://www.xiaohongshu.com/search_result?keyword={quote_plus(keyword)}"
        wanted = ("/explore/", "discovery/item")
        domain = "xiaohongshu.com"
    else:
        return []

    seen = set()
    out = []

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=not headed,
        )
        page = context.new_page()
        page.set_default_timeout(9000)

        try:
            page.goto(target, wait_until="domcontentloaded")
            page.wait_for_timeout(900)
            for _ in range(2):
                page.mouse.wheel(0, 1800)
                page.wait_for_timeout(450)

            hrefs = page.eval_on_selector_all(
                "a[href]",
                "els => els.map(e => e.href).filter(Boolean)",
            )
        except Exception:
            try:
                context.close()
            except Exception:
                pass
            return []

        for u in hrefs:
            u = (u or "").strip()
            if not u.startswith("http"):
                continue
            host = (urlparse(u).netloc or "").lower()
            if domain not in host:
                continue
            low = u.lower()
            if not any(token in low for token in wanted):
                continue
            key = u.split("?", 1)[0]
            if key in seen:
                continue
            seen.add(key)
            out.append(u)
            if len(out) >= max_count:
                break

        try:
            context.close()
        except Exception:
            pass

    return out


@app.get("/api/account/status")
def account_status(platform: str = Query(..., pattern="^(instagram|xiaohongshu)$")):
    profile_dir = _platform_profile_dir(platform)
    has_profile = _profile_has_data(profile_dir)
    session_valid = _profile_has_valid_session(platform) if has_profile else False
    return {
        "platform": platform,
        "connected": has_profile,
        "session_valid": session_valid,
        "profile_dir": str(profile_dir.relative_to(BASE)),
        "login_url": _platform_login_url(platform),
    }


@app.post("/api/account/connect")
def account_connect(platform: str = Query(..., pattern="^(instagram|xiaohongshu)$")):
    if STRICT_NO_SERVER_PERSIST:
        raise HTTPException(403, "개인정보 모드에서는 서버에 로그인 세션을 저장할 수 없습니다.")

    existing = OPEN_CONNECTORS.get(platform)
    if existing and existing.is_alive():
        return {
            "ok": True,
            "platform": platform,
            "message": "연결용 로그인 창이 이미 열려 있습니다. 그 창에서 로그인 후 닫아주세요.",
        }

    thread = threading.Thread(target=_open_account_browser, args=(platform,), daemon=True)
    OPEN_CONNECTORS[platform] = thread
    thread.start()
    return {
        "ok": True,
        "platform": platform,
        "message": "앱 전용 로그인 창을 열었습니다. 그 창에서 로그인한 뒤 닫으면 세션이 저장됩니다.",
    }


@app.post("/api/account/disconnect")
def account_disconnect(platform: str = Query(..., pattern="^(instagram|xiaohongshu)$")):
    profile_dir = _platform_profile_dir(platform)
    try:
        if profile_dir.exists():
            shutil.rmtree(profile_dir, ignore_errors=True)
        profile_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        raise HTTPException(500, f"연결 해제 실패: {exc}")
    return {
        "ok": True,
        "platform": platform,
        "message": "연결 세션을 삭제했습니다.",
    }

class UrlRequest(BaseModel):
    url: str


class SubtitleRenderRequest(BaseModel):
    job_id: str
    srt_text: str
    font_family: str = "Malgun Gothic"
    font_size: int = 28
    position: str = "bottom"
    margin_v: int = 28
    quality: str = "balanced"


@app.post("/api/translate-video")
async def translate_video(
    video: UploadFile = File(...),
    target_lang: str = Form("en"),
    quality: str = Form("balanced"),
    mode: str = Form("transcribe"),
):
    if STRICT_NO_SERVER_PERSIST:
        raise HTTPException(403, "개인정보 모드에서는 업로드/번역 파일을 서버에 저장하지 않습니다.")

    ext = Path(video.filename or "video.mp4").suffix.lower() or ".mp4"
    if ext not in {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}:
        raise HTTPException(400, "지원되지 않는 영상 형식입니다. mp4/mov/mkv/webm/m4v/avi 파일을 업로드하세요.")

    quality_key = (quality or "balanced").lower()
    quality_profile = TRANSLATE_QUALITY_PRESETS.get(quality_key)
    if not quality_profile:
        allowed = ", ".join(TRANSLATE_QUALITY_PRESETS.keys())
        raise HTTPException(400, f"지원되지 않는 품질 옵션입니다: {quality}. 사용 가능: {allowed}")

    mode_key = (mode or "transcribe").lower()
    if mode_key not in {"transcribe", "translate"}:
        raise HTTPException(400, "지원되지 않는 모드입니다. transcribe 또는 translate 를 사용하세요.")

    job_id = f"vt_{uuid4().hex[:12]}"
    out_video_dir = VIDEOS / job_id
    out_video_dir.mkdir(parents=True, exist_ok=True)
    out_sub_dir = SUBTITLES / job_id
    out_sub_dir.mkdir(parents=True, exist_ok=True)

    input_video = out_video_dir / f"input{ext}"
    with input_video.open("wb") as f:
        f.write(await video.read())

    _translate_job_update(
        job_id,
        status="queued",
        stage="queued",
        message="작업이 대기열에 등록되었습니다.",
        mode=mode_key,
        target_lang=target_lang.lower(),
        target_lang_label=TRANSLATE_LANG_LABELS.get(target_lang.lower(), target_lang.lower()),
        quality=quality_key,
        whisper_model=quality_profile["whisper_model"],
        input_video=str(input_video.relative_to(BASE)),
        output_video_dir=str(out_video_dir.relative_to(BASE)),
        subtitle_dir=str(out_sub_dir.relative_to(BASE)),
    )
    TRANSLATE_JOB_EXECUTOR.submit(
        _run_translate_job,
        job_id,
        input_video,
        out_video_dir,
        out_sub_dir,
        target_lang,
        quality_key,
        quality_profile,
        mode_key,
    )

    return {
        "ok": True,
        "job_id": job_id,
        "status": "queued",
        "stage": "queued",
        "message": "작업이 시작되었습니다. 상태를 확인하세요.",
        "status_url": f"/api/translate-video/status/{job_id}",
    }


@app.get("/api/translate-video/status/{job_id}")
def translate_video_status(job_id: str):
    job = _translate_job_snapshot(job_id)
    if not job:
        raise HTTPException(404, "작업을 찾을 수 없습니다.")
    return _translate_job_response(job)


@app.post("/api/translate-video/render-subtitles")
def render_subtitles(req: SubtitleRenderRequest):
    if STRICT_NO_SERVER_PERSIST:
        raise HTTPException(403, "개인정보 모드에서는 서버 재렌더링을 지원하지 않습니다.")

    quality_key = (req.quality or "balanced").lower()
    quality_profile = dict(TRANSLATE_QUALITY_PRESETS.get(quality_key) or {})
    if not quality_profile:
        allowed = ", ".join(TRANSLATE_QUALITY_PRESETS.keys())
        raise HTTPException(400, f"지원되지 않는 품질 옵션입니다: {req.quality}. 사용 가능: {allowed}")

    job_id = (req.job_id or "").strip()
    if not job_id:
        raise HTTPException(400, "job_id가 필요합니다.")

    srt_text = (req.srt_text or "").replace("\r\n", "\n").strip()
    if "-->" not in srt_text:
        raise HTTPException(400, "유효한 SRT 내용이 아닙니다. 시간 구간(-->)이 필요합니다.")

    video_dir = VIDEOS / job_id
    if not video_dir.exists():
        raise HTTPException(404, "작업 영상을 찾을 수 없습니다.")

    input_video = next((p for p in sorted(video_dir.glob("input.*")) if p.is_file()), None)
    if not input_video:
        raise HTTPException(404, "원본 입력 영상을 찾을 수 없습니다.")

    subtitle_dir = SUBTITLES / job_id
    subtitle_dir.mkdir(parents=True, exist_ok=True)
    rev = int(time.time())
    edited_srt = subtitle_dir / f"{job_id}_edited_{rev}.srt"
    edited_video = video_dir / f"{job_id}_edited_{rev}.mp4"

    try:
        edited_srt.write_text(srt_text + "\n", encoding="utf-8")
        quality_profile["subtitle_style"] = _subtitle_force_style(
            req.font_family,
            req.font_size,
            req.position,
            req.margin_v,
        )
        _burn_subtitles_to_video(input_video, edited_srt, edited_video, quality_profile)
    except RuntimeError as exc:
        raise HTTPException(500, str(exc))
    except Exception as exc:
        raise HTTPException(500, f"자막 재합성 실패: {exc}")

    return {
        "ok": True,
        "job_id": job_id,
        "video_file": str(edited_video.relative_to(BASE)),
        "srt_file": str(edited_srt.relative_to(BASE)),
        "video_url": f"/api/files/video/{quote(edited_video.name, safe='')}",
        "srt_url": f"/api/files/subtitle/{quote(edited_srt.name, safe='')}",
    }

def normalize_flat(e: dict) -> dict:
    """Normalize a flat-extraction entry (no per-video HTTP request needed)."""
    vid = e.get("id") or ""
    raw_views = e.get("view_count")
    raw_dur = e.get("duration")

    # Best thumbnail: prefer the largest from the thumbnails list, fall back to url
    thumb = ""
    thumbs = e.get("thumbnails") or []
    if thumbs:
        # Pick the one with the highest width, else last entry
        best = max(thumbs, key=lambda t: t.get("width") or 0)
        thumb = best.get("url") or ""
    if not thumb:
        thumb = e.get("thumbnail") or ""
    if not thumb and vid:
        thumb = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"

    return {
        "id": vid,
        "platform": "youtube",
        "title": e.get("title") or "",
        "url": e.get("webpage_url") or e.get("url") or (f"https://www.youtube.com/watch?v={vid}" if vid else ""),
        "thumbnail": thumb,
        "channel": e.get("channel") or e.get("uploader") or e.get("channel_id") or "",
        "channel_id": e.get("channel_id") or e.get("uploader_id") or "",
        "upload_date": e.get("upload_date") or "",
        "duration": n(raw_dur) if raw_dur is not None else 0,
        "view_count": n(raw_views) if raw_views is not None else None,
        "like_count": None,
        "description": e.get("description") or "",
        "has_subtitles": None,
    }

@app.get("/api/health")
def health():
    return {"ok": True}

@app.get("/api/search")
def search(
    q: str = Query(..., min_length=1),
    min_views: int = 0,
    max_views: int = 10**15,
    date_from: str = "",
    date_to: str = "",
    limit: int = 24,
    offset: int = 0,
):
    """
    Keyword-only YouTube search.

    IMPORTANT:
    - We never filter a result unless the user explicitly supplied that filter.
    - "Load more" increases the requested search window using offset/limit.
    - We do not discard videos because optional metadata is missing.
    - The search pool is deliberately larger than the visible page.
    """
    page_size = max(1, min(limit, 100))
    start_at = max(0, offset)

    if is_youtube_url(q):
        candidates = [{"url": q, "id": None}]
    elif is_channel_query(q):
        # Fetch videos from a channel URL or @handle
        channel_url = q.strip() if q.strip().startswith("http") else f"https://www.youtube.com/{q.strip()}"
        opts = {
            "quiet": True,
            "skip_download": True,
            "extract_flat": True,
            "ignoreerrors": True,
            "playlistend": min(max(start_at + page_size, 50), 200),
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(channel_url, download=False)
        except Exception as exc:
            raise HTTPException(502, str(exc))

        candidates = []
        entries = (info or {}).get("entries", [])
        # Channel pages may have nested entries
        if entries and isinstance(entries[0], dict) and entries[0].get("entries"):
            entries = entries[0]["entries"]
        for e in entries:
            if not e:
                continue
            vid = e.get("id")
            url = e.get("webpage_url") or e.get("url")
            if vid and url:
                # Keep full flat entry for normalize_flat()
                candidates.append(e)
    else:
        # Fetch a larger window every time. yt-dlp/YouTube search is ranked;
        # the UI exposes pages from that ranked result set rather than
        # silently dropping entries.
        search_count = min(max(start_at + page_size, 50), 500)
        opts = {
            "quiet": True,
            "skip_download": True,
            "extract_flat": True,
            "ignoreerrors": True,
            "noplaylist": True,
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"ytsearch{search_count}:{q}", download=False)
        except Exception as exc:
            raise HTTPException(502, str(exc))

        candidates = []
        for e in (info or {}).get("entries", []):
            if not e:
                continue
            vid = e.get("id")
            url = e.get("webpage_url") or e.get("url")
            if vid and url:
                # Keep the full flat entry so normalize_flat() can use it
                candidates.append(e)

    # No deduplication by title; only the same video ID is a duplicate.
    seen = set()
    unique = []
    for c in candidates:
        key = c.get("id") or c.get("url") or c.get("webpage_url")
        if key in seen:
            continue
        seen.add(key)
        unique.append(c)

    page_candidates = unique[start_at:start_at + page_size]
    out = []

    if is_youtube_url(q):
        # Direct URL: need full metadata (single request is fine)
        url0 = page_candidates[0].get("url") or page_candidates[0].get("webpage_url") if page_candidates else None
        raw = get_detail(url0) if url0 else None
        if raw:
            v = normalize(raw)
            if min_views > 0 and v["view_count"] is not None and v["view_count"] < min_views:
                pass
            elif max_views < 10**15 and v["view_count"] is not None and v["view_count"] > max_views:
                pass
            elif date_from and v["upload_date"] and v["upload_date"] < date_from.replace("-", ""):
                pass
            elif date_to and v["upload_date"] and v["upload_date"] > date_to.replace("-", ""):
                pass
            else:
                out.append(v)
    else:
        # Keyword / channel search: flat entries already have enough metadata.
        # No per-video HTTP requests needed — use normalize_flat() directly.
        for c in page_candidates:
            v = normalize_flat(c)
            if min_views > 0 and v["view_count"] is not None and v["view_count"] < min_views:
                continue
            if max_views < 10**15 and v["view_count"] is not None and v["view_count"] > max_views:
                continue
            if date_from and v["upload_date"] and v["upload_date"] < date_from.replace("-", ""):
                continue
            if date_to and v["upload_date"] and v["upload_date"] > date_to.replace("-", ""):
                continue
            out.append(v)

    return {
        "query": q,
        "offset": start_at,
        "limit": page_size,
        "count": len(out),
        "has_more": len(unique) > start_at + page_size,
        "searched_candidates": len(unique),
        "items": out
    }


@app.get("/api/search-social")
def search_social(
    platform: str = Query(..., pattern="^(instagram|xiaohongshu)$"),
    q: str = Query(..., min_length=1),
    min_views: int = 0,
    max_views: int = 10**15,
    date_from: str = "",
    date_to: str = "",
    limit: int = 24,
    offset: int = 0,
):
    """Keyword search for Instagram/Xiaohongshu using platform-native search pages."""
    page_size = max(1, min(limit, 50))
    start_at = max(0, offset)
    keyword = q.strip()
    cache_key = (platform, keyword.lower(), int(min_views), int(max_views), date_from, date_to, page_size, start_at)

    cached = _social_cache_get(cache_key)
    if cached is not None:
        return cached

    requested = start_at + page_size + 12
    has_profile = _profile_has_data(_platform_profile_dir(platform))

    items = []
    if has_profile:
        items = [
            item for item in _extract_platform_native_items(platform, keyword, requested)
            if not _is_login_redirect_result(platform, item.get("url", ""))
        ]
    if items:
        items.sort(key=lambda item: (_social_url_priority(platform, item.get("url", "")), item.get("title", "")))
        sliced = items[start_at:start_at + page_size]
        result = {
            "query": q,
            "platform": platform,
            "offset": start_at,
            "limit": page_size,
            "count": len(sliced),
            "has_more": len(items) > start_at + page_size,
            "searched_candidates": len(items),
            "items": sliced,
        }
        _social_cache_set(cache_key, result)
        return result

    urls = []
    try:
        urls = _extract_web_urls(platform, keyword, requested)
    except Exception:
        urls = []

    fallback_items = []
    for url in urls:
        if _is_login_redirect_result(platform, url):
            continue
        if _social_url_priority(platform, url) >= 99:
            continue
        fallback_items.append(_social_result_from_url(platform, url, keyword))

    if fallback_items:
        fallback_items.sort(key=lambda item: (_social_url_priority(platform, item.get("url", "")), item.get("title", "")))
        sliced = fallback_items[start_at:start_at + page_size]
        result = {
            "query": q,
            "platform": platform,
            "offset": start_at,
            "limit": page_size,
            "count": len(sliced),
            "has_more": len(fallback_items) > start_at + page_size,
            "searched_candidates": len(fallback_items),
            "items": sliced,
        }
        _social_cache_set(cache_key, result)
        return result

    result = {
        "query": q,
        "platform": platform,
        "offset": start_at,
        "limit": page_size,
        "count": 1,
        "has_more": False,
        "searched_candidates": 1,
        "items": [_social_search_page_item(platform, q)],
    }
    _social_cache_set(cache_key, result)
    return result

@app.post("/api/download/{video_id}")
def download(video_id: str):
    url = f"https://www.youtube.com/watch?v={video_id}"
    temp_root = _mk_temp_dir("vs_download_")
    out_dir = temp_root / video_id
    try:
        _info, path = _download_video_with_fallback(url, out_dir)
    except Exception as exc:
        _cleanup_path(temp_root)
        raise HTTPException(502, str(exc))

    if not path.exists():
        _cleanup_path(temp_root)
        raise HTTPException(500, "다운로드 파일을 찾을 수 없습니다.")
    return _file_download_response(path, temp_root)

@app.post("/api/subtitles/{video_id}")
def subtitles(video_id: str):
    """Download subtitles/captions if YouTube exposes them."""
    temp_root = _mk_temp_dir("vs_subtitles_")
    out_dir = temp_root / video_id
    out_dir.mkdir(parents=True, exist_ok=True)
    url = f"https://www.youtube.com/watch?v={video_id}"
    opts = _yt_dlp_opts(
        skip_download=True,
        writesubtitles=True,
        writeautomaticsub=True,
        subtitleslangs=["ko", "en"],
        subtitlesformat="srt/vtt/best",
        outtmpl=str(out_dir / "%(title).180s.%(ext)s"),
    )
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(url, download=True)
    except Exception as exc:
        _cleanup_path(temp_root)
        raise HTTPException(502, str(exc))

    files = sorted(out_dir.glob("*"))
    if not files:
        _cleanup_path(temp_root)
        raise HTTPException(404, "No subtitles/captions are available for this video.")

    if len(files) == 1:
        return _file_download_response(files[0], temp_root)

    zip_path = temp_root / f"{video_id}_subtitles.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            if f.is_file():
                zf.write(f, arcname=f.name)
    return _file_download_response(zip_path, temp_root)

@app.post("/api/audio/{video_id}")
def audio(video_id: str):
    temp_root = _mk_temp_dir("vs_audio_")
    source_dir = temp_root / video_id
    try:
        _info, video = _download_video_with_fallback(f"https://www.youtube.com/watch?v={video_id}", source_dir)
    except Exception as exc:
        _cleanup_path(temp_root)
        raise HTTPException(502, str(exc))

    out = temp_root / f"{video.stem[:180]}.mp3"
    cmd = [
        "ffmpeg", "-y", "-i", str(video),
        "-vn", "-codec:a", "libmp3lame", "-q:a", "2", str(out)
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError:
        _cleanup_path(temp_root)
        raise HTTPException(500, "FFmpeg is not installed or not in PATH.")
    except subprocess.CalledProcessError as exc:
        _cleanup_path(temp_root)
        raise HTTPException(500, exc.stderr[-2000:])
    if not out.exists():
        _cleanup_path(temp_root)
        raise HTTPException(500, "오디오 파일 생성에 실패했습니다.")
    return _file_download_response(out, temp_root)


@app.post("/api/audio-url")
def audio_from_url(req: UrlRequest):
    temp_root = _mk_temp_dir("vs_audio_url_")
    source_dir = temp_root / "source"
    try:
        _info, video = _download_video_with_fallback(req.url, source_dir)
    except Exception as exc:
        _cleanup_path(temp_root)
        raise HTTPException(502, str(exc))

    out = temp_root / f"{video.stem[:180]}.mp3"
    cmd = [
        "ffmpeg", "-y", "-i", str(video),
        "-vn", "-codec:a", "libmp3lame", "-q:a", "2", str(out)
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError:
        _cleanup_path(temp_root)
        raise HTTPException(500, "FFmpeg is not installed or not in PATH.")
    except subprocess.CalledProcessError as exc:
        _cleanup_path(temp_root)
        raise HTTPException(500, exc.stderr[-2000:])

    if not out.exists():
        _cleanup_path(temp_root)
        raise HTTPException(500, "오디오 파일 생성에 실패했습니다.")
    return _file_download_response(out, temp_root)

@app.get("/api/history")
def history():
    return {"items": []}

@app.get("/api/files/{kind}/{filename}")
def file(kind: str, filename: str):
    roots = {
        "audio": AUDIO,
        "video": VIDEOS,
        "subtitle": SUBTITLES,
        "thumbnail": THUMBNAILS
    }
    root = roots.get(kind)
    if not root:
        raise HTTPException(400, "Invalid file type.")
    # Use literal filename matching because glob metacharacters (e.g. [] in titles)
    # break Path.rglob(pattern) lookups.
    match = next((p for p in root.rglob("*") if p.is_file() and p.name == filename), None)
    if not match:
        raise HTTPException(404, "File not found.")
    encoded = quote(filename, safe='')
    return FileResponse(
        match,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded}"}
    )


@app.get("/api/info")
def get_info(url: str = Query(...)):
    """Fetch video metadata from any supported URL (Instagram, Xiaohongshu, etc.)."""
    opts = _yt_dlp_opts(
        skip_download=True,
        ignoreerrors=False,
    )
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        raise HTTPException(502, str(exc))
    if not info:
        raise HTTPException(404, "정보를 찾을 수 없습니다.")
    result = normalize(info)
    result["platform"] = detect_platform(url)
    return result


@app.post("/api/download-url")
def download_from_url(req: UrlRequest):
    """Download video from any URL (Instagram, Xiaohongshu, etc.)."""
    temp_root = _mk_temp_dir("vs_download_url_")
    out_dir = temp_root / "video"
    try:
        _info, path = _download_video_with_fallback(req.url, out_dir)
    except Exception as exc:
        _cleanup_path(temp_root)
        raise HTTPException(502, str(exc))

    if not path.exists():
        _cleanup_path(temp_root)
        raise HTTPException(500, "다운로드 파일을 찾을 수 없습니다.")
    return _file_download_response(path, temp_root)


@app.post("/api/subtitles-url")
def subtitles_from_url(req: UrlRequest):
    """Download subtitles/captions from any supported URL."""
    try:
        with yt_dlp.YoutubeDL(_yt_dlp_opts(skip_download=True)) as ydl:
            info_pre = ydl.extract_info(req.url, download=False)
    except Exception as exc:
        raise HTTPException(502, str(exc))

    video_id = (info_pre or {}).get("id") or "unknown"
    temp_root = _mk_temp_dir("vs_subtitles_url_")
    out_dir = temp_root / video_id
    out_dir.mkdir(parents=True, exist_ok=True)

    opts = _yt_dlp_opts(
        skip_download=True,
        writesubtitles=True,
        writeautomaticsub=True,
        subtitleslangs=["ko", "en", "zh", "zh-Hans", "zh-Hant", "zh-TW"],
        subtitlesformat="srt/vtt/best",
        outtmpl=str(out_dir / "%(title).180s.%(ext)s"),
    )
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(req.url, download=True)
    except Exception as exc:
        _cleanup_path(temp_root)
        raise HTTPException(502, str(exc))

    files = sorted(out_dir.glob("*"))
    if not files:
        _cleanup_path(temp_root)
        raise HTTPException(404, "이 영상에는 자막/캡션이 없습니다.")

    if len(files) == 1:
        return _file_download_response(files[0], temp_root)

    zip_path = temp_root / f"{video_id}_subtitles.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            if f.is_file():
                zf.write(f, arcname=f.name)
    return _file_download_response(zip_path, temp_root)
