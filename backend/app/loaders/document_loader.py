"""
文档加载器（Load 阶段）
支持: PDF / Word(docx) / Excel(xlsx) / Markdown(md) / TXT / HTML

⚠️ 设计原则：优先使用轻量依赖，避免 unstructured 在 Windows 上的安装/运行问题。
"""
from __future__ import annotations

import os
from typing import Optional

from langchain_core.documents import Document


SUPPORTED_EXTS = {
    ".pdf", ".docx", ".doc",
    ".xlsx", ".xls",
    ".md", ".markdown",
    ".txt", ".log", ".csv",
    ".html", ".htm",
}


def get_ext(filename: str) -> str:
    return os.path.splitext(filename)[1].lower()


def load_document(file_path: str, file_name: Optional[str] = None) -> list[Document]:
    """
    统一入口：根据文件扩展名选择 Loader，返回 Document 列表。
    """
    ext = get_ext(file_name or file_path)

    # PDF → PyPDFLoader（依赖 pypdf，稳定）
    if ext == ".pdf":
        return _load_pdf(file_path, file_name)

    # Word → Docx2txtLoader（依赖 python-docx，稳定）
    if ext in {".docx", ".doc"}:
        return _load_word(file_path, file_name)

    # Excel → 用 openpyxl 直接读，避免 unstructured
    if ext in {".xlsx", ".xls"}:
        return _load_excel(file_path, file_name)

    # Markdown → 直接按纯文本读（MD 就是文本）
    if ext in {".md", ".markdown"}:
        return _load_text(file_path, file_name, file_type="md")

    # 纯文本
    if ext in {".txt", ".log", ".csv"}:
        return _load_text(file_path, file_name)

    # HTML → BeautifulSoup 解析
    if ext in {".html", ".htm"}:
        return _load_html(file_path, file_name)

    raise ValueError(f"暂不支持的文件类型: {ext}")


# ==================== 各格式加载实现 ====================

def _load_pdf(file_path: str, name: str) -> list[Document]:
    from langchain_community.document_loaders import PyPDFLoader
    loader = PyPDFLoader(file_path)
    docs = loader.load()
    for d in docs:
        d.metadata.setdefault("source", name)
        d.metadata.setdefault("file_type", "pdf")
    return docs


def _load_word(file_path: str, name: str) -> list[Document]:
    from langchain_community.document_loaders import Docx2txtLoader
    loader = Docx2txtLoader(file_path)
    docs = loader.load()
    for d in docs:
        d.metadata.setdefault("source", name)
        d.metadata.setdefault("file_type", "docx")
    return docs


def _load_excel(file_path: str, name: str) -> list[Document]:
    """用 openpyxl 直接读每个 sheet 的内容，拼成纯文本 Document"""
    from openpyxl import load_workbook
    wb = load_workbook(file_path, read_only=True, data_only=True)
    parts: list[str] = []
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        rows: list[str] = []
        for row in ws.iter_rows(values_only=True):
            # 跳过全空行
            cells = [str(c) if c is not None else "" for c in row]
            if any(cells):
                rows.append(" | ".join(cells))
        if rows:
            parts.append(f"=== Sheet: {sheet_name} ===\n" + "\n".join(rows))
    wb.close()
    content = "\n\n".join(parts)
    return [Document(page_content=content, metadata={"source": name, "file_type": "xlsx"})]


def _load_text(file_path: str, name: str, file_type: str = "txt") -> list[Document]:
    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()
    return [Document(page_content=content, metadata={"source": name, "file_type": file_type})]


def _load_html(file_path: str, name: str) -> list[Document]:
    """用 BeautifulSoup 提取纯文本，降级为纯文本读"""
    try:
        from bs4 import BeautifulSoup
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            soup = BeautifulSoup(f, "lxml")
        # 去掉 script/style 标签
        for tag in soup(["script", "style"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        return [Document(page_content=text, metadata={"source": name, "file_type": "html"})]
    except Exception:
        return _load_text(file_path, name, file_type="html")
