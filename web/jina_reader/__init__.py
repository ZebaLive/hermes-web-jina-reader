from .provider import JinaReaderProvider


def register(ctx) -> None:
    ctx.register_web_search_provider(JinaReaderProvider())
