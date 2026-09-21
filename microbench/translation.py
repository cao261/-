"""
Translation utilities for MicroBench.

Provides:
- chunk_section_text(): split long sections into LLM-friendly chunks at
  paragraph boundaries, with overlap to preserve context continuity.
- TranslationCache: in-memory LRU cache for translations, keyed by section
  text hash + section_key + model name. Skips redundant LLM calls when the
  user re-clicks translate or reopens a paper.
"""

import hashlib
import re
from collections import OrderedDict
from threading import Lock
from typing import Optional


# ---------------------------------------------------------------------------
# Section chunking
# ---------------------------------------------------------------------------

# Soft target chunk size (chars). Smaller chunks → shorter LLM round-trip
# time → less likely to hit upstream load-balancer timeouts (a 60s+ M2.7
# thinking response is exactly when connections get killed by intermediaries).
CHUNK_TARGET_SIZE = 5000

# Hard ceiling for a single chunk — never go above this even for paragraphs.
CHUNK_HARD_MAX = 7000

# Overlap between consecutive chunks so the LLM has continuity context.
CHUNK_OVERLAP = 300


_PARAGRAPH_RE = re.compile(r"\n\s*\n")


def chunk_section_text(text: str,
                       target: int = CHUNK_TARGET_SIZE,
                       hard_max: int = CHUNK_HARD_MAX,
                       overlap: int = CHUNK_OVERLAP) -> list[str]:
    """
    Split a section into chunks at paragraph boundaries.

    Strategy:
    1. If text fits in `target`, return [text].
    2. Otherwise, split on double-newlines (paragraph boundaries).
    3. Greedily accumulate paragraphs into chunks of ~`target` chars.
    4. Add `overlap` chars from the previous chunk's tail to each chunk so
       the LLM keeps continuity (e.g., pronoun references, equations).
    5. If a single paragraph exceeds `hard_max`, split it on sentence ends.

    Returns a list of non-empty chunks (in order).
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= target:
        return [text]

    paragraphs = [p.strip() for p in _PARAGRAPH_RE.split(text) if p.strip()]
    if not paragraphs:
        return [text[:hard_max]]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for para in paragraphs:
        para_len = len(para)
        # If this paragraph alone exceeds hard_max, flush current then split para
        if para_len > hard_max:
            if current:
                chunks.append("\n\n".join(current))
                current, current_len = [], 0
            chunks.extend(_split_oversized(para, hard_max))
            continue

        # Adding this paragraph would exceed target → flush & start new chunk
        if current and current_len + para_len + 2 > target:
            chunks.append("\n\n".join(current))
            # Start next chunk with overlap from the previous one
            overlap_text = _tail_overlap("\n\n".join(current), overlap)
            current = [overlap_text, para] if overlap_text else [para]
            current_len = sum(len(p) for p in current)
        else:
            current.append(para)
            current_len += para_len

    if current:
        chunks.append("\n\n".join(current))

    return [c.strip() for c in chunks if c.strip()]


def _split_oversized(para: str, hard_max: int) -> list[str]:
    """Split a single paragraph that exceeds hard_max on sentence ends."""
    sentences = re.split(r"(?<=[.!?])\s+", para)
    out: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for s in sentences:
        if cur and cur_len + len(s) > hard_max:
            out.append(" ".join(cur))
            cur, cur_len = [], 0
        cur.append(s)
        cur_len += len(s) + 1
    if cur:
        out.append(" ".join(cur))
    return [o for o in out if o.strip()]


def _tail_overlap(text: str, overlap: int) -> str:
    """Return the last `overlap` chars of `text` (rounded to word boundary)."""
    if len(text) <= overlap:
        return text
    tail = text[-overlap:]
    # Round to a word boundary so we don't cut mid-word
    space = tail.find(" ")
    if space > 0 and space < overlap // 2:
        tail = tail[space + 1:]
    return tail


# ---------------------------------------------------------------------------
# Translation cache
# ---------------------------------------------------------------------------

DEFAULT_CACHE_SIZE = 64


class TranslationCache:
    """
    Simple thread-safe LRU cache for translation results.

    Key: (section_key, text_hash, model_name)
    Value: translation string + metadata (char_count, chunked flag)

    LRU eviction keeps the cache bounded; entries are evicted when full.
    Thread safety via a single Lock — the cache is on a single FastAPI worker
    so contention is negligible.
    """

    def __init__(self, max_size: int = DEFAULT_CACHE_SIZE):
        self._max_size = max_size
        self._store: "OrderedDict[tuple, dict]" = OrderedDict()
        self._lock = Lock()
        self.hits = 0
        self.misses = 0

    @staticmethod
    def _key_for(section_key: str, text: str, model: str) -> tuple:
        text_hash = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]
        return (section_key or "", text_hash, model or "")

    def get(self, section_key: str, text: str, model: str) -> Optional[dict]:
        key = self._key_for(section_key, text, model)
        with self._lock:
            if key not in self._store:
                self.misses += 1
                return None
            self._store.move_to_end(key)
            self.hits += 1
            # Return a copy so callers can't mutate the cache
            return dict(self._store[key])

    def put(self, section_key: str, text: str, model: str, value: dict) -> None:
        key = self._key_for(section_key, text, model)
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
                self._store[key] = dict(value)
            else:
                self._store[key] = dict(value)
                if len(self._store) > self._max_size:
                    self._store.popitem(last=False)

    def stats(self) -> dict:
        with self._lock:
            return {
                "size": len(self._store),
                "max_size": self._max_size,
                "hits": self.hits,
                "misses": self.misses,
            }

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
            self.hits = 0
            self.misses = 0


# Module-level singleton — initialized by app.py at startup
_cache: Optional[TranslationCache] = None


def get_cache() -> TranslationCache:
    """Get (or lazily create) the global translation cache."""
    global _cache
    if _cache is None:
        _cache = TranslationCache()
    return _cache


def reset_cache() -> None:
    """Reset the global cache (mainly for tests)."""
    global _cache
    _cache = TranslationCache()


# ---------------------------------------------------------------------------
# Cross-section RAG helper
# ---------------------------------------------------------------------------

# How many chars of each section to include in cross-section context.
# Stay well under model context window even for 8 sections.
PER_SECTION_BUDGET = 6000


def build_cross_section_context(sections: list[dict],
                                question: str,
                                per_section_budget: int = PER_SECTION_BUDGET) -> str:
    """
    Build a multi-section context block for cross-chapter QA.

    Each section is labeled with [section_key] so the LLM can reference it.
    Sections longer than `per_section_budget` are truncated (kept head + tail)
    so the total stays bounded.
    """
    parts = [f"用户问题: {question}\n\n--- 论文章节内容 (按章节标注) ---\n"]
    for s in sections:
        key = (s.get("section") or "?").upper()
        title = s.get("title") or key
        text = (s.get("text") or "").strip()
        if len(text) > per_section_budget:
            half = per_section_budget // 2
            text = text[:half] + "\n\n... [中段省略] ...\n\n" + text[-half:]
        parts.append(f"\n[{key}] {title}\n{text}\n")
    return "".join(parts)


__all__ = [
    "CHUNK_TARGET_SIZE",
    "CHUNK_HARD_MAX",
    "CHUNK_OVERLAP",
    "PER_SECTION_BUDGET",
    "chunk_section_text",
    "TranslationCache",
    "get_cache",
    "reset_cache",
    "build_cross_section_context",
]