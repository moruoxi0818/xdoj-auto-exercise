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


SYSTEM_PROMPT = """你是竞赛编程解题器。根据题目给出能通过全部测试点的 **C 语言**程序。

硬性要求：
1. 只用 C 语言写（C11），只输出一个完整可编译的程序，包含需要的头文件
   （stdio.h / math.h / string.h / stdlib.h 等）。
2. 代码中不得出现任何注释：// 和 /* */ 都不要写。
3. 只用标准输入读、标准输出写：scanf/printf。严禁输出任何提示语、调试信息、说明文字。
4. main 返回 int，结尾 return 0;
5. 必须能被 gcc -std=c11 -Wall 编译通过；同时也要能被 g++ 编译通过（评测机编译器不确定）：
   malloc/calloc/realloc 的返回值必须显式强制转换（char *p = (char *)malloc(n);），
   不使用可变长数组，不使用 GCC 扩展，不用 C++ 关键字当标识符。
6. 输出格式一律以题目的文字描述（输入/输出说明）为准；题目样例只用来核对数值，
   样例里的空格数量、空行、缩进可能不规范，绝对不要照抄。题目说「数据之间用一个空格分隔」
   就输出一个空格，说「每行一个」就每行一个。小数位数按题目说的来（如保留两位用 %.2f）。
7. 边界情况要考虑：0、负数、空输入、多组数据（题目若说明多组才用循环）。
8. 代码要紧凑，不要空行堆砌，不要打印调试信息。
9. 只输出一个 ```c 代码块，代码块外不要写任何解释。

题面公式的还原（重要）：
- 题面会以截图形式给出，公式的排版以**图片**为准：分式的分数线、上下标、根号、
  绝对值竖线在文字稿里都会丢失或走形，读图才能拿到准确表达式。
- 文字稿只用来核对样例、数据范围和输出格式。文字稿里出现的孤立数字或字母行
  （如「+ 2x +」下面一行「1」再一行「x」）多半是被打散的分式，真实含义是 + 1/x。
- 写完代码前，把题面公式里的每一项（每个加数、每个乘数、每个分母、根号、绝对值）
  与代码中的表达式逐项对一遍，尤其是负分支/else 分支里的项最容易漏。
- 分段函数：每个分支都要有对应代码路径，不要把两个分支合并化简。

读入的健壮性（必须遵守）：
- 绝不允许出现可能死循环的读入。用 while(scanf(...)==1) 或 while((c=getchar())!=EOF) 这类形式。
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


def fix_broken_strings(code):
    """修复字符串字面量里的真实换行

    GLM 系列经常把 printf 里的 `\\n` 直接写成一个断行：
        printf("%d %d
        ", a, b);
    这在 C 里是非法的（missing terminating " character），一定要在编译前修掉。
    C 本身也不允许字符串跨行（只能用反斜杠续行），所以字符串内的换行一律还原成转义。
    """
    out = []
    in_str = in_chr = False
    i, n = 0, len(code)
    while i < n:
        c = code[i]
        if c == "\\" and (in_str or in_chr) and i + 1 < n:
            out.append(c)
            out.append(code[i + 1])
            i += 2
            continue
        if not in_str and not in_chr:
            if c == '"':
                in_str = True
            elif c == "'":
                in_chr = True
            out.append(c)
            i += 1
            continue
        if c == "\n":
            out.append("\\n")
            i += 1
            continue
        if c == "\r":
            i += 1
            continue
        if in_str and c == '"':
            in_str = False
        elif in_chr and c == "'":
            in_chr = False
        out.append(c)
        i += 1
    return "".join(out)


def extract_code(text):
    m = re.search(r"```(?:cpp|cc|c\+\+|c)?[ \t]*\r?\n(.*?)```", text, re.S | re.I)
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
    model = model or ("glm-4.5v" if images else "glm-4.5-flash")
    max_tokens = int(max_tokens or cfg.get("AI_MAX_TOKENS") or 4096)
    url = base + "/chat/completions"

    ASK = "根据题目要求只给出源代码，要求代码中没有注释，使用C语言。"

    if images:
        head = ASK + extra
        if (statement or "").strip():
            # 有图也把文字带上：图看公式结构，文字看样例，两边互相校对
            head += ("\n\n【题面文字（PDF 抽取，公式可能失真，一律以图片为准）】\n"
                     + statement.strip())
        content = [{"type": "text", "text": head}]
        for img in images:
            content.append({"type": "image_url", "image_url": {"url": _b64_image(img)}})
    else:
        content = ASK + "\n\n" + (statement or "").strip() + "\n\n" + extra


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
                return fix_broken_strings(extract_code(ch["message"]["content"]))
        except (AuthError, Truncated):
            raise
        except Exception as e:
            last = repr(e)
        time.sleep(1.5 * (i + 1))
    raise RuntimeError("调用模型失败: " + str(last))


def solve_with_fallback(statement="", images=None, extra="", cfg=None, temperature=0.0):
    """视觉模型主出码，失败退到文本模型

    题面里真正会出错的是公式排版（分式、上下标），只有看图才准，所以有图时优先走
    视觉模型；视觉链路断了（无权限、截断、超时）再退回纯文字，至少不至于整题失败。
    """
    cfg = cfg or load_env()
    images = images or []
    text_model = cfg.get("AI_MODEL") or "glm-4.5-flash"
    fallback_model = cfg.get("AI_FALLBACK_MODEL") or "glm-4-flash"
    vision_model = cfg.get("AI_VISION_MODEL") or "glm-4.5v"

    # 计划表：(模型名, 要发的图)；图只发给视觉模型，文本模型拿到的是去掉图之后的调用
    plan = []
    if images:
        plan.append((vision_model, images))
    plan.append((text_model, []))
    if not images:
        plan.append((fallback_model, []))

    # 老视觉模型输出上限很小，按平台实际情况压一下；新模型一律用配置里的上限
    caps = {"glm-4v-flash": 2048, "glm-4v": 2048}
    default_cap = int(cfg.get("AI_MAX_TOKENS") or 4096)
    last = None
    tried = set()
    for m, imgs in plan:
        if m in tried:
            continue
        tried.add(m)
        try:
            return solve(statement, imgs, extra, cfg, model=m,
                         max_tokens=caps.get(m, default_cap),
                         temperature=temperature)
        except AuthError:
            raise                      # Key 的问题换模型也没用，直接上抛
        except Exception as e:
            last = e
            print(f"    模型 {m} 失败：{e}")
    raise RuntimeError("所有模型都失败：" + str(last))



if __name__ == "__main__":
    print(json.dumps(load_env(), ensure_ascii=False)[:160])
