from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import quote, quote_plus, urljoin, urlparse

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests

from ..proxy import DNS_OPTIONS
from .config import portals


ANIME_SAMA_HEADERS: Dict[str, str] = {
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

website_origin = ""
scraper = cffi_requests.Session(impersonate="chrome", curl_options=DNS_OPTIONS)


@dataclass
class MangaInfo:
    title: str
    catalogue_url: str
    scan_url: str
    cover_url: str = ""
    slug: str = ""
    exact_title: str = ""
    genres: List[str] = field(default_factory=list)
    languages: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]


def _origin(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return get_website_url()
    return f"{parsed.scheme}://{parsed.netloc}"


def _headers(referer: Optional[str] = None, accept: Optional[str] = None) -> Dict[str, str]:
    headers = dict(ANIME_SAMA_HEADERS)
    if accept:
        headers["Accept"] = accept
    if referer:
        headers["Referer"] = referer
    elif website_origin:
        headers["Referer"] = website_origin + "/"
    return headers


def _get(url: str, **kwargs: Any):
    referer = kwargs.pop("referer", None)
    headers = kwargs.pop("headers", None) or _headers(referer)
    return scraper.get(url, headers=headers, timeout=kwargs.pop("timeout", 20), **kwargs)


def get_website_url(portal: Optional[str] = None) -> str:
    global website_origin

    if website_origin:
        return website_origin

    portal = portal or portals.get("anime-sama") or "https://anime-sama.to"
    if not portal.startswith(("http://", "https://")):
        portal = "https://" + portal

    response = _get(portal)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html5lib")
    recommended = soup.select_one("a.btn-primary[href]")
    final_url = recommended["href"] if recommended else response.url

    try:
        head = scraper.head(final_url, headers=_headers(portal), timeout=12, allow_redirects=True)
        if getattr(head, "url", None):
            final_url = head.url
    except Exception:
        try:
            landing = _get(final_url, referer=portal, timeout=12)
            final_url = landing.url
        except Exception:
            pass

    parsed = urlparse(final_url)
    if not parsed.scheme or not parsed.netloc:
        raise RuntimeError("Impossible de resoudre le domaine Anime-Sama.")
    website_origin = f"{parsed.scheme}://{parsed.netloc}"
    return website_origin


def _slug_from_url(url: str) -> str:
    parts = [part for part in urlparse(url).path.split("/") if part]
    try:
        index = parts.index("catalogue")
        return parts[index + 1]
    except (ValueError, IndexError):
        return ""


def _is_scan_catalogue_url(url: str) -> bool:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    return any(part.startswith("scan") for part in parts[2:]) if len(parts) >= 3 and parts[0] == "catalogue" else False


def _catalogue_from_scan_url(url: str) -> str:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 3 or parts[0] != "catalogue":
        return url
    scan_index = next((index for index, part in enumerate(parts) if index >= 2 and part.startswith("scan")), None)
    if scan_index is None:
        return url
    path = "/" + "/".join(parts[:scan_index]).rstrip("/") + "/"
    return parsed._replace(path=path, query="", fragment="").geturl()


def _scan_url_from_catalogue(url: str) -> str:
    base = url.rstrip("/") + "/"
    return urljoin(base, "scan/vf/")


def _ensure_trailing_slash(url: str) -> str:
    return url if url.endswith("/") else url + "/"


def _text(node: Any, strip: bool = True) -> str:
    if not node:
        return ""
    value = node.get_text("", strip=False)
    return value.strip() if strip else value


def _split_values(value: str) -> List[str]:
    if not value:
        return []
    return [
        item.strip()
        for item in re.split(r"[,/|-]", value)
        if item and item.strip()
    ]


def _info_value(card: Any, label: str) -> str:
    target = label.strip().lower()
    for row in card.select(".info-row"):
        row_label = row.select_one(".info-label")
        if row_label and _text(row_label).rstrip(":").lower() == target:
            value = row.select_one(".info-value")
            return _text(value)
    return ""


def _has_scans(card: Any) -> bool:
    types = _info_value(card, "Types").lower()
    if "scans" in types:
        return True
    for value in card.select(".info-value"):
        if "scans" in _text(value).lower():
            return True
    return False


def _scan_variants_from_catalogue(catalogue_url: str) -> List[Dict[str, str]]:
    try:
        response = _get(catalogue_url)
        response.raise_for_status()
    except Exception:
        return []

    variants: List[Dict[str, str]] = []
    seen = set()
    for label, path in re.findall(
        r"panneauScan\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*\)",
        response.text,
        flags=re.I,
    ):
        raw_path = path.strip()
        if "scan" not in raw_path.lower():
            continue
        scan_url = _ensure_trailing_slash(urljoin(catalogue_url.rstrip("/") + "/", raw_path))
        if scan_url in seen:
            continue
        seen.add(scan_url)
        variants.append({"label": label.strip(), "scan_url": scan_url})
    return variants


def search_manga(query: str) -> List[MangaInfo]:
    query = (query or "").strip()
    if not query:
        return []

    origin = get_website_url()
    url = f"{origin}/catalogue/?search={quote_plus(query)}"
    response = _get(url)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html5lib")
    container = soup.find("div", {"id": "list_catalog"})
    if not container:
        return []

    results: List[MangaInfo] = []
    for card in container.find_all("div", recursive=False):
        if not _has_scans(card):
            continue

        link = card.find("a", href=True)
        if not link:
            continue
        catalogue_url = urljoin(origin + "/", link["href"])
        image = card.find("img")
        title_node = card.select_one(".card-title") or card.find("h2") or card.find("h3")
        title = _text(title_node) or _slug_from_url(catalogue_url).replace("-", " ").title()
        cover_url = urljoin(catalogue_url, image.get("src", "")) if image else ""

        variants = _scan_variants_from_catalogue(catalogue_url) or [
            {"label": "", "scan_url": _scan_url_from_catalogue(catalogue_url)}
        ]
        genres = _split_values(_info_value(card, "Genres"))
        languages = _split_values(_info_value(card, "Langues")) or ["VF"]
        for variant in variants:
            variant_label = variant.get("label") or ""
            variant_title = f"{title} - {variant_label}" if variant_label and len(variants) > 1 else title
            results.append(
                MangaInfo(
                    title=variant_title,
                    exact_title=title,
                    catalogue_url=catalogue_url,
                    scan_url=variant["scan_url"],
                    cover_url=cover_url,
                    slug=_slug_from_url(catalogue_url),
                    genres=list(genres),
                    languages=list(languages),
                )
            )
    return results


def get_manga_info(catalogue_url: str) -> MangaInfo:
    if not catalogue_url:
        raise ValueError("URL catalogue manga manquante.")

    source_url = catalogue_url
    is_scan_url = _is_scan_catalogue_url(source_url)
    base_catalogue_url = _catalogue_from_scan_url(source_url)
    origin = _origin(base_catalogue_url)
    if not urlparse(base_catalogue_url).netloc:
        base_catalogue_url = urljoin(origin + "/", base_catalogue_url)
    if not urlparse(source_url).netloc:
        source_url = urljoin(origin + "/", source_url)
    fetch_url = source_url if is_scan_url else base_catalogue_url

    response = _get(fetch_url)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html5lib")

    title_node = soup.select_one("#titreOeuvre")
    exact_title = _text(title_node, strip=False)
    title = exact_title.strip() or _slug_from_url(catalogue_url).replace("-", " ").title()
    cover = soup.select_one("#coverOeuvre") or soup.select_one("#imgOeuvre") or soup.find("img")
    cover_url = urljoin(catalogue_url, cover.get("src", "")) if cover else ""

    genres: List[str] = []
    genres_title = soup.find(string=re.compile(r"^\s*Genres\s*$", re.I))
    if genres_title:
        next_text = genres_title.find_parent().find_next(string=True)
        if next_text:
            genres = _split_values(str(next_text))

    return MangaInfo(
        title=title,
        exact_title=exact_title or title,
        catalogue_url=base_catalogue_url,
        scan_url=_ensure_trailing_slash(source_url) if is_scan_url else _scan_url_from_catalogue(base_catalogue_url),
        cover_url=cover_url,
        slug=_slug_from_url(base_catalogue_url),
        genres=genres,
        languages=["VF"],
    )


def _chapter_sort_key(value: str):
    try:
        return (0, float(value))
    except (TypeError, ValueError):
        return (1, str(value))


def _build_image_url(origin: str, exact_title: str, chapter: str, page: int) -> str:
    title_path = quote(exact_title, safe="")
    chapter_path = quote(str(chapter), safe="")
    return f"{origin}/s2/scans/{title_path}/{chapter_path}/{page}.jpg"


def fetch_chapters(scan_url: str) -> List[List[str]]:
    if not scan_url:
        raise ValueError("URL scan manga manquante.")

    response = _get(scan_url)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html5lib")
    title_node = soup.select_one("#titreOeuvre")
    exact_title = _text(title_node, strip=False)
    if not exact_title:
        image = soup.select_one("#imgOeuvre, #coverOeuvre")
        exact_title = image.get("alt", "") if image else ""
    if not exact_title:
        raise RuntimeError("Titre exact Anime-Sama introuvable.")

    origin = _origin(scan_url)
    api_url = f"{origin}/s2/scans/get_nb_chap_et_img.php"
    data_response = _get(api_url, params={"oeuvre": exact_title}, referer=scan_url)
    data_response.raise_for_status()
    data = data_response.json()
    if not isinstance(data, dict) or data.get("error"):
        raise RuntimeError(data.get("error") if isinstance(data, dict) else "Chapitres indisponibles.")

    chapters: List[List[str]] = []
    for chapter in sorted(data.keys(), key=_chapter_sort_key):
        try:
            page_count = int(data[chapter])
        except (TypeError, ValueError):
            page_count = 0
        chapters.append([
            _build_image_url(origin, exact_title, str(chapter), page)
            for page in range(1, max(0, page_count) + 1)
        ])
    return chapters


def _is_allowed_image_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme in {"http", "https"} and "anime-sama" in host and "/s2/scans/" in parsed.path


def fetch_image(url: str):
    if not _is_allowed_image_url(url):
        raise ValueError("URL image manga non autorisee.")
    return _get(
        url,
        stream=True,
        referer=_origin(url) + "/",
        headers=_headers(_origin(url) + "/", accept="image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"),
        timeout=25,
    )
