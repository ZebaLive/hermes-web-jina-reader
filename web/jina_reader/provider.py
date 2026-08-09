from dataclasses import dataclass
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from agent.web_search_provider import WebSearchProvider, get_provider_env
from tools.url_safety import is_safe_url
from tools.website_policy import check_website_access


DEFAULT_READER_URL = "https://r.jina.ai"
DEFAULT_PROCESSING_TIMEOUT = 60
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_ERROR_DETAIL_BYTES = 1000


@dataclass(frozen=True)
class _Settings:
    reader_url: str
    api_key: str
    processing_timeout: int


def _load_settings() -> _Settings:
    reader_url = get_provider_env("JINA_READER_URL") or DEFAULT_READER_URL
    try:
        parsed = urlsplit(reader_url)
        _ = parsed.port  # raises ValueError on an out-of-range port
    except (TypeError, ValueError) as exc:
        raise ValueError("JINA_READER_URL must be a valid HTTP(S) URL") from exc

    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("JINA_READER_URL must be a host-only HTTP(S) URL")

    timeout_value = get_provider_env("JINA_READER_TIMEOUT") or str(
        DEFAULT_PROCESSING_TIMEOUT
    )
    try:
        processing_timeout = int(timeout_value)
    except (TypeError, ValueError) as exc:
        raise ValueError("JINA_READER_TIMEOUT must be an integer from 1 to 180") from exc
    if not 1 <= processing_timeout <= 180:
        raise ValueError("JINA_READER_TIMEOUT must be from 1 to 180")

    return _Settings(
        reader_url=urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                f"{parsed.path.rstrip('/')}/",
                "",
                "",
            )
        ),
        api_key=get_provider_env("JINA_READER_API_KEY"),
        processing_timeout=processing_timeout,
    )


def _validate_target(url: str) -> None:
    """Gate a target URL through Hermes' own policy and SSRF checks.

    ``is_safe_url`` already handles scheme, private/benchmark ranges, always-blocked
    cloud metadata hostnames, proxy-DNS environments, and the
    ``security.allow_private_urls`` toggle — so this only adds the one thing it does
    not cover: refusing to hand embedded credentials to the Reader service.

    ponytail: best-effort, not an enforcement boundary. Reader performs the actual
    fetch, so no pre-flight check here can see a redirect into a private address or
    a name re-resolved after we checked it. Put an egress policy in front of Reader
    if you need a real boundary.
    """
    try:
        parsed = urlsplit(url)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid target URL: {url}") from exc

    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"Target URL must not embed credentials: {url}")

    if blocked := check_website_access(url):
        raise ValueError(blocked["message"])

    if not is_safe_url(url):
        raise ValueError(f"Blocked by Hermes URL safety policy: {url}")


def _parse_body(body: str, target_url: str) -> tuple[str, str]:
    content_marker = re.search(r"(?m)^Markdown Content:[ \t]*(?:\r?\n|$)", body)
    if content_marker is None:
        return target_url, body

    title_marker = re.search(
        r"(?m)^Title:[ \t]*([^\r\n]*)", body[: content_marker.start()]
    )
    title = title_marker.group(1).strip() if title_marker else target_url
    return title or target_url, body[content_marker.end() :]


def _read_body(response: httpx.Response) -> str:
    content_type = response.headers.get("content-type")
    if content_type and not content_type.split(";", 1)[0].strip().lower().startswith("text/"):
        raise ValueError(f"Jina Reader returned unsupported content type: {content_type}")

    content_length = response.headers.get("content-length")
    if content_length:
        try:
            declared_size = int(content_length)
        except ValueError:
            declared_size = 0
        if declared_size > MAX_RESPONSE_BYTES:
            raise ValueError("Jina Reader response exceeds the 5 MiB maximum")

    chunks = []
    total_size = 0
    for chunk in response.iter_bytes():
        total_size += len(chunk)
        if total_size > MAX_RESPONSE_BYTES:
            response.close()
            raise ValueError("Jina Reader response exceeds the 5 MiB maximum")
        chunks.append(chunk)

    body = b"".join(chunks).decode(response.encoding or "utf-8")
    if not body.strip():
        raise ValueError("Jina Reader returned an empty response")
    return body


def _fetch_one(
    client: httpx.Client, target_url: str, settings: _Settings
) -> dict[str, Any]:
    _validate_target(target_url)
    headers = {
        "X-Respond-With": "markdown",
        "X-Timeout": str(settings.processing_timeout),
    }
    if settings.api_key:
        headers["Authorization"] = f"Bearer {settings.api_key}"

    with client.stream(
        "POST",
        settings.reader_url,
        data={"url": target_url},
        headers=headers,
    ) as response:
        if not 200 <= response.status_code < 300:
            message = f"Jina Reader returned HTTP {response.status_code}"
            if retry_after := response.headers.get("retry-after"):
                message += f" (Retry-After: {retry_after})"
            detail = b""
            for chunk in response.iter_bytes():
                detail += chunk
                if len(detail) >= MAX_ERROR_DETAIL_BYTES:
                    break
            if decoded := detail[:MAX_ERROR_DETAIL_BYTES].decode("utf-8", "replace").strip():
                message += f": {decoded}"
            raise ValueError(message)
        body = _read_body(response)

    title, content = _parse_body(body, target_url)
    return {
        "url": target_url,
        "title": title,
        "content": content,
        "raw_content": content,
        "metadata": {
            "engine": "jina-reader",
            "sourceURL": target_url,
            "title": title,
            "status_code": response.status_code,
            "content_type": response.headers.get("content-type"),
        },
    }


class JinaReaderProvider(WebSearchProvider):
    @property
    def name(self) -> str:
        return "jina-reader"

    @property
    def display_name(self) -> str:
        return "Jina Reader"

    def is_available(self) -> bool:
        return True

    def supports_search(self) -> bool:
        return False

    def supports_extract(self) -> bool:
        return True

    def extract(self, urls: list[str], **kwargs: Any) -> list[dict[str, Any]]:
        if not urls:
            return []
        settings = _load_settings()

        timeout = httpx.Timeout(settings.processing_timeout + 5.0)
        with httpx.Client(timeout=timeout) as client:
            results = []
            for url in urls:
                try:
                    results.append(_fetch_one(client, url, settings))
                except Exception as exc:
                    results.append({"url": url, "title": "", "error": str(exc)})
        return results

    def get_setup_schema(self) -> dict[str, Any]:
        return {
            "name": "Jina Reader",
            "badge": "free · self-hostable",
            "tag": "Extract only (no search); hosted r.jina.ai or your own Reader.",
            "env_vars": [
                {
                    "key": "JINA_READER_URL",
                    "prompt": "Reader base URL (blank for https://r.jina.ai)",
                    "url": "https://github.com/jina-ai/reader",
                },
                {
                    "key": "JINA_READER_API_KEY",
                    "prompt": "Jina API key (blank for the keyless free tier)",
                    "url": "https://jina.ai/reader/",
                },
                {
                    "key": "JINA_READER_TIMEOUT",
                    "prompt": "Reader processing timeout in seconds, 1-180 (blank for 60)",
                    "url": "",
                },
            ],
        }
