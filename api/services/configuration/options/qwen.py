"""DashScope Qwen realtime speech options shared by configuration and runtime."""

from urllib.parse import urlencode, urlsplit, urlunsplit

QWEN_REALTIME_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
QWEN_REALTIME_URLS = [
    QWEN_REALTIME_URL,
    "wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime",
]
QWEN_STT_MODEL = "qwen3-asr-flash-realtime"
QWEN_TTS_MODEL = "qwen3-tts-flash-realtime"
QWEN_STT_LANGUAGES = [
    "auto",
    "zh",
    "yue",
    "en",
    "ja",
    "de",
    "ko",
    "ru",
    "fr",
    "pt",
    "ar",
    "it",
    "es",
    "hi",
    "id",
    "th",
    "tr",
    "uk",
    "vi",
    "cs",
    "da",
    "fil",
    "fi",
    "is",
    "ms",
    "no",
    "pl",
    "sv",
]
QWEN_TTS_LANGUAGES = [
    "Auto",
    "Chinese",
    "English",
    "German",
    "Italian",
    "Portuguese",
    "Spanish",
    "Japanese",
    "Korean",
    "French",
    "Russian",
]
QWEN_TTS_VOICES = ["Cherry", "Serena", "Ethan", "Chelsie", "Ryan", "Vivian"]


def validate_qwen_base_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "wss"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") != "/api-ws/v1/realtime"
    ):
        raise ValueError(
            "Qwen base_url must be a wss:// URL ending in /api-ws/v1/realtime "
            "without credentials, query parameters, or a fragment"
        )
    return value.rstrip("/")


def qwen_realtime_url(base_url: str, model: str) -> str:
    parsed = urlsplit(validate_qwen_base_url(base_url))
    return urlunsplit(parsed._replace(query=urlencode({"model": model})))
