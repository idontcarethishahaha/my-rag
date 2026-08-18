"""
文本切块器 —— 参考 RAG-Pro，支持多种分块策略：
  - recursive（默认）：按自然分隔符递归拆分，带 overlap
  - intelligent：按文档标题/章节边界智能拆分，小段落自动合并
  - table：表格数据优化，每一行都带表头上下文
  - parent_child：真正的两层父子分块（大父块→小嵌块，检索命中子块后取完整父块作上下文）

核心思路（与 RAG-Pro 一致）：
  1. 优先保证语义完整，不是硬切字符数
  2. 基于 token 估算（中文 ~1.5 字/token，英文 ~4 字/token）
  3. 相邻块之间加上下文重叠（overlap），避免关键信息在切分处丢失
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from typing import Literal

from langchain_core.documents import Document


# ==================================
# 可用的分块方法
# ==================================
ChunkMethod = Literal["recursive", "intelligent", "table", "parent_child"]


# ==================================
# 数据结构（与 RAG-Pro TextChunk 保持一致语义）
# ==================================
@dataclass
class TextChunk:
    text: str
    chunk_index: int = 0
    page_number: int | None = None
    section_title: str | None = None
    token_count: int = 0
    parent_chunk_index: int | None = None      # parent_child 模式下：子块引用的父块 index
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
# 1. 递归分块器（默认核心）
# ==================================
class RecursiveChunker:
    """按自然分隔符递归拆分，带 overlap。"""

    SEPARATORS = ["\n\n", "\n", ". ", "！", "？", "。", "；", "，", "、", " ", ""]

    def __init__(self, chunk_size_tokens: int = 512, chunk_overlap_tokens: int = 64):
        self.chunk_size = chunk_size_tokens
        self.chunk_overlap = chunk_overlap_tokens

    def _split_by_separator(self, text: str, separators: list[str]) -> list[str]:
        if not text.strip():
            return []
        if _estimate_tokens(text) <= self.chunk_size:
            return [text]
        if not separators:
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
                if current_parts:
                    result.append(separator.join(current_parts))
                    current_parts = []
                    current_tokens = 0
                result.extend(self._split_by_separator(part, remaining))
            elif current_tokens + part_tokens > self.chunk_size:
                result.append(separator.join(current_parts))
                current_parts = [part]
                current_tokens = part_tokens
            else:
                current_parts.append(part)
                current_tokens += part_tokens
        if current_parts:
            result.append(separator.join(current_parts))
        return result

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

    def _docs_to_pages(self, docs: list[Document]) -> list[dict]:
        pages: list[dict] = []
        for doc in docs:
            pages.append({
                "text": doc.page_content or "",
                "page_number": doc.metadata.get("page"),
                "section_title": doc.metadata.get("section_title"),
                "metadata": dict(doc.metadata),
            })
        return pages

    def chunk_pages(self, pages: list[dict]) -> list[TextChunk]:
        all_chunks: list[TextChunk] = []
        chunk_index = 0
        for page in pages:
            raw_chunks = self._split_by_separator(page["text"], self.SEPARATORS)
            overlapped = self._add_overlap(raw_chunks)
            for piece in overlapped:
                piece = piece.strip()
                if not piece:
                    continue
                all_chunks.append(TextChunk(
                    text=piece,
                    chunk_index=chunk_index,
                    page_number=page.get("page_number"),
                    section_title=page.get("section_title"),
                    token_count=_estimate_tokens(piece),
                    metadata=dict(page.get("metadata") or {}),
                ))
                chunk_index += 1
        return all_chunks

    def chunk_documents(self, docs: list[Document]) -> list[Document]:
        pages = self._docs_to_pages(docs)
        chunks = self.chunk_pages(pages)
        return _text_chunks_to_langchain(chunks)


# ==================================
# 2. IntelligentChunker —— 按章节/标题智能拆分
# ==================================
class IntelligentChunker:
    """
    基于文档结构（标题、段落）智能分块：
      1. 检测标题行（#、章节、序号标题）
      2. 小段落自动合并，保证最小块大小
      3. 超过块大小的章节再退回递归分块
    """

    def __init__(
        self,
        chunk_size_tokens: int = 512,
        chunk_overlap_tokens: int = 64,
        min_chunk_tokens: int = 50,
    ):
        self.chunk_size = chunk_size_tokens
        self.chunk_overlap = chunk_overlap_tokens
        self.min_chunk_size = min_chunk_tokens
        self.recursive_chunker = RecursiveChunker(chunk_size_tokens, chunk_overlap_tokens)

    def chunk_pages(self, pages: list[dict]) -> list[TextChunk]:
        all_chunks: list[TextChunk] = []
        chunk_index = 0
        for page in pages:
            sections = self._detect_sections(page["text"])
            merged = self._merge_small_sections(sections)
            for sec in merged:
                if _estimate_tokens(sec["text"]) > self.chunk_size:
                    temp_page = {
                        "text": sec["text"],
                        "page_number": page.get("page_number"),
                        "section_title": sec.get("title") or page.get("section_title"),
                        "metadata": dict(page.get("metadata") or {}),
                    }
                    sub_chunks = self.recursive_chunker.chunk_pages([temp_page])
                    for sc in sub_chunks:
                        sc.chunk_index = chunk_index
                        all_chunks.append(sc)
                        chunk_index += 1
                else:
                    all_chunks.append(TextChunk(
                        text=sec["text"].strip(),
                        chunk_index=chunk_index,
                        page_number=page.get("page_number"),
                        section_title=sec.get("title") or page.get("section_title"),
                        token_count=_estimate_tokens(sec["text"]),
                        metadata=dict(page.get("metadata") or {}),
                    ))
                    chunk_index += 1
        return all_chunks

    def chunk_documents(self, docs: list[Document]) -> list[Document]:
        pages = [{
            "text": d.page_content or "",
            "page_number": d.metadata.get("page"),
            "section_title": d.metadata.get("section_title"),
            "metadata": dict(d.metadata),
        } for d in docs]
        chunks = self.chunk_pages(pages)
        return _text_chunks_to_langchain(chunks)

    # ---- 内部工具 ----
    HEADING_PATTERN = re.compile(
        r'^(#{1,6}\s+.+|第[一二三四五六七八九十\d]+[章节部分].+|[一二三四五六七八九十\d]+[、\.]\s*.+)$'
    )

    def _detect_sections(self, text: str) -> list[dict]:
        sections = []
        lines = text.split('\n')
        current = {"title": None, "text": ""}
        for line in lines:
            stripped = line.strip()
            if self.HEADING_PATTERN.match(stripped):
                if current["text"].strip():
                    sections.append(current)
                current = {"title": stripped, "text": line + "\n"}
            else:
                current["text"] += line + "\n"
        if current["text"].strip():
            sections.append(current)
        return sections if sections else [{"title": None, "text": text}]

    def _merge_small_sections(self, sections: list[dict]) -> list[dict]:
        if not sections:
            return sections
        merged: list[dict] = []
        i = 0
        while i < len(sections):
            cur = sections[i].copy()
            cur_tokens = _estimate_tokens(cur["text"])
            while cur_tokens < self.min_chunk_size and i + 1 < len(sections):
                i += 1
                nxt = sections[i]
                cur["text"] += "\n\n" + nxt["text"]
                cur_tokens = _estimate_tokens(cur["text"])
                if not cur.get("title") and nxt.get("title"):
                    cur["title"] = nxt["title"]
            merged.append(cur)
            i += 1
        return merged


# ==================================
# 3. TableChunker —— 表格分块（每行带表头）
# ==================================
class TableChunker:
    """
    针对表格数据的分块优化：
      - CSV 数据（metadata 含 headers）：每一行前缀带完整表头列名，让检索时搜到行就知道列含义
      - Markdown 表格：整体保留，一张表一个 chunk
      - 非表格内容：退回 RecursiveChunker
    """

    def __init__(self, chunk_size_tokens: int = 512):
        self.chunk_size = chunk_size_tokens
        self.recursive_chunker = RecursiveChunker(chunk_size_tokens, 0)

    def chunk_pages(self, pages: list[dict]) -> list[TextChunk]:
        all_chunks: list[TextChunk] = []
        chunk_index = 0
        for page in pages:
            meta = page.get("metadata") or {}
            headers = meta.get("headers")
            text = page["text"] or ""
            if headers:
                lines = text.strip().split('\n')
                for line in lines:
                    if not line.strip():
                        continue
                    row_ctx = f"表格数据（列：{', '.join(map(str, headers))}）\n{line}"
                    all_chunks.append(TextChunk(
                        text=row_ctx,
                        chunk_index=chunk_index,
                        page_number=page.get("page_number"),
                        section_title=f"数据行 {chunk_index + 1}",
                        token_count=_estimate_tokens(row_ctx),
                        metadata={**meta, "type": "table_row"},
                    ))
                    chunk_index += 1
                continue

            md_tables = self._extract_md_tables(text)
            if md_tables:
                for idx, tbl in enumerate(md_tables):
                    all_chunks.append(TextChunk(
                        text=tbl["text"],
                        chunk_index=chunk_index,
                        page_number=page.get("page_number"),
                        section_title=f"Table {idx + 1}",
                        token_count=_estimate_tokens(tbl["text"]),
                        metadata={**meta, "type": "table"},
                    ))
                    chunk_index += 1
            else:
                sub = self.recursive_chunker.chunk_pages([page])
                for sc in sub:
                    sc.chunk_index = chunk_index
                    all_chunks.append(sc)
                    chunk_index += 1
        return all_chunks

    def chunk_documents(self, docs: list[Document]) -> list[Document]:
        pages = [{
            "text": d.page_content or "",
            "page_number": d.metadata.get("page"),
            "section_title": d.metadata.get("section_title"),
            "metadata": dict(d.metadata),
        } for d in docs]
        chunks = self.chunk_pages(pages)
        return _text_chunks_to_langchain(chunks)

    MD_TABLE_PATTERN = re.compile(
        r'(\|.+\|[\r\n]+\|[-:\s|]+\|[\r\n]+(?:\|.+\|[\r\n]+)+)'
    )

    def _extract_md_tables(self, text: str) -> list[dict]:
        tables = []
        for i, m in enumerate(self.MD_TABLE_PATTERN.finditer(text)):
            tables.append({"index": i + 1, "text": m.group(1).strip()})
        return tables


# ==================================
# 4. ParentChildChunker —— 真正的两层父子分块
# ==================================
class ParentChildChunker:
    """
    两层父子分块（与 RAG-Pro 完全一致）：
      - 父块：大块（默认 1536 tokens，overlap=0），用作最终上下文
      - 子块：小块（默认 512 tokens，overlap=64），用于 embedding 和向量检索
      - 每个子块通过 parent_chunk_index 引用到所属父块

    索引阶段：子块 + parent_content（父块的完整文本）写入向量库 metadata
    检索阶段：命中子块后，优先用 metadata.parent_content 作为 LLM 上下文
    """

    def __init__(
        self,
        child_chunk_size_tokens: int = 512,
        child_overlap_tokens: int = 64,
        parent_chunk_size_tokens: int = 1536,
    ):
        self.child_chunker = RecursiveChunker(child_chunk_size_tokens, child_overlap_tokens)
        self.parent_chunker = RecursiveChunker(parent_chunk_size_tokens, 0)

    def chunk_pages(self, pages: list[dict]) -> tuple[list[TextChunk], list[TextChunk]]:
        """
        返回 (child_chunks, parent_chunks)
        child_chunks 带 parent_chunk_index 和 parent_content，可直接入库
        """
        parent_chunks = self.parent_chunker.chunk_pages(pages)
        child_chunks: list[TextChunk] = []
        child_index = 0
        for parent in parent_chunks:
            temp_page = {
                "text": parent.text,
                "page_number": parent.page_number,
                "section_title": parent.section_title,
                "metadata": dict(parent.metadata),
            }
            children = self.child_chunker.chunk_pages([temp_page])
            for child in children:
                child.chunk_index = child_index
                child.parent_chunk_index = parent.chunk_index
                # 直接存父块内容，检索时不用再去 DB 查
                child.metadata["parent_chunk_index"] = parent.chunk_index
                child.metadata["parent_content"] = parent.text
                child_chunks.append(child)
                child_index += 1
        return child_chunks, parent_chunks

    def chunk_documents(self, docs: list[Document]) -> list[Document]:
        """
        对外兼容接口：只返回子块（子块 metadata 已带 parent_content）。
        如需同时拿父块，请调用 chunk_pages()。
        """
        pages = [{
            "text": d.page_content or "",
            "page_number": d.metadata.get("page"),
            "section_title": d.metadata.get("section_title"),
            "metadata": dict(d.metadata),
        } for d in docs]
        child_chunks, _ = self.chunk_pages(pages)
        return _text_chunks_to_langchain(child_chunks)


# ==================================
# 工具：TextChunk → LangChain Document
# ==================================
def _text_chunks_to_langchain(chunks: list[TextChunk]) -> list[Document]:
    out: list[Document] = []
    for c in chunks:
        meta = dict(c.metadata or {})
        meta["chunk_index"] = c.chunk_index
        if c.page_number is not None:
            meta["page"] = c.page_number
        if c.section_title:
            meta["section_title"] = c.section_title
        meta["token_count"] = c.token_count
        if c.parent_chunk_index is not None:
            meta["parent_chunk_index"] = c.parent_chunk_index
        out.append(Document(page_content=c.text, metadata=meta))
    return out


# ==================================
# 工厂：按方法获取分块器
# ==================================
def get_chunker(
    method: ChunkMethod = "recursive",
    chunk_size: int = 512,          # tokens
    chunk_overlap: int = 64,        # tokens
    min_chunk_size: int = 50,       # tokens，仅 intelligent 使用
):
    if method == "recursive":
        return RecursiveChunker(chunk_size, chunk_overlap)
    if method == "intelligent":
        return IntelligentChunker(chunk_size, chunk_overlap, min_chunk_size)
    if method == "table":
        return TableChunker(chunk_size)
    if method == "parent_child":
        return ParentChildChunker(chunk_size, chunk_overlap)
    raise ValueError(f"未知的分块方法: {method}，支持: recursive / intelligent / table / parent_child")


# ==================================
# 对外兼容接口（LangChain Document 进/出）
# ==================================
def split_documents(
    documents: list[Document],
    chunk_size: int = 500,            # 传进来是字符数（与老接口兼容）
    chunk_overlap: int = 80,
    method: ChunkMethod = "recursive",
) -> list[Document]:
    """
    对外统一入口。
    默认方法=recursive；可通过 method 参数切换分块策略。
    """
    if not documents:
        return []
    token_size = max(256, chunk_size // 2)
    token_overlap = max(32, chunk_overlap // 2)
    chunker = get_chunker(method, token_size, token_overlap)
    return chunker.chunk_documents(documents)


def split_single_text(
    text: str,
    chunk_size: int = 500,
    chunk_overlap: int = 80,
    method: ChunkMethod = "recursive",
    metadata: dict | None = None,
) -> list[Document]:
    doc = Document(page_content=text, metadata=metadata or {})
    return split_documents([doc], chunk_size, chunk_overlap, method=method)
