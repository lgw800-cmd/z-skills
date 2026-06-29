#!/usr/bin/env python3
"""1-web-pack 采集器：在 1-web-research-pack 基础上增强。

相对基础版的增强点：
- 图片：srcset 选最大档、懒加载属性全覆盖、picture>source、Referer 防盗链、
  magic-bytes 纠正扩展名、内容 hash 去重、跳过 tracking 像素与装饰图标
- 视频：只识别 <video>/<source>/正文直链 mp4、平台页与 m3u8，写入媒体清单；
  下载交给 z-video-downloader
- GitHub：repo 链接优先 GitHub API 取 README，blob 链接转 raw，真正兑现 SKILL 承诺
- 兜底：直抓失败或正文弱时使用 r.jina.ai（继承原逻辑）
- 新增 04-media-inventory.md 媒体链接清单

依赖 readability-lxml，请用 miniconda python 运行：
  /Users/zz/miniconda3/bin/python3 collect_web_pack.py ...
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import re
import sys
import time
from collections import deque
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

try:
    import readability  # noqa: F401  仅做环境预检
except ImportError:
    sys.stderr.write(
        "missing readability-lxml; run with /Users/zz/miniconda3/bin/python3\n"
    )
    raise

from bs4 import BeautifulSoup, Tag

THIS_SCRIPT = Path(__file__).resolve()


def find_base_script() -> Path:
    candidates = [
        THIS_SCRIPT.with_name("collect_web_research_pack.py"),
    ]
    for parent in THIS_SCRIPT.parents:
        candidates.append(
            parent / ".agent/skills/1-web-research-pack/scripts/collect_web_research_pack.py"
        )
        candidates.append(
            parent / "1-web-research-pack/scripts/collect_web_research_pack.py"
        )

    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.exists():
            return candidate
    searched = "\n".join(f"- {candidate}" for candidate in candidates)
    raise RuntimeError(f"Cannot find base collector. Searched:\n{searched}")


BASE_SCRIPT = find_base_script()

spec = importlib.util.spec_from_file_location("web_research_pack_base", BASE_SCRIPT)
if spec is None or spec.loader is None:
    raise RuntimeError(f"Cannot load base collector: {BASE_SCRIPT}")
base = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = base
spec.loader.exec_module(base)


# ---------------------------------------------------------------- 全局配置

FORCE_ROOTS: set[str] = set()
ORIGINAL_SHOULD_SKIP = base.should_skip_url
MAX_IMAGE_MB = 20.0
IMAGE_HASHES: dict[str, str] = {}     # sha256 -> local_path（跨页面去重）
VIDEO_LINKS_SEEN: set[str] = set()
CURRENT_PAGE_VIDEOS: list[str] = []   # 由 patched _extract_article_soup 填充

VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".m4v", ".mkv", ".flv", ".ogv"}

PLATFORM_VIDEO_RE = re.compile(
    r"("
    r"(?:www\.|m\.)?youtube\.com/(?:watch\?|embed/|shorts/|v/)"
    r"|youtu\.be/"
    r"|(?:www\.)?bilibili\.com/video/"
    r"|player\.bilibili\.com/player\.html"
    r"|(?:www\.)?vimeo\.com/\d+"
    r"|player\.vimeo\.com/video/"
    r"|(?:twitter|x)\.com/[^/]+/status/\d+"
    r"|(?:www\.)?tiktok\.com/@[^/]+/video/"
    r"|\.m3u8(?:\?|$)"
    r")",
    re.I,
)

TRACKING_IMG_RE = re.compile(
    r"(pixel|spacer|blank\.|1x1|tracking|beacon|impression|"
    r"shields\.io|badge\.svg|badgen\.net|herokuapp\.com/badge|favicon)",
    re.I,
)

WEAK_MARKERS = [
    "enable javascript",
    "javascript is not available",
    "this browser is no longer supported",
    "something went wrong",
    "please enable cookies",
    "access denied",
    "checking your browser",
    "just a moment",
    "performing security verification",
    "requiring captcha",
    "verify you are human",
    "target url returned error 403",
    "log in to",
    "sign up now",
]

MAGIC_BYTES = [
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF8", ".gif"),
]


def normalize(url: str) -> str:
    return base.normalize_url(url)


# ---------------------------------------------------------------- URL 过滤

def patched_should_skip_url(url: str, root_hosts: set[str], same_domain_only: bool) -> tuple[bool, str]:
    normalized = normalize(url)
    if normalized in FORCE_ROOTS:
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"}:
            return True, "non-http"
        path = parsed.path.lower()
        ext = Path(path).suffix
        if ext in base.SKIP_EXTENSIONS or ext in base.IMAGE_EXTENSIONS:
            return True, "asset-link"
        return False, ""
    return ORIGINAL_SHOULD_SKIP(url, root_hosts, same_domain_only)


base.should_skip_url = patched_should_skip_url


# ---------------------------------------------------------------- 图片增强

def _srcset_largest(srcset: str) -> str:
    best, best_w = "", -1
    for part in str(srcset).split(","):
        bits = part.strip().split()
        if not bits:
            continue
        width = 0
        if len(bits) > 1 and bits[1].endswith("w"):
            try:
                width = int(float(bits[1][:-1]))
            except ValueError:
                width = 0
        if width > best_w:
            best, best_w = bits[0], width
    return best


def _looks_like_placeholder(value: str) -> bool:
    lowered = value.lower()
    return (
        lowered.startswith("data:")
        or "placeholder" in lowered
        or "/loading." in lowered
        or lowered.endswith("blank.gif")
    )


def patched_choose_img_url(tag: Tag, base_url: str) -> str:
    candidates: list[str] = []
    srcset = tag.get("srcset") or tag.get("data-srcset")
    if srcset:
        largest = _srcset_largest(str(srcset))
        if largest:
            candidates.append(largest)
    picture = tag.find_parent("picture") if isinstance(tag, Tag) else None
    if picture is not None:
        for source in picture.find_all("source"):
            source_set = source.get("srcset") or source.get("data-srcset")
            if source_set:
                largest = _srcset_largest(str(source_set))
                if largest:
                    candidates.append(largest)
                break
    for attr in ("data-src", "data-original", "data-lazy-src", "data-actualsrc",
                 "data-echo", "data-url", "src"):
        value = tag.get(attr)
        if value:
            candidates.append(str(value))
    for candidate in candidates:
        candidate = candidate.strip()
        if not candidate or _looks_like_placeholder(candidate):
            continue
        return urljoin(base_url, candidate)
    return ""


base._choose_img_url = patched_choose_img_url


def _is_tracking_or_decorative(tag_or_none, url: str) -> bool:
    if TRACKING_IMG_RE.search(url):
        return True
    if isinstance(tag_or_none, Tag):
        for attr in ("width", "height"):
            value = str(tag_or_none.get(attr) or "").strip().rstrip("px")
            if value.isdigit() and int(value) <= 3:
                return True
    return False


def _sniff_ext(content: bytes, fallback: str) -> str:
    head = content[:64]
    for magic, ext in MAGIC_BYTES:
        if head.startswith(magic):
            return ext
    if head.startswith(b"RIFF") and b"WEBP" in head[:16]:
        return ".webp"
    if head[4:12] in (b"ftypavif", b"ftypavis"):
        return ".avif"
    if head.lstrip().startswith((b"<svg", b"<?xml")):
        return ".svg"
    return fallback


def patched_download_image(
    source_url: str,
    page_url: str,
    session: requests.Session,
    assets_dir: Path,
    global_image_index: list[int],
) -> dict:
    source_url = urljoin(page_url, source_url)
    if not source_url or source_url.startswith("data:"):
        return {"source_url": source_url, "status": "skipped", "error": "inline-or-empty-image"}
    if _is_tracking_or_decorative(None, source_url):
        return {"source_url": source_url, "status": "skipped", "error": "tracking-or-decorative"}
    try:
        response = session.get(
            source_url,
            timeout=20,
            headers={
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                "Referer": page_url,
            },
        )
        response.raise_for_status()
        content = response.content
        limit = int(MAX_IMAGE_MB * 1024 * 1024)
        if len(content) > limit:
            return {"source_url": source_url, "status": "failed",
                    "error": f"image-larger-than-{MAX_IMAGE_MB}MB"}
        if len(content) < 128:
            return {"source_url": source_url, "status": "skipped", "error": "too-small"}
        content_type = (response.headers.get("Content-Type") or "").lower()
        path_ext = Path(urlparse(source_url).path).suffix.lower()
        sniffed = _sniff_ext(content, "")
        if "image" not in content_type and path_ext not in base.IMAGE_EXTENSIONS and not sniffed:
            return {"source_url": source_url, "status": "failed",
                    "error": f"not-image-content-type:{content_type or 'unknown'}"}
        digest = hashlib.sha256(content).hexdigest()
        if digest in IMAGE_HASHES:
            return {"source_url": source_url, "local_path": IMAGE_HASHES[digest],
                    "status": "ok", "bytes": len(content), "note": "dedup"}
        global_image_index[0] += 1
        filename = base._asset_name(global_image_index[0], source_url, response)
        ext = sniffed or path_ext
        if ext and not filename.lower().endswith(ext):
            filename = str(Path(filename).with_suffix(ext))
        local_path = assets_dir / filename
        local_path.write_bytes(content)
        IMAGE_HASHES[digest] = f"assets/{filename}"
        return {
            "source_url": source_url,
            "local_path": f"assets/{filename}",
            "status": "ok",
            "bytes": len(content),
            "content_type": content_type.split(";")[0],
        }
    except Exception as exc:
        return {"source_url": source_url, "status": "failed", "error": str(exc)[:200]}


base.download_image = patched_download_image


# ---------------------------------------------------------------- 视频链接发现

def collect_videos_from_html(html: str, final_url: str) -> list[str]:
    urls: list[str] = []
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        return urls
    for video in soup.find_all("video"):
        for attr in ("src", "data-src"):
            if video.get(attr):
                urls.append(urljoin(final_url, str(video[attr])))
        for source in video.find_all("source"):
            for attr in ("src", "data-src"):
                if source.get(attr):
                    urls.append(urljoin(final_url, str(source[attr])))
    for frame in soup.find_all(["iframe", "embed"]):
        src = frame.get("src") or frame.get("data-src") or ""
        if src and PLATFORM_VIDEO_RE.search(str(src)):
            urls.append(urljoin(final_url, str(src)))
    for anchor in soup.find_all("a", href=True):
        href = urljoin(final_url, str(anchor["href"]))
        if Path(urlparse(href).path).suffix.lower() in VIDEO_EXTENSIONS:
            urls.append(href)
    deduped: list[str] = []
    for url in urls:
        if url.startswith(("http://", "https://")) and url not in deduped:
            deduped.append(url)
    return deduped


ORIGINAL_EXTRACT_ARTICLE = base._extract_article_soup


def patched_extract_article_soup(html: str, final_url: str):
    CURRENT_PAGE_VIDEOS.clear()
    CURRENT_PAGE_VIDEOS.extend(collect_videos_from_html(html, final_url))
    return ORIGINAL_EXTRACT_ARTICLE(html, final_url)


base._extract_article_soup = patched_extract_article_soup


def classify_video_url(url: str) -> str:
    if Path(urlparse(url).path).suffix.lower() in VIDEO_EXTENSIONS:
        return "direct"
    if PLATFORM_VIDEO_RE.search(url):
        return "platform"
    return "video-link"


def build_video_link_records(video_urls: list[str]) -> list[dict]:
    records: list[dict] = []
    for url in video_urls:
        normalized = normalize(url)
        if normalized in VIDEO_LINKS_SEEN:
            continue
        VIDEO_LINKS_SEEN.add(normalized)
        records.append(
            {
                "url": normalized,
                "kind": classify_video_url(normalized),
                "status": "detected",
                "local_path": "",
                "note": "download with z-video-downloader",
            }
        )
    return records


# ---------------------------------------------------------------- GitHub 优先

GITHUB_REPO_RE = re.compile(r"^https?://github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")
GITHUB_BLOB_RE = re.compile(r"^https?://github\.com/([\w.-]+)/([\w.-]+)/blob/(.+)$")


def absolutize_markdown_images(markdown: str, raw_base: str) -> str:
    def _fix(match: re.Match) -> str:
        alt, target = match.group(1), match.group(2).strip()
        if target.startswith(("http://", "https://", "data:")):
            return match.group(0)
        return f"![{alt}]({urljoin(raw_base, target.lstrip('./'))})"

    return re.sub(r"!\[([^\]]*)\]\(([^)\s]+)\)", _fix, markdown)


def try_github_page(session: requests.Session, url: str, depth: int, out_dir: Path,
                    assets_dir: Path, index: int, root_hosts: set[str],
                    same_domain_only: bool, global_image_index: list[int]):
    """github repo/blob 链接优先 API 与 raw，成功返回 PageResult，失败返回 None。"""
    repo_match = GITHUB_REPO_RE.match(url)
    blob_match = GITHUB_BLOB_RE.match(url)
    if not repo_match and not blob_match:
        return None
    try:
        if repo_match:
            owner, repo = repo_match.group(1), repo_match.group(2)
            api = f"https://api.github.com/repos/{owner}/{repo}/readme"
            response = session.get(api, timeout=20,
                                   headers={"Accept": "application/vnd.github.raw+json"})
            response.raise_for_status()
            title = f"{owner}/{repo} README"
            markdown = response.text
            raw_base = f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/"
            final_url = api
        else:
            owner, repo, rest = blob_match.groups()
            raw = f"https://raw.githubusercontent.com/{owner}/{repo}/{rest}"
            response = session.get(raw, timeout=20)
            response.raise_for_status()
            name = Path(urlparse(raw).path).name
            title = f"{owner}/{repo} {name}"
            text = response.text
            if name.lower().endswith((".md", ".markdown", ".rst", ".txt")):
                markdown = text
            else:
                lang = Path(name).suffix.lstrip(".")
                markdown = f"```{lang}\n{text}\n```"
            raw_base = raw.rsplit("/", 1)[0] + "/"
            final_url = raw
        markdown = absolutize_markdown_images(markdown, raw_base)
        markdown, images = base.localize_markdown_images(
            markdown, final_url, session, assets_dir, global_image_index)
        links = base.extract_markdown_links(markdown, url, root_hosts, same_domain_only)
        role = base.page_role(depth)
        filename = base.page_filename(index, title, depth)
        note = "> Capture Method: github-api\n\n"
        base.write_page_markdown(out_dir / filename, title, url, final_url, role,
                                 note + markdown, links)
        result = base.PageResult(
            url=url, final_url=final_url, title=title, filename=filename,
            status="ok", depth=depth, role=role, links=links, images=images,
        )
        result.error = "github-api"
        return result
    except Exception:
        return None


# ---------------------------------------------------------------- Jina 兜底（继承原逻辑）

def jina_reader_url(url: str) -> str:
    return "https://r.jina.ai/http://" + url


def parse_jina_markdown(raw: str, fallback_url: str) -> tuple[str, str]:
    title = ""
    for line in raw.splitlines()[:20]:
        if line.startswith("Title:"):
            title = line.replace("Title:", "", 1).strip()
            break
    body = raw
    marker = "Markdown Content:"
    if marker in raw:
        body = raw.split(marker, 1)[1].strip()
    title = title or urlparse(fallback_url).path.strip("/") or fallback_url
    return title, clean_reader_markdown(body)


def clean_reader_markdown(markdown: str) -> str:
    lines = markdown.splitlines()
    cleaned: list[str] = []
    skip_until_heading = False
    drop_exact = {
        "Don’t miss what’s happening",
        "Don't miss what's happening",
        "People on X are the first to know.",
        "See new posts",
        "Sign up with Apple",
        "Create account",
        "Appearance settings",
        "Toggle navigation",
    }
    for raw_line in lines:
        line = raw_line.strip()
        lower = line.lower()
        if line in {"## New to X?", "New to X?"}:
            skip_until_heading = True
            continue
        if skip_until_heading:
            if line.startswith("#") and "new to x" not in lower:
                skip_until_heading = False
            else:
                continue
        if line in drop_exact:
            continue
        if re.search(r"\]\(https://x\.com/(login|i/flow/signup|tos|privacy)", line):
            continue
        if lower.startswith("by signing up, you agree"):
            continue
        cleaned.append(raw_line)
    body = "\n".join(cleaned)
    body = re.sub(r"\n{4,}", "\n\n\n", body).strip()
    return body


def markdown_quality_is_weak(path: Path) -> bool:
    if not path.exists():
        return True
    text = path.read_text(encoding="utf-8", errors="replace")
    return _text_is_weak(text)


def markdown_text_is_weak(title: str, markdown: str) -> bool:
    return _text_is_weak(f"{title}\n{markdown}")


def _text_is_weak(text: str) -> bool:
    lowered = text.lower()
    if any(marker in lowered for marker in WEAK_MARKERS):
        return True
    body = re.sub(r"https?://\S+", "", text)
    body = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", body)
    body = re.sub(r"\[[^\]]*\]\([^)]*\)", "", body)
    visible = re.sub(r"\s+", "", body)
    return len(visible) < 350


def process_with_jina(
    session: requests.Session,
    url: str,
    depth: int,
    out_dir: Path,
    assets_dir: Path,
    index: int,
    root_hosts: set[str],
    same_domain_only: bool,
    global_image_index: list[int],
    previous: object | None = None,
):
    fallback_target = url
    previous_final_url = getattr(previous, "final_url", "") if previous is not None else ""
    if previous_final_url and previous_final_url != url:
        previous_path = Path(urlparse(previous_final_url).path.lower())
        if previous_path.suffix == ".pdf":
            fallback_target = previous_final_url
    reader = jina_reader_url(fallback_target)
    role = base.page_role(depth)
    try:
        response = session.get(reader, timeout=40, headers={"Accept": "text/plain,*/*"})
        response.raise_for_status()
        title, markdown = parse_jina_markdown(response.text, fallback_target)
        if markdown_text_is_weak(title, markdown):
            raise RuntimeError("r.jina.ai returned weak or access-limited content")
        markdown, images = base.localize_markdown_images(
            markdown, fallback_target, session, assets_dir, global_image_index
        )
        links = base.extract_markdown_links(markdown, fallback_target, root_hosts, same_domain_only)
        filename = base.page_filename(index, title, depth)
        previous_filename = getattr(previous, "filename", "")
        previous_path = out_dir / previous_filename if previous_filename else None
        if (
            previous is not None
            and getattr(previous, "status", "") == "ok"
            and previous_path is not None
            and previous_path.exists()
            and not markdown_quality_is_weak(previous_path)
        ):
            filename = previous_filename
        fallback_note = (
            "> Capture Method: r.jina.ai fallback after direct extraction was unavailable or weak\n\n"
        )
        base.write_page_markdown(
            out_dir / filename,
            title,
            url,
            reader,
            role,
            fallback_note + markdown,
            links,
        )
        return base.PageResult(
            url=url,
            final_url=reader,
            title=title,
            filename=filename,
            status="ok",
            depth=depth,
            role=role,
            links=links,
            images=images,
            error="used-r.jina.ai-fallback",
        )
    except Exception as exc:
        if previous is not None and getattr(previous, "status", "") == "ok":
            previous.error = f"{getattr(previous, 'error', '')}; jina-failed: {exc}".strip("; ")
            return previous
        filename = getattr(previous, "filename", "") or base.page_filename(index, url, depth)
        return base.PageResult(
            url=url,
            final_url=url,
            title=url,
            filename=filename,
            status="failed",
            depth=depth,
            role=role,
            error=f"direct failed; r.jina.ai fallback failed: {exc}",
        )


# ---------------------------------------------------------------- 单页处理

def process_page(
    session: requests.Session,
    url: str,
    depth: int,
    out_dir: Path,
    assets_dir: Path,
    index: int,
    root_hosts: set[str],
    same_domain_only: bool,
    global_image_index: list[int],
    use_jina: bool,
):
    CURRENT_PAGE_VIDEOS.clear()

    github_result = try_github_page(session, url, depth, out_dir, assets_dir, index,
                                    root_hosts, same_domain_only, global_image_index)
    if github_result is not None:
        page_videos: list[str] = []
        for link in getattr(github_result, "links", []):
            link_url = link.get("url", "")
            if Path(urlparse(link_url).path).suffix.lower() in VIDEO_EXTENSIONS:
                page_videos.append(link_url)
            elif PLATFORM_VIDEO_RE.search(link_url):
                page_videos.append(link_url)
        github_result.videos = build_video_link_records(page_videos)
        return github_result

    direct = base.process_page(
        session, url, depth, out_dir, assets_dir, index,
        root_hosts, same_domain_only, global_image_index,
    )
    page_videos = list(CURRENT_PAGE_VIDEOS)
    # 入口本身就是平台视频页或视频直链时，纳入媒体清单候选
    if PLATFORM_VIDEO_RE.search(url) or \
            Path(urlparse(url).path).suffix.lower() in VIDEO_EXTENSIONS:
        page_videos.insert(0, url)

    result = direct
    if use_jina:
        direct_path = out_dir / getattr(direct, "filename", "")
        needs_fallback = direct.status != "ok" or markdown_quality_is_weak(direct_path)
        if needs_fallback:
            fallback = process_with_jina(
                session, url, depth, out_dir, assets_dir, index,
                root_hosts, same_domain_only, global_image_index, previous=direct,
            )
            if (
                getattr(fallback, "filename", "") != getattr(direct, "filename", "")
                and direct_path.exists()
                and direct_path.name.endswith(".md")
            ):
                direct_path.unlink()
            result = fallback

    # 正文链接里的直链视频与平台视频也纳入清单（含被 skip 的社交域名链接）
    for link in getattr(result, "links", []) or []:
        link_url = link.get("url", "")
        if Path(urlparse(link_url).path).suffix.lower() in VIDEO_EXTENSIONS:
            page_videos.append(link_url)
        elif PLATFORM_VIDEO_RE.search(link_url):
            page_videos.append(link_url)

    result.videos = build_video_link_records(list(dict.fromkeys(page_videos)))
    return result


# ---------------------------------------------------------------- 清单补充

def rewrite_generator_name(out_dir: Path) -> None:
    readme = out_dir / "README.md"
    if not readme.exists():
        return
    text = readme.read_text(encoding="utf-8")
    text = text.replace("`1-web-research-pack`", "`1-web-pack`")
    if "04-media-inventory.md" not in text:
        text = text.replace(
            "- `03-reading-map.md`",
            "- `03-reading-map.md`\n- `04-media-inventory.md`",
        )
    readme.write_text(text, encoding="utf-8")


def write_media_inventory(out_dir: Path, title: str, pages: list) -> None:
    lines = [
        f"# Media Inventory: {title}",
        "",
        "## Video Links",
        "",
        "Video download is delegated to `z-video-downloader`. This file only records detected links.",
        "",
        "| Status | Kind | Page | Download Skill | Source URL | Note |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    has_video = False
    for page in pages:
        for video in getattr(page, "videos", []) or []:
            has_video = True
            note = video.get("error") or video.get("bytes") or ""
            lines.append(
                "| " + " | ".join([
                    base._escape_table(video.get("status")),
                    base._escape_table(video.get("kind")),
                    base._escape_table(page.filename or page.url),
                    "`z-video-downloader`",
                    base._escape_table(video.get("url")),
                    base._escape_table(video.get("note") or note),
                ]) + " |"
            )
    if not has_video:
        lines.append("| none | none | none | none | none | no video links found |")
    out_dir.joinpath("04-media-inventory.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- 主流程

def main() -> int:
    global MAX_IMAGE_MB
    parser = argparse.ArgumentParser(
        description="Collect body pages, body links, images and video links into a local writing material pack."
    )
    parser.add_argument("urls", nargs="+", help="Entry URLs")
    parser.add_argument("--out-root", default="Clippings/Reading")
    parser.add_argument("--title", default="")
    parser.add_argument("--max-pages", type=int, default=80)
    parser.add_argument("--max-depth", type=int, default=1)
    parser.add_argument("--same-domain-only", action="store_true")
    parser.add_argument("--delay", type=float, default=0.2)
    parser.add_argument("--no-jina", action="store_true", help="Disable r.jina.ai fallback")
    parser.add_argument("--max-image-mb", type=float, default=20.0)
    parser.add_argument("--videos", choices=["off", "direct", "all"], default="",
                        help=argparse.SUPPRESS)
    parser.add_argument("--max-video-mb", type=float, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--browser-cookies", default="", help=argparse.SUPPRESS)
    args = parser.parse_args()

    MAX_IMAGE_MB = args.max_image_mb
    if args.videos or args.max_video_mb is not None or args.browser_cookies:
        print(
            "warning: video downloading moved to z-video-downloader; "
            "z-web-pack records video links only",
            file=sys.stderr,
        )

    roots = [normalize(url) for url in args.urls]
    FORCE_ROOTS.clear()
    FORCE_ROOTS.update(roots)
    root_hosts = {urlparse(url).netloc.lower() for url in roots}
    title = args.title or base.slugify(urlparse(roots[0]).path.strip("/") or urlparse(roots[0]).netloc)
    out_root = Path(args.out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    out_dir = base.make_out_dir(out_root, title)
    assets_dir = out_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": base.USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.7",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
        }
    )

    queue = deque((url, 0) for url in roots)
    seen: set[str] = set()
    pages = []
    skipped_queue = []
    global_image_index = [0]

    while queue and len(pages) < args.max_pages:
        url, depth = queue.popleft()
        if url in seen:
            continue
        seen.add(url)
        skip, reason = patched_should_skip_url(url, root_hosts, args.same_domain_only)
        if skip:
            skipped_queue.append({"url": url, "reason": reason})
            continue
        print(f"[{len(pages) + 1}/{args.max_pages}] depth={depth} {url}", flush=True)
        result = process_page(
            session, url, depth, out_dir, assets_dir, len(pages) + 1,
            root_hosts, args.same_domain_only, global_image_index, not args.no_jina,
        )
        pages.append(result)
        if result.status == "ok" and depth < args.max_depth:
            for link in result.links:
                if link.get("skipped"):
                    continue
                linked_url = link["url"]
                if linked_url not in seen:
                    queue.append((linked_url, depth + 1))
        time.sleep(args.delay)

    while queue:
        url, _depth = queue.popleft()
        if url not in seen:
            skipped_queue.append({"url": url, "reason": "max-pages-reached"})

    base.write_inventory(out_dir, title, roots, pages, skipped_queue, args.max_depth, args.max_pages)
    write_media_inventory(out_dir, title, pages)
    rewrite_generator_name(out_dir)
    jina_count = sum(1 for p in pages if "r.jina.ai" in p.final_url)
    videos_found = sum(1 for p in pages for _v in (getattr(p, "videos", []) or []))
    print(out_dir)
    print(f"pages_ok={sum(1 for p in pages if p.status == 'ok')}")
    print(f"main_ok={sum(1 for p in pages if p.status == 'ok' and p.role == 'MAIN')}")
    print(f"linked_ok={sum(1 for p in pages if p.status == 'ok' and p.role == 'LINKED')}")
    print(f"images_ok={sum(1 for p in pages for img in p.images if img.get('status') == 'ok')}")
    print(f"videos_found={videos_found}")
    print(f"pages_failed_or_skipped={sum(1 for p in pages if p.status != 'ok')}")
    print(f"jina_fallback_pages={jina_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
