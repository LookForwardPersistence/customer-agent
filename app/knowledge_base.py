"""Knowledge base loader + retrieval.

Retrieval is deliberately dependency-free: character-bigram scoring
(works well for short Chinese FAQ text). Trade-off documented in README:
swapping to embeddings/vector search only requires re-implementing
`search()` with the same signature.
"""

from __future__ import annotations

import json
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"


def _bigrams(text: str) -> set[str]:
    text = "".join(text.split())
    return {text[i : i + 2] for i in range(len(text) - 1)} | set(text)


class KnowledgeBase:
    def __init__(self, path: Path | None = None):
        path = path or DATA_DIR / "knowledge_base.json"
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        self.meta = raw["store"]
        self.entries = raw["entries"]
        self._index = [
            {"id": e["id"], "topic": e["topic"], "bigrams": _bigrams(e["topic"] * 3 + e["content"])}
            for e in self.entries
        ]

    def search(self, query: str, top_k: int = 3, min_score: float = 0.12) -> list[dict]:
        """Return the most relevant entries as [{id, topic, content, score}]."""
        q = _bigrams(query)
        scored = []
        for e, idx in zip(self.entries, self._index):
            if not q:
                continue
            overlap = len(q & idx["bigrams"])
            score = overlap / max(len(q), 1)
            if score >= min_score:
                scored.append({**e, "score": round(score, 3)})
        scored.sort(key=lambda x: -x["score"])
        return scored[:top_k]


kb = KnowledgeBase()
