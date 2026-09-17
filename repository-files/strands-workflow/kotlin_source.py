"""Lexical masking for the supported Kotlin test declaration inventory."""

import re


def _comment_end(text: str, index: int) -> int:
    if text.startswith("//", index):
        end = text.find("\n", index)
        return len(text) if end == -1 else end
    index += 2
    depth = 1
    while index < len(text) and depth:
        if text.startswith("/*", index):
            depth += 1
            index += 2
        elif text.startswith("*/", index):
            depth -= 1
            index += 2
        else:
            index += 1
    return index


def _template_end(text: str, index: int) -> int:
    depth = 1
    while index < len(text) and depth:
        if text.startswith(("//", "/*"), index):
            index = _comment_end(text, index)
        elif text[index] in {'"', "'"}:
            index = _literal_end(text, index)
        else:
            depth += (text[index] == "{") - (text[index] == "}")
            index += 1
    return index


def _literal_end(text: str, index: int) -> int:
    quote = '"""' if text.startswith('"""', index) else text[index]
    index += len(quote)
    while index < len(text):
        if text.startswith(quote, index):
            return index + len(quote)
        if len(quote) == 1 and text[index] == "\\":
            index += 2
        elif quote != "'" and text.startswith("${", index):
            index = _template_end(text, index + 2)
        else:
            index += 1
    return len(text)


def mask_kotlin(text: str, *, keep_strings: bool = False) -> str:
    """Blank comments and literals while preserving offsets and line breaks."""
    result = list(text)
    index = 0
    while index < len(text):
        start = index
        comment = text.startswith(("//", "/*"), index)
        if comment:
            index = _comment_end(text, index)
        elif text[index] in {'"', "'"}:
            index = _literal_end(text, index)
        else:
            index += 1
            continue
        if comment or not keep_strings:
            result[start:index] = re.sub(r"[^\r\n]", " ", text[start:index])
    return "".join(result)
