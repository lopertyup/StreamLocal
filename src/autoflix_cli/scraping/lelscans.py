from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests

from ..proxy import DNS_OPTIONS


LELSCANS_HEADERS: Dict[str, str] = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}

website_origin = "https://lelscans.net"
INDEX_URL = f"{website_origin}/lecture-en-ligne-one-piece"

scraper = cffi_requests.Session(impersonate="chrome", curl_options=DNS_OPTIONS)


@dataclass
class LelScansManga:
    title: str
    detail_url: str
    cover_url: str = ""
    latest_chapter: str = ""
    latest_chapter_url: str = ""
    genres: List[str] = field(default_factory=list)
    status: str = "Scans"


@dataclass
class LelScansChapter:
    title: str
    url: str
    chapter: str = ""
    pages: int = 1


def _headers(referer: Optional[str] = None) -> Dict[str, str]:
    headers = dict(LELSCANS_HEADERS)
    if referer:
        headers["Referer"] = referer
    return headers


def _get(url: str, **kwargs: Any):
    referer = kwargs.pop("referer", None)
    headers = kwargs.pop("headers", None) or _headers(referer)
    return scraper.get(url, headers=headers, timeout=kwargs.pop("timeout", 20), **kwargs)


def _absolute(url: str, base_url: str = website_origin) -> str:
    if not url:
        return ""
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
    value = re.sub(r"^lecture\s+(?:en\s+)?ligne\s+", "", value, flags=re.I)
    value = re.sub(r"\s+lecture\s+(?:en\s+)?ligne.*$", "", value, flags=re.I)
    value = re.sub(r"\s+scan(?:s)?(?:\s+\d+(?:\.\d+)?)?$", "", value, flags=re.I)
    return value.strip(" -")


def _title_from_slug(slug: str) -> str:
    return re.sub(r"\s+", " ", (slug or "").replace("-", " ")).strip().title()


def _is_lelscans_url(value: str) -> bool:
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    return parsed.scheme in {"http", "https"} and host.endswith("lelscans.net")


def _chapter_parts(url: str) -> Optional[Tuple[str, str]]:
    parts = [part for part in urlparse(url).path.split("/") if part]
    if len(parts) != 2 or not parts[0].startswith("scan-"):
        return None
    return parts[0][5:], unquote(parts[1])


def _slug_from_detail_url(url: str) -> str:
    path = urlparse(url).path.strip("/")
    if path.endswith(".php"):
        path = path[:-4]
    for prefix in ("lecture-en-ligne-", "lecture-ligne-"):
        if path.startswith(prefix):
            return path[len(prefix) :]
    return ""


def _cover_url_for_slug(slug: str) -> str:
    return f"{website_origin}/mangas/{slug}/thumb_cover.jpg" if slug else ""


def _detail_url_from_slug(slug: str) -> str:
    if not slug:
        return ""
    if slug == "one-piece":
        return f"{website_origin}/lecture-en-ligne-{slug}"
    return f"{website_origin}/lecture-ligne-{slug}.php"


def _extract_title(soup: BeautifulSoup, slug: str = "") -> str:
    meta = soup.select_one('meta[name="lelscan"][content]')
    if meta and meta.get("content"):
        return _clean_title(str(meta["content"]))

    og_title = soup.select_one('meta[property="og:title"][content]')
    if og_title and og_title.get("content"):
        title = _clean_title(str(og_title["content"]))
        if title:
            return title

    for link in soup.select('a[href*="lecture"]'):
        title = _clean_title(_text(link))
        if title and (not slug or _normalize(slug) in _normalize(title) or _normalize(title) in _normalize(slug)):
            return title

    if soup.title and soup.title.string:
        title = _clean_title(soup.title.string)
        title = re.sub(r"\s+lecture\s+(?:en\s+)?ligne.*$", "", title, flags=re.I).strip()
        if title:
            return title

    return _title_from_slug(slug)


def _extract_cover_url(soup: BeautifulSoup, base_url: str, slug: str = "") -> str:
    for selector in (
        'meta[property="og:image"][content]',
        'meta[name="twitter:image"][content]',
    ):
        node = soup.select_one(selector)
        if node and node.get("content"):
            return _absolute(str(node["content"]), base_url)

    image = soup.select_one('img[src*="thumb_cover"]') or soup.select_one("#header-image img[src]")
    if image and image.get("src"):
        return _absolute(str(image["src"]), base_url)
    return _cover_url_for_slug(slug)


def _find_series_detail_url(soup: BeautifulSoup, base_url: str, slug: str = "") -> str:
    for link in soup.select('a[href*="lecture"]'):
        href = _absolute(str(link.get("href", "")), base_url)
        if not href:
            continue
        path = urlparse(href).path.lower()
        if "lecture" not in path:
            continue
        if slug and slug not in path:
            continue
        return href
    return _detail_url_from_slug(slug)


def _chapter_sort_key(chapter: str):
    try:
        return (0, float(str(chapter).replace(",", ".")))
    except (TypeError, ValueError):
        return (1, str(chapter))


def _chapter_title(chapter: str, raw_title: str = "") -> str:
    raw_title = _clean_title(raw_title)
    if raw_title and raw_title != chapter:
        return raw_title
    return f"Chapitre {chapter}" if chapter else "Chapitre"


def _chapter_from_option(option: Any, base_url: str) -> Optional[LelScansChapter]:
    value = str(option.get("value", "")).strip()
    url = _absolute(value, base_url)
    parts = _chapter_parts(url)
    if not parts:
        return None
    _slug, chapter = parts
    title = _chapter_title(chapter, _text(option))
    return LelScansChapter(title=title, url=url, chapter=chapter, pages=1)


def _chapter_from_anchor(anchor: Any, base_url: str) -> Optional[LelScansChapter]:
    href = str(anchor.get("href", "")).strip()
    url = _absolute(href, base_url)
    parts = _chapter_parts(url)
    if not parts:
        return None
    _slug, chapter = parts
    title = _chapter_title(chapter, _text(anchor))
    return LelScansChapter(title=title, url=url, chapter=chapter, pages=1)


def _dedupe_chapters(chapters: List[LelScansChapter]) -> List[LelScansChapter]:
    deduped: List[LelScansChapter] = []
    seen = set()
    for chapter in chapters:
        key = chapter.url
        if key in seen:
            continue
        seen.add(key)
        deduped.append(chapter)
    return deduped


def _parse_chapters(soup: BeautifulSoup, base_url: str, sort: bool = True) -> List[LelScansChapter]:
    chapters: List[LelScansChapter] = []
    for option in soup.select("select option[value]"):
        chapter = _chapter_from_option(option, base_url)
        if chapter:
            chapters.append(chapter)

    if not chapters:
        for anchor in soup.select("a[href]"):
            chapter = _chapter_from_anchor(anchor, base_url)
            if chapter:
                chapters.append(chapter)

    chapters = _dedupe_chapters(chapters)
    if sort:
        chapters.sort(key=lambda item: _chapter_sort_key(item.chapter))
    return chapters


def _parse_manga_list(soup: BeautifulSoup, base_url: str) -> List[LelScansManga]:
    mangas: List[LelScansManga] = []
    seen = set()
    for option in soup.select("select option[value]"):
        detail_url = _absolute(str(option.get("value", "")).strip(), base_url)
        path = urlparse(detail_url).path.lower()
        if "lecture" not in path:
            continue
        if detail_url in seen:
            continue
        seen.add(detail_url)
        slug = _slug_from_detail_url(detail_url)
        title = _clean_title(_text(option)) or _title_from_slug(slug)
        if not title:
            continue
        mangas.append(
            LelScansManga(
                title=title,
                detail_url=detail_url,
                cover_url=_cover_url_for_slug(slug),
            )
        )
    return mangas


def _latest_chapter(soup: BeautifulSoup, base_url: str) -> Tuple[str, str]:
    chapters = _parse_chapters(soup, base_url, sort=False)
    if not chapters:
        return "", ""
    return chapters[0].chapter, chapters[0].url


def search_manga(query: str) -> List[LelScansManga]:
    query = (query or "").strip()
    if not query:
        return []

    if _is_lelscans_url(query):
        return [get_manga_info(query)]

    response = _get(INDEX_URL)
    response.raise_for_status()
    base_url = _response_url(response, INDEX_URL)
    items = _parse_manga_list(_soup(response.text), base_url)
    normalized_query = _normalize(query)
    return [
        item
        for item in items
        if normalized_query in _normalize(item.title) or _normalize(item.title) in normalized_query
    ]


def get_manga_info(detail_url: str) -> LelScansManga:
    if not detail_url:
        raise ValueError("URL Lelscans manquante.")

    source_url = _absolute(detail_url)
    response = _get(source_url)
    response.raise_for_status()
    final_url = _response_url(response, source_url)
    soup = _soup(response.text)

    chapter_parts = _chapter_parts(final_url) or _chapter_parts(source_url)
    slug = chapter_parts[0] if chapter_parts else _slug_from_detail_url(final_url) or _slug_from_detail_url(source_url)
    title = _extract_title(soup, slug)
    series_url = _find_series_detail_url(soup, final_url, slug)
    if not series_url and not chapter_parts:
        series_url = final_url
    cover_url = _extract_cover_url(soup, final_url, slug)
    latest_chapter, latest_chapter_url = _latest_chapter(soup, final_url)

    return LelScansManga(
        title=title,
        detail_url=series_url or final_url,
        cover_url=cover_url,
        latest_chapter=latest_chapter,
        latest_chapter_url=latest_chapter_url,
    )


def get_chapters(detail_url: str) -> List[LelScansChapter]:
    if not detail_url:
        raise ValueError("URL Lelscans manquante.")

    source_url = _absolute(detail_url)
    response = _get(source_url)
    response.raise_for_status()
    final_url = _response_url(response, source_url)
    return _parse_chapters(_soup(response.text), final_url, sort=True)


def _navigation_count(soup: BeautifulSoup, base_url: str) -> int:
    page_numbers = set()
    for node in soup.select("#navigation a[href], #navigation option[value]"):
        label = _text(node)
        if label.isdigit():
            page_numbers.add(int(label))
            continue
        raw_url = str(node.get("href") or node.get("value") or "")
        url = _absolute(raw_url, base_url)
        parts = [part for part in urlparse(url).path.split("/") if part]
        if len(parts) >= 3 and parts[-1].isdigit():
            page_numbers.add(int(parts[-1]))
    return max(page_numbers) if page_numbers else 1


def _first_page_image(soup: BeautifulSoup, base_url: str) -> str:
    image = soup.select_one("#image img[src]") or soup.select_one('img[src*="/mangas/"]')
    if not image or not image.get("src"):
        return ""
    url = _absolute(str(image["src"]), base_url)
    parsed = urlparse(url)
    return parsed._replace(query="", fragment="").geturl()


def get_pages(chapter_url: str) -> List[str]:
    if not chapter_url:
        raise ValueError("URL du chapitre Lelscans manquante.")

    source_url = _absolute(chapter_url)
    response = _get(source_url)
    response.raise_for_status()
    final_url = _response_url(response, source_url)
    soup = _soup(response.text)

    first_image = _first_page_image(soup, final_url)
    if not first_image:
        return []

    page_count = _navigation_count(soup, final_url)
    match = re.match(r"^(?P<base>.*/)(?P<number>\d+)(?P<ext>\.[A-Za-z0-9]+)$", first_image)
    if not match:
        return [first_image]

    width = len(match.group("number"))
    base = match.group("base")
    ext = match.group("ext")
    return [f"{base}{index:0{width}d}{ext}" for index in range(page_count)]
