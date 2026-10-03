"""Find web links without maintaining a list of supported video sites."""

import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit

from telegram import MessageEntity

WEB_LINK_RE = re.compile(r"https?://[^\s<>\"\u201c\u201d]+", re.I)


class InvalidLink(ValueError):
    pass


def validate_web_url(url):
    """Allow HTTP(S) URLs; reject credentials and explicitly local addresses.

    This is input validation, not a substitute for outbound network isolation.
    A hostname or redirect can still resolve to an internal address.
    """
    try:
        parsed = urlsplit(url)
        hostname = (parsed.hostname or "").rstrip(".").lower()
        port = parsed.port
        if (parsed.scheme not in ("http", "https") or not hostname
                or parsed.username is not None or parsed.password is not None
                or any(c.isspace() or ord(c) < 32 for c in url)
                or "\\" in url or "%" in hostname or (port is not None and port == 0)):
            raise InvalidLink("Send a valid HTTP or HTTPS video link without embedded credentials.")
        if hostname == "localhost" or hostname.endswith((".localhost", ".local", ".internal")):
            raise InvalidLink("Please send a public website link.")
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            if "." not in hostname or not re.fullmatch(r"[\w.-]+", hostname):
                raise InvalidLink("Please send a public website link.") from None
        else:
            if not address.is_global:
                raise InvalidLink("Please send a public website link.")
    except ValueError as exc:
        if isinstance(exc, InvalidLink):
            raise
        raise InvalidLink("Send a valid HTTP or HTTPS video link.") from None
    return url


def clean_pasted_url(url):
    url = url.rstrip(".,!;:\u2019'")
    # Drop prose/Markdown closing brackets, but preserve balanced URL characters.
    for opening, closing in (("(", ")"), ("[", "]"), ("{", "}")):
        while url.endswith(closing) and url.count(closing) > url.count(opening):
            url = url[:-1]
    return url


def normalize_url(url):
    validate_web_url(url)
    parsed = urlsplit(url)
    if parsed.hostname in ("x.com", "twitter.com", "www.x.com", "www.twitter.com", "m.x.com", "m.twitter.com"):
        match = re.search(r"/status/(\d+)", parsed.path)
        if match:
            return f"https://x.com/i/status/{match.group(1)}"
    # Normalize scheme/host for case-sensitive extractor patterns. Leave signed
    # paths and queries intact, including escapes and tracking/share parameters.
    hostname = parsed.hostname.encode("idna").decode("ascii")
    # TikTok's video and /t/ share extractors expect www. Keep vm/vt hosts
    # intact so yt-dlp's TikTok redirect extractor can resolve them.
    if hostname in ("tiktok.com", "m.tiktok.com") and re.fullmatch(
            r"/(?:(?:@[^/]+/video|share/video|embed)/\d+|t/\w+)/?", parsed.path):
        hostname = "www.tiktok.com"
    host = f"[{hostname}]" if ":" in hostname else hostname
    authority = f"{host}:{parsed.port}" if parsed.port else host
    return urlunsplit((parsed.scheme, authority, parsed.path, parsed.query, parsed.fragment))


def message_url(message):
    """Return the first web link in text/caption, including hidden hyperlinks."""
    text = getattr(message, "text", None)
    entities = getattr(message, "entities", ()) or ()
    parse = getattr(message, "parse_entity", None)
    if not text:
        text = getattr(message, "caption", None) or ""
        entities = getattr(message, "caption_entities", ()) or ()
        parse = getattr(message, "parse_caption_entity", None)
    candidates = []
    for entity in entities:
        if entity.type == MessageEntity.TEXT_LINK:
            url = entity.url or ""
        elif entity.type == MessageEntity.URL and parse:
            url = parse(entity)  # Telegram offsets are UTF-16, not Python indices.
            if not re.match(r"^[a-z][a-z0-9+.-]*:", url, re.I):
                url = "https://" + url
        else:
            continue
        if url.lower().startswith(("http://", "https://")):
            candidates.append((entity.offset, 0, url))
    for match in WEB_LINK_RE.finditer(text):
        offset = len(text[:match.start()].encode("utf-16-le")) // 2
        candidates.append((offset, 1, clean_pasted_url(match.group())))
    if not candidates:
        return None
    return normalize_url(min(candidates)[2])
