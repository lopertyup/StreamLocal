from __future__ import annotations

import argparse
import json
import socket
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from flask import Flask, Response, jsonify, request, stream_with_context

from . import scan_manga


@dataclass
class ImageCandidate:
    index: int
    image_url: str
    page_url: str
    source: str
    selector: str = ""
    tag: str = ""
    alt: str = ""
    width: str = ""
    height: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _default_output() -> Path:
    return _repo_root() / "data" / "scan_manga_image_locations.json"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _css_selector(node: Any) -> str:
    parts: List[str] = []
    current = node
    while current is not None and getattr(current, "name", None) not in {None, "[document]"}:
        name = str(current.name)
        node_id = current.get("id") if hasattr(current, "get") else ""
        if node_id:
            parts.append(f"{name}#{node_id}")
            break
        classes = current.get("class") if hasattr(current, "get") else None
        suffix = ""
        if classes:
            suffix = "." + ".".join(str(item) for item in classes[:3])
        index = 1
        previous = getattr(current, "previous_sibling", None)
        while previous is not None:
            if getattr(previous, "name", None) == current.name:
                index += 1
            previous = getattr(previous, "previous_sibling", None)
        parts.append(f"{name}{suffix}:nth-of-type({index})")
        current = getattr(current, "parent", None)
    return " > ".join(reversed(parts))


def _add_candidate(
    candidates: List[ImageCandidate],
    seen: set[str],
    page_url: str,
    image_url: str,
    source: str,
    selector: str = "",
    tag: str = "",
    alt: str = "",
    width: str = "",
    height: str = "",
) -> None:
    image_url = scan_manga._absolute(str(image_url or "").strip(), page_url)
    if not image_url or image_url.startswith("data:") or image_url in seen:
        return
    parsed = urlparse(image_url)
    if parsed.scheme not in {"http", "https"}:
        return
    seen.add(image_url)
    candidates.append(
        ImageCandidate(
            index=len(candidates),
            image_url=image_url,
            page_url=page_url,
            source=source,
            selector=selector,
            tag=tag,
            alt=alt,
            width=width,
            height=height,
        )
    )


def collect_image_candidates(page_url: str) -> List[ImageCandidate]:
    response = scan_manga._get_html(page_url)
    final_url = scan_manga._response_url(response, page_url)
    soup = BeautifulSoup(response.text or "", "html.parser")
    candidates: List[ImageCandidate] = []
    seen: set[str] = set()

    for node in soup.select('meta[property="og:image"][content], meta[name="twitter:image"][content]'):
        _add_candidate(
            candidates,
            seen,
            final_url,
            str(node.get("content") or ""),
            "meta",
            selector=_css_selector(node),
            tag=str(node)[:500],
        )

    for node in soup.select("img[src], img[data-src], img[data-lazy], img[data-original], source[srcset]"):
        raw_url = (
            node.get("data-src")
            or node.get("data-lazy")
            or node.get("data-original")
            or node.get("src")
            or node.get("srcset")
            or ""
        )
        if "," in str(raw_url):
            raw_url = str(raw_url).split(",", 1)[0].strip().split(" ", 1)[0]
        _add_candidate(
            candidates,
            seen,
            final_url,
            str(raw_url),
            "dom",
            selector=_css_selector(node),
            tag=str(node)[:500],
            alt=str(node.get("alt") or ""),
            width=str(node.get("width") or ""),
            height=str(node.get("height") or ""),
        )

    for match in scan_manga.IMAGE_URL_RE.finditer(response.text or ""):
        _add_candidate(candidates, seen, final_url, match.group("url"), "html")

    if scan_manga._is_chapter_url(final_url):
        try:
            for page_number, image_url in enumerate(scan_manga.get_pages(final_url), start=1):
                _add_candidate(
                    candidates,
                    seen,
                    final_url,
                    image_url,
                    "reader-api",
                    selector=f"reader-api:p[{page_number}]",
                    tag=f"chapter_payload.p[{page_number}]",
                )
        except Exception:
            pass

    return candidates


def _save_selection(output: Path, candidate: ImageCandidate) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    existing: List[Dict[str, Any]] = []
    if output.exists():
        try:
            data = json.loads(output.read_text(encoding="utf-8"))
            if isinstance(data, list):
                existing = data
        except Exception:
            existing = []
    payload = candidate.to_dict()
    payload["selected_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    existing.append(payload)
    output.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")


def _html(candidates: List[ImageCandidate], output: Path, page_url: str) -> str:
    cards = []
    for candidate in candidates:
        data = candidate.to_dict()
        meta = json.dumps(data, ensure_ascii=False)
        cards.append(
            f"""
            <button class="card" type="button" data-index="{candidate.index}">
              <img src="/image/{candidate.index}" loading="lazy" alt="">
              <span class="idx">#{candidate.index}</span>
              <strong>{candidate.source}</strong>
              <code>{candidate.selector or candidate.image_url}</code>
              <small>{candidate.image_url}</small>
              <script type="application/json" id="candidate-{candidate.index}">{meta}</script>
            </button>
            """
        )
    return f"""<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Scan-Manga image picker</title>
  <style>
    body {{ margin: 0; font-family: Segoe UI, Arial, sans-serif; background: #111; color: #eee; }}
    header {{ position: sticky; top: 0; z-index: 2; padding: 14px 18px; background: #191919; border-bottom: 1px solid #333; }}
    h1 {{ margin: 0 0 6px; font-size: 18px; }}
    p {{ margin: 0; color: #bbb; font-size: 13px; }}
    main {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 12px; padding: 14px; }}
    .card {{ min-width: 0; padding: 0; border: 1px solid #333; background: #1b1b1b; color: #eee; text-align: left; cursor: pointer; border-radius: 6px; overflow: hidden; }}
    .card:hover {{ border-color: #7ab7ff; }}
    .card.selected {{ border-color: #6ee7a8; box-shadow: 0 0 0 2px #6ee7a844 inset; }}
    img {{ display: block; width: 100%; height: 240px; object-fit: contain; background: #080808; }}
    .idx, strong, code, small {{ display: block; margin: 8px 10px; }}
    .idx {{ color: #7ab7ff; font-weight: 700; }}
    code, small {{ overflow-wrap: anywhere; color: #bbb; font-size: 11px; }}
    #status {{ color: #6ee7a8; }}
  </style>
</head>
<body>
  <header>
    <h1>Scan-Manga image picker</h1>
    <p>Page: <code>{page_url}</code></p>
    <p>Sortie: <code>{output}</code> <span id="status"></span></p>
  </header>
  <main>{''.join(cards) or '<p>Aucune image trouvee.</p>'}</main>
  <script>
    document.addEventListener("click", async (event) => {{
      const card = event.target.closest(".card");
      if (!card) return;
      const index = Number(card.dataset.index);
      const response = await fetch("/select", {{
        method: "POST",
        headers: {{"Content-Type": "application/json"}},
        body: JSON.stringify({{index}})
      }});
      const payload = await response.json();
      if (!response.ok) {{
        document.getElementById("status").textContent = "Erreur: " + (payload.message || payload.error);
        return;
      }}
      document.querySelectorAll(".card").forEach((item) => item.classList.remove("selected"));
      card.classList.add("selected");
      document.getElementById("status").textContent = "Enregistre: #" + index;
    }});
  </script>
</body>
</html>"""


def create_picker_app(page_url: str, output: Path) -> Flask:
    candidates = collect_image_candidates(page_url)
    app = Flask(__name__)

    @app.route("/")
    def index():
        return Response(_html(candidates, output, page_url), mimetype="text/html")

    @app.route("/image/<int:index>")
    def image(index: int):
        if index < 0 or index >= len(candidates):
            return Response("Image inconnue.", status=404)
        candidate = candidates[index]
        if scan_manga._is_allowed_image_url(candidate.image_url):
            upstream = scan_manga.fetch_image(candidate.image_url)
        else:
            upstream = scan_manga._get(
                candidate.image_url,
                stream=True,
                referer=page_url,
                headers={
                    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                    "Referer": page_url,
                },
            )
        headers = {"Content-Type": upstream.headers.get("Content-Type") or "image/jpeg"}

        def generate():
            for chunk in upstream.iter_content(chunk_size=16384):
                if chunk:
                    yield chunk

        return Response(stream_with_context(generate()), status=upstream.status_code, headers=headers)

    @app.route("/select", methods=["POST"])
    def select():
        payload = request.get_json(silent=True) or {}
        index = int(payload.get("index", -1))
        if index < 0 or index >= len(candidates):
            return jsonify({"error": "invalid_index", "message": "Index image invalide."}), 422
        _save_selection(output, candidates[index])
        return jsonify({"ok": True, "candidate": candidates[index].to_dict(), "output": str(output)})

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspecte les images d'une page Scan-Manga.")
    parser.add_argument("url", help="URL de la page Scan-Manga a inspecter.")
    parser.add_argument("--output", default=str(_default_output()), help="Fichier JSON de sauvegarde.")
    parser.add_argument("--port", type=int, default=0, help="Port local. 0 choisit un port libre.")
    args = parser.parse_args()

    output = Path(args.output).resolve()
    port = args.port or _free_port()
    app = create_picker_app(args.url, output)
    print(f"Image picker: http://127.0.0.1:{port}")
    print(f"Sortie JSON: {output}")
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
