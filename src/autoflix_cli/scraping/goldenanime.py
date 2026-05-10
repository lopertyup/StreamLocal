import base64
import hashlib
import json
import re

from Crypto.Cipher import AES
from curl_cffi import requests as cffi_requests

from ..proxy import DNS_OPTIONS

from .config import portals

scraper = cffi_requests.Session(impersonate="chrome", curl_options=DNS_OPTIONS)

ALLANIME_KEY_PHRASE = b"Xot36i3lK3:v1"
GOLDENANIME_REQUEST_TIMEOUT = 5
ALLANIME_API_TIMEOUT = 8
ALLANIME_CLOCK_TIMEOUT = 4


class AnimeExtractor:
    """
    Original Version (VO) anime extractor based on CineStream.
    Targets M3U8 streams and video players.
    """

    def __init__(self):
        # --- Source URLs loaded from source_portal.jsonc ---
        self.sudatchi_base = portals.get("sudatchi", "https://sudatchi.com")
        self.animetsu_base = portals.get("animetsu", "https://animetsu.live")
        self.animetsu_api = portals.get("animetsu-api", "https://b.animetsu.live")
        self.animetsu_proxy = portals.get(
            "animetsu-proxy", "https://ani.metsu.site/proxy"
        )
        self.allanime_api = portals.get("allanime-api", "https://api.allanime.day/api")
        self.allanime_referer = portals.get("allanime-referer", "https://allmanga.to")
        self.allanime_base = portals.get("allanime-base", "https://allanime.day")
        self.anizone_base = portals.get("anizone", "https://anizone.to")

        self.headers = {
            "Referer": self.sudatchi_base + "/",
            "Origin": self.sudatchi_base,
        }
        self.animetsu_headers = {
            "Referer": self.animetsu_base + "/",
            "Origin": self.animetsu_base,
        }

    def _decrypt_allanime(self, hex_str):
        """Allanime hex decryption (XOR 56)."""
        try:
            # Extract hex part after '-'
            hex_part = hex_str.split("-")[-1]
            bytes_data = bytes.fromhex(hex_part)
            return "".join(chr(b ^ 56) for b in bytes_data)
        except Exception:
            return hex_str

    def _allanime_headers(self):
        return {
            "Referer": self.allanime_referer + "/",
            "Origin": "https://youtu-chan.com",
        }

    def _decrypt_allanime_payload(self, payload):
        """Decrypt Allanime's AES-CTR tobeparsed payload."""
        raw = base64.b64decode(payload + "=" * (-len(payload) % 4))
        if len(raw) < 30:
            return {}

        nonce = raw[1:13]
        encrypted = raw[13:-16]
        key = hashlib.sha256(ALLANIME_KEY_PHRASE).digest()
        cipher = AES.new(key, AES.MODE_CTR, nonce=nonce, initial_value=2)
        decrypted = cipher.decrypt(encrypted).decode("utf-8")
        return json.loads(decrypted)

    def _allanime_episode_payload(self, response_data):
        data = response_data.get("data", {}) if isinstance(response_data, dict) else {}
        if isinstance(data, dict) and data.get("tobeparsed"):
            decrypted = self._decrypt_allanime_payload(data["tobeparsed"])
            if isinstance(decrypted, dict):
                return decrypted
        return data if isinstance(data, dict) else {}

    def _decode_allanime_source_url(self, url):
        if not url:
            return ""
        if url.startswith("--"):
            url = self._decrypt_allanime(url)
        if url.startswith("/"):
            url = self.allanime_base.rstrip("/") + url
        return url

    def _allanime_title_key(self, value):
        value = (value or "").lower()
        value = re.sub(r"[^a-z0-9]+", " ", value)
        return re.sub(r"\s+", " ", value).strip()

    def _allanime_stream_type(self, url, source_type="", hls=False):
        lower_url = (url or "").lower()
        lower_type = (source_type or "").lower()
        if hls or ".m3u8" in lower_url or "master" in lower_url:
            return "M3U8"
        if (
            lower_url.endswith(".mp4")
            or "/mp4/" in lower_url
            or "fast4speed" in lower_url
            or "mp4" in lower_type
        ):
            return "MP4"
        if lower_type == "iframe":
            return "IFRAME"
        return "Player/M3U8"

    def _allanime_score(self, priority, is_direct=False):
        try:
            score = int(float(priority or 0) * 10)
        except (TypeError, ValueError):
            score = 0
        return score + (5 if is_direct else 0)

    def _allanime_priority(self, source):
        try:
            return float(source.get("priority") or 0)
        except (AttributeError, TypeError, ValueError):
            return 0.0

    def _fetch_allanime_clock_links(self, url):
        if "/clock?" in url:
            url = url.replace("/clock?", "/clock.json?", 1)

        try:
            response = scraper.get(
                url,
                headers=self._allanime_headers(),
                timeout=ALLANIME_CLOCK_TIMEOUT,
            )
            if response.status_code != 200:
                return []
            data = response.json()
        except Exception:
            return []

        links = data.get("links", []) if isinstance(data, dict) else []
        if not isinstance(links, list):
            return []
        return links

    def search_sudatchi(self, anilist_id, episode=1):
        """Extraction from Sudatchi (direct M3U8)."""
        base_url = self.sudatchi_base
        api_url = f"{base_url}/api/episode/{anilist_id}/{episode}"

        try:
            response = scraper.get(
                api_url,
                headers=self.headers,
                timeout=GOLDENANIME_REQUEST_TIMEOUT,
            )
            if response.status_code != 200:
                return []

            data = response.json()
            ep_id = data.get("episode", {}).get("id")
            if not ep_id:
                return []

            # The stream link is an API call that returns the M3U8 (Sudatchi logic)
            stream_url = f"{base_url}/api/streams?episodeId={ep_id}"

            return [
                {
                    "source": "Sudatchi",
                    "quality": "1080p",
                    "url": stream_url,
                    "type": "M3U8",
                }
            ]
        except Exception:
            return []

    def search_allanime(self, title, episode=1):
        """Extraction from Allanime using GQL hashes and current payload decoding."""
        api_url = self.allanime_api
        headers = self._allanime_headers()

        # Latest GQL Hashes from CineStream
        search_hash = "a24c500a1b765c68ae1d8dd85174931f661c71369c89b92b88b75a725afc471c"
        ep_hash = "d405d0edd690624b66baba3068e0edc3ac90f1597d898a1ec8db4e5c43c00fec"

        try:
            # 1. Search (Query Hash)
            vars_search = {
                "search": {"query": title, "types": ["TV", "Movie"]},
                "limit": 26,
                "page": 1,
                "translationType": "sub",
                "countryOrigin": "ALL",
            }
            ext_search = {"persistedQuery": {"version": 1, "sha256Hash": search_hash}}

            r = scraper.get(
                api_url,
                params={
                    "variables": json.dumps(vars_search),
                    "extensions": json.dumps(ext_search),
                },
                headers=headers,
                timeout=ALLANIME_API_TIMEOUT,
            )
            shows = r.json().get("data", {}).get("shows", {}).get("edges", [])
            if not shows:
                return []

            # Match title logic
            show_id = None
            title_key = self._allanime_title_key(title)
            for edge in shows:
                name = self._allanime_title_key(edge.get("name"))
                eng = self._allanime_title_key(edge.get("englishName"))
                if name == title_key or eng == title_key:
                    show_id = edge.get("_id")
                    break

            if not show_id and shows:
                # Fuzzy fallback
                for edge in shows:
                    name = self._allanime_title_key(edge.get("name"))
                    eng = self._allanime_title_key(edge.get("englishName"))
                    if title_key and (title_key in name or title_key in eng):
                        show_id = edge.get("_id")
                        break

            if not show_id and shows:
                show_id = shows[0].get("_id")

            if not show_id:
                return []

            # 2. Links (EP Hash)
            vars_ep = {
                "showId": show_id,
                "translationType": "sub",
                "episodeString": str(episode),
            }
            ext_ep = {"persistedQuery": {"version": 1, "sha256Hash": ep_hash}}

            r = scraper.get(
                api_url,
                params={
                    "variables": json.dumps(vars_ep),
                    "extensions": json.dumps(ext_ep),
                },
                headers=headers,
                timeout=ALLANIME_API_TIMEOUT,
            )
            episode_data = self._allanime_episode_payload(r.json())
            episode_payload = episode_data.get("episode", episode_data)
            sources = (
                episode_payload.get("sourceUrls", [])
                if isinstance(episode_payload, dict)
                else []
            )

            results = []
            has_direct_source = False
            for src in sorted(sources, key=self._allanime_priority, reverse=True):
                if not isinstance(src, dict):
                    continue

                source_name = src.get("sourceName", "Default")
                source_type = src.get("type", "")
                priority = src.get("priority")
                url = self._decode_allanime_source_url(src.get("sourceUrl", ""))

                if not url.startswith("http"):
                    continue

                if "/clock" in url:
                    if has_direct_source:
                        continue
                    for link in self._fetch_allanime_clock_links(url):
                        if not isinstance(link, dict):
                            continue
                        link_url = link.get("link") or link.get("url")
                        if not link_url:
                            continue
                        quality = link.get("resolutionStr") or link.get("quality") or "HLS"
                        stream_type = self._allanime_stream_type(
                            link_url,
                            source_type,
                            bool(link.get("hls")),
                        )
                        direct = stream_type in {"M3U8", "MP4", "VIDEO"}
                        if direct:
                            has_direct_source = True
                        results.append(
                            {
                                "source": f"Allanime ({source_name} {quality})",
                                "quality": quality,
                                "url": link_url,
                                "type": stream_type,
                                "headers": headers,
                                "direct": direct,
                                "score": self._allanime_score(priority, direct),
                            }
                        )
                    continue

                stream_type = self._allanime_stream_type(url, source_type)
                direct = stream_type in {"M3U8", "MP4", "VIDEO"}
                if direct:
                    has_direct_source = True
                results.append(
                    {
                        "source": f"Allanime ({source_name})",
                        "quality": "MP4" if stream_type == "MP4" else "Multi",
                        "url": url,
                        "type": stream_type,
                        "headers": headers,
                        "direct": direct if stream_type != "IFRAME" else True,
                        "score": self._allanime_score(priority, direct),
                    }
                )
            results.sort(key=lambda item: int(item.get("score") or 0), reverse=True)
            return results
        except Exception:
            return []

    def search_anizone(self, title, episode=1):
        """Extraction from Anizone (Updated Search and DOM selection)."""
        base_url = self.anizone_base
        try:
            # 1. Search (New endpoint)
            r = scraper.get(
                f"{base_url}/search?keyword={title}",
                headers=self.headers,
                timeout=GOLDENANIME_REQUEST_TIMEOUT,
            )

            # Match slug logic
            matches = re.finditer(r'href="/anime/([^"]+)"', r.text)
            best_slug = None
            t_slug = title.lower().replace(" ", "-")

            for m in matches:
                slug = m.group(1)
                if not best_slug:
                    best_slug = slug
                if slug == t_slug:
                    best_slug = slug
                    break
                if t_slug in slug and "season" not in slug:
                    best_slug = slug

            if not best_slug:
                return []

            # 2. Episode page
            ep_url = f"{base_url}/anime/{best_slug}/{episode}"
            r = scraper.get(
                ep_url,
                headers=self.headers,
                timeout=GOLDENANIME_REQUEST_TIMEOUT,
            )

            # Find media-player src (M3U8)
            player_match = re.search(r'<media-player[^>]+src="([^"]+)"', r.text)

            if player_match:
                return [
                    {
                        "source": "Anizone",
                        "quality": "1080p",
                        "url": player_match.group(1),
                        "type": "M3U8",
                    }
                ]
        except Exception:
            pass
        return []

    def search_animetsu(self, title, anilist_id, episode=1):
        """Extraction from Animetsu (Updated Gojo V2 API)."""
        base_api = f"{self.animetsu_api}/v2/api"
        headers = self.animetsu_headers

        try:
            # 1. Search (V2 API)
            r = scraper.get(
                f"{base_api}/anime/search/?query={title}",
                headers=headers,
                timeout=GOLDENANIME_REQUEST_TIMEOUT,
            )
            results = r.json().get("results", [])

            # Find match by title or anilist_id
            gojo_id = None
            for item in results:
                if item.get("anilist_id") == anilist_id:
                    gojo_id = item["id"]
                    break

            if not gojo_id and results:
                # Fallback to title match
                t_lower = title.lower().strip()
                for item in results:
                    titles = item.get("title", {})
                    if (
                        t_lower in (titles.get("english") or "").lower()
                        or t_lower in (titles.get("romaji") or "").lower()
                    ):
                        gojo_id = item["id"]
                        break

            if not gojo_id:
                return []

            # 2. Servers
            r = scraper.get(
                f"{base_api}/anime/servers/{gojo_id}/{episode}",
                headers=headers,
                timeout=GOLDENANIME_REQUEST_TIMEOUT,
            )
            servers_data = r.json()

            results = []
            for server_obj in servers_data:
                server_id = server_obj.get("id")
                if not server_id:
                    continue

                for lang in ["sub", "dub"]:
                    try:
                        # 3. Stream Links (Oppai endpoint)
                        r = scraper.get(
                            f"{base_api}/anime/oppai/{gojo_id}/{episode}?server={server_id}&source_type={lang}",
                            headers=headers,
                            timeout=GOLDENANIME_REQUEST_TIMEOUT,
                        )
                        stream_data = r.json()
                        sources = stream_data.get("sources", [])

                        # Subtitles
                        subs = []
                        for sub in stream_data.get("subtitles", []):
                            subs.append(
                                {"lang": sub.get("lang"), "url": sub.get("url")}
                            )

                        for src in sources:
                            url = src.get("url")
                            if not url:
                                continue

                            # Apply proxy if relative
                            if not url.startswith("http"):
                                url = f"{self.animetsu_proxy}/{url.lstrip('/')}"

                            results.append(
                                {
                                    "source": f"Animetsu ({lang.upper()} - {server_id})",
                                    "quality": src.get("quality", "1080p"),
                                    "url": url,
                                    "type": (
                                        "M3U8"
                                        if src.get("type") != "video/mp4"
                                        else "MP4"
                                    ),
                                    "subtitles": subs if subs else None,
                                }
                            )
                    except Exception:
                        continue
            return results
        except Exception:
            return []

    def _dedupe_results(self, results):
        unique = {}
        for result in results:
            url = result.get("url")
            if url and url not in unique:
                unique[url] = result
        return list(unique.values())

    def _has_direct_stream(self, results):
        for result in results:
            url = (result.get("url") or "").lower()
            stream_type = (result.get("type") or "").upper()
            if (
                stream_type in {"M3U8", "MP4", "VIDEO"}
                or ".m3u8" in url
                or "master" in url
            ):
                return True
        return False

    def extract_vo(self, title=None, anilist_id=None, episode=1):
        """Search, deduplication, and sorting."""
        results = []

        if title:
            results.extend(self.search_allanime(title, episode))
            if self._has_direct_stream(results):
                return self._dedupe_results(results)

        if anilist_id:
            results.extend(self.search_sudatchi(anilist_id, episode))
        if title:
            results.extend(self.search_anizone(title, episode))
        if title and anilist_id:
            results.extend(self.search_animetsu(title, anilist_id, episode))

        return self._dedupe_results(results)


# Instantiate a global instance for easy use
goldenanime = AnimeExtractor()
