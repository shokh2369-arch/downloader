#!/usr/bin/env python3
"""
Telegram media downloader bot: YouTube (pytube), Instagram (instaloader),
Facebook (facebook-scraper + Playwright), Twitter/Pinterest (Playwright). No yt-dlp. Parallel per-user tasks.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# Parallel download workers (I/O-bound)
_DOWNLOAD_WORKERS = 6

from dotenv import load_dotenv
from telegram import Message, Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import Conflict, TimedOut

import instaloader
from instaloader import Post
from instaloader.exceptions import (
    InstaloaderException,
    PrivateProfileNotFollowedException,
    LoginRequiredException,
    ConnectionException,
)

load_dotenv()

# ---------------------------------------------------------------------------
# Config (single source of truth; overridable via env)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Config:
    base_dir: Path
    downloads_dir: Path
    download_timeout: int
    max_video_mb: int
    max_video_bytes: int
    caption: str
    supported_domains: tuple[str, ...]

    @classmethod
    def from_env(cls) -> Config:
        base = Path(__file__).resolve().parent
        max_mb = int(os.getenv("MAX_VIDEO_MB", "50"))
        # Default: system temp (no "downloads" folder in project). Set DOWNLOADS_DIR in .env to use a fixed path.
        downloads_dir = os.getenv("DOWNLOADS_DIR")
        if downloads_dir:
            downloads_path = Path(downloads_dir).resolve()
        else:
            downloads_path = Path(tempfile.gettempdir()) / "telegram_downloader_bot"
        return cls(
            base_dir=base,
            downloads_dir=downloads_path,
            download_timeout=int(os.getenv("DOWNLOAD_TIMEOUT", "120")),
            max_video_mb=max_mb,
            max_video_bytes=max_mb * 1024 * 1024,
            caption=os.getenv("BOT_CAPTION", "⬇️ @downloaderin123_bot"),
            supported_domains=(
                "instagram", "tiktok", "twitter.com", "x.com",
                "facebook", "fb.watch", "fb.com", "pinterest", "pin.it",
                "youtube.com", "youtu.be",
            ),
        )


CONFIG = Config.from_env()
BASE_DIR = CONFIG.base_dir
DOWNLOADS_DIR = CONFIG.downloads_dir
DOWNLOAD_TIMEOUT = CONFIG.download_timeout
MAX_VIDEO_MB = CONFIG.max_video_mb
MAX_VIDEO_BYTES = CONFIG.max_video_bytes
CAPTION = CONFIG.caption
SUPPORTED_DOMAINS = CONFIG.supported_domains

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
)
logger = logging.getLogger(__name__)
logging.getLogger("instaloader").setLevel(logging.WARNING)
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

# Minimum size for a file to be considered valid video (avoid sending HTML/error as .mp4)
MIN_VIDEO_BYTES = 50 * 1024  # 50 KB
# Max images to include in a carousel (video + photos) so we don't send 100 items
MAX_CAROUSEL_IMAGES = 20

# Cookie files we write; invalid ones are auto-deleted at startup so deployment can re-fetch
COOKIE_FILES = ("youtube.txt", "facebook.txt", "twitter.txt", "pinterest.txt", "instagram.txt")


def _cookie_file_has_invalid_expiry(path: Path) -> bool:
    """True if file contains Netscape cookie lines with expires=-1 (invalid for some parsers)."""
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        return "\t-1\t" in text
    except OSError:
        return False


def cleanup_invalid_cookie_files() -> None:
    """Delete cookie .txt files that contain invalid expiry (-1) so they are re-created on first use (e.g. after deployment)."""
    for name in COOKIE_FILES:
        path = BASE_DIR / name
        if _cookie_file_has_invalid_expiry(path):
            _safe_unlink(path)
            logger.info("Removed invalid cookie file %s (will be re-created when needed)", name)


def _safe_rmtree(path: Path) -> None:
    """Remove directory tree; never raise."""
    try:
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


def _safe_unlink(path: Path) -> None:
    """Remove file; never raise."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def run_cmd(
    cmd: list[str],
    cwd: Path | None = None,
    timeout: int = DOWNLOAD_TIMEOUT,
) -> tuple[str, int]:
    """Run external command; return (stderr_or_stdout, returncode). Log failures."""
    try:
        r = subprocess.run(
            cmd,
            cwd=cwd or BASE_DIR,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        out = (r.stderr or r.stdout or "").strip()
        if r.returncode != 0 and out:
            logger.warning("[%s] %s", cmd[0], out[:500])
        return out, r.returncode
    except subprocess.TimeoutExpired:
        logger.warning("[%s] timeout", cmd[0])
        return "timeout", -1
    except Exception as e:
        logger.warning("[%s] %s", cmd[0], e)
        return str(e), -1


def recent_files(
    since_ts: float,
    directory: Path,
    buffer_sec: float = 1.0,
) -> list[Path]:
    """Return sorted list of files in directory modified after since_ts - buffer_sec."""
    files: list[Path] = []
    try:
        cutoff = since_ts - buffer_sec
        for p in directory.rglob("*"):
            if p.is_file() and p.stat().st_mtime > cutoff:
                files.append(p)
    except OSError:
        pass
    return sorted(files)


def detect_type(files: list[Path]) -> str:
    for p in files:
        if p.suffix.lower() in (".mp4", ".mov"):
            return "video"
    return "image"


def extract_links(text: str) -> list[str]:
    """Extract supported media URLs from text; strips trailing punctuation from URLs."""
    raw = re.findall(r"https?://[^\s<>)\]]+", (text or "").strip())
    return [u.rstrip(".,;:)") for u in raw if any(d in u.lower() for d in SUPPORTED_DOMAINS)]


def is_youtube(link: str) -> bool:
    return "youtube.com" in link.lower() or "youtu.be" in link.lower()


def download_youtube_pytube(link: str, task_dir: Path) -> list[Path] | None:
    """
    Download YouTube video with pytube (like mehran-sfz/youtube_downloader_telegram_bot):
    720p progressive MP4 when available, else best progressive. No cookies needed for many videos.
    Returns list of downloaded file paths or None on failure.
    """
    try:
        from pytube import YouTube
    except ImportError:
        return None
    try:
        yt = YouTube(link)
        # Prefer 720p progressive MP4 (single file, no merge) like the reference bot
        streams = yt.streams.filter(only_audio=False, file_extension="mp4", progressive=True)
        stream = streams.filter(res="720p").first()
        if not stream and streams:
            # Else highest resolution progressive (parse "720p" -> 720)
            stream = max(
                streams,
                key=lambda s: int(s.resolution.replace("p", "")) if getattr(s, "resolution", None) else 0,
            )
        if not stream:
            return None
        out_path = stream.download(output_path=str(task_dir))
        if not out_path or not Path(out_path).is_file():
            return None
        return [Path(out_path)]
    except Exception as e:
        logger.debug("pytube YouTube: %s", e)
        return None


def is_facebook_video_url(link: str) -> bool:
    """True if URL looks like a Facebook video (watch, reel, /videos/). Else photo/post → use scraper."""
    lower = link.lower()
    if "fb.watch" in lower:
        return True
    if "facebook.com" not in lower and "fb.com" not in lower:
        return False
    return "/watch" in lower or "/videos/" in lower or "/reel/" in lower or "?v=" in lower


def instagram_shortcode(link: str) -> str | None:
    if not link or "instagram.com" not in link.lower():
        return None
    m = re.search(r"instagram\.com/(?:p|reel)/([A-Za-z0-9_-]+)", link, re.IGNORECASE)
    return m.group(1).strip() if m and m.group(1) else None


# ---------------------------------------------------------------------------
# YouTube: get cookies in headless (pseudo) mode (reserved for future use; currently YouTube uses pytube only)
# ---------------------------------------------------------------------------
# Session cookies use expires=-1; yt-dlp skips those. Use far-future expiry so they are accepted.
_SESSION_COOKIE_EXPIRY = 2147483647  # max 32-bit signed, ~year 2038


def _cookies_to_netscape(cookies: list[dict]) -> str:
    """Convert browser-style cookie list to Netscape format. Domain must have leading dot when include_subdomains is TRUE (Python cookiejar requirement)."""
    lines = ["# Netscape HTTP Cookie File", "# https://www.youtube.com"]
    for c in cookies:
        raw_domain = (c.get("domain") or "").strip()
        domain = f".{raw_domain.lstrip('.')}" if raw_domain else ""
        path = c.get("path", "/")
        secure = "TRUE" if c.get("secure") else "FALSE"
        exp = c.get("expires")
        if exp is None or exp == -1:
            expiry = _SESSION_COOKIE_EXPIRY
        else:
            try:
                expiry = int(exp)
            except (TypeError, ValueError):
                expiry = _SESSION_COOKIE_EXPIRY
        name = c.get("name", "")
        value = c.get("value", "")
        if not domain or not name:
            continue
        lines.append(f"{domain}\tTRUE\t{path}\t{secure}\t{expiry}\t{name}\t{value}")
    return "\n".join(lines)


def get_youtube_cookies_headless(cookie_path: Path, timeout_sec: int = 25) -> bool:
    """
    Use headless browser (Playwright) to open YouTube and capture cookies,
    save in Netscape format to cookie_path. Returns True if cookies were written.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("playwright not installed. pip install playwright && playwright install chromium")
        return False
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 720},
            )
            page = context.new_page()
            page.goto("https://www.youtube.com", wait_until="domcontentloaded", timeout=timeout_sec * 1000)
            page.wait_for_timeout(1200)
            cookies = context.cookies()
            browser.close()
        if not cookies:
            return False
        netscape = _cookies_to_netscape(cookies)
        cookie_path.write_text(netscape, encoding="utf-8")
        logger.info("YouTube cookies saved from headless browser to %s", cookie_path.name)
        return True
    except Exception as e:
        logger.warning("Headless YouTube cookies: %s", e)
        return False


def _playwright_cookies_to_netscape(cookies: list[dict], comment: str = "Facebook") -> str:
    """Convert Playwright context.cookies() to Netscape format. Domain must have leading dot when include_subdomains is TRUE (Python cookiejar requirement)."""
    lines = [f"# Netscape HTTP Cookie File", f"# {comment}"]
    for c in cookies:
        raw_domain = (c.get("domain") or "").strip()
        domain = f".{raw_domain.lstrip('.')}" if raw_domain else ""
        path = c.get("path") or "/"
        secure = "TRUE" if c.get("secure") else "FALSE"
        exp = c.get("expires") if c.get("expires") is not None else -1
        try:
            expiry = int(exp) if (exp is not None and exp != -1) else _SESSION_COOKIE_EXPIRY
        except (TypeError, ValueError):
            expiry = _SESSION_COOKIE_EXPIRY
        name = c.get("name") or ""
        value = c.get("value") or ""
        if not domain or not name:
            continue
        # Second column TRUE = include subdomains; cookiejar requires domain to start with . then
        lines.append(f"{domain}\tTRUE\t{path}\t{secure}\t{expiry}\t{name}\t{value}")
    return "\n".join(lines)


def get_facebook_cookies_headless(cookie_path: Path, timeout_sec: int = 25) -> bool:
    """
    Use headless browser (Playwright) to open Facebook and capture cookies,
    save in Netscape format to cookie_path. Returns True if cookies were written.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("Playwright not installed; cannot get Facebook cookies")
        return False
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 720},
            )
            page = context.new_page()
            page.goto("https://www.facebook.com", wait_until="domcontentloaded", timeout=timeout_sec * 1000)
            page.wait_for_timeout(1000)
            cookies = context.cookies()
            browser.close()
        if not cookies:
            return False
        netscape = _playwright_cookies_to_netscape(cookies)
        cookie_path.write_text(netscape, encoding="utf-8")
        logger.info("Facebook cookies saved from headless browser to %s", cookie_path.name)
        return True
    except Exception as e:
        logger.warning("Headless Facebook cookies: %s", e)
        return False


def resolve_facebook_share_url(url: str, timeout_sec: int = 14) -> str | None:
    """
    Resolve Facebook share URL (e.g. facebook.com/share/v/...) to the real video/page URL.
    Uses redirect + og:url fallback. Returns final URL or None.
    """
    if "facebook.com/share" not in url.lower():
        return None
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("Playwright not installed; cannot resolve Facebook share URL")
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 720},
            )
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_sec * 1000)
            page.wait_for_timeout(1400)
            final = page.url
            # If still on share URL, try og:url then any watch/video link on the page
            if final and "/share/" in final.lower():
                try:
                    og_url = page.evaluate(
                        "() => document.querySelector('meta[property=\"og:url\"]')?.getAttribute('content') || ''"
                    )
                    if og_url and isinstance(og_url, str) and "facebook.com" in og_url and "/share/" not in og_url:
                        final = og_url.strip()
                except Exception:
                    pass
                if "/share/" in final.lower():
                    try:
                        watch_url = page.evaluate(
                            "() => { const a = document.querySelector('a[href*=\"facebook.com/watch\"]') "
                            "|| document.querySelector('a[href*=\"fb.watch\"]') "
                            "|| document.querySelector('a[href*=\"/videos/\"]'); return a ? a.href : ''; }"
                        )
                        if watch_url and isinstance(watch_url, str) and "facebook.com" in watch_url and "/share/" not in watch_url:
                            final = watch_url.strip()
                    except Exception:
                        pass
            browser.close()
        if not final or "facebook.com" not in final.lower() or "/share/" in final:
            return None
        if "/login" in final.lower() or "login.php" in final.lower():
            return None
        if final == url:
            return None
        logger.info("Resolved Facebook share URL to %s", final[:80])
        return final
    except Exception as e:
        logger.warning("Resolve Facebook share URL: %s", e)
        return None


def _netscape_cookies_to_playwright(cookie_path: Path) -> list[dict]:
    """Parse Netscape cookie file and return list of dicts for context.add_cookies()."""
    out: list[dict] = []
    try:
        text = cookie_path.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            domain, _inc, path, secure, expires, name, value = parts[0], parts[1], parts[2], parts[3], parts[4], parts[5], parts[6]
            if domain.startswith("."):
                domain = domain[1:]
            try:
                exp = int(expires) if expires != "0" else -1
            except ValueError:
                exp = -1
            out.append({
                "name": name,
                "value": value,
                "domain": domain,
                "path": path or "/",
                "expires": exp,
                "secure": secure.upper() == "TRUE",
                "httpOnly": False,
                "sameSite": "Lax",
            })
    except Exception as e:
        logger.warning("Parse Netscape cookies %s: %s", cookie_path.name, e)
    return out


def _download_url_to_file(url: str, dest: Path, timeout_sec: int = 45, referer: str | None = None) -> bool:
    """Download URL to dest; return True on success. Optional referer for Facebook CDN."""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
        if referer:
            headers["Referer"] = referer
        elif "fbcdn" in url.lower() or "facebook.com" in url.lower():
            headers["Referer"] = "https://www.facebook.com/"
        elif "pbs.twimg.com" in url.lower() or "twimg.com" in url.lower():
            headers["Referer"] = "https://twitter.com/"
        elif "pinimg.com" in url.lower():
            headers["Referer"] = "https://www.pinterest.com/"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            dest.write_bytes(resp.read())
        return dest.is_file() and dest.stat().st_size > 0
    except Exception as e:
        logger.debug("Download %s: %s", url[:60], e)
        return False


def _download_urls_parallel(
    url_dest_pairs: list[tuple[str, Path]],
    timeout_sec: int = 45,
    max_workers: int = _DOWNLOAD_WORKERS,
) -> list[Path]:
    """Download multiple URLs to destinations in parallel; return list of Paths that succeeded."""
    if not url_dest_pairs:
        return []

    def _one(pair: tuple[str, Path]) -> Path | None:
        u, d = pair
        return d if _download_url_to_file(u, d, timeout_sec) else None

    with ThreadPoolExecutor(max_workers=min(max_workers, len(url_dest_pairs))) as ex:
        results = list(ex.map(_one, url_dest_pairs))
    return [p for p in results if p is not None]


def download_facebook_media_scraper(url: str, task_dir: Path) -> tuple[list[Path], str] | None:
    """
    Use facebook-scraper (HTTP, no browser) to get post by URL. Supports carousel: video + multiple photos.
    Returns (files, "video"|"image") or None. No yt-dlp, no Playwright.
    """
    if "facebook.com" not in url.lower() and "fb.watch" not in url.lower() and "fb.com" not in url.lower():
        return None
    try:
        from facebook_scraper import get_posts
    except ImportError:
        logger.debug("facebook-scraper not available; using next method")
        return None
    task_dir.mkdir(parents=True, exist_ok=True)
    cookies_path = BASE_DIR / "facebook.txt"
    cookies = str(cookies_path) if cookies_path.is_file() else None
    try:
        # post_urls accepts full URLs; youtube_dl=True helps extract video URL (including carousel)
        opts = {"allow_extra_requests": True}
        posts = list(get_posts("facebook", post_urls=[url], cookies=cookies, timeout=16, extra_info=False, options=opts, youtube_dl=True))
        if not posts:
            return None
        post = posts[0]
        video_url = post.get("video")
        image_url = post.get("image")
        images_list = post.get("images") or []
        if image_url and image_url not in images_list:
            images_list = [image_url] + list(images_list)
        # Carousel: video first, then up to MAX_CAROUSEL_IMAGES photos
        images_list = list(images_list)[:MAX_CAROUSEL_IMAGES]
        downloaded: list[Path] = []
        if video_url and isinstance(video_url, str):
            dest = task_dir / "fb_video_0.mp4"
            if _download_url_to_file(video_url, dest, timeout_sec=32) and dest.stat().st_size >= MIN_VIDEO_BYTES:
                downloaded.append(dest)
        # If post had video but we got none, return None so Playwright can try
        if video_url and not any(p.suffix.lower() in (".mp4", ".mov") for p in downloaded):
            return None
        img_pairs: list[tuple[str, Path]] = []
        for i, u in enumerate(images_list):
            if not u or not isinstance(u, str):
                continue
            path_part = urlparse(u).path or ""
            ext = Path(path_part).suffix if path_part else ".jpg"
            if ext.lower() not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
                ext = ".jpg"
            img_pairs.append((u, task_dir / f"fb_image_{i}{ext}"))
        downloaded.extend(_download_urls_parallel(img_pairs, timeout_sec=28))
        if not downloaded:
            return None
        has_video = any(p.suffix.lower() in (".mp4", ".mov") for p in downloaded)
        logger.info("Facebook (facebook-scraper): downloaded %s items", len(downloaded))
        return (downloaded, "video" if has_video else "image")
    except Exception as e:
        logger.warning("Facebook scraper: %s", e)
        return None


def download_facebook_media_playwright(url: str, task_dir: Path, timeout_sec: int = 14) -> tuple[list[Path], str] | None:
    """
    Fallback: use Playwright to open Facebook page, extract video/image URLs from DOM. Supports carousel (video + photos).
    Returns (files, "video"|"image") or None.
    """
    if "facebook.com" not in url.lower() and "fb.watch" not in url.lower() and "fb.com" not in url.lower():
        return None
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    task_dir.mkdir(parents=True, exist_ok=True)
    effective_url = url
    if "facebook.com/share" in url.lower():
        resolved = resolve_facebook_share_url(url, timeout_sec=12)
        if resolved:
            effective_url = resolved
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 720},
            )
            cookie_file = BASE_DIR / "facebook.txt"
            if cookie_file.is_file():
                cookies = _netscape_cookies_to_playwright(cookie_file)
                if cookies:
                    context.add_cookies(cookies)
            page = context.new_page()
            page.goto(effective_url, wait_until="domcontentloaded", timeout=timeout_sec * 1000)
            page.wait_for_timeout(1200)
            media_urls = page.evaluate("""
                () => {
                    const out = { videos: [], images: [] };
                    const addVideo = (u) => { if (u && typeof u === 'string' && (u.includes('fbcdn') || u.includes('video') || u.includes('.mp4') || u.includes('facebook')) && !out.videos.includes(u)) out.videos.push(u); };
                    const addImage = (u) => { if (u && typeof u === 'string' && !out.images.some(i => i.split('?')[0] === u.split('?')[0])) out.images.push(u); };
                    // Prefer og:video first (main video URL)
                    ['og:video:secure_url', 'og:video', 'og:video:url'].forEach(prop => {
                        const m = document.querySelector('meta[property="' + prop + '"]');
                        if (m && m.content) addVideo(m.content);
                    });
                    document.querySelectorAll('video source[src], video[src]').forEach(s => addVideo(s.src || s.getAttribute('src') || ''));
                    document.querySelectorAll('a[href*=".mp4"], a[href*="fbcdn"][href*="video"], a[href*="video.xx.fbcdn"]').forEach(a => addVideo(a.href || ''));
                    document.querySelectorAll('[data-video-url], [data-src]').forEach(el => { const u = el.getAttribute('data-video-url') || el.getAttribute('data-src'); if (u && u.includes('video')) addVideo(u); });
                    document.querySelectorAll('img[src*="fbcdn"], img[src*="scontent"]').forEach(img => addImage(img.src || img.getAttribute('src') || ''));
                    const ogImg = document.querySelector('meta[property="og:image"]');
                    if (ogImg && ogImg.content) addImage(ogImg.content);
                    return out;
                }
            """)
            browser.close()
        if not isinstance(media_urls, dict):
            return None
        videos = list(media_urls.get("videos") or [])[:8]   # try more URLs (og:video first)
        images = list(media_urls.get("images") or [])[:MAX_CAROUSEL_IMAGES]
        if not videos and not images:
            return None
        downloaded: list[Path] = []
        for i, u in enumerate(videos):
            if not u or not isinstance(u, str):
                continue
            dest = task_dir / f"fb_video_{i}.mp4"
            if _download_url_to_file(u, dest, timeout_sec=32) and dest.stat().st_size >= MIN_VIDEO_BYTES:
                downloaded.append(dest)
                break  # one video only (carousel: video + photos)
        img_pairs = []
        for i, u in enumerate(images):
            if not u or not isinstance(u, str):
                continue
            path_part = urlparse(u).path or ""
            ext = Path(path_part).suffix if path_part else ".jpg"
            if ext.lower() not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
                ext = ".jpg"
            img_pairs.append((u, task_dir / f"fb_image_{i}{ext}"))
        downloaded.extend(_download_urls_parallel(img_pairs, timeout_sec=28))
        if not downloaded:
            return None
        has_video = any(p.suffix.lower() in (".mp4", ".mov") for p in downloaded)
        logger.info("Facebook (Playwright fallback): downloaded %s items", len(downloaded))
        return (downloaded, "video" if has_video else "image")
    except Exception as e:
        logger.warning("Facebook Playwright: %s", e)
        return None


def download_twitter_media_playwright(url: str, task_dir: Path, timeout_sec: int = 20) -> tuple[list[Path], str] | None:
    """
    Use Playwright to open tweet page, extract image URLs from DOM (pbs.twimg.com), download.
    Returns (files, "image") or None. No yt-dlp; works for photo tweets when cookies are present.
    """
    if "twitter.com" not in url.lower() and "x.com" not in url.lower():
        return None
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    task_dir.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 720},
            )
            cookie_file = BASE_DIR / "twitter.txt"
            if cookie_file.is_file():
                cookies = _netscape_cookies_to_playwright(cookie_file)
                if cookies:
                    context.add_cookies(cookies)
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_sec * 1000)
            page.wait_for_timeout(2600)
            image_urls = page.evaluate("""
                () => {
                    const seen = new Set();
                    const out = [];
                    const isMedia = (u) => u && typeof u === 'string' && u.includes('pbs.twimg.com/media') && !u.includes('profile_images');
                    const add = (u) => {
                        if (isMedia(u) && !seen.has(u.split('?')[0])) {
                            seen.add(u.split('?')[0]);
                            out.push(u);
                        }
                    };
                    document.querySelectorAll('img[src*="pbs.twimg.com/media"]').forEach(img => add(img.src || img.getAttribute('src') || ''));
                    const og = document.querySelector('meta[property="og:image"]');
                    if (og && og.content && isMedia(og.content)) add(og.content);
                    return out;
                }
            """)
            browser.close()
        if not image_urls or not isinstance(image_urls, list):
            return None
        image_urls = list(image_urls)[:MAX_CAROUSEL_IMAGES]
        img_pairs = []
        for i, u in enumerate(image_urls):
            if not u or not isinstance(u, str):
                continue
            path_part = urlparse(u).path or ""
            ext = Path(path_part).suffix if path_part else ".jpg"
            if ext.lower() not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
                ext = ".jpg"
            img_pairs.append((u, task_dir / f"tw_image_{i}{ext}"))
        downloaded = _download_urls_parallel(img_pairs, timeout_sec=40)
        if not downloaded:
            return None
        logger.info("Twitter (Playwright): downloaded %s image(s)", len(downloaded))
        return (downloaded, "image")
    except Exception as e:
        logger.warning("Twitter Playwright: %s", e)
        return None


def download_pinterest_media_playwright(url: str, task_dir: Path, timeout_sec: int = 18) -> tuple[list[Path], str] | None:
    """
    Use Playwright to open Pinterest pin page, extract image URLs (og:image, i.pinimg.com), download.
    Returns (files, "image") or None. Works for image pins when yt-dlp fails.
    """
    if "pinterest.com" not in url.lower() and "pin.it" not in url.lower():
        return None
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    task_dir.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 720},
            )
            cookie_file = BASE_DIR / "pinterest.txt"
            if cookie_file.is_file():
                cookies = _netscape_cookies_to_playwright(cookie_file)
                if cookies:
                    context.add_cookies(cookies)
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_sec * 1000)
            page.wait_for_timeout(2500)
            # Only use og:image = the actual pin image. Skip all other imgs (logos, icons, placeholders).
            image_urls = page.evaluate("""
                () => {
                    const og = document.querySelector('meta[property="og:image"]');
                    if (!og || !og.content || typeof og.content !== 'string') return [];
                    const u = og.content.trim();
                    if (!u || (!u.includes('pinimg.com') && !u.includes('pinterest'))) return [];
                    return [u];
                }
            """)
            browser.close()
        if not image_urls or not isinstance(image_urls, list):
            return None
        image_urls = list(image_urls)[:MAX_CAROUSEL_IMAGES]
        img_pairs = []
        for i, u in enumerate(image_urls):
            if not u or not isinstance(u, str):
                continue
            path_part = urlparse(u).path or ""
            ext = Path(path_part).suffix if path_part else ".jpg"
            if ext.lower() not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
                ext = ".jpg"
            img_pairs.append((u, task_dir / f"pin_image_{i}{ext}"))
        downloaded = _download_urls_parallel(img_pairs, timeout_sec=40)
        if not downloaded:
            return None
        logger.info("Pinterest (Playwright): downloaded %s image(s)", len(downloaded))
        return (downloaded, "image")
    except Exception as e:
        logger.warning("Pinterest Playwright: %s", e)
        return None


# ---------------------------------------------------------------------------
# Instagram via Instaloader (public posts only)
# ---------------------------------------------------------------------------
def get_instaloader_media(target_dir: Path) -> list[Path]:
    exts = {".jpg", ".jpeg", ".png", ".mp4", ".mov"}
    if not target_dir.is_dir():
        return []
    return sorted(p for p in target_dir.rglob("*") if p.is_file() and p.suffix.lower() in exts)


# Chrome UA + Referer improve Instagram CDN speed (default UA without referer can be ~50KB/s).
INSTAGRAM_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def download_instagram_instaloader(shortcode: str, download_dir: Path) -> list[Path]:
    # Suppress instaloader's 403/retry messages to stderr (e.g. "JSON Query to graphql/query: 403 Forbidden [retrying...]")
    stderr_orig = sys.stderr
    devnull = None
    try:
        devnull = open(os.devnull, "w")
        sys.stderr = devnull
    except Exception:
        pass
    try:
        loader = instaloader.Instaloader(
            dirname_pattern=str(download_dir),
            filename_pattern="{shortcode}_{mediaid}",
            download_videos=True,
            download_video_thumbnails=False,
            download_geotags=False,
            download_comments=False,
            save_metadata=False,
            compress_json=False,
            quiet=True,  # no 403/retry messages to console
            max_connection_attempts=3,
            sleep=False,  # no delay between requests — faster; single-post use keeps rate-limit risk low
            user_agent=INSTAGRAM_UA,  # Chrome UA + Referer below improve CDN speed (see instaloader #1233)
            request_timeout=35.0,
        )
        # Referer speeds up media downloads (avoids ~50KB/s throttling when missing).
        try:
            loader.context._session.headers["Referer"] = "https://www.instagram.com/"
        except Exception:
            pass
        post = Post.from_shortcode(loader.context, shortcode)
        loader.download_post(post, target=shortcode)
        return get_instaloader_media(download_dir)
    finally:
        sys.stderr = stderr_orig
        if devnull is not None:
            try:
                devnull.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Video re-encode (ffmpeg only; no yt-dlp)
# ---------------------------------------------------------------------------
# Scale + format for all devices; H.264 baseline + AAC + faststart for iPhone/iOS (baseline = max compatibility)
_FFMPEG_SCALE_FIT = (
    r'-vf "scale=if(gt(iw\,ih)\,1920\,-2):if(gt(iw\,ih)\,-2\,1920),scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p" '
    "-c:v libx264 -preset veryfast -profile:v baseline -level 3.0 -c:a aac -ac 2 -b:a 128k -movflags +faststart"
)
# Same filter for direct ffmpeg (no shell, so no comma escape)
_FFMPEG_VF_SCALE = "scale=if(gt(iw,ih),1920,-2):if(gt(iw,ih),-2,1920),scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p"


def reencode_video_ios_compatible(src: Path) -> Path | None:
    """Re-encode video to H.264 baseline + AAC + faststart so it plays on iPhone. Returns new path or None."""
    out = src.parent / (src.stem + "_ios.mp4")
    # veryfast preset = faster encode; baseline + level 3.0 = device compatibility
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-vf", _FFMPEG_VF_SCALE,
        "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "baseline", "-level", "3.0",
        "-c:a", "aac", "-ac", "2", "-b:a", "128k", "-movflags", "+faststart",
        str(out),
    ]
    err, code = run_cmd(cmd, timeout=300)
    if code == 0 and out.is_file() and out.stat().st_size >= MIN_VIDEO_BYTES:
        return out
    _safe_unlink(out)
    # Fallback: remux only (copy streams + faststart) — works when source is already playable
    cmd_fast = ["ffmpeg", "-y", "-i", str(src), "-c", "copy", "-movflags", "+faststart", str(out)]
    err2, code2 = run_cmd(cmd_fast, timeout=120)
    if code2 == 0 and out.is_file() and out.stat().st_size >= MIN_VIDEO_BYTES:
        logger.info("Video sent with faststart remux (re-encode skipped)")
        return out
    _safe_unlink(out)
    if code != 0:
        logger.warning("iOS re-encode failed for %s: %s", src.name, (err or "")[:150])
    return None


def download_other(link: str, task_dir: Path) -> tuple[list[Path], str] | None:
    """
    Download from link into task_dir. No yt-dlp. YouTube: pytube only. Facebook/Twitter/Pinterest: scraper + Playwright.
    """
    task_dir.mkdir(parents=True, exist_ok=True)

    if is_youtube(link):
        # YouTube: pytube only (720p progressive MP4)
        pytube_files = download_youtube_pytube(link, task_dir)
        if pytube_files:
            return pytube_files, detect_type(pytube_files)
        return None

    # Facebook: run scraper and Playwright in parallel; use first successful result (faster)
    is_facebook = "facebook.com" in link.lower() or "fb.watch" in link.lower() or "fb.com" in link.lower()
    if is_facebook:
        facebook_cookie_path = BASE_DIR / "facebook.txt"
        if not facebook_cookie_path.is_file():
            get_facebook_cookies_headless(facebook_cookie_path)
        effective = link
        if "facebook.com/share" in link.lower():
            resolved = resolve_facebook_share_url(link)
            if resolved:
                effective = resolved
        def only_photos_if_carousel(files: list[Path], typ: str) -> tuple[list[Path], str]:
            img_exts = (".jpg", ".jpeg", ".png", ".webp", ".gif")
            has_vid = any(p.suffix.lower() in (".mp4", ".mov") for p in files)
            has_img = any(p.suffix.lower() in img_exts for p in files)
            if has_vid and has_img:
                return ([p for p in files if p.suffix.lower() in img_exts], "image")
            return (files, typ)
        # Parallel run: two subdirs so outputs don't clash
        dir_s, dir_p = task_dir / "s", task_dir / "p"
        dir_s.mkdir(parents=True, exist_ok=True)
        dir_p.mkdir(parents=True, exist_ok=True)
        with ThreadPoolExecutor(max_workers=2) as ex:
            fut_s = ex.submit(download_facebook_media_scraper, effective, dir_s)
            fut_p = ex.submit(download_facebook_media_playwright, effective, dir_p)
            done, _ = wait([fut_s, fut_p], timeout=22, return_when=FIRST_COMPLETED)
            for f in done:
                try:
                    result = f.result(timeout=0)
                except Exception:
                    continue
                if result:
                    return only_photos_if_carousel(*result)
            # If first completed had no result, wait for the other (remaining time)
            pending = [x for x in (fut_s, fut_p) if x not in done]
            if pending:
                done2, _ = wait(pending, timeout=12)
                for f in done2:
                    try:
                        result = f.result(timeout=0)
                    except Exception:
                        continue
                    if result:
                        return only_photos_if_carousel(*result)
        return None

    # Twitter/X: Playwright for images only
    if "twitter.com" in link.lower() or "x.com" in link.lower():
        result = download_twitter_media_playwright(link, task_dir)
        if result:
            return result
        return None

    # Pinterest: Playwright for images only
    if "pinterest.com" in link.lower() or "pin.it" in link.lower():
        result = download_pinterest_media_playwright(link, task_dir)
        if result:
            return result
        return None

    # TikTok and other domains: not supported without yt-dlp
    return None


# ---------------------------------------------------------------------------
# Send media (pytube, instaloader, scraper, Playwright output)
# ---------------------------------------------------------------------------
async def send_media(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    file_path: Path,
    media_type: str,
    reply_to_id: int,
) -> bool:
    """Send file to chat; for video >50MB send as document. Returns False on any API error."""
    try:
        size = file_path.stat().st_size
        with open(file_path, "rb") as f:
            if media_type == "video":
                if size > MAX_VIDEO_BYTES:
                    await update.effective_chat.send_document(
                        document=f, caption=CAPTION + " (50MB+)", reply_to_message_id=reply_to_id,
                    )
                else:
                    await update.effective_chat.send_video(
                        video=f, caption=CAPTION, supports_streaming=True, reply_to_message_id=reply_to_id,
                    )
            else:
                await update.effective_chat.send_photo(
                    photo=f, caption=CAPTION, reply_to_message_id=reply_to_id,
                )
        return True
    except Exception as e:
        err_msg = str(e).lower()
        logger.warning(
            "send_media path=%s size=%s type=%s error=%s",
            file_path.name, size, media_type, err_msg[:200],
        )
        return False


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
COOKIES_HELP = (
    "🍪 **YouTube cookies — uzoq ishlashi uchun**\n\n"
    "YouTube brauzerda aktiv bo‘lganda cookie’larni almashtiradi. Quyidagi usul ularni uzoqroq saqlaydi:\n\n"
    "1. **Incognito/private** oyna oching, YouTube’da login qiling.\n"
    "2. **Xuddi shu** tabda: https://www.youtube.com/robots.txt (incognitoda faqat shu tab ochiq bo‘lsin).\n"
    "3. **youtube.com** cookie’larini **Netscape** formatida eksport qiling (Get cookies.txt / Cookie-Editor).\n"
    "4. Faylni **youtube.txt** deb saqlab, bot papkasiga qo‘ying; keyin incognito oynani **yoping**.\n\n"
    "Session yopilgach cookie’lar aylanmaydi, shuning uchun uzoqroq ishlaydi. Qayta kerak bo‘lsa, xuddi shu usulni takrorlang."
)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Salom!\n\n"
        "• **YouTube** — pytube.\n"
        "• **Instagram** — public post/reel.\n"
        "• **Facebook, X, Pinterest** — scraper / Playwright (images; Facebook also video when available).\n\n"
        "Cookie’larni uzoq ishlatish: /cookies",
        parse_mode="Markdown",
    )


async def cmd_cookies(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send instructions so YouTube cookies last longer (incognito + robots.txt export)."""
    await update.message.reply_text(COOKIES_HELP, parse_mode="Markdown")


async def _process_links_task(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    links: list[str],
    status_msg: Message,
    reply_to_id: int,
) -> None:
    """Process links in background; one task per user so many users are served in parallel."""
    chat_id = update.effective_chat.id if update.effective_chat else 0
    user_id = update.effective_user.id if update.effective_user else 0
    status_deleted = [False]

    async def try_delete_status() -> None:
        if status_deleted[0]:
            return
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=status_msg.message_id)
            status_deleted[0] = True
        except Exception:
            pass

    def cleanup_ig_dir(ig_dir: Path) -> None:
        _safe_rmtree(ig_dir)

    try:
        for link in links:
            shortcode = instagram_shortcode(link)
            if shortcode:
                ig_dir = DOWNLOADS_DIR / "ig" / f"task_{update.update_id}_{id(link)}"
                ig_dir.mkdir(parents=True, exist_ok=True)
                try:
                    files = await asyncio.to_thread(download_instagram_instaloader, shortcode, ig_dir)
                except PrivateProfileNotFollowedException:
                    await status_msg.edit_text("❌ Bu profil private. Faqat public post/reel ishlaydi.")
                    cleanup_ig_dir(ig_dir)
                    continue
                except LoginRequiredException:
                    await status_msg.edit_text("❌ Instagram login soʻrayapti. Keyinroq urinib koʻring.")
                    cleanup_ig_dir(ig_dir)
                    continue
                except ConnectionException:
                    await status_msg.edit_text("❌ Instagramga ulanib boʻlmadi. Ulovni tekshiring.")
                    cleanup_ig_dir(ig_dir)
                    continue
                except InstaloaderException as e:
                    err_text = str(e)
                    msg = "❌ Instagram blok qildi. Keyinroq yoki boshqa link." if ("403" in err_text or "Forbidden" in err_text) else f"❌ Yuklab boʻlmadi: {err_text[:150]}"
                    await status_msg.edit_text(msg)
                    cleanup_ig_dir(ig_dir)
                    continue
                except Exception as e:
                    logger.exception("Instagram download user_id=%s link=%s", user_id, link[:50])
                    await status_msg.edit_text(f"❌ Xatolik: {str(e)[:100]}")
                    cleanup_ig_dir(ig_dir)
                    continue

                if not files:
                    await status_msg.edit_text("❌ Postda media topilmadi.")
                    cleanup_ig_dir(ig_dir)
                    continue

                await try_delete_status()
                video_paths = [p for p in files if p.suffix.lower() in (".mp4", ".mov")]
                reencoded_list = await asyncio.gather(
                    *[asyncio.to_thread(reencode_video_ios_compatible, p) for p in video_paths]
                ) if video_paths else []
                vid_idx = 0
                for fp in files:
                    is_video = fp.suffix.lower() in (".mp4", ".mov")
                    to_send = (reencoded_list[vid_idx] or fp) if is_video else fp
                    if is_video:
                        vid_idx += 1
                    await send_media(update, context, to_send, "video" if is_video else "image", reply_to_id)
                cleanup_ig_dir(ig_dir)
                continue

            # Unique dir per (user, update, link) so concurrent users never get each other's files
            link_slug = hashlib.sha256(link.encode()).hexdigest()[:12]
            task_dir = DOWNLOADS_DIR / "dl" / f"{update.update_id}_{user_id}_{link_slug}"
            result = await asyncio.to_thread(download_other, link, task_dir)
            if not result:
                if "instagram" in link.lower():
                    err = "❌ Instagram: brauzerda login yoki instagram.txt cookie."
                elif "facebook.com" in link.lower() or "fb.watch" in link.lower() or "fb.com" in link.lower():
                    err = "❌ Facebook: media topilmadi. facebook.txt cookie qoʻying yoki linkni brauzerda ochib tekshiring."
                else:
                    err = "❌ Yuklab boʻlmadi"
                await status_msg.edit_text(err)
                _safe_rmtree(task_dir)
                continue

            files, _ = result
            await try_delete_status()
            video_paths = [p for p in files if p.suffix.lower() in (".mp4", ".mov")]
            reencoded_list = await asyncio.gather(
                *[asyncio.to_thread(reencode_video_ios_compatible, p) for p in video_paths]
            ) if video_paths else []
            vid_idx = 0
            for fp in files:
                is_video = fp.suffix.lower() in (".mp4", ".mov")
                to_send = (reencoded_list[vid_idx] or fp) if is_video else fp
                if is_video:
                    vid_idx += 1
                ok = await send_media(update, context, to_send, "video" if is_video else "image", reply_to_id)
                if not ok:
                    await update.message.reply_text(
                        "❌ Media yuborib boʻlmadi (Telegram cheklovi yoki tarmoq xatosi).",
                        reply_to_message_id=reply_to_id,
                    )
                _safe_unlink(to_send)
                if to_send != fp:
                    _safe_unlink(fp)
            _safe_rmtree(task_dir)

        await try_delete_status()
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.exception("process_links_task user_id=%s", user_id)
        try:
            await status_msg.edit_text(f"❌ Xatolik: {str(e)[:150]}")
        except Exception:
            pass


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply immediately with status, then process links in a background task (parallel per user)."""
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    links = extract_links(text)
    if not links:
        return

    status_msg = await update.message.reply_text("⏳ Yuklanmoqda…")
    reply_to_id = update.message.message_id
    task = asyncio.create_task(
        _process_links_task(update, context, links, status_msg, reply_to_id),
        name=f"dl_{update.update_id}",
    )

    def _on_done(t: asyncio.Task[Any]) -> None:
        exc = t.exception()
        if exc is not None:
            logger.warning("background task %s failed: %s", t.get_name(), exc)

    task.add_done_callback(_on_done)


def _run_health_server(port: int) -> None:
    """Run a minimal HTTP server for GET/HEAD / and /health (e.g. Docker HEALTHCHECK, Render monitor)."""

    class Handler(BaseHTTPRequestHandler):
        def _health_ok(self) -> bool:
            p = self.path.split("?")[0].rstrip("/") or "/"
            return p in ("/health", "/")

        def do_GET(self) -> None:
            if self._health_ok():
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"ok")
            else:
                self.send_response(404)
                self.end_headers()

        def do_HEAD(self) -> None:
            if self._health_ok():
                self.send_response(200)
                self.end_headers()
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            logger.debug("health %s", args[0] if args else "")

    server = HTTPServer(("0.0.0.0", port), Handler)
    try:
        server.serve_forever()
    except Exception as e:
        logger.warning("health server: %s", e)
    finally:
        server.server_close()

def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        logger.error("BOT_TOKEN not set (use .env or export)")
        raise SystemExit(1)

    cleanup_invalid_cookie_files()

    health_port = int(os.getenv("HEALTH_PORT", "8080"))
    health_thread = threading.Thread(target=_run_health_server, args=(health_port,), daemon=True)
    health_thread.start()
    logger.info("Health server listening on 0.0.0.0:%s", health_port)

    app = (
        Application.builder()
        .token(token)
        .post_init(_log_startup)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cookies", cmd_cookies))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(_error_handler)
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


async def _log_startup(app: Application) -> None:
    """Log startup config (no secrets)."""
    logger.info(
        "Bot started | parallel=True | timeout=%ss | max_video=%sMB",
        CONFIG.download_timeout,
        CONFIG.max_video_mb,
    )


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors; for Conflict (multiple instances) log a clear message."""
    err = context.error
    if err is None:
        return
    if isinstance(err, Conflict):
        logger.warning(
            "Telegram Conflict: another bot instance is running (e.g. old deploy). "
            "Ensure only one instance uses this token, or wait for the other to stop."
        )
        return
    if isinstance(err, TimedOut):
        logger.warning("Telegram request timed out: %s", err)
        return
    logger.exception("Update %s caused error: %s", update, err)


if __name__ == "__main__":
    main()
