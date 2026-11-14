# -*- coding: utf-8 -*-
"""统一文本编码：默认 GBK（cp936）

为什么默认 GBK：评测机、记事本、Excel、cmd 都是 Windows 中文环境的默认代码页，
源码和日志按 GBK 落盘，提交后不会因为 UTF-8 中文被按 GBK 解析而出现乱码或编译失败。

约定：
* 写入：先按默认编码编码；遇到 GBK 表达不了的字符（表情符号等）退回 UTF-8，返回实际编码。
* 读取：先按默认编码解码，失败再换另一种，老文件（UTF-8）仍然可读。
* 追加：沿用已有文件的编码，避免同一文件里 UTF-8 与 GBK 混排成乱码。
* 环境变量 XDOJ_ENCODING 可覆盖默认值（如 utf-8）。
"""

import os

DEFAULT = (os.environ.get("XDOJ_ENCODING") or "gbk").strip() or "gbk"


def configure(name=None):
    """切换默认编码（由 load_env() 在读到 .env 的 XDOJ_ENCODING 时调用）

    各模块都通过 charset.DEFAULT 现取现用，所以这里改全局变量即可全项目生效。
    """
    global DEFAULT
    v = (name or "").strip()
    if v:
        DEFAULT = v
    return DEFAULT


def _norm(name):
    """编码名归一：utf-8 / utf8 / cp936 / GB2312 随手写都能认"""
    return (name or "").replace("-", "").replace("_", "").strip().lower()


def is_utf8(name=None):
    return _norm(name or DEFAULT) in ("utf8", "utf8sig", "u8", "65001")


def _alt(name=None):
    return "gbk" if is_utf8(name) else "utf-8"


def encode(text, encoding=None):
    """文本 -> 字节，返回 (data, 实际使用的编码)"""
    enc = encoding or DEFAULT
    s = text if isinstance(text, str) else str(text or "")
    try:
        return s.encode(enc), enc
    except (UnicodeEncodeError, LookupError):
        fallback = _alt(enc)
        return s.encode(fallback, errors="replace"), fallback


def decode(data, encoding=None):
    """字节 -> 文本，按默认编码优先，失败自动换另一种"""
    if isinstance(data, str):
        return data
    enc = encoding or DEFAULT
    for e in (enc, _alt(enc)):
        try:
            return data.decode(e)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode(enc, errors="replace")


def detect(path, default=None):
    """判断已有文件当初用的编码

    只有「含非 ASCII 字符且能按 UTF-8 解码」才认定为 UTF-8，纯 ASCII 一律按默认编码处理，
    这样新写的文件仍然落到 GBK，不会因为旧文件是 ASCII 就被永久锁死在 UTF-8。
    """
    fallback = default or DEFAULT
    try:
        with open(path, "rb") as f:
            data = f.read(65536)
    except OSError:
        return fallback
    if not data or data.isascii():
        return fallback
    tail = data
    for _ in range(4):
        try:
            tail.decode("utf-8")
            return "utf-8"
        except UnicodeDecodeError:
            tail = tail[:-1]          # 截断可能切在多字节字符中间，逐字节回退再试
    return fallback


def read_text(path, encoding=None):
    with open(path, "rb") as f:
        return decode(f.read(), encoding)


def write_text(path, text, encoding=None):
    """整篇覆盖写，返回实际使用的编码"""
    data, used = encode(text, encoding)
    with open(path, "wb") as f:
        f.write(data)
    return used


def append_text(path, text, encoding=None):
    """追加写，沿用已有文件的编码，返回实际使用的编码"""
    used = encoding or detect(path)
    data, used = encode(text, used)
    with open(path, "ab") as f:
        f.write(data)
    return used


def subprocess_kw(encoding=None):
    """子进程文本模式参数：统一按默认编码解码，坏字节替换掉不抛异常"""
    return {"text": True, "encoding": encoding or DEFAULT, "errors": "replace"}
