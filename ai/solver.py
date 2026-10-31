# -*- coding: utf-8 -*-
"""OpenAI 兼容接口调用：题面（文本或图片）-> C++ 源码"""

import base64
import json
import os
import re
import time

import requests


def load_env(path=None):
    if path is None:
        try:
            import paths
            path = paths.ensure_env()
        except Exception:
            here = os.path.dirname(os.path.abspath(__file__))
            path = os.path.join(here, "..", ".env")
    cfg = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    for k in list(cfg) + ["AI_API_KEY", "AI_BASE_URL", "AI_MODEL",
                          "AI_MAX_TOKENS", "AI_FALLBACK_MODEL", "AI_VISION_MODEL",
                          "XDOJ_USER", "XDOJ_PASS"]:
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    return cfg


def save_env(cfg):
    """把配置写回 .env（保留其它键）"""
    try:
        import paths
        path = paths.ensure_env()
    except Exception:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
    order = ["XDOJ_USER", "XDOJ_PASS", "AI_API_KEY", "AI_BASE_URL", "AI_MODEL",
             "AI_MAX_TOKENS", "AI_FALLBACK_MODEL", "AI_VISION_MODEL"]
    keys = [k for k in order if k in cfg] + [k for k in cfg if k not in order]
    lines = ["# XDOJ 自动刷题配置（本机保存，勿外传）"]
    for k in keys:
        v = cfg.get(k)
        if v is None:
            continue
        lines.append(f"{k}={v}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


SYSTEM_PROMPT = """你是竞赛编程解题器。根据题目描述写出能通过全部测试点的 C++17 程序。

硬性要求：
1. 输出一个完整可编译的程序，必须包含需要的头文件（stdio.h / string.h / math.h 等）。
2. 只用标准输入读、标准输出写：scanf/printf 或 cin/cout。严禁输出任何提示语、调试信息、说明文字。
3. main 返回 int，结尾 return 0;
4. 必须能被 g++ -std=c++17 -Wall 编译通过：不要用变长数组，不要依赖 C 的隐式 void* 转换。
5. 输出格式一律以题目的文字描述（输入/输出说明）为准；题目样例只用来核对数值，
   样例里的空格数量、空行、缩进可能不规范，绝对不要照抄。题目说「数据之间用一个空格分隔」
   就输出一个空格，说「每行一个」就每行一个。
6. 边界情况要考虑：0、负数、空输入、多组数据（题目若说明多组才用循环）。
7. 代码要紧凑：**不要写任何注释**，不要空行堆砌，不要打印调试信息。注释和冗余代码只会
   让输出变长，一旦超出长度上限就会写不完整。
8. 只输出一个 ```cpp 代码块，代码块外不要写任何解释。

读入的健壮性（必须遵守）：
- 绝不允许出现可能死循环的读入。用 while(scanf(...)==1)、while(cin>>x) 或 while((c=cin.get())!=EOF) 这类形式。
- 若题目规定“读到某个字符结束”，必须同时处理输入自然结束的情况：读到 EOF 也要退出循环并输出已统计结果。
- 题面里的全角符号（如 ！）是排版造成的，实际数据里按对应的半角符号（!）理解，但判断结束符时两种都应当能正确结束。
"""


def _b64_image(path):
    with open(path, "rb") as f:
        data = base64.b64encode(f.read()).decode()
    ext = os.path.splitext(path)[1].lower().lstrip(".") or "png"
    if ext == "jpg":
        ext = "jpeg"
    return f"data:image/{ext};base64,{data}"


def extract_code(text):
    m = re.search(r"```(?:cpp|cc|c\+\+|c)?\s*\n(.*?)```", text, re.S)
    if m:
        return m.group(1).strip() + "\n"
    lines = text.strip().splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith("#include") or ln.startswith("int main"):
            return "\n".join(lines[i:]).strip() + "\n"
    return text.strip() + "\n"


class Truncated(RuntimeError):
    pass


class AuthError(RuntimeError):
    """Key 无效 / 额度用尽 —— 这类错误重试和换模型都没用，必须停下来告诉用户"""


def _cap_from_error(text):
    m = re.search(r"\[1,\s*(\d+)\]", text or "")
    return int(m.group(1)) if m else None


HARD_CAP = 8192          # 输出被截断时，max_tokens 最多提升到这个值


def solve(statement="", images=None, extra="", cfg=None, model=None,
          max_tokens=None, retries=3, timeout=240, temperature=0.0):
    cfg = cfg or load_env()
    key = cfg.get("AI_API_KEY")
    if not key:
        raise AuthError("缺少 AI_API_KEY，请先在设置里填写")

    base = cfg.get("AI_BASE_URL", "https://open.bigmodel.cn/api/paas/v4").rstrip("/")
    images = images or []
    model = model or (cfg.get("AI_VISION_MODEL") if images else cfg.get("AI_MODEL"))
    model = model or "glm-4.5-flash"
    max_tokens = int(max_tokens or cfg.get("AI_MAX_TOKENS") or 4096)
    url = base + "/chat/completions"

    if images:
        content = [{"type": "text", "text": "请解题并给出完整 C++ 源代码。" + extra}]
        for img in images:
            content.append({"type": "image_url", "image_url": {"url": _b64_image(img)}})
    else:
        content = (statement or "").strip() + "\n\n" + extra + "\n请据此写出 C++ 代码。"

    payload = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": content}],
        "temperature": float(temperature),
        "max_tokens": max_tokens,
    }
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}

    last = None
    bumped = False
    for i in range(retries):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=timeout)
            if r.status_code in (401, 403):
                # 重试和换模型都救不了，直接把错误抛出去让上层中止整批
                raise AuthError("HTTP %d 身份验证失败：Key 无效、已过期或额度用尽"
                                % r.status_code)
            if r.status_code == 400:
                cap = _cap_from_error(r.text)
                if cap and cap < payload["max_tokens"]:
                    payload["max_tokens"] = cap
                    last = f"平台把 max_tokens 限制为 {cap}"
                    print(f"    max_tokens 下调为 {cap}")
                    continue
                last = "HTTP 400: " + r.text[:250]
            elif r.status_code != 200:
                last = f"HTTP {r.status_code}: {r.text[:250]}"
            else:
                ch = r.json()["choices"][0]
                if ch.get("finish_reason") == "length":
                    if not bumped and payload["max_tokens"] < HARD_CAP:
                        # 先试着把上限翻倍重发，不要立刻换模型（换过去往往上限更低）
                        bumped = True
                        payload["max_tokens"] = min(payload["max_tokens"] * 2, HARD_CAP)
                        print(f"    输出被截断，max_tokens 提升为 {payload['max_tokens']}")
                        continue
                    raise Truncated(f"{model} 输出被截断（max_tokens={payload['max_tokens']}）")
                return extract_code(ch["message"]["content"])
        except (AuthError, Truncated):
            raise
        except Exception as e:
            last = repr(e)
        time.sleep(1.5 * (i + 1))
    raise RuntimeError("调用模型失败: " + str(last))


def solve_with_fallback(statement="", images=None, extra="", cfg=None, temperature=0.0):
    cfg = cfg or load_env()
    if images:
        chain = [cfg.get("AI_VISION_MODEL") or "glm-4v-flash", "glm-4v-flash"]
    else:
        chain = [cfg.get("AI_MODEL") or "glm-4.5-flash",
                 cfg.get("AI_FALLBACK_MODEL") or "glm-4-flash-250414"]
    caps = {"glm-4v-flash": 1024, "glm-4v": 2048}
    last = None
    tried = []
    for m in chain:
        if m in tried:
            continue
        tried.append(m)
        try:
            return solve(statement, images, extra, cfg, model=m,
                         max_tokens=caps.get(m, int(cfg.get("AI_MAX_TOKENS") or 4096)),
                         temperature=temperature)
        except AuthError:
            raise                      # Key 的问题换模型也没用，直接上抛
        except Exception as e:
            last = e
            print(f"    模型 {m} 失败：{e}")
    raise RuntimeError("所有模型都失败：" + str(last))


if __name__ == "__main__":
    print(json.dumps(load_env(), ensure_ascii=False)[:160])
