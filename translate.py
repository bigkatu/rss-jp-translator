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
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, unquote, urljoin, urlparse, urlunparse
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
NINTENDO_SALE_PAGES = 5
NINTENDO_SALE_HISTORY_LIMIT = 50
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
    '[data-test-selector="body-content"]',
    ".markdown-body",
    "article",
    "[role='main']",
    ".post-content",
    ".post-body",
    ".entry-content",
    ".article-content",
]

TAG_LIKE_TITLE_RE = re.compile(
    r"^(?:[A-Za-z0-9_.-]+-)?v?\d+(?:\.\d+){1,3}(?:[-+][A-Za-z0-9_.-]+)?$"
)


def should_translate_title(title: str) -> bool:
    """リリースタグ名のような機械的なタイトルは翻訳しない。"""
    return not TAG_LIKE_TITLE_RE.match((title or "").strip())


def is_low_signal_entry(title: str, text: str) -> bool:
    """タグ名だけ、または内部修正のみのリリースは配信しない。"""
    compact = re.sub(r"\s+", " ", (text or "")).strip()
    compact = compact.replace("• ", "").strip()
    title = (title or "").strip()
    if not TAG_LIKE_TITLE_RE.match(title):
        return False
    if not compact:
        return True
    if re.fullmatch(r"Release\s+\S+", compact, flags=re.IGNORECASE):
        return True
    if compact.lower() in {"internal fixes", "what's changed internal fixes", "whats changed internal fixes"}:
        return True
    return len(compact) < 40


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

    host = urlparse(url).netloc.lower()
    selectors = ARTICLE_SELECTORS
    if host == "github.com" or host.endswith(".github.com"):
        selectors = ['[data-test-selector="body-content"]', ".markdown-body"]

    candidates = []
    for selector in selectors:
        candidates.extend(soup.select(selector))

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
    type: str = "rss"

    @classmethod
    def from_config(cls, data: dict) -> "FeedDef":
        return cls(
            name=data["name"],
            url=data["url"],
            title=data.get("title"),
            type=data.get("type", "rss"),
        )


@dataclass
class SaleProduct:
    product_id: str
    name: str
    sale_label: str
    sale_price: Optional[int]
    url: str
    image_url: str = ""
    manufacturer: str = ""
    variation: str = ""

    @property
    def signature(self) -> str:
        payload = {
            "name": self.name,
            "sale_label": self.sale_label,
            "sale_price": self.sale_price,
            "url": self.url,
        }
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _walk_json(value: Any):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def extract_initial_json(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script", {"type": "application/json"}):
        text = script.string or script.get_text()
        if "__PRELOADED_STATE__" in text:
            return json.loads(text)
    raise ValueError("Nintendo Store initial JSON not found")


def extract_sale_products(data: dict) -> list[SaleProduct]:
    product_lists: list[list[dict]] = []
    for value in _walk_json(data):
        if not isinstance(value, list):
            continue
        products = [
            item for item in value
            if isinstance(item, dict)
            and item.get("name")
            and item.get("saleLabel")
            and item.get("variationMasterId")
        ]
        if products:
            product_lists.append(products)

    if not product_lists:
        return []

    raw_products = max(product_lists, key=len)
    products: list[SaleProduct] = []
    for item in raw_products:
        product_id = str(item.get("variationMasterId") or item.get("id") or "")
        if not product_id:
            continue
        image_url = ""
        image_data = item.get("imageUrl")
        if isinstance(image_data, dict):
            image_url = image_data.get("squareHeroBanner") or image_data.get("heroBanner") or ""
        products.append(
            SaleProduct(
                product_id=product_id,
                name=str(item.get("name") or ""),
                sale_label=str(item.get("saleLabel") or ""),
                sale_price=item.get("salePrice"),
                url=f"https://store-jp.nintendo.com/item/software/{product_id}",
                image_url=image_url,
                manufacturer=str(item.get("manufacturerName") or ""),
                variation=str(item.get("variation") or ""),
            )
        )
    return products


def fetch_nintendo_sale_products(url: str) -> list[SaleProduct]:
    session = requests.Session()
    headers = {"User-Agent": USER_AGENT}
    resp = session.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    if "__PRELOADED_STATE__" not in resp.text:
        match = re.search(r"document\.location\.href = decodeURIComponent\('([^']+)'\)", resp.text)
        if match:
            redirect_url = urljoin(resp.url, unquote(match.group(1)))
            host = urlparse(resp.url).hostname or "store-jp.nintendo.com"
            session.cookies.set("cookietest", "1", domain=host, path="/")
            resp = session.get(redirect_url, headers=headers, timeout=30)
            resp.raise_for_status()
    return extract_sale_products(extract_initial_json(resp.text))


def url_with_page(url: str, page: int) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["page"] = str(page)
    return urlunparse(parsed._replace(query=urlencode(query)))


def fetch_nintendo_sale_pages(url: str, pages: int = NINTENDO_SALE_PAGES) -> list[SaleProduct]:
    products: list[SaleProduct] = []
    seen_ids: set[str] = set()
    for page in range(1, pages + 1):
        page_url = url_with_page(url, page)
        page_products = fetch_nintendo_sale_products(page_url)
        print(f"  page {page}: {len(page_products)} products", flush=True)
        for product in page_products:
            if product.product_id in seen_ids:
                continue
            seen_ids.add(product.product_id)
            products.append(product)
        time.sleep(0.5)
    return products


def process_nintendo_sale_feed(fd: FeedDef) -> tuple[bool, str]:
    print(f"\n=== {fd.name}: {fd.url}", flush=True)
    cache = load_cache(fd.name)
    seen: dict[str, str] = cache.get("seen", {})
    history: list[dict] = cache.get("history", [])

    products = fetch_nintendo_sale_pages(fd.url)
    if not products:
        msg = "商品が取得できませんでした"
        print(f"  ! {msg}", flush=True)
        return False, msg

    now = datetime.now(timezone.utc)
    changes: list[dict] = []
    current_seen: dict[str, str] = {}
    for product in products:
        current_seen[product.product_id] = product.signature
        if seen.get(product.product_id) == product.signature:
            continue
        entry = {
            "id": f"{product.product_id}:{product.signature}",
            "product_id": product.product_id,
            "title": f"{product.name} {product.sale_label}",
            "name": product.name,
            "sale_label": product.sale_label,
            "sale_price": product.sale_price,
            "url": product.url,
            "image_url": product.image_url,
            "manufacturer": product.manufacturer,
            "variation": product.variation,
            "detected_at": now.isoformat(),
        }
        changes.append(entry)

    if changes:
        old_ids = {entry.get("id") for entry in changes}
        history = changes + [entry for entry in history if entry.get("id") not in old_ids]
        history = history[:NINTENDO_SALE_HISTORY_LIMIT]

    fg = FeedGenerator()
    fg.id(fd.url)
    fg.title(fd.title or "Nintendo Store セール差分")
    fg.link(href=fd.url, rel="alternate")
    fg.subtitle("My Nintendo Store セール中ソフトの新着・割引変更")
    fg.language("ja")
    fg.updated(now)

    for entry in history[:NINTENDO_SALE_HISTORY_LIMIT]:
        fe = fg.add_entry()
        fe.id(f"nintendo-sale:{entry['id']}")
        fe.title(entry["title"])
        fe.link(href=entry["url"], rel="alternate")
        fe.updated(entry.get("detected_at") or now)
        price = entry.get("sale_price")
        rows = [
            f"<p><strong>{xml_escape(entry['name'])}</strong></p>",
            f"<p>{xml_escape(entry.get('sale_label') or '')}</p>",
        ]
        if price is not None:
            rows.append(f"<p>価格: {price:,} 円</p>")
        if entry.get("manufacturer"):
            rows.append(f"<p>メーカー: {xml_escape(entry['manufacturer'])}</p>")
        if entry.get("variation"):
            rows.append(f"<p>{xml_escape(entry['variation'])}</p>")
        if entry.get("image_url"):
            rows.append(f'<p><img src="{xml_escape(entry["image_url"])}" alt="{xml_escape(entry["name"])}"></p>')
        rows.append(f'<p><a href="{xml_escape(entry["url"])}">My Nintendo Storeで開く</a></p>')
        fe.content("".join(rows), type="html")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fg.atom_file(str(OUT_DIR / f"{fd.name}.xml"), pretty=True)
    save_cache(fd.name, {"seen": current_seen, "history": history})

    msg = f"OK: {len(products)} products ({len(changes)} changes, {len(history)} history)"
    print(f"  {msg}", flush=True)
    return True, msg


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
    skipped_count = 0
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
        if is_low_signal_entry(title_en, source_text):
            print(f"  -> skipping low-signal release: {title_en[:80]}", flush=True)
            skipped_count += 1
            continue

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
                title_ja = translate(title_en) if title_en and should_translate_title(title_en) else title_en
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

    emitted_count = len(entries) - skipped_count
    msg = f"OK: {emitted_count} entries ({translated_count} translated, {emitted_count - translated_count} cached, {skipped_count} skipped)"
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
    feed_defs = [FeedDef.from_config(f) for f in config.get("feeds", [])]

    base_url = os.environ.get("PAGES_BASE_URL", "").rstrip("/")
    repo_slug = os.environ.get("GITHUB_REPOSITORY", "")

    results: list[tuple[FeedDef, bool, str]] = []
    failures = 0
    for fd in feed_defs:
        try:
            if fd.type == "nintendo_sale":
                ok, msg = process_nintendo_sale_feed(fd)
            else:
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
