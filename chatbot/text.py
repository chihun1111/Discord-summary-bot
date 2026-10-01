"""Korean-friendly lexical indexing, with no external tokenizer or model."""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable


def words(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return re.findall(r"[^\W_]+", normalized, flags=re.UNICODE)


def encoded(prefix: str, word: str) -> str:
    # ASCII terms keep the FTS query grammar separate from untrusted chat text.
    return prefix + word.encode("utf-8").hex()


def index_terms(text: str) -> str:
    result: list[str] = []
    for word in words(text):
        result.append(encoded("w", word))
        result.extend(encoded("b", word[i:i + 2]) for i in range(len(word) - 1))
    return " ".join(result)


def match_query(text: str) -> str:
    if not text.strip() or len(text) > 200:
        raise ValueError("검색어를 1~200자로 입력하세요.")
    query_words = list(dict.fromkeys(words(text)))
    if not query_words or len(query_words) > 12:
        raise ValueError("검색어는 문자/숫자를 포함하는 단어 1~12개로 입력하세요.")
    clauses: list[str] = []
    for word in query_words:
        exact = encoded("w", word)
        if len(word) == 1:
            clauses.append(exact)  # One-letter queries match whole tokens only.
        else:
            phrase = " ".join(encoded("b", word[i:i + 2]) for i in range(len(word) - 1))
            clauses.append(f'({exact} OR "{phrase}")')
    return " AND ".join(clauses)


def safe_chunks(text: str, limit: int = 1850) -> Iterable[str]:
    while text:
        if len(text) <= limit:
            yield text
            break
        cut = text.rfind("\n", 0, limit + 1)
        if cut < limit // 2:
            cut = limit
        yield text[:cut]
        text = text[cut:].lstrip("\n")
