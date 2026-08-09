"""Self-check for the pure helpers. Run: python test_provider.py

No network, no framework. Stubs the Hermes host module so the provider imports
standalone.
"""

import os
import sys
import types

def _module(name, **attrs):
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    sys.modules[name] = mod
    return mod


# Mirrors agent/web_search_provider.py: single argument, returns "" when unset,
# and strips. A two-argument stub here would hide the real signature.
_module(
    "agent.web_search_provider",
    WebSearchProvider=type("WebSearchProvider", (), {}),
    get_provider_env=lambda name: (os.environ.get(name) or "").strip(),
)
_module("agent", web_search_provider=sys.modules["agent.web_search_provider"])
# Policy/SSRF gating is Hermes', tested upstream; here it just has to be called.
_module("tools")
_module("tools.url_safety", is_safe_url=lambda url: _SAFE)
_module("tools.website_policy", check_website_access=lambda url: _POLICY)
_SAFE = True
_POLICY = None
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web.jina_reader.provider import (  # noqa: E402
    _load_settings,
    _parse_body,
    _validate_target,
)


def _env(**overrides):
    for key in [k for k in os.environ if k.startswith("JINA_READER_")]:
        del os.environ[key]
    os.environ.update(overrides)


def test_parse_body():
    assert _parse_body("Title: Hi\n\nMarkdown Content:\n# Body", "u") == ("Hi", "# Body")
    # no marker: whole body is content, URL is the title
    assert _parse_body("plain text", "u") == ("u", "plain text")
    # empty title falls back to the URL
    assert _parse_body("Title:   \n\nMarkdown Content:\ntext", "u") == ("u", "text")
    # only the first marker splits; later look-alikes stay in the content
    assert _parse_body("Markdown Content:\nA\nTitle: fake\n", "u") == ("u", "A\nTitle: fake\n")


def test_load_settings():
    _env()
    settings = _load_settings()
    assert settings.reader_url == "https://r.jina.ai/"
    assert settings.processing_timeout == 60
    assert settings.api_key == ""

    # trailing slash normalized, proxy path prefix preserved
    _env(JINA_READER_URL="http://localhost:3333")
    assert _load_settings().reader_url == "http://localhost:3333/"
    _env(JINA_READER_URL="http://localhost:3333/read")
    assert _load_settings().reader_url == "http://localhost:3333/read/"

    # get_provider_env strips, so surrounding whitespace must not break anything
    _env(JINA_READER_TIMEOUT=" 30 ", JINA_READER_API_KEY="  k  ")
    settings = _load_settings()
    assert settings.processing_timeout == 30
    assert settings.api_key == "k"

    # blank means "use the default", not "invalid"
    _env(JINA_READER_URL="", JINA_READER_TIMEOUT="")
    settings = _load_settings()
    assert settings.reader_url == "https://r.jina.ai/"
    assert settings.processing_timeout == 60

    for bad in [
        {"JINA_READER_URL": "http://localhost:99999"},
        {"JINA_READER_URL": "ftp://example.com"},
        {"JINA_READER_URL": "https://u:p@example.com"},
        {"JINA_READER_URL": "https://example.com/?q=1"},
        {"JINA_READER_TIMEOUT": "0"},
        {"JINA_READER_TIMEOUT": "181"},
        {"JINA_READER_TIMEOUT": "abc"},
    ]:
        _env(**bad)
        try:
            _load_settings()
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad}")
    _env()


def _blocks(url):
    try:
        _validate_target(url)
    except ValueError:
        return True
    return False


def test_validate_target():
    global _SAFE, _POLICY
    _SAFE, _POLICY = True, None

    # the one check that is ours: never hand credentials to the Reader service
    assert _blocks("https://user:pw@example.com")
    assert _blocks("https://user@example.com")
    assert not _blocks("https://example.com/path?q=1")

    # both Hermes gates must actually be consulted, and each alone must block
    _SAFE = False
    assert _blocks("https://example.com")
    _SAFE = True
    _POLICY = {"message": "blocked by policy"}
    assert _blocks("https://example.com")
    _POLICY = None
    assert not _blocks("https://example.com")


if __name__ == "__main__":
    for name, case in sorted(globals().items()):
        if name.startswith("test_"):
            case()
            print(f"ok {name}")
    print("all passed")
