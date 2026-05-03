#!/usr/bin/env python3
"""
RSS/Atom フィードを日本語に翻訳して再出力するスクリプト。

- ソースフィードを feeds.yaml から読む
- 各エントリのタイトル + 本文を deep-translator で日本語化
- 翻訳結果は cache/<name>.json にキャッシュして再翻訳を回避
- output/<name>.xml に Atom 形式で書き出す
- output/index.html を生成（フィードURL一覧）
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from xml.sax.saxutils import escape as xml_escape

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup
from deep_translator import GoogleTranslator, MyMemoryTranslator
from feedgen.feed import FeedGenerator

ROOT = Path(__file__).parent
CACHE_DIR = ROOT / "cache"
OUT_DIR = ROOT / "output"
CONFIG_PATH = ROOT / "feeds.yaml"

CHUNK_SIZE = 4500            # GoogleTranslator は 5000 文字制限
MAX_ENTRIES_PER_FEED = 25    # 1 フィードあたりの最大エントリ数
TRANSLATE_RETRIES = 2

USER_AGENT = (
    "Mozilla/5.0 (compatible; rss-jp-translator/1.0; "
    "+https://github.com/) GitHubActionsBot"
)


# --------------------------------------------------------------------------- #
# 翻訳ロジック
# --------------------------------------------------------------------------- #

def chunk_text(text: str, size: int = CHUNK_SIZE) -> list[str]:
    """改行・句読点境界で text を size 文字以内に分割する。"""
    text = text or ""
    chunks: list[str] = []
    while text:
        if len(text) <= size:
            chunks.append(text)
            break
        # 文末（。 . ! ? \n）で切れる位置を探す
        window = text[:size]
        cut = max(
            window.rfind("\n\n"),
            window.rfind("。"),
            window.rfind(". "),
            window.rfind("! "),
            window.rfind("? "),
        )
        if cut < size // 2:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = size
        chunks.append(text[:cut + 1])
        text = text[cut + 1:]
    return chunks


def _try_translate(chunk: str, target: str) -> Optional[str]:
    # 1) Google 翻訳（無料エンドポイント、deep-translator経由）
    for attempt in range(TRANSLATE_RETRIES):
        try:
            out = GoogleTranslator(source="auto", target=target).translate(chunk)
            if out:
                return out
        except Exception as e:
            print(f"  ! Google attempt {attempt + 1} failed: {e}", flush=True)
            time.sleep(1.5 * (attempt + 1))

    # 2) MyMemory（無料・無認証、品質はやや劣る）
    try:
        out = MyMemoryTranslator(source="en-US", target="ja-JP").translate(chunk)
        if out:
            return out
    except Exception as e:
        print(f"  ! MyMemory failed: {e}", flush=True)

    return None


def translate(text: str, target: str = "ja") -> str:
    if not text or not text.strip():
        return text
    chunks = chunk_text(text)
    out_chunks = []
    for c in chunks:
        translated = _try_translate(c, target)
        if translated is None:
            translated = c  # 翻訳失敗時は原文を温存
        out_chunks.append(translated)
        time.sleep(0.4)  # 連打しない
    return "\n".join(out_chunks)


# --------------------------------------------------------------------------- #
# キャッシュ
# --------------------------------------------------------------------------- #

def load_cache(name: str) -> dict:
    p = CACHE_DIR / f"{name}.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_cache(name: str, cache: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / f"{name}.json").write_text(
        json.dumps(cache, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# フィード処理
# --------------------------------------------------------------------------- #

def fetch_feed(url: str) -> feedparser.FeedParserDict:
    """User-Agent を付けて取得 → feedparser に渡す。"""
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
        resp.raise_for_status()
        return feedparser.parse(resp.content)
    except Exception as e:
        print(f"  ! requests.get failed ({e}); falling back to feedparser direct", flush=True)
        return feedparser.parse(url)


def html_to_text(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    # 改行を保つために <br> と <li> を改行に変換
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for li in soup.find_all("li"):
        li.insert_before("• ")
        li.append("\n")
    text = soup.get_text("\n")
    # 連続改行を整理
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


ARTICLE_SELECTORS = [
    "article",
    "main",
    "[role='main']",
    ".post-content",
    ".post-body",
    ".entry-content",
    ".article-content",
    ".content",
]


def extract_article_payload(url: str) -> tuple[str, str]:
    """記事ページから本文HTMLとテキストをできるだけ抽出する。"""
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        print(f"  ! article fetch failed ({e})", flush=True)
        return "", ""

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup.find_all(["script", "style", "noscript", "svg", "canvas", "iframe", "form", "nav", "header", "footer", "aside"]):
        tag.decompose()

    candidates = []
    for selector in ARTICLE_SELECTORS:
        candidates.extend(soup.select(selector))
    if soup.body:
        candidates.append(soup.body)
    else:
        candidates.append(soup)

    best = None
    best_len = 0
    seen: set[int] = set()
    for candidate in candidates:
        marker = id(candidate)
        if marker in seen:
            continue
        seen.add(marker)
        fragment = "".join(str(child) for child in candidate.children).strip()
        text = html_to_text(fragment)
        length = len(text)
        if length > best_len:
            best = candidate
            best_len = length

    if best is None or best_len == 0:
        return "", ""

    fragment = "".join(str(child) for child in best.children).strip()
    text = html_to_text(fragment)
    return fragment, text


def text_to_html(text: str) -> str:
    """翻訳済み plain text を簡易な HTML に戻す（改行 → <br>）。"""
    return xml_escape(text).replace("\n", "<br>\n")


@dataclass
class FeedDef:
    name: str
    url: str
    title: Optional[str] = None


def process_feed(fd: FeedDef) -> tuple[bool, str]:
    print(f"\n=== {fd.name}: {fd.url}", flush=True)
    cache = load_cache(fd.name)

    parsed = fetch_feed(fd.url)
    if parsed.bozo and not parsed.entries:
        msg = f"フィード取得失敗: {parsed.bozo_exception}"
        print(f"  ! {msg}", flush=True)
        return False, msg

    src_title = parsed.feed.get("title") or fd.name
    src_link = parsed.feed.get("link") or fd.url
    src_subtitle = parsed.feed.get("subtitle", "")

    fg = FeedGenerator()
    fg.id(fd.url)
    fg.title(fd.title or f"[JP] {src_title}")
    fg.link(href=src_link, rel="alternate")
    fg.link(href=fd.url, rel="via")
    fg.subtitle(f"日本語訳: {src_subtitle}" if src_subtitle else "日本語訳版")
    fg.language("ja")
    fg.updated(datetime.now(timezone.utc))

    entries = parsed.entries[:MAX_ENTRIES_PER_FEED]
    print(f"  entries: {len(entries)}", flush=True)

    translated_count = 0
    for entry in entries:
        eid = entry.get("id") or entry.get("link") or entry.get("title", "")
        if not eid:
            continue
        ekey = hashlib.sha256(eid.encode("utf-8")).hexdigest()[:20]

        title_en = entry.get("title", "") or ""
        # まず feed 側の本文を取り、足りなければ記事ページを見に行く
        content_html = ""
        if entry.get("content"):
            content_html = entry.content[0].get("value", "") or ""
        if not content_html:
            content_html = entry.get("summary", "") or ""

        content_text = html_to_text(content_html)
        source_html = content_html
        source_text = content_text
        source_is_html = bool(entry.get("content"))

        article_link = entry.get("link") or ""
        if article_link:
            page_html, page_text = extract_article_payload(article_link)
            if page_text and (not source_text or len(page_text) > len(source_text) + 150):
                source_html = page_html or source_html
                source_text = page_text
                source_is_html = bool(page_html)

        original_body = source_html if source_is_html else xml_escape(source_html or source_text)

        cached = cache.get(ekey)
        # 元タイトル/本文に変更がなければキャッシュを使う
        if (
            cached
            and cached.get("title_en") == title_en
            and cached.get("content_en_hash") == hashlib.md5(source_text.encode("utf-8")).hexdigest()
        ):
            title_ja = cached["title_ja"]
            content_ja_html = cached["content_ja_html"]
        else:
            print(f"  -> translating: {title_en[:80]}", flush=True)
            try:
                title_ja = translate(title_en) if title_en else title_en
                if source_text:
                    content_ja_text = translate(source_text)
                    content_ja_html = (
                        '<div lang="ja"><h3>日本語訳</h3>'
                        f"<div>{text_to_html(content_ja_text)}</div></div>"
                        '<hr><div lang="en"><h3>Original (English)</h3>'
                        f"{original_body}</div>"
                    )
                else:
                    content_ja_html = original_body
                cache[ekey] = {
                    "title_en": title_en,
                    "title_ja": title_ja,
                    "content_en_hash": hashlib.md5(source_text.encode("utf-8")).hexdigest(),
                    "content_ja_html": content_ja_html,
                    "translated_at": datetime.now(timezone.utc).isoformat(),
                }
                translated_count += 1
            except Exception as e:
                print(f"  ! translation error: {e}", flush=True)
                title_ja = title_en
                content_ja_html = original_body

        fe = fg.add_entry()
        fe.id(eid)
        fe.title(title_ja or "(タイトルなし)")
        link = entry.get("link") or src_link
        fe.link(href=link, rel="alternate")
        if entry.get("published"):
            try:
                fe.published(entry.published)
            except Exception:
                pass
        if entry.get("updated"):
            try:
                fe.updated(entry.updated)
            except Exception:
                pass
        if entry.get("author"):
            try:
                fe.author({"name": entry.author})
            except Exception:
                pass
        fe.content(content_ja_html, type="html")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"{fd.name}.xml"
    fg.atom_file(str(out_path), pretty=True)
    save_cache(fd.name, cache)

    msg = f"OK: {len(entries)} entries ({translated_count} translated, {len(entries) - translated_count} cached)"
    print(f"  {msg}", flush=True)
    return True, msg


# --------------------------------------------------------------------------- #
# index.html 生成
# --------------------------------------------------------------------------- #

INDEX_TEMPLATE = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>翻訳済み RSS フィード</title>
<style>
  :root {{ color-scheme: light; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Hiragino Sans", "Yu Gothic UI", sans-serif;
         max-width: 720px; margin: 40px auto; padding: 0 20px; color: #1a1a1a; line-height: 1.6; }}
  h1 {{ font-size: 22px; margin-bottom: 4px; }}
  .sub {{ color: #666; font-size: 13px; margin-bottom: 24px; }}
  .feed {{ border: 1px solid #ece8de; border-radius: 8px; padding: 14px 18px; margin-bottom: 12px; background: #fff; }}
  .feed h2 {{ font-size: 15px; margin: 0 0 6px; }}
  .feed .src {{ color: #888; font-size: 12px; }}
  .feed code {{ background: #f3efe7; padding: 4px 8px; border-radius: 4px;
               font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 12px;
               display: inline-block; margin-top: 6px; word-break: break-all; }}
  .feed .status {{ font-size: 12px; padding: 2px 8px; border-radius: 3px; }}
  .ok {{ background: #e3f4ea; color: #1d6f3a; }}
  .err {{ background: #fff4f0; color: #8a3219; }}
  .footer {{ color: #888; font-size: 12px; margin-top: 32px; }}
</style>
</head>
<body>
<h1>翻訳済み RSS フィード</h1>
<div class="sub">最終更新: {updated} ／ Reeder などの RSS リーダーに下記URLを登録してください。</div>
{rows}
<div class="footer">
  Generated by <a href="https://github.com/{repo_slug}">{repo_slug}</a> ／
  毎時自動更新 ／ 翻訳エンジン: Google Translate (deep-translator) → MyMemory フォールバック
</div>
</body>
</html>
"""

def write_index(rows: list[tuple[FeedDef, bool, str]], base_url: str, repo_slug: str) -> None:
    parts = []
    for fd, ok, msg in rows:
        cls = "ok" if ok else "err"
        feed_url = f"{base_url}/{fd.name}.xml" if base_url else f"{fd.name}.xml"
        parts.append(
            f'<div class="feed"><h2>{xml_escape(fd.title or fd.name)}'
            f' <span class="status {cls}">{xml_escape(msg)}</span></h2>'
            f'<div class="src">ソース: <a href="{xml_escape(fd.url)}">{xml_escape(fd.url)}</a></div>'
            f'<div>登録URL: <code>{xml_escape(feed_url)}</code></div>'
            "</div>"
        )
    html = INDEX_TEMPLATE.format(
        updated=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        rows="\n".join(parts),
        repo_slug=repo_slug or "your-username/rss-jp-translator",
    )
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "index.html").write_text(html, encoding="utf-8")


# --------------------------------------------------------------------------- #
# エントリポイント
# --------------------------------------------------------------------------- #

def main() -> int:
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    feed_defs = [FeedDef(**f) for f in config.get("feeds", [])]

    base_url = os.environ.get("PAGES_BASE_URL", "").rstrip("/")
    repo_slug = os.environ.get("GITHUB_REPOSITORY", "")

    results: list[tuple[FeedDef, bool, str]] = []
    failures = 0
    for fd in feed_defs:
        try:
            ok, msg = process_feed(fd)
        except Exception as e:
            traceback.print_exc()
            ok, msg = False, f"例外: {e}"
        results.append((fd, ok, msg))
        if not ok:
            failures += 1

    write_index(results, base_url, repo_slug)
    print(f"\n=== Done. failures: {failures}/{len(results)} ===", flush=True)
    # 1 つでも成功していれば exit 0（全失敗の時だけ非ゼロ）
    return 0 if failures < len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
