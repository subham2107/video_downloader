import pytest

from validation import InvalidURLError, parse_youtube_url, safe_filename

VID = "dQw4w9WgXcQ"


@pytest.mark.parametrize(
    "url",
    [
        f"https://www.youtube.com/watch?v={VID}",
        f"https://youtube.com/watch?v={VID}",
        f"http://www.youtube.com/watch?v={VID}",
        f"https://m.youtube.com/watch?v={VID}",
        f"https://music.youtube.com/watch?v={VID}",
        f"https://www.youtube.com/watch?v={VID}&t=42s&list=PL123",
        f"https://www.youtube.com/watch?feature=share&v={VID}",
        f"https://youtu.be/{VID}",
        f"https://youtu.be/{VID}?si=abc123",
        f"https://www.youtube.com/shorts/{VID}",
        f"https://www.youtube.com/shorts/{VID}/",
        f"https://www.youtube.com/embed/{VID}",
        f"https://www.youtube-nocookie.com/embed/{VID}",
        f"https://www.youtube.com/live/{VID}",
        f"  https://youtu.be/{VID}  ",
        f"youtu.be/{VID}",
        f"www.youtube.com/watch?v={VID}",
        f"HTTPS://WWW.YOUTUBE.COM/watch?v={VID}",
        f"https://www.youtube.com./watch?v={VID}",
    ],
)
def test_accepts_supported_formats(url):
    video = parse_youtube_url(url)
    assert video.video_id == VID
    assert video.canonical_url == f"https://www.youtube.com/watch?v={VID}"


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "not a url",
        "https://www.youtube.com/",
        "https://www.youtube.com/watch",
        "https://www.youtube.com/watch?v=",
        "https://www.youtube.com/watch?v=short",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQX",  # 12 chars
        "https://www.youtube.com/watch?v=dQw4w9WgX$Q",  # bad char
        f"https://www.youtube.com/watch?v={VID}&v=aaaaaaaaaaa",  # ambiguous
        "https://www.youtube.com/playlist?list=PL1234567890",
        "https://www.youtube.com/@somechannel",
        "https://www.youtube.com/channel/UC1234567890",
        f"https://youtu.be/{VID}/extra",
        "https://youtu.be/",
        f"ftp://www.youtube.com/watch?v={VID}",
        f"javascript:alert(1)//www.youtube.com/watch?v={VID}",
        f"file:///etc/passwd?v={VID}",
        "https://www.youtube.com/watch?v=" + "a" * 3000,
    ],
)
def test_rejects_malformed(url):
    with pytest.raises(InvalidURLError):
        parse_youtube_url(url)


@pytest.mark.parametrize(
    "url",
    [
        f"https://evil.com/watch?v={VID}",
        f"https://youtube.com.evil.com/watch?v={VID}",
        f"https://evilyoutube.com/watch?v={VID}",
        f"https://notyoutu.be/{VID}",
        f"https://youtu.be.attacker.net/{VID}",
        f"https://www.youtube.com@evil.com/watch?v={VID}",
        f"https://user:pass@www.youtube.com/watch?v={VID}",
        f"https://www.youtube.com:8080/watch?v={VID}",
        f"https://127.0.0.1/watch?v={VID}",
        f"http://169.254.169.254/latest/meta-data?v={VID}",
        f"http://localhost/embed/{VID}",
        f"https://www.youtube.com\\@evil.com/watch?v={VID}",
        f"https://www.youtube.com/watch?v={VID}\n&x=1",
        f"https://www.you\ttube.com/watch?v={VID}",
    ],
)
def test_rejects_unexpected_hosts(url):
    with pytest.raises(InvalidURLError):
        parse_youtube_url(url)


@pytest.mark.parametrize("value", [None, 123, ["https://youtu.be/dQw4w9WgXcQ"], {"url": "x"}])
def test_rejects_non_strings(value):
    with pytest.raises(InvalidURLError):
        parse_youtube_url(value)


@pytest.mark.parametrize(
    "title, expected",
    [
        ("My Song", "My Song"),
        ('AC/DC: "Back in Black"', "AC DC Back in Black"),
        ("../../etc/passwd", "etc passwd"),
        ("a\x00b\x1fc", "a b c"),
        ("   ...   ", "fallback"),
        ("", "fallback"),
        (None, "fallback"),
        ("CON", "fallback"),
        ("Café — Ünïcødé 🎵", "Café — Ünïcødé 🎵"),
        ("x" * 300, "x" * 100),
    ],
)
def test_safe_filename(title, expected):
    assert safe_filename(title, fallback="fallback") == expected


def test_safe_filename_never_contains_separators():
    name = safe_filename("a/b\\c<d>e|f?g*h:i", fallback="f")
    assert not any(ch in name for ch in '/\\<>|?*:"')
