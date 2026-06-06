#!/usr/bin/env python3
import argparse
import datetime as dt
import importlib.util
import re
import sys
import time
from collections import deque
from pathlib import Path
from urllib.parse import urlparse

import requests


PROJECT_ROOT = Path(__file__).resolve().parents[4]
BASE_SCRIPT = PROJECT_ROOT / ".agent/skills/1-web-research-pack/scripts/collect_web_research_pack.py"

spec = importlib.util.spec_from_file_location("web_research_pack_base", BASE_SCRIPT)
if spec is None or spec.loader is None:
    raise RuntimeError(f"Cannot load base collector: {BASE_SCRIPT}")
base = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = base
spec.loader.exec_module(base)


FORCE_ROOTS: set[str] = set()
ORIGINAL_SHOULD_SKIP = base.should_skip_url
WEAK_MARKERS = [
    "enable javascript",
    "javascript is not available",
    "this browser is no longer supported",
    "something went wrong",
    "please enable cookies",
    "access denied",
    "checking your browser",
    "just a moment",
    "log in to",
    "sign up now",
]


def normalize(url: str) -> str:
    return base.normalize_url(url)


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


def jina_reader_url(url: str) -> str:
    return "https://r.jina.ai/http://r.jina.ai/http://" + url


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
    reader = jina_reader_url(url)
    role = base.page_role(depth)
    try:
        response = session.get(reader, timeout=45, headers={"Accept": "text/plain,*/*"})
        response.raise_for_status()
        title, markdown = parse_jina_markdown(response.text, url)
        markdown, images = base.localize_markdown_images(
            markdown, url, session, assets_dir, global_image_index
        )
        links = base.extract_markdown_links(markdown, url, root_hosts, same_domain_only)
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
            f"> Capture Method: r.jina.ai fallback after direct extraction was unavailable or weak\n\n"
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
    direct = base.process_page(
        session,
        url,
        depth,
        out_dir,
        assets_dir,
        index,
        root_hosts,
        same_domain_only,
        global_image_index,
    )
    if not use_jina:
        return direct
    direct_path = out_dir / getattr(direct, "filename", "")
    needs_fallback = direct.status != "ok" or markdown_quality_is_weak(direct_path)
    if not needs_fallback:
        return direct
    fallback = process_with_jina(
        session,
        url,
        depth,
        out_dir,
        assets_dir,
        index,
        root_hosts,
        same_domain_only,
        global_image_index,
        previous=direct,
    )
    if (
        getattr(fallback, "filename", "") != getattr(direct, "filename", "")
        and direct_path.exists()
        and direct_path.name.endswith(".md")
    ):
        direct_path.unlink()
    return fallback


def rewrite_generator_name(out_dir: Path) -> None:
    for name in ["README.md"]:
        path = out_dir / name
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace("`1-web-research-pack`", "`1-web-pack`"), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Collect body pages, body links and images into a local writing material pack."
    )
    parser.add_argument("urls", nargs="+", help="Entry URLs")
    parser.add_argument("--out-root", default="Clippings/Reading")
    parser.add_argument("--title", default="")
    parser.add_argument("--max-pages", type=int, default=80)
    parser.add_argument("--max-depth", type=int, default=1)
    parser.add_argument("--same-domain-only", action="store_true")
    parser.add_argument("--delay", type=float, default=0.2)
    parser.add_argument("--no-jina", action="store_true", help="Disable r.jina.ai fallback")
    args = parser.parse_args()

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
        result = process_page(
            session,
            url,
            depth,
            out_dir,
            assets_dir,
            len(pages) + 1,
            root_hosts,
            args.same_domain_only,
            global_image_index,
            not args.no_jina,
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
    rewrite_generator_name(out_dir)
    jina_count = sum(1 for p in pages if "r.jina.ai" in p.final_url)
    print(out_dir)
    print(f"pages_ok={sum(1 for p in pages if p.status == 'ok')}")
    print(f"main_ok={sum(1 for p in pages if p.status == 'ok' and p.role == 'MAIN')}")
    print(f"linked_ok={sum(1 for p in pages if p.status == 'ok' and p.role == 'LINKED')}")
    print(f"images_ok={sum(1 for p in pages for img in p.images if img.get('status') == 'ok')}")
    print(f"pages_failed_or_skipped={sum(1 for p in pages if p.status != 'ok')}")
    print(f"jina_fallback_pages={jina_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
