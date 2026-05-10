# Implementer Un Nouveau Site De Scan

Cette fiche sert de procedure pour les futures demandes du type:

> implemente le site lelscan: https://exemple.tld/...

Objectif: ajouter un provider de scans qui fonctionne dans l'app desktop, comme Anime-Sama, sans casser la lecture, la progression, les favoris ni le build `.exe`.

## Architecture A Connaitre

- `src/autoflix_cli/app/scans.py`
  Contient le contrat provider (`ScanProvider`), les modeles (`ScanSummary`, `ScanDetails`, `ScanChapter`, `ScanPage`) et le registre `ScanService`.
- `src/autoflix_cli/scraping/manga.py`
  Scraper historique Anime-Sama. S'en servir comme reference de style, mais ne pas y empiler les autres sites.
- `src/autoflix_cli/app/server.py`
  Expose les routes `/api/scans/...` consommees par l'interface.
- `src/autoflix_cli/app/static/app.js`
  Lecteur scan cote UI. Il attend des URLs d'images dans `ScanPage.url`.
- `tests/test_scan_api.py`
  Test de contrat API pour un provider scan.
- `build/autoflix.spec`
  Ajouter les nouveaux modules en `hiddenimports` pour securiser le `.exe`.

Important: les nouveaux sites doivent passer par `/api/scans` et `ScanService`. Les routes `/api/manga/...` sont le chemin historique Anime-Sama.

## Workflow Obligatoire

1. Identifier le site

- Partir de l'URL complete donnee par l'utilisateur.
- Determiner:
  - l'URL de base officielle;
  - l'URL ou API de recherche;
  - l'URL detail d'un manga;
  - la structure des chapitres;
  - la source reelle des images;
  - les headers/referers necessaires pour charger les pages et images.
- Utiliser `curl_cffi.requests.Session(impersonate="chrome", curl_options=DNS_OPTIONS)` si le site bloque `requests`.
- Toujours utiliser `urljoin` pour normaliser les liens relatifs.

2. Creer un scraper dedie

Creer un fichier du style:

```text
src/autoflix_cli/scraping/lelscan.py
```

Structure recommandee:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests

from ..proxy import DNS_OPTIONS

HEADERS: Dict[str, str] = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
}

scraper = cffi_requests.Session(impersonate="chrome", curl_options=DNS_OPTIONS)
website_origin = "https://site.example"


@dataclass
class LelScanManga:
    title: str
    detail_url: str
    cover_url: str = ""
    genres: List[str] = field(default_factory=list)
    status: str = ""


@dataclass
class LelScanChapter:
    id: str
    title: str
    url: str
    chapter: str = ""
    pages: int = 0


def _get(url: str, **kwargs: Any):
    headers = dict(HEADERS)
    referer = kwargs.pop("referer", None)
    if referer:
        headers["Referer"] = referer
    return scraper.get(url, headers=headers, timeout=kwargs.pop("timeout", 20), **kwargs)


def search_manga(query: str) -> List[LelScanManga]:
    ...


def get_manga_info(detail_url: str) -> LelScanManga:
    ...


def get_chapters(detail_url: str) -> List[LelScanChapter]:
    ...


def get_pages(chapter_url: str) -> List[str]:
    ...
```

Garder le scraper pur: il retourne des objets simples ou des URLs, sans connaitre Flask ni l'UI.

3. Ajouter l'adapter provider

Dans `src/autoflix_cli/app/scans.py`:

- importer le scraper;
- creer une classe `<Nom>ScanProvider`;
- renseigner `info`;
- convertir les objets scraper en `ScanSummary`, `ScanDetails`, `ScanChapter`, `ScanPage`;
- utiliser `encode_payload` / `decode_payload` pour `content_id` et les IDs qui contiennent des URLs.

Skeleton:

```python
from ..scraping import lelscan as lelscan_scraper


class LelScanProvider:
    info = ScanProviderInfo(
        id="lelscan",
        name="LelScan",
        label="LelScan",
        languages=["fr"],
    )

    def search(self, query: str, language: str = "fr") -> List[ScanSummary]:
        del language
        results = []
        for item in lelscan_scraper.search_manga(query):
            content_id = encode_payload({
                "detail_url": item.detail_url,
                "title": item.title,
                "cover_url": item.cover_url,
            })
            results.append(ScanSummary(
                provider_id=self.info.id,
                provider_name=self.info.name,
                content_id=content_id,
                title=item.title,
                image=item.cover_url,
                genres=list(item.genres),
                languages=["fr"],
                status=item.status,
            ))
        return results

    def get_details(self, content_id: str, language: str = "fr") -> ScanDetails:
        del language
        payload = decode_payload(content_id)
        info = lelscan_scraper.get_manga_info(payload["detail_url"])
        chapters = [
            ScanChapter(
                id=encode_payload({"chapter_url": chapter.url, "chapter": chapter.chapter}),
                title=chapter.title,
                chapter=chapter.chapter,
                language="fr",
                pages=chapter.pages,
            )
            for chapter in lelscan_scraper.get_chapters(info.detail_url)
        ]
        return ScanDetails(
            provider_id=self.info.id,
            provider_name=self.info.name,
            content_id=content_id,
            title=info.title,
            image=info.cover_url,
            genres=list(info.genres),
            languages=["fr"],
            status=info.status,
            chapters=chapters,
        )

    def get_chapters(self, content_id: str, language: str = "fr") -> List[ScanChapter]:
        return self.get_details(content_id, language).chapters

    def get_pages(self, content_id: str, chapter_id: str, quality: str = "data") -> List[ScanPage]:
        del content_id, quality
        payload = decode_payload(chapter_id)
        pages = lelscan_scraper.get_pages(payload["chapter_url"])
        if not pages:
            raise ProviderError("chapter_pages_unavailable", "Pages du chapitre indisponibles.", 502)
        return [
            ScanPage(index=index, url=url, filename=f"{index + 1}.jpg", quality="image")
            for index, url in enumerate(pages)
        ]
```

4. Enregistrer le provider

Dans `ScanService.__init__`, ajouter le provider par defaut:

```python
providers if providers is not None else {
    "anime_sama": AnimeSamaMangaProvider(),
    "lelscan": LelScanProvider(),
}
```

Si le provider supporte une autre langue que `fr` ou `en`, mettre a jour `SUPPORTED_SCAN_LANGUAGES`.

5. Gerer les images protegees

Par defaut, `ScanPage.url` est chargee directement par le navigateur.

Si le site refuse le hotlink, CORS, ou impose un `Referer`, ne pas laisser l'UI casser:

- ajouter une fonction `fetch_image(url)` dans le scraper;
- ajouter une route proxy explicite dans `server.py` ou une URL de page de type `/api/scans/<provider>/image?url=...`;
- retourner cette URL proxy dans `ScanPage.url`;
- valider strictement les URLs autorisees avant de les proxyfier.

Anime-Sama fait cela via `/api/manga/image`; pour un nouveau provider, preferer une route scan/provider plutot que reutiliser `/api/manga/image`.

6. Tests A Ajouter

Ajouter au minimum un test provider sans reseau reel:

- mocker le scraper ou le session HTTP;
- verifier `search`;
- verifier `get_details`;
- verifier `get_pages`;
- verifier une route Flask si une route image proxy est ajoutee.

S'inspirer de:

- `tests/test_scan_api.py`
- `tests/test_manga_api.py`

Commandes de validation:

```powershell
node --check src\autoflix_cli\app\static\app.js
python -m py_compile src\autoflix_cli\desktop.py
python -m pytest
```

7. Build `.exe`

Si un nouveau module est cree, ajouter les hidden imports dans `build/autoflix.spec`, par exemple:

```python
"autoflix_cli.scraping.lelscan",
```

Puis construire avec:

```bat
build\build.bat
```

8. Checklist De Fin

- Le provider apparait dans `/api/scans/providers`.
- Une recherche retourne `media_kind="scan"` et `content_type="manga"`.
- Les `content_id` et `chapter.id` sont stables.
- Les chapitres sont dans un ordre de lecture coherent.
- Les pages chargent dans le lecteur.
- La progression fonctionne en mode vertical et page.
- Les erreurs reseau deviennent des `ProviderError` lisibles quand possible.
- `python -m pytest` passe.
- Le `.exe` embarque le module dans `build/autoflix.spec`.
