"""Strict YouTube URL validation.

The user-supplied URL is never passed to yt-dlp. We only extract the 11-character
video ID and rebuild a canonical ``https://www.youtube.com/watch?v=ID`` URL from it,
which removes any possibility of server-side request forgery through the input.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

MAX_URL_LENGTH = 2048

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# Hosts serving full YouTube pages (watch / shorts / embed paths).
YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtube-nocookie.com",
        "www.youtube-nocookie.com",
    }
)
# Short-link hosts where the path is the video ID itself.
SHORT_HOSTS = frozenset({"youtu.be", "www.youtu.be"})

# Path prefixes on YOUTUBE_HOSTS that are followed directly by a video ID.
ID_PATH_PREFIXES = ("shorts", "embed", "live", "v")


class InvalidURLError(ValueError):
    """Raised when a URL is not an accepted YouTube video URL."""


@dataclass(frozen=True)
class YouTubeVideo:
    video_id: str

    @property
    def canonical_url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"


def _check_id(candidate: str | None) -> str:
    if candidate is None or not VIDEO_ID_RE.fullmatch(candidate):
        raise InvalidURLError("The link does not contain a valid YouTube video ID.")
    return candidate


def parse_youtube_url(raw: object) -> YouTubeVideo:
    """Validate ``raw`` and return the video it points to.

    Raises ``InvalidURLError`` with a user-safe message for anything that is not a
    plain link to a single YouTube video.
    """
    if not isinstance(raw, str):
        raise InvalidURLError("Please enter a YouTube link.")
    url = raw.strip()
    if not url:
        raise InvalidURLError("Please enter a YouTube link.")
    if len(url) > MAX_URL_LENGTH:
        raise InvalidURLError("The link is too long.")
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url) or "\\" in url:
        raise InvalidURLError("The link contains invalid characters.")

    # Accept links pasted without a scheme, e.g. "youtu.be/abc".
    if "://" not in url:
        url = "https://" + url

    try:
        parts = urlsplit(url)
        port = parts.port  # raises ValueError on a malformed port
    except ValueError as exc:
        raise InvalidURLError("The link is not a valid URL.") from exc

    if parts.scheme.lower() not in ("http", "https"):
        raise InvalidURLError("Only http and https links are supported.")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise InvalidURLError("The link is not a valid YouTube URL.")
    if port is not None:
        raise InvalidURLError("The link is not a valid YouTube URL.")

    host = (parts.hostname or "").lower().rstrip(".")
    segments = [s for s in parts.path.split("/") if s]

    if host in SHORT_HOSTS:
        if len(segments) != 1:
            raise InvalidURLError("The link does not contain a valid YouTube video ID.")
        return YouTubeVideo(_check_id(segments[0]))

    if host in YOUTUBE_HOSTS:
        if segments == ["watch"]:
            values = parse_qs(parts.query, keep_blank_values=True).get("v", [])
            if len(values) != 1:
                raise InvalidURLError("The link does not contain a valid YouTube video ID.")
            return YouTubeVideo(_check_id(values[0]))
        if len(segments) == 2 and segments[0] in ID_PATH_PREFIXES:
            return YouTubeVideo(_check_id(segments[1]))
        raise InvalidURLError(
            "Only links to a single YouTube video are supported (watch, youtu.be, shorts or embed)."
        )

    raise InvalidURLError("Only YouTube links (youtube.com or youtu.be) are supported.")


_UNSAFE_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_WINDOWS_RESERVED = re.compile(r"^(con|prn|aux|nul|com\d|lpt\d)$", re.IGNORECASE)


def safe_filename(title: str | None, fallback: str, max_length: int = 100) -> str:
    """Turn a video title into a filename stem that is safe on every OS."""
    name = unicodedata.normalize("NFKC", title or "")
    name = _UNSAFE_FILENAME_CHARS.sub(" ", name)
    name = "".join(ch for ch in name if unicodedata.category(ch)[0] != "C")
    name = re.sub(r"\s+", " ", name).strip(" .")
    name = name[:max_length].rstrip(" .")
    if not name or _WINDOWS_RESERVED.fullmatch(name):
        name = fallback
    return name
