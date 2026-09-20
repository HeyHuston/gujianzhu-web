from __future__ import annotations

import json
import math
import re
from typing import Dict, Generator, List, Optional

import requests

from config import (
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    INDEX_FILE,
    RETRIEVAL_TOP_K,
)


def _load_records() -> List[Dict]:
    if not INDEX_FILE.exists():
        return []
    try:
        return json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _deepseek_ready() -> bool:
    key = (DEEPSEEK_API_KEY or "").strip()
    return bool(key) and key.isascii() and len(key) >= 20


def _lang_label(lang: str) -> str:
    return "English" if str(lang).lower().startswith("en") else "中文"


_LANG_SWITCH_RE = re.compile(
    r"^(请|帮我)?(用|以|换成|改为)?\s*(中文|汉语|英文|英语|english|chinese)\s*(回答|回复|讲解|说明|讲|说|来)?[。！？.!?\s]*$",
    re.IGNORECASE,
)


def _prior_user_query(history: Optional[List[Dict]]) -> str:
    for item in reversed(history or []):
        if str(item.get("role") or "") != "user":
            continue
        content = str(item.get("content") or "").strip()
        if content and not _LANG_SWITCH_RE.match(content):
            return content
    return ""


def _resolve_turn(query: str, lang: str, history: Optional[List[Dict]]) -> tuple[str, str]:
    """返回检索问句。AI 导览对话固定中文，不跟页面语言、也不跟英文提问切换。"""
    del lang  # 导览回答语言不使用调用方传入的站点语言
    q = (query or "").strip()
    if _LANG_SWITCH_RE.match(q):
        prior = _prior_user_query(history)
        return (prior or q), "zh"
    return q, "zh"


def _history_for_prompt(query: str, history: Optional[List[Dict]], lang: str = "zh") -> List[Dict]:
    items = list(history or [])
    # 中文回答时丢掉纯英文的上一轮，避免模型顺着英文接着写
    if not str(lang).lower().startswith("en"):
        cleaned = []
        for item in items:
            role = str(item.get("role") or "")
            content = str(item.get("content") or "")
            if role == "assistant" and content and not re.search(r"[\u4e00-\u9fff]", content):
                continue
            if role == "user" and _LANG_SWITCH_RE.match(content.strip()):
                continue
            cleaned.append(item)
        items = cleaned
    elif _LANG_SWITCH_RE.match((query or "").strip()):
        items = [
            item
            for item in items
            if str(item.get("role") or "") == "user"
            and not _LANG_SWITCH_RE.match(str(item.get("content") or "").strip())
        ]
    messages: List[Dict] = []
    for item in items[-6:]:
        role = str(item.get("role") or "")
        content = str(item.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content[:1200]})
    return messages


def _pop_sentences(buf: str, final: bool = False, cited: bool = True) -> tuple[list[str], str]:
    """把模型增量切成完整句子，便于 SSE 一句一句下发。"""
    out: list[str] = []
    end_re = re.compile(r"[。！？!?]")
    incomplete_cite = re.compile(r"[ \t]*\[?\d*$")
    complete_cite = re.compile(r"[ \t]*\[\d+\]")
    while buf:
        match = end_re.search(buf)
        newline_at = buf.find("\n")
        line_has_cite = newline_at != -1 and re.search(r"\[\d+\]", buf[:newline_at])
        if match is None and not line_has_cite:
            break
        # 先按「一句一行且已带引用」切开
        if match is None or (line_has_cite and newline_at < match.start()):
            text = buf[:newline_at].strip()
            buf = buf[newline_at + 1 :]
            if text:
                out.append(text)
            continue
        end = match.end()
        if cited:
            tail = buf[end:]
            cite = complete_cite.match(tail)
            if cite:
                end += cite.end()
            elif not final and incomplete_cite.match(tail):
                # 句号刚到，引用编号可能还没写完
                break
        text = buf[:end].strip()
        buf = buf[end:]
        if buf.startswith("\n"):
            buf = buf[1:]
        if text:
            out.append(text)
    if final and buf.strip():
        out.append(buf.strip())
        buf = ""
    return out, buf


ALIAS_TO_BUILDING = {
    "故宫": "北京故宫",
    "紫禁城": "北京故宫",
    "太和殿": "北京故宫",
    "中和殿": "北京故宫",
    "保和殿": "北京故宫",
    "养心殿": "北京故宫",
    "四合院": "北京四合院",
    "乔家": "乔家大院",
    "乔家大院": "乔家大院",
    "拙政园": "苏州拙政园",
    "卢沟桥": "卢沟桥",
    "颐和园": "颐和园",
    "平遥": "平遥古城墙",
    "平遥古城": "平遥古城墙",
    "平遥古城墙": "平遥古城墙",
    "广济桥": "潮州广济桥",
    "宝带桥": "宝带桥",
    "泸定桥": "泸定桥",
    "龙脑桥": "泸州龙脑桥",
    "鱼沼飞梁": "鱼沼飞梁",
    "淮安府衙": "淮安府衙",
    "霍州署": "霍州署",
    "阆中古城": "阆中古城",
}

_BUILDING_STOP = {"中国", "建筑", "历史", "文化", "传统", "研究", "古城", "大院", "古桥", "北京", "苏州"}


def _detect_target_buildings(query: str, records: List[Dict]) -> set:
    targets = set()
    q = query or ""
    for alias, building in ALIAS_TO_BUILDING.items():
        if alias and alias in q:
            targets.add(building)
    buildings = {
        str((item.get("meta", {}) or {}).get("building") or "").strip()
        for item in records
        if str((item.get("meta", {}) or {}).get("building") or "").strip()
    }
    # 长名优先，避免短词误伤
    for building in sorted(buildings, key=len, reverse=True):
        if building in q:
            targets.add(building)
            continue
        short = building
        for prefix in ("北京", "苏州", "泸州", "潮州"):
            if short.startswith(prefix) and len(short) > len(prefix) + 1:
                short = short[len(prefix) :]
                break
        if short and len(short) >= 2 and short in q and short not in _BUILDING_STOP:
            targets.add(building)
    return targets


def _query_focus_terms(query: str) -> List[str]:
    q = re.sub(
        r"(请|麻烦|帮我|一下|介绍|说说|讲讲|请问|什么|怎么|如何|有哪些|是什么|的|吗|呢|啊|吧)",
        " ",
        query or "",
    )
    terms: List[str] = []
    for seg in re.findall(r"[\u4e00-\u9fff]{2,}", q):
        if seg in _BUILDING_STOP or seg in terms:
            continue
        terms.append(seg)
    for kw in ("布局", "特点", "特色", "结构", "历史", "始建", "年代", "位置", "中轴", "彩画", "保护", "园林", "桥梁", "城墙"):
        if kw in (query or "") and kw not in terms:
            terms.append(kw)
    return terms


def _tokens(text: str) -> set:
    text = (text or "").lower()
    tokens = set()
    # English / numeric tokens
    tokens.update(re.findall(r"[a-z0-9]+", text))
    # Chinese bigram tokens for better recall
    for seg in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(seg) == 1:
            tokens.add(seg)
            continue
        for i in range(len(seg) - 1):
            tokens.add(seg[i : i + 2])
    return tokens


def _expand_query(query: str) -> str:
    expanded = [query]
    for alias, building in ALIAS_TO_BUILDING.items():
        if alias in query:
            expanded.append(building)
    return " ".join(expanded)


def _contains_chinese_phrase(text: str, query: str) -> bool:
    for seg in re.findall(r"[\u4e00-\u9fff]{2,}", query):
        if seg and seg in text:
            return True
    return False


def _bm25(q_tokens: set, d_tokens: List[str], df: Dict[str, int], avgdl: float, n_docs: int) -> float:
    if not q_tokens or not d_tokens:
        return 0.0
    k1 = 1.5
    b = 0.75
    tf = {}
    for t in d_tokens:
        tf[t] = tf.get(t, 0) + 1
    dl = max(1, len(d_tokens))
    score = 0.0
    for q in q_tokens:
        if q not in tf:
            continue
        nqi = df.get(q, 0)
        idf = math.log((n_docs - nqi + 0.5) / (nqi + 0.5) + 1.0)
        f = tf[q]
        denom = f + k1 * (1 - b + b * dl / max(1.0, avgdl))
        score += idf * ((f * (k1 + 1)) / denom)
    return score


def retrieve(query: str, top_k: int = RETRIEVAL_TOP_K) -> List[Dict]:
    expanded_query = _expand_query(query)
    q_tokens = _tokens(expanded_query)
    records = _load_records()

    if not records:
        return []

    tokenized_docs = []
    df = {}
    total_len = 0
    for item in records:
        content = item.get("content", "")[:1500]
        d_tokens_list = list(_tokens(content))
        tokenized_docs.append(d_tokens_list)
        total_len += max(1, len(d_tokens_list))
        for t in set(d_tokens_list):
            df[t] = df.get(t, 0) + 1
    n_docs = len(records)
    avgdl = total_len / float(max(1, n_docs))

    target_buildings = _detect_target_buildings(query, records)
    focus_terms = _query_focus_terms(query)

    scored = []
    for idx, item in enumerate(records):
        content = item.get("content", "")
        meta = item.get("meta", {}) or {}
        building = meta.get("building", "")
        # 问到具体建筑时，先只在该建筑文献里搜，避免故宫资料淹没其它建筑
        if target_buildings and building not in target_buildings:
            continue
        d_tokens = tokenized_docs[idx]
        score = _bm25(q_tokens, d_tokens, df, avgdl, n_docs)

        source = meta.get("source", "")
        province = meta.get("province", "")
        category = meta.get("category", "")
        meta_text = " ".join([source, building, province, category])

        if _contains_chinese_phrase(content[:1200], query):
            score += 1.2
        if _contains_chinese_phrase(meta_text, query):
            score += 1.4
        if target_buildings and building in target_buildings:
            score += 3.0
        if building and building in query:
            score += 2.0
        if province and province in query:
            score += 1.0
        if category and category in query:
            score += 0.8
        for term in focus_terms:
            if term and term in (content[:800] or ""):
                score += 0.45
            if term and term in meta_text:
                score += 0.35
        head = (content or "")[:80].lstrip()
        if _looks_like_sentence_start(head):
            score += 0.4
        # 只有当前问题真在问概况/历史时，才给“基本资料”类切片加分
        if any(k in query for k in ("介绍", "概况", "历史", "始建", "什么时候", "何时")) and re.search(
            r"(始建|位于|占地|建筑面积|基本资料|世界遗产)", content[:400] or ""
        ):
            score += 1.2
        if re.search(r"(作者简介|基金项目|学人档案|生于\d{4})", content[:400] or ""):
            score -= 1.2

        scored.append((score, item))

    scored.sort(key=lambda x: x[0], reverse=True)
    # 同一篇文献只保留与问题最匹配的切片
    best_by_source: Dict[str, tuple] = {}
    for score, item in scored:
        if score <= 0:
            continue
        meta = item.get("meta", {}) or {}
        source_key = meta.get("relative_path") or meta.get("source") or ""
        if not source_key:
            continue
        content = item.get("content", "") or ""
        building = str(meta.get("building") or "")
        readable = _chunk_readability(content, building, query)
        # 问句关键词命中次数，拉开“介绍故宫”和“太和殿特点”的差异
        hit = sum(1 for term in focus_terms if term and term in content)
        quality = score + readable * 0.05 + hit * 0.8
        prev = best_by_source.get(source_key)
        if prev is None or quality > prev[0]:
            best_by_source[source_key] = (quality, score, item)

    top_items = sorted(best_by_source.values(), key=lambda x: x[0], reverse=True)[:top_k]
    top_items = [(score, item) for _, score, item in top_items]

    refs: List[Dict] = []
    for idx, (score, item) in enumerate(top_items, start=1):
        meta = item.get("meta", {})
        refs.append(
            {
                "index": idx,
                "content": item.get("content", ""),
                "score": round(score, 4),
                "source": meta.get("source", ""),
                "province": meta.get("province", ""),
                "building": meta.get("building", ""),
                "category": meta.get("category", ""),
                "relative_path": meta.get("relative_path", ""),
            }
        )
    return refs


def _normalize_chunk(text: str) -> str:
    text = (text or "").replace("\x00", "").replace("\r", "\n")
    # PDF 常在行中硬换行，先把「汉字换行汉字」拼回一句
    text = re.sub(r"(?<=[\u4e00-\u9fffA-Za-z0-9）)」』》])\n+(?=[\u4e00-\u9fffA-Za-z0-9（(「『《])", "", text)
    text = re.sub(r"[ \t\u3000]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def _polish_sentence(text: str) -> str:
    sent = re.sub(r"\s+", "", (text or "").replace("\x00", "")).strip()
    for _ in range(5):
        prev = sent
        sent = re.sub(r"^[A-Za-z][A-Za-z\s]{1,40}", "", sent)
        sent = re.sub(r"^(谈往|History|ART|OBSERVATION)+", "", sent, flags=re.IGNORECASE)
        sent = re.sub(r"^\d+(\.\d+){0,4}", "", sent)
        sent = re.sub(r"^[（(][一二三四五六七八九十\d]+[）)]", "", sent)
        sent = re.sub(r"^(概念辨析|基本资料|摘要|引言|建设沿革及空间组织|空间组织|建设沿革)[:：]?", "", sent)
        sent = re.sub(r"^源[:：].{0,40}[）)]", "", sent)
        sent = re.sub(r"^[^。]{0,30}硕士学位论文", "", sent)
        # 去掉句中/句末残留脚注数字，如「博物院2，」
        sent = re.sub(r"(?<=[\u4e00-\u9fff])\d+(?=[，。！？、]|$)", "", sent)
        if sent == prev:
            break
    return sent.strip()


def _looks_like_sentence_start(text: str) -> bool:
    text = (text or "").lstrip(" \t\n\"'“”‘’")
    if not text:
        return False
    # 切片开头常见半句续写
    if re.match(
        r"^(的|了|着|与|和|而|则|等|之|也|为|以|其|此|该|都|还|又|就|才|并|及|或|到|向|从|把|被|让|使|给|中|上|下|内|外|后|前|来|去|于|对|在于|源)",
        text,
    ):
        return False
    if re.match(r"^[，,、；;：:）\)」』》…—\-]", text):
        return False
    if re.match(r"^(图\d+|表\d+|参见|作者改绘|硕士学位论文)", text):
        return False
    return bool(re.match(r"[\u4e00-\u9fff《「『（(0-9一二三四五六七八九十]", text))


def _looks_garbled(text: str) -> bool:
    if not text:
        return True
    if re.search(r"(祖古|先南|定紫|规院|文物定|皇\s*系|建四|筑都|南作者|美丽西|被雷击提起|紫禁帝|作者改绘|硕士学位论文)", text):
        return True
    chinese = len(re.findall(r"[\u4e00-\u9fff]", text))
    commas = text.count("，") + text.count(",")
    if chinese > 65 and commas >= 5:
        return True
    if re.search(r"[\u4e00-\u9fff]\d{2,}[\u4e00-\u9fff]", text):
        return True
    # 图注/页眉残留
    if re.search(r"(手绘地图|改绘\)|西安建筑)", text):
        return True
    return False


def _is_noisy_sentence(text: str) -> bool:
    if not text:
        return True
    compact = re.sub(r"\s+", "", text)
    if _looks_garbled(compact):
        return True
    if compact.count(".") >= 4 or compact.count("…") >= 2 or compact.count("．") >= 4:
        return True
    if re.search(
        r"(基金项目|作者简介|关键词|中图分类号|文献标识码|参考文献|目录|DOI|学报|美术观察|生於|生于|考入|毕业后|学人档案)",
        text,
    ):
        return True
    if re.search(r"(图\d+|表\d+)", text) and len(re.findall(r"[\u4e00-\u9fff]", text)) < 22:
        return True
    if re.search(r"^\d+$", compact):
        return True
    chinese = len(re.findall(r"[\u4e00-\u9fff]", text))
    if chinese < 12 or chinese > 90:
        return True
    if not re.search(r"[。！？]$", compact):
        return True
    return False


def _score_sentence(text: str, building: str, query: str) -> int:
    chinese_n = len(re.findall(r"[\u4e00-\u9fff]", text))
    score = min(chinese_n, 36)
    name_bits = [building]
    if building.startswith("北京") or building.startswith("苏州") or building.startswith("泸州"):
        name_bits.append(building[2:])
    for kw in name_bits:
        if kw and len(kw) >= 2 and kw in text:
            score += 40
    for term in _query_focus_terms(query):
        if term and term in text:
            score += 30
    if re.search(r"(始建|位于|占地|建筑面积|中轴|外朝|内廷|世界遗产|明清|宫殿)", text):
        if any(k in (query or "") for k in ("介绍", "概况", "历史", "始建", "什么时候", "何时", "是什么")):
            score += 28
        else:
            score += 6
    if 18 <= chinese_n <= 48:
        score += 18
    if re.match(r"^(第.+章|壹、|一、|二、|三、|\d+\.|本文分)", text):
        score -= 25
    if re.search(r"(先生|生于|考入|毕业|台湾|上海)", text) and building and building not in text:
        score -= 80
    if _looks_garbled(text):
        score -= 120
    return score


def _chunk_readability(content: str, building: str = "", query: str = "") -> float:
    sents = _iter_complete_sentences(content)
    if not sents:
        return 0.0
    return float(max(_score_sentence(s, building, query) for s in sents))


def _iter_complete_sentences(text: str) -> List[str]:
    normalized = _normalize_chunk(text)
    if not normalized:
        return []

    # 切片若从半句开始，丢掉第一个句号前的残片
    body = normalized
    if not _looks_like_sentence_start(body):
        cut = re.search(r"[。！？]", body)
        if cut:
            body = body[cut.end() :].lstrip(" \n\"'“”")

    out: List[str] = []
    # 严格按句末标点切开，避免从句子中间起笔
    pieces = re.findall(r"[^。！？]+[。！？]", body)
    for piece in pieces:
        sent = _polish_sentence(piece)
        if not sent:
            continue
        if not _looks_like_sentence_start(sent):
            continue
        if _is_noisy_sentence(sent):
            continue
        out.append(sent)
    return out


def _pick_sentence_from_ref(ref: Dict, query: str, used: Optional[set] = None) -> str:
    content = str(ref.get("content") or "")
    building = str(ref.get("building") or "").strip() or "文献"
    source = str(ref.get("source") or "").strip()
    candidates = _iter_complete_sentences(content)
    used = used if used is not None else set()
    if candidates:
        ranked = sorted(candidates, key=lambda s: _score_sentence(s, building, query), reverse=True)
        for pick in ranked:
            key = re.sub(r"\s+", "", pick)[:28]
            if key in used:
                continue
            if _score_sentence(pick, building, query) >= 36:
                used.add(key)
                return pick
        for pick in ranked:
            key = re.sub(r"\s+", "", pick)[:28]
            if key in used:
                continue
            used.add(key)
            return pick

    title = re.sub(r"\.(pdf|docx?)$", "", source, flags=re.IGNORECASE)
    title = re.sub(r"_+", "·", title).strip() or building
    return f"《{title}》从建筑、空间或历史角度讨论了{building}的相关内容。"


def _extractive_sentences(refs: List[Dict], query: str) -> List[str]:
    """无可用大模型密钥时，按文献切片给出完整中文摘录句。"""
    if not refs:
        return ["知识库中没有检索到与该问题直接相关的文献。"]

    q = (query or "").strip()
    buildings = sorted(
        {str(ref.get("building") or "").strip() for ref in refs if str(ref.get("building") or "").strip()},
        key=len,
        reverse=True,
    )
    topic = next((b for b in buildings if b and (b in q or b[2:] in q)), buildings[0] if buildings else "该建筑")
    focus = "、".join(_query_focus_terms(q)[:4]) or topic
    sentences = [f"根据知识库检索，围绕「{focus}」整理到以下文献要点。"]
    used: set = set()
    for ref in refs:
        ref_building = str(ref.get("building") or "").strip()
        if topic and ref_building and ref_building != topic:
            continue
        pick = _pick_sentence_from_ref(ref, q, used=used)
        sentences.append(f"{pick}[{ref.get('index')}]")
    if len(sentences) == 1:
        for ref in refs:
            pick = _pick_sentence_from_ref(ref, q, used=used)
            sentences.append(f"{pick}[{ref.get('index')}]")
    return sentences


def build_prompt(
    query: str,
    refs: List[Dict],
    lang: str,
    history: Optional[List[Dict]] = None,
    raw_query: str = "",
) -> List[Dict]:
    ref_lines = []
    for item in refs:
        ref_lines.append(
            f"[{item['index']}] {item['content']}\n来源：{item['source']} | 建筑：{item['building']} | 地区：{item['province']}"
        )
    ref_text = "\n\n".join(ref_lines) if ref_lines else "无可用参考资料。"
    if not refs:
        system = (
            "你是古建智寻的中文导览员。当前没有检索到参考文献。"
            "只用中文回复这一句：知识库中没有检索到与该问题直接相关的文献。"
            "不要用常识补充，不要改用英文。"
        )
    else:
        system = (
            "你是古建智寻的中文导览员。回答必须全程使用中文，文献里的英文也要译成中文后再写。"
            "禁止输出英文句子，禁止建议改用英文，禁止说无法用中文回答。"
            "只能根据下面检索到的参考文献作答，不要编造文献里没有的具体数据，也不要脱离这些文献用常识发挥。"
            "每篇文献只写一句，这一句只能依据该篇文献；不同句子必须对应不同的原始文献。"
            "每句单独占一行，句末只标一个编号，例如：太和殿位于紫禁城中轴线之上。[1]"
            "不要在同一句堆叠多个编号，不要重复引用同一篇文献。"
            "某篇文献若与问题关系弱，就跳过它，不要因此拒绝回答。"
            "先直接回答问题，再补充文献中的具体事实，一共 4 到 8 句。"
        )
    user = f"用户问题：{query}\n\n检索到的参考文献：\n{ref_text}"
    if raw_query and raw_query.strip() and raw_query.strip() != query.strip():
        user = "请用中文根据下面检索到的文献重新回答上一个问题。\n\n" + user
    messages: List[Dict] = [{"role": "system", "content": system}]
    messages.extend(_history_for_prompt(raw_query or query, history, "zh"))
    messages.append({"role": "user", "content": user})
    return messages


def _stringify_page_context(page_context: Optional[Dict]) -> str:
    if not page_context:
        return ""
    parts = []
    field_order = [
        ("page_title", "页面标题"),
        ("building_name", "建筑名称"),
        ("section_titles", "关键小节"),
        ("highlights", "页面内容摘要"),
        ("hotspots", "页面热点要点"),
        ("path", "页面路径"),
    ]
    for key, label in field_order:
        value = str((page_context or {}).get(key, "") or "").strip()
        if value:
            parts.append(f"{label}: {value}")
    return "\n".join(parts)


def build_page_narration_prompt(query: str, refs: List[Dict], lang: str, page_context: Optional[Dict]) -> List[Dict]:
    ref_lines = []
    for item in refs:
        ref_lines.append(f"- {item['building']} | {item['source']}\n{item['content'][:240]}")
    ref_text = "\n\n".join(ref_lines) if ref_lines else "暂无可用参考资料。"
    page_text = _stringify_page_context(page_context) or "暂无可用页面信息。"
    target_lang = _lang_label(lang)
    system = (
        f"你是古建智寻平台的AI导览员。请全程使用{target_lang}输出一段适合朗读的口语化导览稿，不要改用另一种语言。"
        "请优先根据当前页面内容讲解，再结合参考文献补充必要的历史、建筑或文化信息。"
        "输出要像导览员在对游客讲解，自然、连贯、有节奏，控制在6到10句。"
        "不要使用 markdown、条目、编号或[1]这类引用标记，也不要写“好的”“下面我来”等客套开场。"
        "如果页面信息不够，就基于现有页面内容做稳妥延展，不要编造细节。"
    )
    user = (
        f"目标：{query}\n\n"
        f"当前页面信息：\n{page_text}\n\n"
        f"参考文献摘要：\n{ref_text}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _build_messages(
    query: str,
    refs: List[Dict],
    lang: str,
    mode: str = "chat",
    page_context: Optional[Dict] = None,
    history: Optional[List[Dict]] = None,
    raw_query: str = "",
) -> List[Dict]:
    if mode == "page_narration":
        return build_page_narration_prompt(query=query, refs=refs, lang=lang, page_context=page_context)
    return build_prompt(query=query, refs=refs, lang=lang, history=history, raw_query=raw_query)


def stream_answer(
    query: str,
    lang: str,
    history: Optional[List[Dict]] = None,
    mode: str = "chat",
    page_context: Optional[Dict] = None,
) -> Generator[str, None, None]:
    cited = mode != "page_narration"
    if mode == "page_narration" and page_context:
        retrieve_query = " ".join(
            part
            for part in [
                query,
                str((page_context or {}).get("building_name", "") or "").strip(),
                str((page_context or {}).get("page_title", "") or "").strip(),
            ]
            if part
        )
        answer_code = "zh"
        raw_query = query
    else:
        retrieve_query, answer_code = _resolve_turn(query, lang, history)
        answer_code = "zh"
        raw_query = query

    refs = retrieve(query=retrieve_query, top_k=RETRIEVAL_TOP_K)
    safe_refs = []
    for ref in refs:
        item = {k: v for k, v in ref.items() if k != "content"}
        item["content"] = ref.get("content", "")[:300]
        safe_refs.append(item)
    yield json.dumps({"type": "kb_sources", "data": safe_refs}, ensure_ascii=False)

    if not _deepseek_ready():
        for sentence in _extractive_sentences(refs, retrieve_query):
            yield json.dumps({"type": "sentence", "data": sentence}, ensure_ascii=False)
        yield json.dumps({"type": "done"}, ensure_ascii=False)
        return

    messages = _build_messages(
        query=retrieve_query,
        refs=refs,
        lang=answer_code,
        mode=mode,
        page_context=page_context,
        history=history,
        raw_query=raw_query,
    )
    response = requests.post(
        DEEPSEEK_BASE_URL.rstrip("/") + "/chat/completions",
        headers={"Authorization": "Bearer " + DEEPSEEK_API_KEY, "Content-Type": "application/json"},
        json={"model": DEEPSEEK_MODEL, "messages": messages, "stream": True, "temperature": 0.3},
        stream=True,
        timeout=120,
    )
    response.raise_for_status()
    pending = ""
    for raw_line in response.iter_lines(decode_unicode=True):
        line = (raw_line or "").strip()
        if not line.startswith("data: "):
            continue
        data = line[6:]
        if data == "[DONE]":
            break
        try:
            payload = json.loads(data)
            delta = payload.get("choices", [{}])[0].get("delta", {}).get("content", "")
        except Exception:
            delta = ""
        if not delta:
            continue
        pending += delta
        sentences, pending = _pop_sentences(pending, final=False, cited=cited)
        for sentence in sentences:
            yield json.dumps({"type": "sentence", "data": sentence}, ensure_ascii=False)
    sentences, pending = _pop_sentences(pending, final=True, cited=cited)
    for sentence in sentences:
        yield json.dumps({"type": "sentence", "data": sentence}, ensure_ascii=False)
    yield json.dumps({"type": "done"}, ensure_ascii=False)
