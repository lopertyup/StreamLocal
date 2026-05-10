from __future__ import annotations

import re
import unicodedata
import base64
import json
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote, quote_plus, unquote, urljoin, urlparse

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests

from ..proxy import DNS_OPTIONS


SCAN_MANGA_HEADERS: Dict[str, str] = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
}

website_origin = "https://www.scan-manga.com"
MOBILE_ORIGIN = "https://m.scan-manga.com"
HOME_URL = f"{website_origin}/?po"
STATIC_ORIGIN = "https://static.scan-manga.com"
SEARCH_API_URL = "https://bqj.scan-manga.com/search/quick.json"
CHAPTER_IMPERSONATE_PROFILES = ("chrome", "chrome136", "chrome124", "chrome120")


def _new_session(impersonate: str = "chrome"):
    return cffi_requests.Session(impersonate=impersonate, curl_options=DNS_OPTIONS)


scraper = _new_session()

CHAPTER_URL_RE = re.compile(
    r"/lecture-en-ligne/(?P<series>.+?)-Chapitre-(?P<chapter>.+?)-(?P<language>[A-Z]{2})_(?P<id>\d+)\.html",
    re.I,
)
IMAGE_URL_RE = re.compile(
    r"""(?P<quote>["'])?(?P<url>(?:https?:)?//[^"'\s<>]+?\.(?:jpe?g|png|webp|gif)(?:\?[^"'\s<>]*)?|/[^"'\s<>]+?\.(?:jpe?g|png|webp|gif)(?:\?[^"'\s<>]*)?)(?P=quote)?""",
    re.I,
)
PAID_MARKERS = (
    "abonnement",
    "acheter",
    "achat",
    "coins",
    "credit",
    "credits",
    "crédit",
    "crédits",
    "locked",
    "payant",
    "premium",
    "prix",
    "unlock",
    "verrouille",
    "verrouillé",
    "vip",
    "€",
)


class ScanMangaCloudflareError(RuntimeError):
    """Raised when Scan-Manga serves a Cloudflare challenge/block page."""


class ScanMangaPaidContentError(RuntimeError):
    """Raised when a chapter is marked as paid/premium."""


@dataclass
class ScanMangaManga:
    title: str
    detail_url: str
    cover_url: str = ""
    description: str = ""
    latest_chapter: str = ""
    latest_chapter_url: str = ""
    genres: List[str] = field(default_factory=list)
    status: str = "Scans"


@dataclass
class ScanMangaChapter:
    title: str
    url: str
    chapter: str = ""
    pages: int = 1


def _headers(
    referer: Optional[str] = None,
    accept: Optional[str] = None,
) -> Dict[str, str]:
    headers = dict(SCAN_MANGA_HEADERS)
    if accept:
        headers["Accept"] = accept
    if referer:
        headers["Referer"] = referer
    else:
        headers["Referer"] = HOME_URL
    return headers


def _get(url: str, **kwargs: Any):
    referer = kwargs.pop("referer", None)
    headers = kwargs.pop("headers", None) or _headers(referer)
    session = kwargs.pop("session", None) or scraper
    return session.get(url, headers=headers, timeout=kwargs.pop("timeout", 20), **kwargs)


def _post(url: str, **kwargs: Any):
    referer = kwargs.pop("referer", None)
    headers = kwargs.pop("headers", None) or _headers(referer)
    session = kwargs.pop("session", None) or scraper
    return session.post(url, headers=headers, timeout=kwargs.pop("timeout", 20), **kwargs)


def _get_html(url: str, **kwargs: Any):
    response = _get(url, **kwargs)
    _raise_for_cloudflare(response)
    response.raise_for_status()
    return response


def _api_headers(referer: str = HOME_URL) -> Dict[str, str]:
    return {
        "Accept": "*/*",
        "Accept-Language": SCAN_MANGA_HEADERS["Accept-Language"],
        "Content-Type": "application/json; charset=UTF-8",
        "Origin": website_origin,
        "Referer": referer,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
    }


def _raise_for_cloudflare(response: Any) -> None:
    headers = getattr(response, "headers", {}) or {}
    status_code = int(getattr(response, "status_code", 0) or 0)
    server = str(headers.get("server") or headers.get("Server") or "").lower()
    mitigated = str(headers.get("cf-mitigated") or headers.get("CF-Mitigated") or "").lower()
    cf_ray = headers.get("cf-ray") or headers.get("CF-Ray")
    text = str(getattr(response, "text", "") or "")
    text_sample = text[:5000].lower()
    challenge_markers = (
        "just a moment",
        "checking your browser",
        "/cdn-cgi/challenge-platform",
        "cf-chl",
        "cf-browser-verification",
    )
    if mitigated == "challenge":
        raise ScanMangaCloudflareError("Scan-Manga demande une verification Cloudflare.")
    if any(marker in text_sample for marker in challenge_markers):
        raise ScanMangaCloudflareError("Scan-Manga demande une verification Cloudflare.")
    if "cloudflare" in server and status_code in {403, 429, 500, 503}:
        if status_code == 500 and text.strip():
            return
        suffix = f" ({status_code})" if status_code else ""
        if cf_ray:
            suffix += f" cf-ray={cf_ray}"
        raise ScanMangaCloudflareError("Scan-Manga est bloque par Cloudflare" + suffix + ".")


def _absolute(url: str, base_url: str = website_origin) -> str:
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    return urljoin(base_url if base_url.endswith("/") else base_url + "/", url)


def _response_url(response: Any, fallback: str) -> str:
    return getattr(response, "url", None) or fallback


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html or "", "html.parser")


def _text(node: Any) -> str:
    if not node:
        return ""
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def _normalize(value: str) -> str:
    value = unicodedata.normalize("NFD", value or "")
    value = "".join(char for char in value if unicodedata.category(char) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _clean_title(value: str) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    value = re.sub(r"\s+[-|]\s+scan[- ]?manga.*$", "", value, flags=re.I)
    value = re.sub(r"\s+lecture\s+en\s+ligne.*$", "", value, flags=re.I)
    value = re.sub(r"\s+scan(?:s)?(?:\s+vf|\s+fr)?$", "", value, flags=re.I)
    return value.strip(" -")


def _title_from_slug(slug: str) -> str:
    return re.sub(r"\s+", " ", unquote(slug or "").replace("-", " ")).strip().title()


def _is_scan_manga_url(value: str) -> bool:
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    return parsed.scheme in {"http", "https"} and (host == "scan-manga.com" or host.endswith(".scan-manga.com"))


def _chapter_parts(url: str) -> Optional[Tuple[str, str, str]]:
    match = CHAPTER_URL_RE.search(urlparse(url).path)
    if not match:
        return None
    return (
        unquote(match.group("series")),
        unquote(match.group("chapter")),
        match.group("language").lower(),
    )


def _is_chapter_url(url: str) -> bool:
    return _chapter_parts(url) is not None


def _node_markers(node: Any) -> str:
    values: List[str] = []
    current = node
    depth = 0
    while current is not None and depth < 3:
        if getattr(current, "name", "") in {"body", "html"}:
            break
        values.append(_text(current))
        classes = current.get("class") if hasattr(current, "get") else None
        if classes:
            values.extend(str(item) for item in classes)
        for attr in ("id", "title", "aria-label", "data-status", "data-type"):
            value = current.get(attr) if hasattr(current, "get") else None
            if value:
                values.append(str(value))
        current = getattr(current, "parent", None)
        depth += 1
    return _normalize(" ".join(values))


def _is_paid_node(node: Any) -> bool:
    markers = _node_markers(node)
    return any(marker in markers for marker in PAID_MARKERS)


def _page_mentions_paid_content(soup: BeautifulSoup) -> bool:
    body = _normalize(_text(soup.body or soup))
    if not body:
        return False
    return any(marker in body for marker in PAID_MARKERS)


def _chapter_from_url(url: str, raw_title: str = "") -> Optional[ScanMangaChapter]:
    parts = _chapter_parts(url)
    if not parts:
        return None
    _series, chapter, language = parts
    title = _clean_title(raw_title)
    if not title or _normalize(title) == _normalize(chapter):
        title = f"Chapitre {chapter}"
    if language and language != "fr":
        title = f"{title} {language.upper()}"
    return ScanMangaChapter(title=title, url=url, chapter=chapter, pages=1)


def _chapter_sort_key(chapter: str):
    cleaned = str(chapter or "").replace(",", ".")
    match = re.search(r"\d+(?:\.\d+)?", cleaned)
    if match:
        try:
            return (0, float(match.group(0)), cleaned)
        except ValueError:
            pass
    return (1, cleaned)


def _dedupe_chapters(chapters: Iterable[ScanMangaChapter]) -> List[ScanMangaChapter]:
    deduped: List[ScanMangaChapter] = []
    seen = set()
    for chapter in chapters:
        key = chapter.url
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(chapter)
    return deduped


def _extract_chapters(soup: BeautifulSoup, base_url: str, sort: bool = True) -> List[ScanMangaChapter]:
    chapters: List[ScanMangaChapter] = []
    for node in soup.select('a[href*="/lecture-en-ligne/"], option[value*="/lecture-en-ligne/"]'):
        raw_url = str(node.get("href") or node.get("value") or "").strip()
        url = _absolute(raw_url, base_url)
        if not _is_chapter_url(url) or _is_paid_node(node):
            continue
        chapter = _chapter_from_url(url, _text(node))
        if chapter:
            chapters.append(chapter)

    chapters = _dedupe_chapters(chapters)
    if sort:
        chapters.sort(key=lambda item: _chapter_sort_key(item.chapter))
    return chapters


def _extract_title(soup: BeautifulSoup, fallback_url: str = "") -> str:
    for selector in (
        'h2[itemprop*="name"]',
        "h2",
        'meta[property="og:title"][content]',
        'meta[name="twitter:title"][content]',
        "h1",
        ".manga-title",
        ".entry-title",
        ".post-title",
        "title",
    ):
        node = soup.select_one(selector)
        if not node:
            continue
        value = str(node.get("content", "")) if hasattr(node, "get") and node.get("content") else _text(node)
        title = _clean_title(value)
        if title:
            parts = _chapter_parts(fallback_url)
            if parts and _normalize(parts[1]) in _normalize(title):
                return _title_from_slug(parts[0])
            return title

    parts = _chapter_parts(fallback_url)
    if parts:
        return _title_from_slug(parts[0])
    path = urlparse(fallback_url).path.strip("/").rsplit("/", 1)[-1]
    return _title_from_slug(re.sub(r"\.html?$", "", path, flags=re.I))


def _extract_cover_url(soup: BeautifulSoup, base_url: str) -> str:
    for selector in (
        'meta[property="og:image"][content]',
        'meta[name="twitter:image"][content]',
        ".manga-cover img[src]",
        ".cover img[src]",
        ".poster img[src]",
        'img[src*="cover"]',
        'img[src*="thumb"]',
    ):
        node = soup.select_one(selector)
        if not node:
            continue
        value = str(node.get("content") or node.get("data-src") or node.get("src") or "").strip()
        if value:
            return _absolute(value, base_url)
    return ""


def _extract_description(soup: BeautifulSoup) -> str:
    for selector in (
        'meta[name="description"][content]',
        ".description",
        ".synopsis",
        "#synopsis",
        ".manga-description",
    ):
        node = soup.select_one(selector)
        if not node:
            continue
        value = str(node.get("content", "")) if hasattr(node, "get") and node.get("content") else _text(node)
        value = re.sub(r"\s+", " ", value).strip()
        if value:
            return value
    return ""


def _extract_genres(soup: BeautifulSoup) -> List[str]:
    genres: List[str] = []
    for node in soup.select('a[href*="genre"], .genres a, .genre a, [rel="tag"]'):
        value = _clean_title(_text(node))
        if value and value not in genres:
            genres.append(value)
    return genres


def _extract_latest(chapters: List[ScanMangaChapter]) -> Tuple[str, str]:
    if not chapters:
        return "", ""
    latest = max(chapters, key=lambda item: _chapter_sort_key(item.chapter))
    return latest.chapter, latest.url


def _manga_from_detail_html(html: str, detail_url: str) -> ScanMangaManga:
    soup = _soup(html)
    chapters = _extract_chapters(soup, detail_url)
    title = _extract_title(soup, detail_url)
    latest_chapter, latest_chapter_url = _extract_latest(chapters)
    return ScanMangaManga(
        title=title,
        detail_url=detail_url,
        cover_url=_extract_cover_url(soup, detail_url),
        description=_extract_description(soup),
        latest_chapter=latest_chapter,
        latest_chapter_url=latest_chapter_url,
        genres=_extract_genres(soup),
    )


def _parse_manga_cards(soup: BeautifulSoup, base_url: str, query: str = "") -> List[ScanMangaManga]:
    mangas: List[ScanMangaManga] = []
    seen = set()
    normalized_query = _normalize(query)
    selectors = (
        'a[href*="/manga/"]',
        'a[href*="/mangas/"]',
        'a[href*=".html"]',
        'a[href*="/lecture-en-ligne/"]',
    )
    for anchor in soup.select(",".join(selectors)):
        if _is_paid_node(anchor):
            continue
        href = str(anchor.get("href", "")).strip()
        detail_url = _absolute(href, base_url)
        if not _is_scan_manga_url(detail_url):
            continue

        chapter = _chapter_from_url(detail_url, _text(anchor))
        if chapter:
            series, _chapter, _language = _chapter_parts(detail_url) or ("", "", "")
            title = _title_from_slug(series)
            latest_chapter = chapter.chapter
            latest_chapter_url = chapter.url
        else:
            title = _clean_title(_text(anchor))
            latest_chapter = ""
            latest_chapter_url = ""

        if not title:
            title = _extract_title(_soup(str(anchor.parent or "")), detail_url)
        if normalized_query and normalized_query not in _normalize(title) and _normalize(title) not in normalized_query:
            continue
        if not title or detail_url in seen:
            continue

        image = ""
        parent = anchor.parent
        if parent:
            image_node = parent.select_one("img[src], img[data-src]")
            if image_node:
                image = _absolute(str(image_node.get("data-src") or image_node.get("src") or ""), base_url)

        seen.add(detail_url)
        mangas.append(
            ScanMangaManga(
                title=title,
                detail_url=detail_url,
                cover_url=image,
                latest_chapter=latest_chapter,
                latest_chapter_url=latest_chapter_url,
            )
        )
    return mangas


def _status_label(value: Any) -> str:
    try:
        status = int(value)
    except (TypeError, ValueError):
        return "Scans"
    return {
        1: "En cours",
        2: "Termine",
        3: "Pause",
        4: "Abandonne",
        5: "A venir",
    }.get(status, "Scans")


def _search_sort_key(item: ScanMangaManga, query: str):
    title = _normalize(item.title)
    normalized_query = _normalize(query)
    if title == normalized_query:
        rank = 0
    elif title.startswith(normalized_query + " ") or title.startswith(normalized_query):
        rank = 1
    elif re.search(rf"\b{re.escape(normalized_query)}\b", title):
        rank = 2
    else:
        rank = 3
    try:
        latest = -float(str(item.latest_chapter).replace(",", "."))
    except (TypeError, ValueError):
        latest = 0.0
    return rank, latest, title


def _genre_names(data: Dict[str, Any], ids: Any) -> List[str]:
    if not isinstance(ids, list):
        return []
    genre_map = data.get("genre")
    if not isinstance(genre_map, dict):
        return []
    genres: List[str] = []
    for genre_id in ids:
        value = genre_map.get(str(genre_id))
        if value is None:
            value = genre_map.get(genre_id)
        if value:
            genres.append(str(value))
    return genres


def _cover_from_search_item(item: Dict[str, Any]) -> str:
    image = str(item.get("image") or item.get("logo") or "").strip()
    if not image:
        return ""
    if image.startswith(("http://", "https://", "//", "/")):
        return _absolute(image, STATIC_ORIGIN)
    return f"{STATIC_ORIGIN}/img/manga/{quote(image, safe='')}"


def _search_manga_api(query: str) -> List[ScanMangaManga]:
    response = _get(
        SEARCH_API_URL,
        params={"term": query},
        headers=_api_headers(website_origin + "/"),
    )
    _raise_for_cloudflare(response)
    response.raise_for_status()
    if not (response.text or "").strip():
        return []
    data = response.json()
    if not isinstance(data, dict):
        return []

    normalized_query = _normalize(query)
    results: List[ScanMangaManga] = []
    seen = set()
    for item in data.get("title") or []:
        if not isinstance(item, dict):
            continue
        title = _clean_title(str(item.get("nom_match") or item.get("nom") or ""))
        if not title or normalized_query not in _normalize(title):
            continue
        raw_url = str(item.get("url") or "").replace("\\/", "/").strip()
        detail_url = _absolute(raw_url, website_origin)
        if detail_url in seen or not _is_scan_manga_url(detail_url):
            continue
        latest_chapter = str(item.get("l_ch") or "").strip()
        seen.add(detail_url)
        results.append(
            ScanMangaManga(
                title=title,
                detail_url=detail_url,
                cover_url=_cover_from_search_item(item),
                latest_chapter=latest_chapter,
                genres=_genre_names(data, item.get("genre")),
                status=_status_label(item.get("statut")),
            )
        )
    results.sort(key=lambda item: _search_sort_key(item, query))
    return results


def search_manga(query: str) -> List[ScanMangaManga]:
    query = (query or "").strip()
    if not query:
        return []

    if _is_scan_manga_url(query):
        return [get_manga_info(query)]

    return _search_manga_api(query)


def get_manga_info(detail_url: str) -> ScanMangaManga:
    if not detail_url:
        raise ValueError("URL Scan-Manga manquante.")
    if not _is_scan_manga_url(detail_url):
        raise ValueError("URL Scan-Manga non autorisee.")

    source_url = _absolute(detail_url)
    response = _get_html(source_url)
    final_url = _response_url(response, source_url)
    manga = _manga_from_detail_html(response.text, final_url)
    if _is_chapter_url(final_url) and not manga.latest_chapter:
        chapter = _chapter_from_url(final_url)
        if chapter:
            manga.latest_chapter = chapter.chapter
            manga.latest_chapter_url = chapter.url
    return manga


def get_chapters(detail_url: str) -> List[ScanMangaChapter]:
    if not detail_url:
        raise ValueError("URL Scan-Manga manquante.")
    if not _is_scan_manga_url(detail_url):
        raise ValueError("URL Scan-Manga non autorisee.")

    source_url = _absolute(detail_url)
    response = _get_html(source_url)
    final_url = _response_url(response, source_url)
    soup = _soup(response.text)
    if _page_mentions_paid_content(soup) and _is_chapter_url(final_url):
        raise ScanMangaPaidContentError("Chapitre Scan-Manga payant.")

    chapters = _extract_chapters(soup, final_url)
    if not chapters and _is_chapter_url(final_url):
        chapter = _chapter_from_url(final_url, _extract_title(soup, final_url))
        if chapter:
            chapters = [chapter]
    return chapters


def _clean_image_url(url: str, base_url: str) -> str:
    absolute = _absolute(url, base_url)
    parsed = urlparse(absolute)
    return parsed._replace(fragment="").geturl()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _chapter_page_cache_path() -> Path:
    return _repo_root() / "data" / "scan_manga_chapter_pages_cache.json"


def _chapter_cache_key(chapter_url: str) -> str:
    return _clean_image_url(chapter_url, website_origin)


def _read_chapter_page_cache() -> Dict[str, Any]:
    path = _chapter_page_cache_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _cached_chapter_pages(chapter_url: str) -> List[str]:
    entry = _read_chapter_page_cache().get(_chapter_cache_key(chapter_url))
    if isinstance(entry, list):
        pages = entry
    elif isinstance(entry, dict):
        pages = entry.get("pages")
    else:
        pages = None
    if not isinstance(pages, list):
        return []
    return [str(url) for url in pages if _is_allowed_image_url(str(url))]


def _save_chapter_page_cache(chapter_url: str, pages: List[str]) -> None:
    clean_pages = [str(url) for url in pages if _is_allowed_image_url(str(url))]
    if not clean_pages:
        return
    path = _chapter_page_cache_path()
    try:
        data = _read_chapter_page_cache()
        key = _chapter_cache_key(chapter_url)
        data[key] = {
            "chapter_url": key,
            "pages": clean_pages,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(path)
    except Exception:
        return


def _extract_page_images(soup: BeautifulSoup, base_url: str) -> List[str]:
    images: List[str] = []
    seen = set()
    selectors = (
        "#readerarea img",
        "#reader img",
        "#image img",
        ".reading-content img",
        ".chapter-content img",
        ".page-break img",
        ".scan-page img",
    )
    for image in soup.select(",".join(selectors)):
        if _is_paid_node(image):
            continue
        raw_url = str(
            image.get("data-src")
            or image.get("data-lazy-src")
            or image.get("data-original")
            or image.get("src")
            or ""
        ).strip()
        if not raw_url or raw_url.startswith("data:"):
            continue
        url = _clean_image_url(raw_url, base_url)
        path = urlparse(url).path.lower()
        if not re.search(r"\.(?:jpe?g|png|webp|gif)$", path):
            continue
        if url in seen:
            continue
        seen.add(url)
        images.append(url)
    return images


def _base64_decode(value: str) -> bytes:
    return base64.b64decode(value + "=" * (-len(value) % 4))


def _decode_base_number(value: str, source_base: int) -> int:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ+/"
    digits = alphabet[:source_base]
    result = 0
    for power, char in enumerate(reversed(value)):
        if char in digits:
            result += digits.index(char) * (source_base ** power)
    return result


def _decode_inline_payload(script_text: str) -> str:
    quote = '"'
    marker = "decodeURIComponent"
    start = script_text.rfind(marker)
    if start < 0:
        return ""
    try:
        after = script_text[start:].split("}(" + quote, 1)[1]
        encoded, rest = after.split(quote + ",", 1)
    except (IndexError, ValueError):
        return ""
    match = re.match(r'(\d+),"([^"]+)",(\d+),(\d+),(\d+)', rest)
    if not match:
        return ""

    _width, key, offset, source_base, _target_base = match.groups()
    offset_int = int(offset)
    source_base_int = int(source_base)
    if source_base_int >= len(key):
        return ""
    delimiter = key[source_base_int]

    output = []
    index = 0
    while index < len(encoded):
        chunk = []
        while index < len(encoded) and encoded[index] != delimiter:
            chunk.append(encoded[index])
            index += 1
        if index >= len(encoded):
            break
        index += 1
        value = "".join(chunk)
        for replacement, char in enumerate(key):
            value = value.replace(char, str(replacement))
        try:
            output.append(chr(_decode_base_number(value, source_base_int) - offset_int))
        except (OverflowError, ValueError):
            return ""
    return "".join(output)


def _chapter_numeric_id(chapter_url: str) -> str:
    match = CHAPTER_URL_RE.search(urlparse(chapter_url).path)
    return match.group("id") if match else ""


def _extract_reader_tokens(soup: BeautifulSoup, chapter_url: str) -> Tuple[str, str, str]:
    chapter_id = _chapter_numeric_id(chapter_url)
    sml = ""
    sme = ""
    for script in soup.select("script"):
        script_text = script.get_text("", strip=False)
        if not script_text:
            continue
        if not chapter_id:
            id_match = re.search(r"\bconst\s+idc\s*=\s*(\d+)", script_text)
            if id_match:
                chapter_id = id_match.group(1)
        decoded = _decode_inline_payload(script_text)
        if decoded and "DataAPI" in decoded:
            sml_match = re.search(r"var\s+sml\s*=\s*'([^']+)'", decoded)
            sme_match = re.search(r"var\s+sme\s*=\s*'([^']+)'", decoded)
            if sml_match:
                sml = sml_match.group(1)
            if sme_match:
                sme = sme_match.group(1)
    if not chapter_id or not sml or not sme:
        raise RuntimeError("Donnees lecteur Scan-Manga introuvables.")
    return chapter_id, sml, sme


def _decode_chapter_payload(payload: str, chapter_id: str) -> Dict[str, Any]:
    inflated = zlib.decompress(_base64_decode(payload.strip())).decode("utf-8")
    cleaned = re.sub(re.escape(format(int(chapter_id), "x")) + r"$", "", inflated)
    decoded = _base64_decode(cleaned[::-1]).decode("utf-8")
    data = json.loads(decoded)
    if not isinstance(data, dict):
        raise RuntimeError("Donnees lecteur Scan-Manga invalides.")
    return data


def _fetch_chapter_data(chapter_url: str, html: str = "", session: Any = None) -> Dict[str, Any]:
    soup = _soup(html) if html else _soup(_get_html(chapter_url, session=session).text)
    chapter_id, sml, sme = _extract_reader_tokens(soup, chapter_url)
    endpoint = f"https://bqj.scan-manga.com/lel/{chapter_id}.json"
    body = {
        "a": sme,
        "b": sml,
        "c": base64.b64encode(
            json.dumps({"gpu": "IC", "connection": "IC"}, separators=(",", ":")).encode("utf-8")
        ).decode("ascii"),
    }
    response = _post(
        endpoint,
        json=body,
        referer=chapter_url,
        headers={
            **_api_headers(chapter_url),
            "Token": "yf",
            "source": chapter_url,
        },
        session=session,
    )
    _raise_for_cloudflare(response)
    response.raise_for_status()
    text = (response.text or "").strip()
    if text == "[]" or not text:
        raise ScanMangaPaidContentError("Chapitre Scan-Manga indisponible.")
    try:
        error_payload = json.loads(text)
        if isinstance(error_payload, dict) and error_payload.get("error"):
            raise ScanMangaPaidContentError(str(error_payload.get("error")))
    except json.JSONDecodeError:
        pass
    return _decode_chapter_payload(text, chapter_id)


def _pages_from_chapter_data(data: Dict[str, Any]) -> List[str]:
    pages = data.get("p")
    if not isinstance(pages, dict):
        return []
    domain = str(data.get("dN") or data.get("dC") or "").strip()
    if not domain:
        return []
    base_url = domain if domain.startswith(("http://", "https://")) else f"https://{domain}"
    path_prefix = "/".join(
        quote(str(part).strip("/"), safe="")
        for part in (data.get("s"), data.get("v"), data.get("c"))
        if part is not None and str(part).strip("/")
    )
    if not path_prefix:
        return []

    def sort_key(value: str):
        try:
            return (0, int(value))
        except (TypeError, ValueError):
            return (1, str(value))

    urls: List[str] = []
    for key in sorted(pages.keys(), key=sort_key):
        page = pages.get(key)
        if not isinstance(page, dict):
            continue
        filename = str(page.get("f") or "").strip()
        extension = str(page.get("e") or "jpg").strip().lstrip(".")
        if not filename:
            continue
        urls.append(f"{base_url}/{path_prefix}/{quote(filename, safe='')}.{extension}")
    return urls


def _get_pages_once(chapter_url: str, session: Any = None) -> List[str]:
    source_url = _absolute(chapter_url)
    response = _get_html(source_url, session=session)
    final_url = _response_url(response, source_url)
    soup = _soup(response.text)
    reader_error: Optional[Exception] = None
    try:
        pages = _pages_from_chapter_data(_fetch_chapter_data(final_url, response.text, session=session))
        if pages:
            _save_chapter_page_cache(final_url, pages)
            return pages
    except ScanMangaPaidContentError:
        raise
    except Exception as exc:
        reader_error = exc

    pages = _extract_page_images(soup, final_url)
    if pages:
        return pages
    if not pages and _page_mentions_paid_content(soup):
        raise ScanMangaPaidContentError("Chapitre Scan-Manga payant.")
    if reader_error is not None:
        raise reader_error
    return []


def get_pages(chapter_url: str) -> List[str]:
    if not chapter_url:
        raise ValueError("URL du chapitre Scan-Manga manquante.")
    if not _is_scan_manga_url(chapter_url):
        raise ValueError("URL Scan-Manga non autorisee.")

    source_url = _absolute(chapter_url)
    try:
        return _get_pages_once(source_url)
    except ScanMangaPaidContentError:
        raise
    except ScanMangaCloudflareError as first_error:
        last_error: Exception = first_error
        for profile in CHAPTER_IMPERSONATE_PROFILES:
            try:
                return _get_pages_once(source_url, session=_new_session(profile))
            except ScanMangaPaidContentError:
                raise
            except ScanMangaCloudflareError as retry_error:
                last_error = retry_error
                continue
        cached_pages = _cached_chapter_pages(source_url)
        if cached_pages:
            return cached_pages
        raise last_error


def _is_allowed_image_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme in {"http", "https"} and (host == "scan-manga.com" or host.endswith(".scan-manga.com"))


def fetch_image(url: str):
    if not _is_allowed_image_url(url):
        raise ValueError("URL image Scan-Manga non autorisee.")
    return _get(
        url,
        stream=True,
        referer=website_origin + "/",
        headers={
            "Accept": "*/*",
            "Accept-Language": SCAN_MANGA_HEADERS["Accept-Language"],
            "Origin": website_origin,
            "Referer": website_origin + "/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "cross-site",
        },
        timeout=25,
    )
