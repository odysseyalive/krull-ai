# Token utilities: prefer tiktoken if available, otherwise fallback to a conservative estimator.
try:
    import tiktoken

    # Use cl100k_base as a sane default; you can change per-model later.
    _enc = tiktoken.get_encoding("cl100k_base")
    def count_tokens(text: str) -> int:
        if not text:
            return 0
        return len(_enc.encode(text))
except Exception:
    # Conservative fallback: slightly smaller chars->token divisor than original
    def count_tokens(text: str) -> int:
        if not text:
            return 0
        # Use a conservative 3 chars/token (original used 4) so compaction triggers earlier
        return max(1, len(text) // 3)
