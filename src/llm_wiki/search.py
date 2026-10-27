"""BM25-lite search over wiki pages."""

import math
import re
from collections import Counter
from dataclasses import dataclass

from llm_wiki.pages import iter_pages
from llm_wiki.registry import WikiRef

K1 = 1.2
B = 0.75
_TOKEN_RE = re.compile(r"\w+")


@dataclass
class SearchHit:
    wiki: str
    path: str
    id: str
    title: str
    summary: str
    status: str
    last_verified: str
    score: float


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text.lower()) if len(t) >= 2]


def _virtual_doc(fm, body: str) -> list[str]:
    tokens: list[str] = []
    for text, weight in (
        (fm.title, 3),
        (fm.summary, 2),
        (" ".join(fm.tags), 2),
        (fm.id, 2),
        (body, 1),
    ):
        tokens += tokenize(text) * weight
    return tokens


def search(
    refs: list[WikiRef],
    query: str,
    top: int = 8,
    status: str | None = None,
    section: str | None = None,
) -> list[SearchHit]:
    q_terms = set(tokenize(query))
    docs: list[tuple[SearchHit, Counter, int]] = []
    for ref in refs:
        for page in iter_pages(ref.path):
            fm = page.fm
            if fm is None:
                continue
            if status is not None and fm.status != status:
                continue
            parts = page.rel.split("/")
            if section is not None and not (len(parts) > 1 and parts[0] == section):
                continue
            tokens = _virtual_doc(fm, page.body)
            hit = SearchHit(
                wiki=ref.name,
                path=str(page.path),
                id=fm.id,
                title=fm.title,
                summary=fm.summary,
                status=fm.status,
                last_verified=fm.last_verified.isoformat(),
                score=0.0,
            )
            docs.append((hit, Counter(tokens), len(tokens)))
    if not docs or not q_terms:
        return []
    n = len(docs)
    avgdl = sum(d[2] for d in docs) / n or 1.0
    idf = {}
    for term in q_terms:
        df = sum(1 for _, tf, _ in docs if term in tf)
        idf[term] = math.log(1 + (n - df + 0.5) / (df + 0.5))
    hits = []
    for hit, tf, dl in docs:
        score = 0.0
        for term in q_terms:
            f = tf.get(term, 0)
            if f:
                score += idf[term] * f * (K1 + 1) / (f + K1 * (1 - B + B * dl / avgdl))
        if score > 0:
            hit.score = score
            hits.append(hit)
    hits.sort(key=lambda h: (-h.score, h.id))
    return hits[:top]
