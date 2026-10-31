# -*- coding: utf-8 -*-
"""题面 PDF -> 文本（优先文字层），必要时渲染成图片"""

import os
import re

import pymupdf

# 抑制 PyMuPDF 自己补出来的“虚空格”：半角字符（数字/字母）后面紧跟中文时，
# 两者之间会留下半个字宽的空档，库默认插一个空格填满，
# 于是题面里的「6是数字」被读成「6 是数字」，样例自检就会假错。
_INHIBIT = getattr(pymupdf, "TEXT_INHIBIT_SPACES", 0)


def pdf_to_text(pdf_bytes, max_pages=6):
    """返回 (text, images)；有文字层就只给 text，没有就渲染图片"""
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    parts = []
    for i, page in enumerate(doc):
        if i >= max_pages:
            break
        raw = page.get_text("text")
        if _INHIBIT:
            # 同一页按“不补空格”再取一次，两版逐行比对，只丢掉凭空补的空格
            tight = page.get_text("text", flags=_INHIBIT)
            raw = _merge_spaces(raw, tight)
        parts.append(raw)
    text = "\n".join(parts)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    if len(text) >= 60:
        doc.close()
        return text, []

    # 扫描版：渲染成图片
    imgs = []
    tmpdir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "data", "shots")
    os.makedirs(tmpdir, exist_ok=True)
    stamp = abs(hash(pdf_bytes)) % 10 ** 8
    for i, page in enumerate(doc):
        if i >= max_pages:
            break
        pix = page.get_pixmap(matrix=pymupdf.Matrix(2, 2))
        p = os.path.join(tmpdir, f"pdf_{stamp}_{i}.png")
        pix.save(p)
        imgs.append(p)
    doc.close()
    return "", imgs


def _is_cjk(ch):
    if not ch:
        return False
    o = ord(ch)
    return (0x3000 <= o <= 0x303F or 0x3400 <= o <= 0x4DBF or 0x4E00 <= o <= 0x9FFF
            or 0xF900 <= o <= 0xFAFF or 0xFF00 <= o <= 0xFFEF)


def _merge_line(a, b):
    """a：默认提取（含虚空格）；b：抑制虚空格版。只丢掉「紧贴中文的那一个空格」"""
    if a == b:
        return a
    if a.replace(" ", "") != b.replace(" ", ""):
        return a                      # 两版内容不同，保守起见不动
    out = []
    i = j = 0
    n, m = len(a), len(b)
    while i < n:
        if a[i] != " ":
            out.append(a[i])
            i += 1
            j += 1
            continue
        if j < m and b[j] == " ":
            out.append(" ")
            i += 1
            j += 1
            continue
        k = i
        while k < n and a[k] == " ":
            k += 1
        run = k - i
        prev_ch = a[i - 1] if i else ""
        next_ch = b[j] if j < m else ""
        # 只处理「单个空格 + 一侧是中文/全角」的情况；
        # 连续多个空格通常是真实分隔符或表格排版间隙，一律保留
        if run == 1 and prev_ch and next_ch and (_is_cjk(prev_ch) or _is_cjk(next_ch)):
            i = k
            continue
        out.append(" " * run)
        i = k
    return "".join(out)


def _merge_spaces(a_text, b_text):
    la, lb = a_text.split("\n"), b_text.split("\n")
    if len(la) != len(lb):
        return a_text
    return "\n".join(_merge_line(x, y) for x, y in zip(la, lb))


# 题面里样例标题的写法非常多，实测见过的：
#   输入样例 / 输出样例 / 输入样例1： / 样例输入3： / 样例1 输入 / 样例1 输出：
#   输入示例1： / 输出示例1：
# 注意空白只能用 [ \t]，不能用 \s：\s 会把换行也吃掉，
# 「输入样例\n\n1 10」就会被截成 "10"，这是之前大量误判的直接原因。
_SEC_RE = re.compile(
    r"(?:(?:输入|输出)[ \t]*(?:样例|示例)[ \t]*\d*"
    r"|(?:样例|示例)[ \t]*\d*[ \t]*(?:输入|输出)[ \t]*\d*)"
    r"[ \t]*[:：]?[ \t]*")

# 段内还有子样例标题：样例1： / 样例2 输出：
_SUB_RE = re.compile(
    r"(?:样例|示例)[ \t]*\d*[ \t]*(?:输入|输出)?[ \t]*[:：][ \t]*")

# 「输入：」「输出：」这种最简写法（有些题根本不用"样例"二字）
_INLINE_RE = re.compile(r"(输入|输出)[ \t]*[:：][ \t]*")

# 样例块后面常跟着这些说明文字，不能算进样例内容
_STOP_HEAD = re.compile(
    r"^(备注|说明|提示|注意|数据范围|评分|本题|其中|约束|限制|要求|知识点|来源|"
    r"输入说明|输出说明|输入格式|输出格式|输入描述|输出描述|样例解释|样例说明)")
_STOP_ANY = re.compile(
    r"(用例规模|规模与约定|数据范围|评测用例|样例说明|样例解释|样例\s*\d*\s*说明)")

_SAMPLE_HEAD = re.compile(r"^(?:样例|示例)[ \t]*\d*[ \t]*(输入|输出)?[ \t]*[:：]")

# 不带冒号的段标题（输入样例 / 输出样例），出现在段中间说明这一段已经结束
_BARE_HEAD = re.compile(r"^(?:输入|输出)[ \t]*(?:样例|示例)[ \t]*$")

# 有些题把序号单独放一行：「样例输出」换行换行「1：」再换行才是内容
_ORDINAL = re.compile(r"^\d{1,2}[ \t]*[:：][ \t]*$")


# 带序号的子标题：样例1 输入 / 样例2 输出： / 示例3输入
_IDX_RE = re.compile(r"(?:样例|示例)[ \t]*(\d{1,2})[ \t]*(输入|输出)[ \t]*[:：]?")

# 只写序号的块标题：样例1： / 示例 2 :
_BLK_RE = re.compile(r"(?:样例|示例)[ \t]*(\d{1,2})?[ \t]*[:：]")


def parse_samples(text):
    """从题面里抽出 (输入, 输出) 样例对，抽不干净就返回空列表"""
    if not text:
        return []
    t = text.replace("\u3000", " ").replace("\xa0", " ")
    pairs = _parse_indexed(t) or _parse_sections(t) or _parse_numbered(t) or _parse_inline(t)
    return [(a, b) for a, b in pairs if a and b][:3]


def _parse_indexed(t):
    """「样例1 输入 / 样例1 输出」按序号配对（序号是最可靠的对应关系）"""
    hits = list(_IDX_RE.finditer(t))
    if len(hits) < 2:
        return []
    ins, outs = {}, {}
    for i, m in enumerate(hits):
        idx = int(m.group(1))
        end = hits[i + 1].start() if i + 1 < len(hits) else len(t)
        body = _clean_block(t[m.end():end])
        if not body:
            continue
        (ins if m.group(2) == "输入" else outs)[idx] = body
    keys = sorted(set(ins) & set(outs))
    return [(ins[k], outs[k]) for k in keys]


def _parse_numbered(t):
    """「样例1：」下面再分「输入：/输出：」的排版（如 399 字符判断）"""
    hits = list(_BLK_RE.finditer(t))
    if not hits:
        return []
    pairs = []
    for i, m in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else len(t)
        pairs.extend(_pair_inline_block(t[m.end():end]))
    return pairs


def _pair_inline_block(block):
    marks = [(m.start(), m.end(), m.group(1)) for m in _INLINE_RE.finditer(block)]
    out = []
    for i, (s, e, kind) in enumerate(marks):
        if kind != "输入":
            continue
        nxt = marks[i + 1] if i + 1 < len(marks) else None
        if not nxt or nxt[2] != "输出":
            continue
        nxt2 = marks[i + 2] if i + 2 < len(marks) else None
        a = _clean_block(block[e:nxt[0]])
        b = _clean_block(block[nxt[1]:(nxt2[0] if nxt2 else len(block))])
        if a and b:
            out.append((a, b))
    return out


def _segments(t):
    """按「输入样例/输出样例」这类标题切段，丢掉没有内容的空标题

    返回 (kind, body, idx)：idx 是标题里的序号（输入示例1 → 1），没有序号则为 None。
    序号是判断「哪个输入配哪个输出」最可靠的依据。
    """
    marks = []
    for m in _SEC_RE.finditer(t):
        kind = "输入" if "输入" in m.group(0) else "输出"
        num = re.search(r"\d{1,2}", m.group(0))
        marks.append((m.start(), m.end(), kind, int(num.group(0)) if num else None))
    segs = []
    for i, (s, e, kind, idx) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(t)
        body = t[e:end]
        # 「输出样例」后面直接跟「样例1 输出：」时，这一层标题本身没有内容，跳过
        if body.strip():
            segs.append((kind, body, idx))
    return segs


def _parts(body):
    """一段里可能套了多个子样例，拆开；拆不开就当整体"""
    subs = _split_sub(body)
    if subs:
        return subs
    c = _clean_block(body)
    return [c] if c else []


def _parse_sections(t):
    segs = _segments(t)
    if not segs:
        return []
    # 按可靠性从高到低尝试：有序号就按序号配；没序号但输入输出数量相等，
    # 就整批对整批配；最后才退回「输入后面紧跟输出」的顺序配对。
    # 顺序配对放在最后，是因为它最容易把「输入示例3」和「输出示例1」凑成一对，
    # 造成期望值张冠李戴（正确代码被判样例不符）。
    for fn in (_pair_by_index, _pair_batched, _pair_sequential):
        pairs = fn(segs)
        if pairs and _consistent(pairs):
            return pairs
    return []


def _pair_by_index(segs):
    """按标题里的序号配对：输入示例1 ↔ 输出示例1"""
    ins, outs = {}, {}
    for kind, body, idx in segs:
        if idx is None:
            return []
        parts = _parts(body)
        if len(parts) != 1:
            return []          # 一段里套了多组，序号不再一一对应，交给其它规则
        (ins if kind == "输入" else outs)[idx] = parts[0]
    if ins and set(ins) == set(outs):
        return [(ins[k], outs[k]) for k in sorted(ins)]
    return []


def _consistent(pairs):
    """配对结果自检：同一个输入不应配上两个不同的输出"""
    seen = {}
    for a, b in pairs:
        if a in seen and seen[a] != b:
            return False
        seen[a] = b
    return True


def _pair_sequential(segs):
    """输入段后面紧跟若干个输出段（最常见的排版）"""
    pairs = []
    i = 0
    while i < len(segs):
        kind, body = segs[i][0], segs[i][1]
        if kind != "输入":
            i += 1
            continue
        ins = _parts(body)
        if not ins:
            i += 1
            continue
        outs, j = [], i + 1
        while j < len(segs) and segs[j][0] == "输出" and len(outs) < len(ins):
            outs.extend(_parts(segs[j][1]))
            j += 1
        if len(outs) >= len(ins):
            pairs.extend(zip(ins, outs[:len(ins)]))
            i = j
        else:
            i += 1
    return pairs


def _pair_batched(segs):
    """先列全部输入、再列全部输出的排版，按顺序一一配对"""
    ins, outs = [], []
    for seg in segs:
        kind, body = seg[0], seg[1]
        (ins if kind == "输入" else outs).extend(_parts(body))
    if ins and len(ins) == len(outs):
        return list(zip(ins, outs))
    return []


def _split_sub(block):
    """段内若还有「样例1：」这类子标题，按子标题切分成多组"""
    hits = list(_SUB_RE.finditer(block))
    if len(hits) < 2:
        return []
    out = []
    for i, m in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else len(block)
        c = _clean_block(block[m.end():end])
        if c:
            out.append(c)
    return out


def _parse_inline(t):
    """「输入：/输出：」型（题面里不出现"样例"两字）"""
    marks = [(m.start(), m.end(), m.group(1)) for m in _INLINE_RE.finditer(t)]
    pairs = []
    for i, (s, e, kind) in enumerate(marks):
        if kind != "输入":
            continue
        nxt = marks[i + 1] if i + 1 < len(marks) else None
        if not nxt or nxt[2] != "输出":
            continue
        nxt2 = marks[i + 2] if i + 2 < len(marks) else None
        pairs.append((t[e:nxt[0]], t[nxt[1]:(nxt2[0] if nxt2 else len(t))]))
    return _finish(pairs)


def _finish(raw_pairs):
    out = []
    for a, b in raw_pairs:
        ca, cb = _clean_block(a or ""), _clean_block(b or "")
        if ca and cb:
            out.append((ca, cb))
    return out


def _clean_block(s):
    """切出一段样例内容：跳过开头空行取第一个非空段落，遇到说明文字就停"""
    if not s:
        return ""
    # 「输入样例」和内容之间经常空一行，所以要跳过空行取第一段非空内容；
    # 样例之后往往再空一行接「用例规模与约定」之类的说明，这部分不能算进样例。
    blocks = [b for b in re.split(r"\n[ \t]*\n", s.strip()) if b.strip()]
    if not blocks:
        return ""
    lines = []
    for ln in blocks[0].splitlines():
        ln = ln.strip()
        if not ln:
            continue
        if _ORDINAL.match(ln):
            continue          # 单独一行的序号（1：）不是样例内容
        if (_STOP_HEAD.match(ln) or _STOP_ANY.search(ln)
                or _SAMPLE_HEAD.match(ln) or _BARE_HEAD.match(ln)):
            break
        lines.append(ln)
        if len(lines) >= 12:
            break
    return "\n".join(lines).strip()


if __name__ == "__main__":
    import sys
    with open(sys.argv[1], "rb") as f:
        txt, imgs = pdf_to_text(f.read())
    print(txt[:1500])
    print("--- samples ---")
    for a, b in parse_samples(txt):
        print("IN :", repr(a))
        print("OUT:", repr(b))
