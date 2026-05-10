from curl_cffi import requests as cffi_requests
import urllib.parse
from urllib.parse import urlparse
from .config import portals
from ..app import diagnostics
from ..proxy import DNS_OPTIONS

scraper = cffi_requests.Session(impersonate="chrome", curl_options=DNS_OPTIONS)


class MediaExtractor:
    """
    Movie and Series extractor based on CineStream.
    Targets M3U8 streams and video players.
    """

    def __init__(self):
        # --- Source URLs loaded from source_portal.jsonc ---
        self.multi_decrypt_api = portals.get(
            "multi-decrypt", "https://enc-dec.app/api"
        )
        self.videasy_api = portals.get("videasy", "https://api.videasy.net")
        self.vidlink_api = portals.get("vidlink", "https://vidlink.pro")
        self.hexa_api = portals.get("hexa", "https://themoviedb.hexa.su")
        self.mapple_api = portals.get("mapple", "https://mapple.uk")
        self.xpass_api = portals.get("xpass", "https://play.xpass.top")

        # --- Referers ---
        self.videasy_referer = portals.get(
            "videasy-referer", "https://player.videasy.net"
        )
        if "cineby.gd" in self.videasy_referer:
            self.videasy_referer = "https://player.videasy.net"
        self.hexa_referer = portals.get("hexa-referer", "https://hexa.su/")

        self.headers = {"Connection": "keep-alive"}

    def _quote(self, text):
        return urllib.parse.quote(text).replace("+", "%20")

    def _videasy_headers(self):
        return {
            "Accept": "*/*",
            "User-Agent": scraper.headers.get("User-Agent", "Mozilla/5.0"),
            "Origin": self.videasy_referer,
            "Referer": self.videasy_referer + "/",
        }

    def _subtitle_url_from_item(self, item):
        if not isinstance(item, dict):
            return ""
        for key in ("url", "file", "src", "uri", "href"):
            value = item.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                return value
        return ""

    def _item_declares_subtitle(self, item, url):
        if not isinstance(item, dict):
            return False
        for key in ("kind", "type", "category", "format", "mimeType", "contentType"):
            value = str(item.get(key) or "").lower()
            if any(marker in value for marker in ("subtitle", "caption", "vtt", "srt")):
                return True
        parsed = urlparse(url)
        path = (parsed.path or "").lower()
        return path.endswith((".vtt", ".srt", ".ass", ".ssa")) or any(
            marker in path for marker in ("subtitle", "caption", "/sub/")
        )

    def _normalize_videasy_subtitle(self, item, forced=False):
        url = self._subtitle_url_from_item(item)
        if not url:
            return None
        if not forced and not self._item_declares_subtitle(item, url):
            return None

        lang = ""
        for key in ("lang", "language", "lang_code", "srclang", "locale"):
            if item.get(key):
                lang = str(item.get(key)).strip()
                break

        label = ""
        for key in ("label", "name", "title"):
            if item.get(key):
                label = str(item.get(key)).strip()
                break

        normalized = {"url": url}
        if lang:
            normalized["lang"] = lang
        if label:
            normalized["label"] = label
        normalized["subtitle_source"] = "videasy_api"
        return normalized

    def _extract_videasy_subtitles(self, data):
        subtitles = []
        seen_urls = set()

        def visit(value, forced=False, key_hint=""):
            key_lower = str(key_hint or "").lower()
            child_forced = forced or any(
                marker in key_lower
                for marker in ("subtitle", "caption", "texttrack", "text_track")
            )
            if isinstance(value, dict):
                normalized = self._normalize_videasy_subtitle(value, forced=child_forced)
                if normalized and normalized["url"] not in seen_urls:
                    seen_urls.add(normalized["url"])
                    subtitles.append(normalized)
                for key, child in value.items():
                    visit(child, child_forced, key)
            elif isinstance(value, list):
                for child in value[:100]:
                    visit(child, child_forced, key_hint)

        visit(data)
        return subtitles

    def _log_videasy_api_response(self, server, data, subtitles):
        keys = list(data.keys())[:24] if isinstance(data, dict) else []
        diagnostics.info(
            "PROVIDER",
            "videasy_api_response",
            provider="GoldenMS",
            server=server,
            keys=keys,
            has_subtitles=bool(subtitles),
            subtitle_sample=subtitles[0] if subtitles else {},
        )

    def search_videasy(
        self, title, tmdb_id=None, imdb_id=None, year=None, season=None, episode=None
    ):
        """Extraction via Videasy (Multi-server)."""
        headers = self._videasy_headers()

        servers = [
            "cdn",
            "myflixerzupcloud",
            "moviebox",
            "primewire",
            "m4uhd",
            "hdmovie",
            "primesrcme",
        ]
        results = []

        if not title:
            return []

        # Double encoding as per CineStream logic
        enc_title = self._quote(self._quote(title))
        media_type = "movie" if season is None else "tv"

        for server in servers:
            try:
                url = f"{self.videasy_api}/{server}/sources-with-title?title={enc_title}&mediaType={media_type}"
                if year:
                    url += f"&year={year}"
                if tmdb_id:
                    url += f"&tmdbId={tmdb_id}"
                if imdb_id:
                    url += f"&imdbId={imdb_id}"
                if season:
                    url += f"&seasonId={season}"
                if episode:
                    url += f"&episodeId={episode}"

                r = scraper.get(url, headers=headers, timeout=10)
                if r.status_code != 200:
                    continue
                enc_data = r.text

                # Decryption via multi-decrypt (passing tmdbId as 'id')
                payload = {"text": enc_data, "id": tmdb_id}
                r_dec = scraper.post(
                    f"{self.multi_decrypt_api}/dec-videasy", json=payload, timeout=10
                )

                if r_dec.status_code == 200:
                    data = r_dec.json().get("result", {})
                    if not isinstance(data, dict):
                        data = {}
                    sources = data.get("sources", [])
                    subs = self._extract_videasy_subtitles(data)
                    self._log_videasy_api_response(server, data, subs)

                    for src in sources:
                        results.append(
                            {
                                "source": f"Videasy ({server.upper()})",
                                "quality": src.get("quality", "Multi"),
                                "url": src.get("url"),
                                "type": (
                                    "M3U8"
                                    if ".m3u8" in src.get("url", "").lower()
                                    else "VIDEO"
                                ),
                                "subtitles": subs,
                                "subtitle_source": "videasy_api" if subs else None,
                                "headers": dict(headers),
                                "stream_context": "videasy",
                            }
                        )
                    if server == "cdn" and results:
                        return results
            except:
                continue
        return results

    def search_vidlink(self, tmdb_id, season=None, episode=None):
        """Extraction via Vidlink."""
        if not tmdb_id:
            return []
        try:
            # 1. Encrypt TMDB ID via API
            r_enc = scraper.get(
                f"{self.multi_decrypt_api}/enc-vidlink?text={tmdb_id}", timeout=10
            )
            enc_data = r_enc.json().get("result")

            headers = {
                "User-Agent": scraper.headers.get("User-Agent", "Mozilla/5.0"),
                "Connection": "keep-alive",
                "Referer": f"{self.vidlink_api}/",
                "Origin": self.vidlink_api,
            }

            if season is None:
                url = f"{self.vidlink_api}/api/b/movie/{enc_data}"
            else:
                url = f"{self.vidlink_api}/api/b/tv/{enc_data}/{season}/{episode}"

            r = scraper.get(url, headers=headers, timeout=10)
            data = r.json()
            m3u8_url = data.get("stream", {}).get("playlist")

            if m3u8_url:
                return [
                    {
                        "source": "Vidlink",
                        "quality": "Multi",
                        "url": m3u8_url,
                        "type": "M3U8",
                        "headers": headers,
                    }
                ]
        except:
            pass
        return []

    def search_hexa(self, tmdb_id, season=None, episode=None):
        """Extraction via Hexa with dynamic security handshake."""
        if not tmdb_id:
            return []
        try:
            # 1. Endpoint
            if season is None:
                url = f"{self.hexa_api}/api/tmdb/movie/{tmdb_id}/images"
            else:
                url = f"{self.hexa_api}/api/tmdb/tv/{tmdb_id}/season/{season}/episode/{episode}/images"

            # 2. Security Handshake
            import secrets

            key = secrets.token_hex(32)  # Generate 32-byte key

            # Fetch X-Cap-Token from multi-decrypt
            r_token = scraper.get(f"{self.multi_decrypt_api}/enc-hexa", timeout=10)
            cap_token = r_token.json().get("result", {}).get("token")

            headers = {
                "User-Agent": scraper.headers.get("User-Agent", "Mozilla/5.0"),
                "Accept": "text/plain",
                "X-Api-Key": key,
                "X-Fingerprint-Lite": "e9136c41504646444",
                "Referer": self.hexa_referer,
                "X-Cap-Token": cap_token,
            }

            # 3. Request
            r_enc = scraper.get(url, headers=headers, timeout=10)
            if r_enc.status_code != 200:
                return []
            enc_data = r_enc.text

            # 4. Decrypt
            payload = {"text": enc_data, "key": key}
            r_dec = scraper.post(
                f"{self.multi_decrypt_api}/dec-hexa", json=payload, timeout=10
            )

            if r_dec.status_code == 200:
                data = r_dec.json().get("result", {})
                sources = data.get("sources", [])
                results = []
                for src in sources:
                    results.append(
                        {
                            "source": f"Hexa ({src.get('server', '').upper()})",
                            "quality": "Multi",
                            "url": src.get("url"),
                            "type": "M3U8",
                            "headers": {"Referer": self.hexa_referer},
                        }
                    )
                return results
        except:
            pass
        return []

    def search_mapple(self, tmdb_id, season=None, episode=None):
        """Extraction via Mapple."""
        if not tmdb_id:
            return []

        base_url = self.mapple_api
        headers = {
            "User-Agent": scraper.headers.get("User-Agent", "Mozilla/5.0"),
            "Referer": f"{base_url}/",
        }

        try:
            # 1. Get Token
            media_type = "movie" if season is None else "tv"
            tv_slug = f"{season}-{episode}" if season else ""
            watch_url = f"{base_url}/watch/{media_type}/{tmdb_id}"
            if season:
                watch_url += f"/{tv_slug}"

            r = scraper.get(watch_url, headers=headers, timeout=10)
            token_match = re.search(
                r'window\.__REQUEST_TOKEN__\s*=\s*"([^"]+)"', r.text
            )
            if not token_match:
                return []
            token = token_match.group(1)

            # 2. Extract Streams (Iterate through sources)
            sources = [
                "mapple",
                "sakura",
                "oak",
                "willow",
                "cherry",
                "pines",
                "magnolia",
                "sequoia",
            ]
            results = []

            for source in sources:
                try:
                    payload = {
                        "data": {
                            "mediaId": int(tmdb_id),
                            "mediaType": media_type,
                            "tv_slug": tv_slug,
                            "source": source,
                        },
                        "endpoint": "stream-encrypted",
                    }

                    r_enc = scraper.post(
                        f"{base_url}/api/encrypt",
                        json=payload,
                        headers=headers,
                        timeout=10,
                    )
                    stream_path = r_enc.json().get("url")
                    if not stream_path:
                        continue

                    final_url = f"{base_url}{stream_path}&requestToken={token}"
                    r_streams = scraper.get(
                        final_url, headers=headers, timeout=10
                    ).json()

                    if r_streams.get("success"):
                        stream_url = r_streams.get("data", {}).get("stream_url")
                        if stream_url:
                            results.append(
                                {
                                    "source": f"Mapple ({source.upper()})",
                                    "quality": "1080p",
                                    "url": stream_url,
                                    "type": "M3U8",
                                    "headers": {"Referer": f"{base_url}/"},
                                }
                            )
                except:
                    continue
            return results
        except:
            return []

    def search_xpass(self, tmdb_id, season=None, episode=None):
        """Extraction via Xpass."""
        if not tmdb_id:
            return []

        base_url = self.xpass_api
        try:
            embed_url = (
                f"{base_url}/e/movie/{tmdb_id}"
                if season is None
                else f"{base_url}/e/tv/{tmdb_id}/{season}/{episode}"
            )
            r = scraper.get(embed_url, headers={"Referer": f"{base_url}/"}, timeout=10)

            # Extract backups/sources via regex
            matches = re.findall(r'href=["\']([^"\']+/playlist/[^"\']+)["\']', r.text)
            results = []

            for url in matches:
                full_url = url if url.startswith("http") else f"{base_url}{url}"
                try:
                    r_json = scraper.get(full_url, timeout=10).json()
                    playlist = r_json.get("playlist", [])[0]
                    sources = playlist.get("sources", [])
                    for src in sources:
                        file_url = src.get("file")
                        if file_url:
                            results.append(
                                {
                                    "source": "Xpass",
                                    "quality": "Multi",
                                    "url": file_url,
                                    "type": (
                                        "M3U8"
                                        if "m3u8" in file_url.lower()
                                        else "VIDEO"
                                    ),
                                    "headers": {"Referer": f"{base_url}/"},
                                }
                            )
                except:
                    continue
            return results
        except:
            return []

    def extract(
        self,
        title=None,
        tmdb_id=None,
        imdb_id=None,
        year=None,
        season=None,
        episode=None,
        include_videasy=False,
    ):
        """Main search method."""
        results = []

        # Priority: Vidlink, Hexa, Xpass, Mapple.
        # Videasy is kept as a helper/debug extractor but is not exposed by
        # the normal GoldenMS playback flow anymore.
        if tmdb_id:
            results.extend(self.search_vidlink(tmdb_id, season, episode))
            results.extend(self.search_hexa(tmdb_id, season, episode))
            results.extend(self.search_xpass(tmdb_id, season, episode))
            results.extend(self.search_mapple(tmdb_id, season, episode))

        if include_videasy and title:
            results.extend(
                self.search_videasy(title, tmdb_id, imdb_id, year, season, episode)
            )

        # Deduplication by URL
        unique = {}
        for r in results:
            if r["url"] and r["url"] not in unique:
                unique[r["url"]] = r
        return list(unique.values())


# Instantiate a global instance for easy use
goldenms_extractor = MediaExtractor()
