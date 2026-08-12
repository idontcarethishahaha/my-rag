"""
文本切块器 —— 参考 RAG-Pro 的 RecursiveChunker 实现。
核心思路（优先保证语义完整，而不是硬切字符数）：
  1. 按自然分隔符递归拆分（段落 → 换行 → 句末标点 → 逗号 → 空格 → 逐字）
  2. 基于 token 估算（中文约 1.5 字/token，英文约 4 字/token）而不是字符数
  3. 相邻块之间加上下文重叠（overlap），避免关键信息刚好在切分处丢失
"""
from __future__ import annotations
from dataclasses import dataclass, field

from langchain_core.documents import Document


# ==================================
# 数据结构
# ==================================
@dataclass
class TextChunk:
    text: str
    chunk_index: int = 0
    page_number: int | None = None
    section_title: str | None = None
    token_count: int = 0
    metadata: dict = field(default_factory=dict)


# ==================================
# Token 估算
# ==================================
def _estimate_tokens(text: str) -> int:
    """
    粗估 token 数（与 RAG-Pro 一致）：
    - 中文：每 1.5 字算 1 token
    - 非中文：每 4 字符算 1 token
    """
    if not text:
        return 0
    chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    other_chars = len(text) - chinese_chars
    return int(chinese_chars / 1.5 + other_chars / 4)


# ==================================
# 递归分块器（核心）
# ==================================
class RecursiveChunker:
    """RAG-Pro 风格：按自然分隔符递归拆分，带 overlap。"""

    SEPARATORS = ["\n\n", "\n", ". ", "！", "？", "。", "；", "，", "、", " ", ""]

    def __init__(self, chunk_size_tokens: int = 512, chunk_overlap_tokens: int = 64):
        self.chunk_size = chunk_size_tokens
        self.chunk_overlap = chunk_overlap_tokens

    # ---------- 递归拆分 ----------
    def _split_by_separator(self, text: str, separators: list[str]) -> list[str]:
        if not text.strip():
            return []

        # 如果整块就够小，直接返回
        if _estimate_tokens(text) <= self.chunk_size:
            return [text]

        if not separators:
            # 最后兜底：按字符硬切
            approx_chars = self.chunk_size * 3
            return [text[i:i + approx_chars] for i in range(0, len(text), approx_chars)]

        separator = separators[0]
        remaining = separators[1:]

        if separator == "":
            approx_chars = self.chunk_size * 3
            parts = [text[i:i + approx_chars] for i in range(0, len(text), approx_chars)]
        else:
            parts = text.split(separator)

        result: list[str] = []
        current_parts: list[str] = []
        current_tokens = 0

        for part in parts:
            part_tokens = _estimate_tokens(part)

            if part_tokens > self.chunk_size:
                # 单个 part 都太大 → 用更细的分隔符递归拆分
                if current_parts:
                    result.append(separator.join(current_parts))
                    current_parts = []
                    current_tokens = 0
                sub = self._split_by_separator(part, remaining)
                result.extend(sub)
            elif current_tokens + part_tokens > self.chunk_size:
                # 累积够了，存一块，然后开始新的
                result.append(separator.join(current_parts))
                current_parts = [part]
                current_tokens = part_tokens
            else:
                current_parts.append(part)
                current_tokens += part_tokens

        if current_parts:
            result.append(separator.join(current_parts))
        return result

    # ---------- 添加 overlap ----------
    def _add_overlap(self, chunks: list[str]) -> list[str]:
        if len(chunks) <= 1:
            return chunks
        overlap_chars = self.chunk_overlap * 3
        result = []
        for i, chunk in enumerate(chunks):
            if i > 0 and overlap_chars > 0:
                prev_tail = chunks[i - 1][-overlap_chars:]
                chunk = prev_tail + " " + chunk
            result.append(chunk.strip())
        return result

    # ---------- 主入口 ----------
    def chunk_documents(self, docs: list[Document]) -> list[Document]:
        all_chunks: list[Document] = []
        chunk_index = 0
        for doc in docs:
            text = doc.page_content or ""
            page_number = doc.metadata.get("page")
            section = doc.metadata.get("section_title")

            raw_chunks = self._split_by_separator(text, self.SEPARATORS)
            overlapped = self._add_overlap(raw_chunks)

            for piece in overlapped:
                piece = piece.strip()
                if not piece:
                    continue
                meta = dict(doc.metadata)
                meta["chunk_index"] = chunk_index
                if page_number is not None:
                    meta["page"] = page_number
                if section:
                    meta["section_title"] = section
                meta["token_count"] = _estimate_tokens(piece)
                all_chunks.append(Document(page_content=piece, metadata=meta))
                chunk_index += 1
        return all_chunks


# ==================================
# 对外兼容接口（LangChain Document 进/出）
# ==================================
def split_documents(
    documents: list[Document],
    chunk_size: int = 500,            # 注意：这里传的是「字符数」，与原接口保持兼容
    chunk_overlap: int = 80,
) -> list[Document]:
    """
    对外统一入口。
    为了兼容原来的调用（传入的 chunk_size 是字符数），我们粗估转换：
    chunk_tokens ≈ chunk_chars / 2（中英文混合的保守估算）
    """
    if not documents:
        return []

    # 字符数 → token 数（RAG-Pro 用 tokens 控制块大小）
    token_size = max(256, chunk_size // 2)
    token_overlap = max(32, chunk_overlap // 2)

    chunker = RecursiveChunker(token_size, token_overlap)
    return chunker.chunk_documents(documents)


def split_single_text(
    text: str,
    chunk_size: int = 500,
    chunk_overlap: int = 80,
    metadata: dict | None = None,
) -> list[Document]:
    doc = Document(page_content=text, metadata=metadata or {})
    return split_documents([doc], chunk_size, chunk_overlap)
