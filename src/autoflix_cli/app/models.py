from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ContentSummary:
    provider_id: str
    provider_name: str
    content_id: str
    title: str
    content_type: str = "unknown"
    image: str = ""
    subtitle: str = ""
    genres: List[str] = field(default_factory=list)
    languages: List[str] = field(default_factory=list)
    year: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Episode:
    id: str
    title: str
    number: Optional[int] = None
    language: Optional[str] = None
    progress: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Season:
    id: str
    title: str
    number: Optional[int] = None
    language: Optional[str] = None
    languages: List[str] = field(default_factory=list)
    episodes: List[Episode] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["episodes"] = [episode.to_dict() for episode in self.episodes]
        return data


@dataclass
class ContentDetails:
    provider_id: str
    provider_name: str
    content_id: str
    title: str
    content_type: str = "unknown"
    image: str = ""
    subtitle: str = ""
    genres: List[str] = field(default_factory=list)
    year: Optional[str] = None
    seasons: List[Season] = field(default_factory=list)
    external_ids: Dict[str, Any] = field(default_factory=dict)
    favorite: bool = False
    progress: Optional[Dict[str, Any]] = None

    def summary(self) -> ContentSummary:
        return ContentSummary(
            provider_id=self.provider_id,
            provider_name=self.provider_name,
            content_id=self.content_id,
            title=self.title,
            content_type=self.content_type,
            image=self.image,
            subtitle=self.subtitle,
            genres=self.genres,
            year=self.year,
        )

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["seasons"] = [season.to_dict() for season in self.seasons]
        data["summary"] = self.summary().to_dict()
        return data


@dataclass
class PlayableSource:
    id: str
    name: str
    source_type: str = "unknown"
    quality: str = ""
    language: Optional[str] = None
    subtitles: List[Dict[str, Any]] = field(default_factory=list)
    has_subtitles: bool = False
    subtitle_languages: List[str] = field(default_factory=list)
    subtitle_source: Optional[str] = None
    score: int = 0
    provider_name: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ProviderError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code

    def to_dict(self) -> Dict[str, Any]:
        return {"error": self.code, "message": self.message}

