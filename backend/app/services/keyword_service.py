"""
关键词提取 + 问题生成服务

参考 RAG-Pro 的 _extract_keywords / _generate_questions 实现：
- 关键词提取：正则提取中文 2 字以上词、英文 3 字以上词，按词频排序取 top_k
- 问题生成：按句子模式（如何/怎么 → 具体做法是什么？；是/为 → 具体指什么？；其他 → 是什么意思？）
- 在分块入库时对每个 chunk 调用，结果存入 metadata
"""
from __future__ import annotations

import re
import logging
from collections import Counter

logger = logging.getLogger(__name__)


def extract_keywords(text: str, top_k: int = 5) -> list[str]:
    """
    从文本中提取关键词：
    - 中文：连续 2 字以上的中文字符
    - 英文：3 字以上的英文单词
    按词频排序，取 top_k 个
    """
    if not text or not text.strip():
        return []

    cn_words = re.findall(r"[\u4e00-\u9fa5]{2,6}", text)
    en_words = re.findall(r"[a-zA-Z]{3,}", text)

    all_words = cn_words + en_words

    stop_words = {
        "的", "了", "是", "在", "和", "与", "或", "也", "都", "就", "还", "又",
        "一个", "可以", "这个", "那个", "什么", "怎么", "为什么", "如何",
        "我们", "你们", "他们", "它们", "自己", "的话", "但是", "因为",
        "所以", "如果", "虽然", "不过", "然后", "已经", "正在", "应该",
        "可能", "或者", "以及", "对于", "关于", "通过", "进行", "比较",
    }
    filtered = [w for w in all_words if w not in stop_words]

    counter = Counter(filtered)
    keywords = [w for w, _ in counter.most_common(top_k)]

    return keywords


def generate_questions(text: str, count: int = 3) -> list[str]:
    """
    根据文本内容生成问题：
    - 找到包含疑问词的句子 → 直接提取
    - 找到"是/为"句式 → "XXX 具体指什么？"
    - 找到"如何/怎么"句式 → "XXX 的具体做法是什么？"
    - 其他 → 取首句生成 "XXX 是什么意思？"
    """
    if not text or not text.strip():
        return []

    sentences = re.split(r"[。？！\n]", text)
    sentences = [s.strip() for s in sentences if len(s.strip()) > 5]

    if not sentences:
        return []

    questions: list[str] = []

    for sent in sentences:
        if len(questions) >= count:
            break

        if re.search(r"[如何|怎么|为什么|是什么|有哪些|是不是|能不能|可以吗]", sent):
            if not sent.endswith("？") and not sent.endswith("?"):
                sent = sent + "？"
            questions.append(sent)
            continue

        m = re.search(r"(.{2,15}?)(?:是|为)(.{2,20})", sent)
        if m:
            subject = m.group(1).strip()
            questions.append(f"{subject}具体指什么？")
            continue

        if len(sent) <= 30:
            questions.append(f"{sent}是什么意思？")

    while len(questions) < count and sentences:
        for sent in sentences:
            if len(questions) >= count:
                break
            q = f"关于「{sent[:20]}…」可以详细说明吗？"
            if q not in questions:
                questions.append(q)

    return questions[:count]
