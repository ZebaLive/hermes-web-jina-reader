# Hermes Jina Reader Provider

A small, extraction-only Hermes Agent backend that sends public HTTP(S) URLs to Jina Reader and returns Markdown. It does not provide search, a new Hermes tool, HTML/document upload, screenshots, Reader search, retries, fallback extraction, or Trafilatura.

## Requirements

- Python 3.10 or newer
- Hermes Agent with directory-plugin support
- `httpx>=0.27,<1`

The plugin has no parser dependency. Install the project dependency in the environment running Hermes, for example:

```bash
python -m pip install 'httpx>=0.27,<1'
```

## Install as a Hermes directory plugin

The supported install mode is a directory plugin, not a Python entry point. Copy or link this repository's `web/jina_reader` directory to:

```text
~/.hermes/plugins/web/jina_reader
```

Do not add an `hermes_agent.plugins` entry point. Enable the directory plugin and select its backend in Hermes configuration:

```yaml
plugins:
  enabled:
    - web/jina_reader

web:
  extract_backend: jina-reader
```

The manifest name is `web-jina-reader`; the registered backend name is `jina-reader`.

## Configuration

| Variable                    | Default             | Meaning                                                                                                                                                                    |
| --------------------------- | ------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `JINA_READER_URL`           | `https://r.jina.ai` | Reader root URL. It must be an HTTP(S) host-only URL and may include a reverse-proxy path prefix.                                                                          |
| `JINA_READER_API_KEY`       | unset               | Optional non-empty token sent as `Authorization: Bearer <token>`. Whitespace-only values are treated as unset.                                                             |
| `JINA_READER_TIMEOUT`       | `60`                | Reader processing timeout in seconds; an integer from `1` through `180`.                                                                                                   |

Values are read through Hermes' `get_provider_env`, so they resolve from `os.environ` first and then `~/.hermes/.env`, and are stripped. A blank value means "use the default".

For a self-hosted Reader, use:

```bash
export JINA_READER_URL=http://localhost:3333
```

The Reader service URL may be local — `JINA_READER_URL` is never subject to the target checks below, which apply only to the URLs sent to Reader.

Target URLs are gated by Hermes' own `tools.url_safety.is_safe_url` and `tools.website_policy.check_website_access`, the same helpers the built-in Firecrawl provider uses, so this plugin honors your existing `security.allow_private_urls` toggle, website blocklist, and always-blocked cloud metadata endpoints instead of adding a second set of rules. The provider adds only one check of its own: target URLs may not embed credentials, which would otherwise be handed to the Reader service.

That gate is best-effort, not an enforcement boundary. Reader performs the actual fetch, so no pre-flight check can see a redirect from a public URL into a private address, or a name re-resolved after the check. Put an egress policy in front of Reader if you need a real boundary.

To read private or localhost targets, enable it the Hermes way — `security.allow_private_urls: true` in `config.yaml`, or `HERMES_ALLOW_PRIVATE_URLS=true`. There is no plugin-specific opt-in.

## Request and result behavior

For every target URL, the provider sends a form-encoded `POST` with `url=<target>`, `X-Respond-With: markdown`, and the configured `X-Timeout`. It always requests Markdown; any optional format argument is ignored. An API key adds a Bearer header.

The complete parsed Markdown is placed in both `content` and `raw_content`. Hermes owns character-limit truncation and its truncation footer. The provider enforces a 5 MiB response limit, including streamed responses. A non-success, non-text, empty, invalid, or oversized response becomes an error for that URL. URLs are processed independently, so one failure does not abort the others. A failed URL yields `{"url", "title": "", "error"}`, matching the built-in Firecrawl provider's per-URL failure shape. There is no retry or fallback; HTTP 429 errors include `Retry-After` when supplied. Non-success responses append up to 1 KiB of the Reader error body to the message, so Reader's own diagnostics (for example `ParamValidationError(url): Domain 'example.com' could not be resolved`) reach the caller.

## Tests

`python tests/test_provider.py` — stdlib asserts over the pure helpers, no network and no test framework.

The provider parses Jina's optional `Title:` and `Markdown Content:` prelude. Without the marker, the complete response body is treated as Markdown and the target URL is used as the title.
