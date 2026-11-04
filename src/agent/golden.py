"""Retrieves past expert work ("golden trios") similar to the user's question.

Prototype of the design in docs/requirements.md: files stand in for the Cloud Storage bucket and an
in-memory similarity search stands in for pgvector. Embeddings are cached on disk by content. If the
embedding service is down, retrieval falls back to word overlap, so answers still benefit from it.
"""
import hashlib
import json
import logging
import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from agent.observability import emit
from agent.resilience import CircuitBreaker

EMBEDDING_MODEL = "gemini-embedding-001"
MIN_SIMILARITY = 0.55  # below this a trio is not really about the same thing
MIN_WORD_OVERLAP = 0.15

Embedder = Callable[[Sequence[str]], list[list[float]]]

_STOPWORDS = frozenset(
    "a an and are by did do does for from how i in is it me of on or our show the to us was we what which who why with you".split()
)


@dataclass(frozen=True)
class Trio:
    id: str
    question: str
    interpretation: str
    sql: str
    report_guidance: str


def load_trios(directory: Path) -> list[Trio]:
    return [Trio(**json.loads(path.read_text())) for path in sorted(directory.glob("*.json"))]


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower())) - _STOPWORDS


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / norm if norm else 0.0


class GoldenIndex:
    def __init__(
        self,
        trios: Sequence[Trio],
        embed: Embedder | None,
        cache_path: Path | None = None,
        top_k: int = 2,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self.trios, self.embed, self.cache_path, self.top_k = list(trios), embed, cache_path, top_k
        self.breaker = breaker or CircuitBreaker("embeddings", threshold=2, cooldown_s=120)
        self._vectors: list[list[float]] | None = None

    def search(self, question: str) -> list[Trio]:
        """The most relevant trios. Never raises: retrieval improves answers but isn't required."""
        try:
            found, method = self._search_by_embedding(question), "embedding"
        except Exception as e:
            self.breaker.record_failure()
            emit("golden_embedding_failed", logging.WARNING, error=str(e)[:200])
            found, method = None, "words"
        if found is None:
            found, method = self._search_by_words(question), "words"
        emit("golden_retrieved", method=method, trios=[t.id for t in found])
        return found

    def _search_by_embedding(self, question: str) -> list[Trio] | None:
        if self.embed is None or not self.trios or not self.breaker.allow():
            return None
        vectors = self._trio_vectors()
        query = self.embed([question])[0]
        self.breaker.record_success()
        scored = sorted(zip((_cosine(query, v) for v in vectors), self.trios), key=lambda s: -s[0])
        return [trio for score, trio in scored[:self.top_k] if score >= MIN_SIMILARITY]

    def _search_by_words(self, question: str) -> list[Trio]:
        asked = _words(question)
        scored = []
        for trio in self.trios:
            known = _words(f"{trio.question} {trio.interpretation}")
            overlap = len(asked & known) / len(asked) if asked else 0.0
            if overlap >= MIN_WORD_OVERLAP:
                scored.append((overlap, trio))
        return [trio for _, trio in sorted(scored, key=lambda s: -s[0])[:self.top_k]]

    def _trio_vectors(self) -> list[list[float]]:
        if self._vectors is None:
            texts = [f"{t.question}\n{t.interpretation}" for t in self.trios]
            keys = [hashlib.sha256(f"{EMBEDDING_MODEL}:{text}".encode()).hexdigest() for text in texts]
            cache = self._read_cache()
            missing = [(k, text) for k, text in zip(keys, texts) if k not in cache]
            if missing:
                new = self.embed([text for _, text in missing])
                cache.update({k: v for (k, _), v in zip(missing, new)})
                self._write_cache(cache)
            self._vectors = [cache[k] for k in keys]
        return self._vectors

    def _read_cache(self) -> dict[str, list[float]]:
        try:
            return json.loads(self.cache_path.read_text()) if self.cache_path else {}
        except (OSError, ValueError):
            return {}

    def _write_cache(self, cache: dict[str, list[float]]) -> None:
        if self.cache_path:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(cache))


def gemini_embedder(timeout_s: float = 5) -> Embedder:
    from google import genai
    from google.genai import types

    client = genai.Client(http_options=types.HttpOptions(
        timeout=int(timeout_s * 1000), retry_options=types.HttpRetryOptions(attempts=1),
    ))

    def embed(texts: Sequence[str]) -> list[list[float]]:
        response = client.models.embed_content(model=EMBEDDING_MODEL, contents=list(texts))
        return [e.values for e in response.embeddings]

    return embed


def format_examples(trios: Sequence[Trio]) -> str:
    """The trios as prompt text. Empty when nothing relevant was found."""
    if not trios:
        return ""
    blocks = [
        f"### Past question: {t.question}\nHow analysts interpreted it: {t.interpretation}\n"
        f"SQL they used:\n{t.sql}\nHow they reported it: {t.report_guidance}"
        for t in trios
    ]
    return (
        "\n\n## How our analysts handled similar questions\n"
        "Follow their definitions and approach, and adapt the SQL to the current question. "
        "Run your own queries for every number. Say so if you use one of these definitions.\n\n"
        + "\n\n".join(blocks)
    )
