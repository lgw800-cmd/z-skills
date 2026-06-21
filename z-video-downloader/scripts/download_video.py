#!/usr/bin/env python3
"""Download videos from direct URLs and yt-dlp supported platforms.

The direct download and platform-download behavior is adapted from
.agent/skills/1-web-pack/scripts/collect_web_pack.py.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
import warnings
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlparse

warnings.filterwarnings(
    "ignore",
    message=r"urllib3 .* doesn't match a supported version!",
)

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_OUT_ROOT = PROJECT_ROOT / "Video" / "Downloads"
YTDLP_CANDIDATES = [
    Path("/Users/zz/miniconda3/bin/yt-dlp"),
    Path(shutil.which("yt-dlp") or ""),
]
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0 Safari/537.36"
)

VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".m4v", ".mkv", ".flv", ".ogv"}
STREAM_EXTENSIONS = {".m3u8", ".mpd"}

PLATFORM_HOST_HINTS = {
    "youtube.com": "YouTube",
    "youtu.be": "YouTube",
    "bilibili.com": "Bilibili",
    "b23.tv": "Bilibili",
    "vimeo.com": "Vimeo",
    "x.com": "X",
    "twitter.com": "Twitter",
    "tiktok.com": "TikTok",
    "douyin.com": "Douyin",
    "instagram.com": "Instagram",
    "facebook.com": "Facebook",
}

INVIDIOUS_INSTANCES = (
    "https://inv.thepixora.com",
)
INVIDIOUS_FALLBACK_ITAG = "18"  # 360p progressive MP4 with audio.


def slugify(text: str, fallback: str = "video-download", max_len: int = 60) -> str:
    value = re.sub(r"[^\w.-]+", "-", text, flags=re.U).strip("-._")
    value = re.sub(r"-{2,}", "-", value)
    if not value:
        value = fallback
    return value[:max_len].strip("-._") or fallback


def safe_filename(text: str, fallback: str = "video") -> str:
    value = unquote(text or "").strip()
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value or fallback


def make_run_dir(out_root: Path, title: str) -> Path:
    date = dt.date.today().isoformat()
    name = slugify(title or "video-download")
    candidate = out_root / f"{date}-{name}"
    if not candidate.exists():
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate
    for index in range(2, 1000):
        with_suffix = out_root / f"{date}-{name}-{index:02d}"
        if not with_suffix.exists():
            with_suffix.mkdir(parents=True, exist_ok=True)
            return with_suffix
    raise RuntimeError(f"Cannot create unique output directory under {out_root}")


def find_ytdlp() -> Path | None:
    for candidate in YTDLP_CANDIDATES:
        if candidate and candidate.exists() and os.access(candidate, os.X_OK):
            return candidate
    return None


def classify_url(url: str) -> str:
    parsed = urlparse(url)
    suffix = Path(parsed.path).suffix.lower()
    if suffix in VIDEO_EXTENSIONS:
        return "direct"
    if suffix in STREAM_EXTENSIONS:
        return "stream"
    return "platform"


def platform_name(url: str) -> str:
    host = urlparse(url).netloc.lower()
    for hint, name in PLATFORM_HOST_HINTS.items():
        if host == hint or host.endswith("." + hint):
            return name
    if classify_url(url) == "direct":
        return "Direct"
    if classify_url(url) == "stream":
        return "Stream"
    return "yt-dlp"


def youtube_video_id(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.strip("/")
    candidate = ""
    if host == "youtu.be" or host.endswith(".youtu.be"):
        candidate = path.split("/", 1)[0]
    elif host == "youtube.com" or host.endswith(".youtube.com"):
        if parsed.path == "/watch":
            candidate = (parse_qs(parsed.query).get("v") or [""])[0]
        elif path.startswith(("shorts/", "embed/", "live/")):
            candidate = path.split("/", 1)[1].split("/", 1)[0]
    if re.fullmatch(r"[\w-]{11}", candidate or ""):
        return candidate
    return ""


def is_youtube_url(url: str) -> bool:
    return bool(youtube_video_id(url))


def fetch_invidious_title(
    session: requests.Session,
    instance: str,
    video_id: str,
    *,
    timeout: int,
) -> str:
    try:
        response = session.get(
            f"{instance.rstrip('/')}/api/v1/videos/{video_id}",
            headers={"User-Agent": DEFAULT_USER_AGENT},
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
        return safe_filename(data.get("title") or "")
    except Exception:  # noqa: BLE001
        return ""


def invidious_redirect_location(
    session: requests.Session,
    url: str,
    *,
    timeout: int,
) -> str:
    response = session.get(
        url,
        headers={"User-Agent": DEFAULT_USER_AGENT, "Range": "bytes=0-2047"},
        timeout=timeout,
        allow_redirects=False,
    )
    if response.is_redirect:
        location = response.headers.get("Location")
        if not location:
            raise RuntimeError("invidious-redirect-without-location")
        return urljoin(url, location)
    response.raise_for_status()
    return url


def resolve_invidious_proxy_url(
    session: requests.Session,
    instance: str,
    video_id: str,
    *,
    itag: str = INVIDIOUS_FALLBACK_ITAG,
    timeout: int,
) -> str:
    base = instance.rstrip("/")
    latest = f"{base}/latest_version?id={video_id}&itag={itag}&local=true"
    first = invidious_redirect_location(session, latest, timeout=timeout)
    return invidious_redirect_location(session, first, timeout=timeout)


def parse_content_range_total(value: str) -> int:
    match = re.search(r"/(\d+)$", value or "")
    if not match:
        return 0
    return int(match.group(1))


def fetch_proxy_range(
    session: requests.Session,
    proxy_url: str,
    start: int,
    end: int,
    *,
    timeout: int,
) -> tuple[requests.Response, bytes]:
    response = session.get(
        proxy_url,
        headers={"User-Agent": DEFAULT_USER_AGENT, "Range": f"bytes={start}-{end}"},
        timeout=timeout,
    )
    data = response.content
    return response, data


def download_youtube_invidious_fallback(
    session: requests.Session,
    url: str,
    out_dir: Path,
    *,
    max_video_mb: float,
    timeout: int,
    instances: tuple[str, ...] | list[str] | None = None,
    chunk_size: int = 1024 * 1024,
) -> dict[str, Any]:
    record = base_record(url, "platform-fallback")
    record["platform"] = "YouTube / Invidious proxy"
    video_id = youtube_video_id(url)
    if not video_id:
        record["note"] = "youtube-video-id-not-found"
        return record

    last_error = ""
    for instance in instances or INVIDIOUS_INSTANCES:
        instance = instance.rstrip("/")
        try:
            title = fetch_invidious_title(session, instance, video_id, timeout=min(timeout, 30))
            filename = f"{title or 'youtube-video'} [{video_id}]-360p.mp4"
            target = unique_path(out_dir / safe_filename(filename))
            proxy_url = resolve_invidious_proxy_url(
                session,
                instance,
                video_id,
                timeout=min(timeout, 45),
            )

            response, first_bytes = fetch_proxy_range(session, proxy_url, 0, 2047, timeout=min(timeout, 45))
            if response.status_code != 206 or not first_bytes.startswith(b"\x00\x00\x00"):
                raise RuntimeError(f"invidious-probe-failed:{response.status_code}")
            total = parse_content_range_total(response.headers.get("Content-Range", ""))
            limit = int(max_video_mb * 1024 * 1024)
            if total and total > limit:
                record["note"] = f"invidious-video-larger-than-{max_video_mb:g}MB"
                return record

            with open(target, "wb") as handle:
                handle.write(first_bytes)
            written = len(first_bytes)
            start = written
            consecutive_failures = 0
            while start < total:
                end = min(start + chunk_size - 1, total - 1)
                chunk = b""
                for attempt in range(1, 8):
                    try:
                        response, chunk = fetch_proxy_range(
                            session,
                            proxy_url,
                            start,
                            end,
                            timeout=min(timeout, 45),
                        )
                        expected = end - start + 1
                        if response.status_code != 206:
                            raise RuntimeError(f"status {response.status_code}")
                        if len(chunk) != expected:
                            raise RuntimeError(f"got {len(chunk)} expected {expected}")
                        consecutive_failures = 0
                        break
                    except Exception as exc:  # noqa: BLE001
                        consecutive_failures += 1
                        last_error = str(exc)[:160]
                        if consecutive_failures >= 6:
                            proxy_url = resolve_invidious_proxy_url(
                                session,
                                instance,
                                video_id,
                                timeout=min(timeout, 45),
                            )
                            consecutive_failures = 0
                        if attempt == 7:
                            raise
                        time.sleep(min(10, attempt * 1.5))
                with open(target, "ab") as handle:
                    handle.write(chunk)
                written += len(chunk)
                if written > limit:
                    target.unlink(missing_ok=True)
                    record["note"] = f"invidious-video-larger-than-{max_video_mb:g}MB"
                    return record
                start = end + 1

            record["status"] = "ok"
            record["files"] = [str(target)]
            record["bytes"] = target.stat().st_size
            record["note"] = f"yt-dlp failed; downloaded via {instance} local=true itag={INVIDIOUS_FALLBACK_ITAG}"
            return record
        except Exception as exc:  # noqa: BLE001
            last_error = f"{instance}: {str(exc)[:220]}"
            continue

    record["note"] = f"invidious-fallback-failed: {last_error}"
    return record


def filename_from_headers(headers: requests.structures.CaseInsensitiveDict[str]) -> str:
    disposition = headers.get("Content-Disposition", "")
    match = re.search(r"filename\*=UTF-8''([^;]+)", disposition, re.I)
    if match:
        return safe_filename(match.group(1))
    match = re.search(r'filename="?([^";]+)"?', disposition, re.I)
    if match:
        return safe_filename(match.group(1))
    return ""


def size_text(size: int | None) -> str:
    if size is None:
        return ""
    units = ["B", "KB", "MB", "GB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def base_record(url: str, kind: str) -> dict[str, Any]:
    return {
        "url": url,
        "kind": kind,
        "platform": platform_name(url),
        "status": "failed",
        "files": [],
        "bytes": 0,
        "note": "",
    }


def download_direct_video(
    session: requests.Session,
    url: str,
    out_dir: Path,
    *,
    max_video_mb: float,
    referer: str = "",
    timeout: int = 30,
) -> dict[str, Any]:
    record = base_record(url, "direct")
    try:
        headers = {"User-Agent": DEFAULT_USER_AGENT}
        if referer:
            headers["Referer"] = referer
        response = session.get(url, timeout=timeout, stream=True, headers=headers)
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").lower()
        if "text/html" in content_type or "application/json" in content_type:
            record["note"] = f"unexpected-content-type:{content_type.split(';')[0]}"
            return record

        limit = int(max_video_mb * 1024 * 1024)
        length = response.headers.get("Content-Length")
        if length:
            try:
                if int(length) > limit:
                    record["note"] = f"video-larger-than-{max_video_mb:g}MB"
                    return record
            except ValueError:
                pass

        parsed = urlparse(url)
        ext = Path(parsed.path).suffix.lower()
        if ext not in VIDEO_EXTENSIONS:
            ext = ".mp4"
        header_name = filename_from_headers(response.headers)
        fallback_stem = safe_filename(Path(parsed.path).stem or "video")
        filename = header_name or f"{fallback_stem}{ext}"
        if Path(filename).suffix.lower() not in VIDEO_EXTENSIONS:
            filename = f"{Path(filename).stem}{ext}"
        target = unique_path(out_dir / filename)

        size = 0
        with open(target, "wb") as handle:
            for chunk in response.iter_content(1024 * 256):
                if not chunk:
                    continue
                size += len(chunk)
                if size > limit:
                    handle.close()
                    target.unlink(missing_ok=True)
                    record["note"] = f"video-larger-than-{max_video_mb:g}MB"
                    return record
                handle.write(chunk)

        record["status"] = "ok"
        record["files"] = [str(target)]
        record["bytes"] = size
        return record
    except Exception as exc:  # noqa: BLE001
        record["note"] = str(exc)[:300]
        return record


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(2, 1000):
        candidate = path.with_name(f"{stem}-{index:02d}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Cannot create unique filename for {path}")


def format_selector(quality: str) -> str:
    if quality == "best":
        return "bv*+ba/b"
    try:
        height = int(quality)
    except ValueError as exc:
        raise ValueError("--quality must be an integer height like 1080 or 'best'") from exc
    return f"bv*[height<={height}]+ba/b[height<={height}]/b"


def build_ytdlp_cmd(
    url: str,
    out_dir: Path,
    *,
    ytdlp: Path,
    quality: str,
    max_video_mb: float,
    browser_cookies: str = "",
    playlist: bool = False,
    write_info_json: bool = True,
    trim_filenames: int = 150,
) -> list[str]:
    template = str(out_dir / "%(title).150B [%(id)s].%(ext)s")
    cmd = [
        str(ytdlp),
        "--no-progress",
        "--print",
        "after_move:filepath",
        "--merge-output-format",
        "mp4",
        "--embed-metadata",
        "--trim-filenames",
        str(trim_filenames),
        "--max-filesize",
        f"{int(max_video_mb)}M",
        "-f",
        format_selector(quality),
        "-o",
        template,
        url,
    ]
    if write_info_json:
        cmd.insert(1, "--write-info-json")
    if playlist:
        cmd.insert(1, "--yes-playlist")
    else:
        cmd.insert(1, "--no-playlist")
    if browser_cookies:
        cmd[1:1] = ["--cookies-from-browser", browser_cookies]
    return cmd


def ytdlp_env() -> dict[str, str]:
    env = os.environ.copy()
    path_parts = [
        "/Users/zz/miniconda3/bin",
        "/opt/homebrew/bin",
        env.get("PATH", ""),
    ]
    env["PATH"] = os.pathsep.join(part for part in path_parts if part)
    return env


def likely_cookie_fix(note: str) -> bool:
    lowered = note.lower()
    markers = [
        "sign in",
        "login",
        "cookies",
        "captcha",
        "bot",
        "precondition",
        "http error 412",
        "http error 403",
        "forbidden",
        "confirm",
    ]
    return any(marker in lowered for marker in markers)


def video_files_from_paths(paths: list[str]) -> list[str]:
    result: list[str] = []
    for raw in paths:
        path = Path(raw.strip())
        if path.exists() and path.suffix.lower() in VIDEO_EXTENSIONS:
            result.append(str(path))
    return result


def download_with_ytdlp(
    url: str,
    out_dir: Path,
    *,
    ytdlp: Path,
    quality: str,
    max_video_mb: float,
    browser_cookies: str = "",
    playlist: bool = False,
    timeout: int = 3600,
) -> dict[str, Any]:
    kind = "stream" if classify_url(url) == "stream" else "platform"
    record = base_record(url, kind)
    cmd = build_ytdlp_cmd(
        url,
        out_dir,
        ytdlp=ytdlp,
        quality=quality,
        max_video_mb=max_video_mb,
        browser_cookies=browser_cookies,
        playlist=playlist,
    )
    try:
        before = {str(path) for path in out_dir.iterdir()}
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=ytdlp_env(),
        )
        printed_files = video_files_from_paths((proc.stdout or "").splitlines())
        after_paths = [
            str(path)
            for path in out_dir.iterdir()
            if str(path) not in before and path.suffix.lower() in VIDEO_EXTENSIONS
        ]
        files = sorted(set(printed_files + after_paths))
        if proc.returncode == 0 and files:
            record["status"] = "ok"
            record["files"] = files
            record["bytes"] = sum(Path(path).stat().st_size for path in files)
            return record

        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        record["note"] = (tail[-1] if tail else f"yt-dlp-exit-{proc.returncode}")[:300]
        if likely_cookie_fix(record["note"]) and not browser_cookies:
            record["note"] += " | 可用 --browser-cookies chrome 重试"
        return record
    except subprocess.TimeoutExpired:
        record["note"] = f"yt-dlp-timeout-{timeout}s"
        return record
    except Exception as exc:  # noqa: BLE001
        record["note"] = str(exc)[:300]
        return record


def download_one(
    session: requests.Session,
    url: str,
    out_dir: Path,
    *,
    ytdlp: Path | None,
    quality: str,
    max_video_mb: float,
    browser_cookies: str,
    playlist: bool,
    prefer_ytdlp: bool,
    invidious_fallback: bool,
    timeout: int,
) -> dict[str, Any]:
    kind = classify_url(url)
    if kind == "direct" and not prefer_ytdlp:
        record = download_direct_video(
            session,
            url,
            out_dir,
            max_video_mb=max_video_mb,
            referer=url,
            timeout=min(timeout, 120),
        )
        if record["status"] == "ok" or ytdlp is None:
            return record
    if ytdlp is None:
        record = base_record(url, "platform")
        record["note"] = "yt-dlp-not-found"
        return record
    record = download_with_ytdlp(
        url,
        out_dir,
        ytdlp=ytdlp,
        quality=quality,
        max_video_mb=max_video_mb,
        browser_cookies=browser_cookies,
        playlist=playlist,
        timeout=timeout,
    )
    if (
        record["status"] != "ok"
        and invidious_fallback
        and is_youtube_url(url)
        and likely_cookie_fix(record.get("note", ""))
    ):
        fallback_record = download_youtube_invidious_fallback(
            session,
            url,
            out_dir,
            max_video_mb=max_video_mb,
            timeout=timeout,
        )
        if fallback_record["status"] == "ok":
            fallback_record["note"] = (
                f"yt-dlp note: {record.get('note', '')} | {fallback_record.get('note', '')}"
            )[:300]
            return fallback_record
        record["note"] = (
            f"{record.get('note', '')} | {fallback_record.get('note', '')}"
        )[:300]
    return record


def write_reports(out_dir: Path, urls: list[str], records: list[dict[str, Any]]) -> None:
    payload = {
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        "out_dir": str(out_dir),
        "urls": urls,
        "records": records,
    }
    (out_dir / "download-report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    ok_count = sum(1 for record in records if record["status"] == "ok")
    lines = [
        "# 视频下载报告",
        "",
        f"- 输出目录：`{out_dir}`",
        f"- 链接数量：{len(records)}",
        f"- 成功：{ok_count}",
        f"- 失败：{len(records) - ok_count}",
        "",
        "| 状态 | 类型 | 平台 | 文件 | 大小 | 链接 | 备注 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for record in records:
        files = "<br>".join(f"`{Path(path).name}`" for path in record.get("files", []))
        lines.append(
            "| "
            + " | ".join(
                [
                    escape_table(record.get("status", "")),
                    escape_table(record.get("kind", "")),
                    escape_table(record.get("platform", "")),
                    files or "",
                    escape_table(size_text(record.get("bytes") or None)),
                    f"<{record.get('url', '')}>",
                    escape_table(record.get("note", "")),
                ]
            )
            + " |"
        )
    (out_dir / "download-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def escape_table(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download videos from direct URLs and yt-dlp platforms.")
    parser.add_argument("urls", nargs="+", help="Video URLs")
    parser.add_argument("--out-root", default=str(DEFAULT_OUT_ROOT), help="Output root directory")
    parser.add_argument("--title", default="", help="Run title used in output folder name")
    parser.add_argument("--quality", default="1080", help="Max video height, e.g. 720/1080, or best")
    parser.add_argument("--max-video-mb", type=float, default=2000.0, help="Per video size limit")
    parser.add_argument("--browser-cookies", default="", help="Read cookies from browser: chrome/safari/edge/firefox")
    parser.add_argument("--playlist", action="store_true", help="Allow playlist downloads")
    parser.add_argument("--prefer-ytdlp", action="store_true", help="Use yt-dlp even for direct video URLs")
    parser.add_argument("--no-invidious-fallback", action="store_true", help="Disable YouTube Invidious proxy fallback")
    parser.add_argument("--timeout", type=int, default=3600, help="yt-dlp timeout in seconds")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    out_root = Path(args.out_root).expanduser()
    title = args.title or platform_name(args.urls[0]).lower()
    out_dir = make_run_dir(out_root, title)

    ytdlp = find_ytdlp()
    session = requests.Session()
    session.headers.update({"User-Agent": DEFAULT_USER_AGENT})

    seen: set[str] = set()
    urls: list[str] = []
    for url in args.urls:
        normalized = url.strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            urls.append(normalized)

    records: list[dict[str, Any]] = []
    for index, url in enumerate(urls, start=1):
        print(f"[{index}/{len(urls)}] {url}", flush=True)
        record = download_one(
            session,
            url,
            out_dir,
            ytdlp=ytdlp,
            quality=args.quality,
            max_video_mb=args.max_video_mb,
            browser_cookies=args.browser_cookies,
            playlist=args.playlist,
            prefer_ytdlp=args.prefer_ytdlp,
            invidious_fallback=not args.no_invidious_fallback,
            timeout=args.timeout,
        )
        records.append(record)
        status = record["status"]
        note = f" ({record['note']})" if record.get("note") else ""
        print(f"  -> {status}{note}", flush=True)

    write_reports(out_dir, urls, records)
    ok_count = sum(1 for record in records if record["status"] == "ok")
    print(out_dir)
    print(f"videos_ok={ok_count}")
    print(f"videos_failed={len(records) - ok_count}")
    if ok_count == len(records):
        return 0
    if ok_count == 0:
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
