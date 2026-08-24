from __future__ import annotations

import hashlib
import re
import sys
from collections import deque
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag

import requests
from bs4 import BeautifulSoup

SOURCE = "https://christopher-palasz-portfolio.chrispalasz.chatgpt.site/"
SOURCE_HOST = urlparse(SOURCE).netloc
OUT = Path("/tmp/portfolio-site")
OUT.mkdir(parents=True, exist_ok=True)

session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0 (GitHub Pages portfolio importer)"})

queue: deque[tuple[str, str]] = deque([(SOURCE, "page")])
seen: set[str] = set()
MAX_FILES = 1500
MAX_FILE_BYTES = 90 * 1024 * 1024

asset_tags = {
    "script": ["src"],
    "link": ["href"],
    "img": ["src"],
    "source": ["src"],
    "video": ["src", "poster"],
    "audio": ["src"],
}

doc_exts = {".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".zip"}


def clean_url(raw: str, base: str) -> str | None:
    if not raw or raw.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
        return None
    absolute = urljoin(base, raw)
    absolute, _ = urldefrag(absolute)
    p = urlparse(absolute)
    if p.scheme not in {"http", "https"}:
        return None
    return absolute


def local_web_path(url: str, content_type: str = "") -> str:
    p = urlparse(url)
    path = p.path or "/"
    is_html = "text/html" in content_type
    suffix = Path(path).suffix

    if p.netloc == SOURCE_HOST:
        if is_html or path.endswith("/") or (not suffix and "text/html" in content_type):
            if not path.endswith("/"):
                path += "/"
            return path + "index.html"
        return path

    host = re.sub(r"[^A-Za-z0-9._-]", "_", p.netloc)
    if not path or path.endswith("/"):
        path += "index"
    if p.query:
        stem = Path(path).stem
        suffix = Path(path).suffix
        qhash = hashlib.sha1(p.query.encode()).hexdigest()[:10]
        path = str(Path(path).with_name(f"{stem}-{qhash}{suffix}"))
    return f"/_external/{host}{path}"


def disk_path(web_path: str) -> Path:
    return OUT / web_path.lstrip("/")


def enqueue(url: str, kind: str) -> None:
    if len(seen) + len(queue) >= MAX_FILES:
        return
    queue.append((url, kind))


def rewrite_ref(url: str, base: str, kind: str) -> str:
    absolute = clean_url(url, base)
    if not absolute:
        return url
    p = urlparse(absolute)
    if p.netloc == SOURCE_HOST:
        if kind == "page":
            path = p.path or "/"
            if path == "/":
                return "/"
            if Path(path).suffix.lower() in doc_exts:
                enqueue(absolute, "asset")
                return path
            enqueue(absolute, "page")
            return path
        enqueue(absolute, "asset")
        return p.path or "/"

    if kind == "asset":
        enqueue(absolute, "asset")
        return local_web_path(absolute)
    return absolute


def process_css(text: str, base_url: str) -> str:
    def repl(match: re.Match[str]) -> str:
        quote = match.group(1) or ""
        raw = match.group(2).strip()
        absolute = clean_url(raw, base_url)
        if not absolute:
            return match.group(0)
        enqueue(absolute, "asset")
        p = urlparse(absolute)
        target = p.path if p.netloc == SOURCE_HOST else local_web_path(absolute)
        return f"url({quote}{target}{quote})"

    return re.sub(r"url\(\s*(['\"]?)([^'\")]+)\1\s*\)", repl, text, flags=re.I)


def save_response(url: str, kind: str, r: requests.Response) -> None:
    ctype = (r.headers.get("content-type") or "").split(";")[0].lower()
    data = r.content
    if len(data) > MAX_FILE_BYTES:
        raise RuntimeError(f"File too large to mirror safely: {url}")

    if "text/html" in ctype or kind == "page":
        text = r.text
        lower = text.lower()
        if "this site uses chatgpt to securely log you in" in lower:
            raise RuntimeError("The source site is still returning the ChatGPT login page instead of the public portfolio.")

        soup = BeautifulSoup(text, "html.parser")

        for tag_name, attrs in asset_tags.items():
            for tag in soup.find_all(tag_name):
                for attr in attrs:
                    if tag.get(attr):
                        tag[attr] = rewrite_ref(tag[attr], url, "asset")
                if tag.get("srcset"):
                    parts = []
                    for item in tag["srcset"].split(","):
                        bits = item.strip().split()
                        if bits:
                            bits[0] = rewrite_ref(bits[0], url, "asset")
                        parts.append(" ".join(bits))
                    tag["srcset"] = ", ".join(parts)

        for a in soup.find_all("a", href=True):
            absolute = clean_url(a["href"], url)
            if not absolute:
                continue
            p = urlparse(absolute)
            if p.netloc == SOURCE_HOST:
                ext = Path(p.path).suffix.lower()
                a["href"] = rewrite_ref(a["href"], url, "asset" if ext in doc_exts else "page")

        out_path = local_web_path(url, "text/html")
        path = disk_path(out_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(soup), encoding="utf-8")
        return

    if "text/css" in ctype or urlparse(url).path.lower().endswith(".css"):
        text = process_css(r.text, url)
        out_path = local_web_path(url, "text/css")
        path = disk_path(out_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return

    out_path = local_web_path(url, ctype)
    path = disk_path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


while queue:
    url, kind = queue.popleft()
    key = urldefrag(url)[0]
    if key in seen:
        continue
    seen.add(key)
    print(f"[{len(seen)}] {kind}: {key}")
    try:
        r = session.get(key, timeout=45, allow_redirects=True)
        r.raise_for_status()
        save_response(r.url, kind, r)
    except Exception as exc:
        if kind == "page" or key == SOURCE:
            print(f"ERROR: {exc}", file=sys.stderr)
            raise
        print(f"WARNING: could not mirror asset {key}: {exc}", file=sys.stderr)

index = OUT / "index.html"
if not index.exists() or index.stat().st_size < 200:
    raise RuntimeError("No usable index.html was produced.")

index_text = index.read_text(encoding="utf-8", errors="ignore").lower()
if SOURCE_HOST.lower() in index_text and "http-equiv=\"refresh\"" in index_text:
    raise RuntimeError("The imported site is still only a redirect.")

(OUT / ".nojekyll").touch()
print(f"\nImported {len(seen)} URLs into {OUT}")
print("Root files:")
for p in sorted(OUT.iterdir()):
    print(" -", p.name)
