"""Hostname comparison helpers."""

_ASCII_LOWER = str.maketrans({
    chr(code): chr(code + 32) for code in range(ord("A"), ord("Z") + 1)
})


def ascii_lowercase(value: str) -> str:
    """Lower only ASCII A-Z, preserving every other code point exactly.

    Python's Unicode lowercasing maps U+212A KELVIN SIGN to ASCII "k". That
    is correct Unicode behaviour, but wrong for host allowlist comparisons
    where non-ASCII lookalikes must stay distinct from ASCII labels.
    """
    return value.translate(_ASCII_LOWER)
