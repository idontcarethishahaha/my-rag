"""
轻量 BM25 稀疏检索（纯 Python，无外部依赖）

参考 RAG-Pro 的 hybrid search 稀疏检索部分：
- 中文按字符 bigram 分词
- 英文按空格 + 标点分词
- BM25 公式：IDF * (k1+1) * tf / (k1*(1-b+b*dl/avgdl))
"""
from __future__ import annotations

import re
import math
import logging
from collections import Counter

logger = logging.getLogger(__name__)

_K1 = 1.5
_B = 0.75


def _tokenize(text: str) -> list[str]:
    """中英文混合分词：中文 bigram + 英文单词"""
    tokens: list[str] = []
    for m in re.finditer(r"[a-zA-Z]{3,}", text):
        tokens.append(m.group().lower())
    cn_chars = re.findall(r"[\u4e00-\u9fa5]", text)
    for i in range(len(cn_chars) - 1):
        tokens.append(cn_chars[i] + cn_chars[i + 1])
    if len(cn_chars) == 1:
        tokens.append(cn_chars[0])
    return tokens


class BM25Index:
    """BM25 索引，支持从文档列表构建和查询"""

    def __init__(self, docs: list[str]):
        self._doc_tokens: list[list[str]] = []
        self._doc_len: list[int] = []
        self._tf: list[Counter] = []
        self._df: Counter = Counter()
        self._avgdl: float = 0.0
        self._n: int = 0
        if docs:
            self._build(docs)

    def _build(self, docs: list[str]) -> None:
        self._n = len(docs)
        total_len = 0
        for doc in docs:
            tokens = _tokenize(doc or "")
            self._doc_tokens.append(tokens)
            self._doc_len.append(len(tokens))
            total_len += len(tokens)
            tf = Counter(tokens)
            self._tf.append(tf)
            for word in tf:
                self._df[word] += 1
        self._avgdl = total_len / self._n if self._n > 0 else 0.0
        logger.info(f"[BM25] 索引构建完成: {self._n} 篇文档, avgdl={self._avgdl:.1f}")

    def search(self, query: str, top_k: int = 20) -> list[tuple[int, float]]:
        """搜索 query，返回 [(doc_index, score), ...] 按分数降序"""
        if self._n == 0:
            return []
        q_tokens = _tokenize(query)
        if not q_tokens:
            return []
        scores: list[float] = [0.0] * self._n
        for q_term in q_tokens:
            df = self._df.get(q_term, 0)
            if df == 0:
                continue
            idf = math.log((self._n - df + 0.5) / (df + 0.5) + 1.0)
            for i in range(self._n):
                tf = self._tf[i].get(q_term, 0)
                if tf == 0:
                    continue
                dl = self._doc_len[i]
                denom = _K1 * (1 - _B + _B * dl / self._avgdl) if self._avgdl > 0 else _K1
                score = idf * (_K1 + 1) * tf / (denom + tf)
                scores[i] += score
        ranked = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)
        return [(idx, s) for idx, s in ranked[:top_k] if s > 0]


def rrf_fuse(
    dense_results: list[tuple[int, float]],
    sparse_results: list[tuple[int, float]],
    k: int = 60,
) -> list[tuple[int, float]]:
    """
    Reciprocal Rank Fusion (RRF) 融合两路检索结果

    参数：
        dense_results: [(doc_index, score), ...]
        sparse_results: [(doc_index, score), ...]
        k: RRF 参数，默认 60

    返回：融合后 [(doc_index, rrf_score), ...] 按分数降序
    """
    rrf_scores: dict[int, float] = {}
    for rank, (idx, _) in enumerate(dense_results, start=1):
        rrf_scores[idx] = rrf_scores.get(idx, 0.0) + 1.0 / (k + rank)
    for rank, (idx, _) in enumerate(sparse_results, start=1):
        rrf_scores[idx] = rrf_scores.get(idx, 0.0) + 1.0 / (k + rank)
    result = sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)
    return result
