"""
文本切块器（Split / Chunk 阶段）
优先使用 LangChain 自带的 RecursiveCharacterTextSplitter，
对中文做了增强（优先按语义分隔符切块，避免句子被切断）。
"""
from __future__ import annotations
from langchain_core.documents import Document
from langchain_text_splitters import (
    RecursiveCharacterTextSplitter,
    MarkdownTextSplitter,
    Language,
)


# 中文友好的分隔符：先按段落/标题分，再按句子，再按字符
CHINESE_SEPARATORS = [
    "\n\n",           # 双换行（段落）
    "\n",             # 单换行
    "。", "！", "？",  # 中文句末标点
    ". ", "! ", "? ",  # 英文句末
    "；", "；", ":", "：",
    "，", "、",
    " ", "",          # 最后兜底：空格 / 逐字
]


def split_documents(
    documents: list[Document],
    chunk_size: int = 500,
    chunk_overlap: int = 80,
) -> list[Document]:
    """
    通用分块入口：根据文档类型选择合适的 splitter
    """
    if not documents:
        return []

    file_type = (documents[0].metadata.get("file_type") or "").lower()

    # Markdown 专用分块器（保留标题结构）
    if file_type in {"md", "markdown"}:
        splitter = MarkdownTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )
    # 通用分块器（PDF/Word/TXT/HTML 等）
    else:
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            separators=CHINESE_SEPARATORS,
            length_function=len,
            is_separator_regex=False,
        )

    chunks = splitter.split_documents(documents)

    # 给每个块打序号
    for idx, chunk in enumerate(chunks):
        chunk.metadata.setdefault("chunk_index", idx)

    return chunks


def split_single_text(
    text: str,
    chunk_size: int = 500,
    chunk_overlap: int = 80,
    metadata: dict | None = None,
) -> list[Document]:
    """便捷：对纯文本直接切块"""
    doc = Document(page_content=text, metadata=metadata or {})
    return split_documents([doc], chunk_size, chunk_overlap)
