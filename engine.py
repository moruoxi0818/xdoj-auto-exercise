# -*- coding: utf-8 -*-
"""刷题引擎：登录 -> 选题 -> 题面 PDF 转文本 -> 出码 -> 编译 -> 样例自检 -> 提交 -> 轮询判定

命令行 auto.py 与网页服务 server.py 共用这一份逻辑。
"""

import glob
import json
import os
import re
import shutil
import subprocess
import threading
import time

from ai.solver import AuthError, load_env, solve_with_fallback
from oj.client import VERDICT_OK, OJ, OJError
from oj.pdftext import parse_samples, pdf_to_text

_NO_WIN = 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW


def _find_gxx():
    """按 PATH -> 环境变量 -> 常见安装目录 -> 浅层扫描 的顺序找 g++"""
    p = shutil.which("g++") or shutil.which("g++.exe")
    if p:
        return p
    for env in ("CXX", "GXX"):
        v = os.environ.get(env)
        if v and os.path.isfile(v):
            return v
    cands = [
        r"C:\mingw64\bin\g++.exe",
        r"C:\mingw32\bin\g++.exe",
        r"C:\MinGW\bin\g++.exe",
        r"C:\msys64\mingw64\bin\g++.exe",
        r"C:\msys64\mingw32\bin\g++.exe",
        r"C:\TDM-GCC-64\bin\g++.exe",
        r"C:\Program Files\mingw64\bin\g++.exe",
        r"C:\Program Files (x86)\mingw64\bin\g++.exe",
        r"C:\w64devkit\bin\g++.exe",
        r"D:\mingw64\bin\g++.exe",
        r"D:\MinGW\bin\g++.exe",
        r"E:\mingw64\bin\g++.exe",
        r"E:\vscode\mingw64\bin\g++.exe",
        r"E:\vscode\mingw32\bin\g++.exe",
    ]
    for c in cands:
        if os.path.isfile(c):
            return c
    # 浅层扫描：找不到就交给用户装
    pats = ("*/mingw*/bin/g++.exe", "*/MinGW*/bin/g++.exe", "*/w64devkit/bin/g++.exe",
            "*/bin/g++.exe", "*/*/bin/g++.exe", "*/Dev-Cpp/MinGW*/bin/g++.exe")
    roots = [os.path.expanduser(r"~\AppData\Local\Programs"),
             r"C:\Program Files", r"C:\Program Files (x86)", r"C:\msys64",
             "C:\\", "D:\\", "E:\\"]
    for root in roots:
        if not os.path.isdir(root):
            continue
        for pat in pats:
            try:
                hits = glob.glob(os.path.join(root, pat))
            except Exception:
                continue
            for hit in hits:
                if os.path.isfile(hit):
                    return hit
    return ""


GXX = _find_gxx()

try:
    import paths
    SRC = paths.SRC_DIR
    SHOTS = paths.SHOTS_DIR
    DATA = paths.DATA_DIR
except Exception:
    ROOT = os.path.dirname(os.path.abspath(__file__))
    SRC = os.path.join(ROOT, "src")
    SHOTS = os.path.join(ROOT, "data", "shots")
    DATA = os.path.join(ROOT, "data")
for _d in (SRC, SHOTS, DATA):
    os.makedirs(_d, exist_ok=True)

# 运行日志：跑完还能复盘，不用靠页面里的滚动条
RUN_LOG = os.path.join(DATA, "run.log")
RUN_JSONL = os.path.join(DATA, "runs.jsonl")
_LOG_LOCK = threading.Lock()

PENDING_WORDS = ("尚未评测", "等待评测", "正在评测", "排队", "正在编译", "正在运行", "等待")


# ---------------- 编译与样例 ----------------

def compile_cpp(path):
    if not GXX:
        return None, ("本机没有找到 g++ 编译器。\n"
                      "请安装 MinGW-w64（或 w64devkit），并把它的 bin 目录加入系统 PATH，"
                      "然后重新启动本程序。")
    exe = os.path.splitext(path)[0] + ".exe"
    r = subprocess.run([GXX, "-std=c++17", "-O2", "-Wall", "-o", exe, path],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       creationflags=_NO_WIN)
    if r.returncode != 0:
        return None, (r.stderr or r.stdout)[:1500]
    return exe, ""


def norm(s):
    lines = [ln.rstrip() for ln in (s or "").replace("\r\n", "\n").split("\n")]
    while lines and not lines[-1].strip():
        lines.pop()
    while lines and not lines[0].strip():
        lines.pop(0)
    return "\n".join(lines)


def run_sample(exe, stdin_data, timeout=6):
    try:
        r = subprocess.run([exe], input=stdin_data, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout,
                           creationflags=_NO_WIN)
        if r.returncode != 0:
            return None, f"运行时退出码 {r.returncode}"
        return r.stdout, ""
    except subprocess.TimeoutExpired:
        return None, "运行超时（可能死循环）"


def strip_comments(code):
    """去掉 // 与 /* */ 注释，字符串和字符字面量里的内容原样保留

    两个目的：一是减少 token，二是不把中文注释交给可能是 GBK 的评测机
    （UTF-8 中文被按 GBK 解析时容易变成非法字节，最坏会编译失败）。
    """
    out = []
    i, n = 0, len(code)
    state = ""          # "" 普通 / "str" / "chr" / "line" / "block"
    while i < n:
        c = code[i]
        nxt = code[i + 1] if i + 1 < n else ""
        if state == "":
            if c == '"':
                state = "str"
            elif c == "'":
                state = "chr"
            elif c == "/" and nxt == "/":
                state = "line"
                i += 2
                continue
            elif c == "/" and nxt == "*":
                state = "block"
                i += 2
                continue
            out.append(c)
            i += 1
            continue
        if state == "str" or state == "chr":
            if c == "\\" and nxt:
                out.append(c)
                out.append(nxt)
                i += 2
                continue
            if (state == "str" and c == '"') or (state == "chr" and c == "'"):
                state = ""
            out.append(c)
            i += 1
            continue
        if state == "line":
            if c == "\n":
                state = ""
                out.append(c)
            i += 1
            continue
        if state == "block":
            if c == "*" and nxt == "/":
                state = ""
                i += 2
                continue
            if c == "\n":
                out.append(c)      # 保住行号，编译报错还能对上
            i += 1
    text = "".join(out)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def _tokens(s):
    """按空白切成数值/词，用来做「只看数值对不对」的宽松比对"""
    return (s or "").split()


def _num_eq(a, b):
    """数值按相对误差比，避免 6.00 与 6.000000 这种被判不符"""
    try:
        x, y = float(a), float(b)
    except (TypeError, ValueError):
        return a == b
    if x == y:
        return True
    return abs(x - y) <= 1e-6 * max(1.0, abs(x), abs(y))


def _tokens_eq(a, b):
    ta, tb = _tokens(a), _tokens(b)
    if len(ta) != len(tb):
        return False
    return all(_num_eq(x, y) for x, y in zip(ta, tb))


def _squeeze(s):
    """行内连续空格压成一个，行首尾空格去掉，保留换行结构"""
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in norm(s).split("\n")]
    return "\n".join(lines).strip()


def check_samples(exe, samples):
    """样例自检，返回 (能否提交, 说明, 差异类别)

    类别：exact 完全一致 / format 只差空格或换行 / fail 内容不符 / none 无样例。
    题面样例里的空格数量经常和文字要求对不上（要求一个空格、样例给两个），
    所以空格、换行、小数尾零都不算错；数值不同才算错。
    """
    if not samples:
        return True, "题目未提供可解析样例，跳过自检", "none"
    space_only = 0
    layout = 0
    for i, (cin, cout) in enumerate(samples, 1):
        got, err = run_sample(exe, cin + "\n")
        if got is None:
            return False, f"样例{i}：{err}", "fail"
        if norm(got) == norm(cout):
            continue
        if _squeeze(got) == _squeeze(cout):
            space_only += 1
            continue
        if _tokens_eq(got, cout):
            layout += 1
            continue
        return False, (f"样例{i} 输出不符\n输入：\n{cin}\n期望：\n{cout}\n实际：\n{got.strip()}"), "fail"
    notes = []
    if space_only:
        notes.append(f"其中 {space_only} 组只有空格数量不同")
    if layout:
        notes.append(f"其中 {layout} 组换行或小数位数不同")
    tail = ("（" + "，".join(notes) + "）") if notes else ""
    return True, f"{len(samples)} 个样例数值全部通过{tail}", ("format" if notes else "exact")


# ---------------- 引擎 ----------------

class Engine:
    """一次运行 = 一个竞赛下的一批题目"""

    def __init__(self, emit=None):
        self.emit = emit or (lambda e: None)
        self.stop_flag = threading.Event()
        self.oj = None

    # 事件
    def log(self, msg, level="info"):
        self.emit({"type": "log", "msg": msg, "level": level})
        # 同时落盘，页面关掉之后还能复盘
        try:
            with _LOG_LOCK:
                with open(RUN_LOG, "a", encoding="utf-8") as f:
                    f.write(time.strftime("[%m-%d %H:%M:%S] ")
                            + f"[{level}] " + str(msg) + "\n")
        except Exception:
            pass

    def _record(self, pid, title, attempts, samples, verdict, kind, secs, detail=""):
        """每道题一行 JSON，方便事后统计和对比"""
        row = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "pid": pid, "title": title,
               "attempts": attempts, "samples": len(samples or []),
               "verdict": verdict, "kind": kind, "secs": round(secs, 1),
               "detail": (detail or "").splitlines()[0][:200] if detail else ""}
        try:
            with _LOG_LOCK:
                with open(RUN_JSONL, "a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def problem(self, pid, **kw):
        d = {"type": "problem", "pid": pid}
        d.update(kw)
        self.emit(d)

    def done(self, **kw):
        d = {"type": "done"}
        d.update(kw)
        self.emit(d)

    # ---------- 登录 / 选题 ----------
    def connect(self, contest, user=None, pwd=None):
        cfg = load_env()
        user = user or cfg.get("XDOJ_USER")
        pwd = pwd or cfg.get("XDOJ_PASS")
        if not user or not pwd:
            raise OJError("缺少账号密码：在页面填写，或在 ojauto/.env 里配置 XDOJ_USER / XDOJ_PASS")

        oj = OJ()
        self.log(f"登录 {user} …")
        oj.login(user, pwd)
        self.log("登录成功", "ok")
        oj.select_contest(contest)
        self.log(f"已切换到考试 {contest}", "ok")
        self.oj = oj
        return oj

    def list_problems(self, contest, user=None, pwd=None):
        oj = self.connect(contest, user, pwd)
        ps = oj.problems()
        done = oj.solved_pids()
        for p in ps:
            p["ac"] = p["pid"] in done
        return ps

    # ---------- 单题 ----------
    def _statement(self, oj, pid, refresh=False):
        """题面缓存：PDF 存本地复用，文本每次重新抽取（免得沿用旧解析器的结果）"""
        pdf_path = os.path.join(SHOTS, f"p{pid}.pdf")
        pdf = b""
        if not refresh and os.path.isfile(pdf_path) and os.path.getsize(pdf_path) > 1000:
            try:
                with open(pdf_path, "rb") as f:
                    pdf = f.read()
            except Exception:
                pdf = b""
        if not pdf:
            pdf = oj.statement_pdf(pid)
            with open(pdf_path, "wb") as f:
                f.write(pdf)
            self.log(f"[{pid}] 题面已下载并缓存")
        else:
            self.log(f"[{pid}] 用本地缓存的题面")
        text, imgs = pdf_to_text(pdf)
        with open(os.path.join(SHOTS, f"p{pid}.txt"), "w", encoding="utf-8") as f:
            f.write(text or "(无文字层)")
        return text, imgs

    def solve_one(self, prob, cfg, retry=1, dry=False, wait=10, poll=25, gap=3,
                  strict=True):
        oj = self.oj
        pid, title = prob["pid"], prob["title"]
        t_start = time.time()
        self.problem(pid, title=title, status="准备中", attempt=0, code="", verdict="")
        self.log(f"[{pid}] {title}")

        if self.stop_flag.is_set():
            return {"pid": pid, "title": title, "verdict": "已停止", "attempts": 0}

        # 1. 题面
        try:
            text, imgs = self._statement(oj, pid)
        except Exception as e:
            self.log(f"[{pid}] 取题面失败：{e}", "err")
            return {"pid": pid, "title": title, "verdict": "题面失败", "attempts": 0,
                    "detail": str(e)}
        samples = parse_samples(text)
        self.log(f"[{pid}] 题面 {len(text)} 字，样例 {len(samples)} 组"
                 + ("，扫描版转图片" if imgs else ""))
        for i, (a, b) in enumerate(samples, 1):
            self.log(f"[{pid}]   样例{i}：{a} -> {b}".replace("\n", " / "))

        extra = (f"这是 XDOJ 第 {pid} 题「{title}」。"
                 f"时间限制 {prob.get('tl')} 秒，内存限制 {prob.get('ml')} KB。"
                 "注意：题面样例里的空格数量可能与题目要求的输出格式不一致，"
                 "输出格式请严格按题目文字描述（输入说明/输出说明）来写，样例只用于核对数值。")
        feedback = ""
        code = ""
        last = None
        kind = ""

        for attempt in range(1, retry + 2):
            if self.stop_flag.is_set():
                return {"pid": pid, "title": title, "verdict": "已停止", "attempts": attempt - 1}

            self.problem(pid, title=title, status="生成代码", attempt=attempt, code=code,
                         verdict="", samples=len(samples))
            self.log(f"[{pid}] 第 {attempt} 次生成代码…")
            # 重做时逐步升温：一直 0 度会让模型重复同一个错误
            temp = (0.0, 0.3, 0.7)[min(attempt - 1, 2)]
            if attempt > 1:
                self.log(f"[{pid}] 重做温度 {temp}")
            t0 = time.time()
            try:
                code = solve_with_fallback(
                    text, imgs,
                    extra=extra + (("\n" + feedback) if feedback else ""), cfg=cfg,
                    temperature=temp)
            except AuthError as e:
                self.log(f"[{pid}] 账号或 Key 鉴权失败，整批中止：{e}", "err")
                self.stop_flag.set()
                return {"pid": pid, "title": title, "verdict": "Key 失效", "attempts": attempt,
                        "detail": str(e)}
            except Exception as e:
                self.log(f"[{pid}] 调模型失败：{e}", "err")
                return {"pid": pid, "title": title, "verdict": "模型失败", "attempts": attempt,
                        "detail": str(e)}

            raw_len = len(code)
            code = strip_comments(code)
            cpp = os.path.join(SRC, f"p{pid}.cpp")
            if os.path.exists(cpp) and attempt > 1:
                # 旧版本留档，方便对比「改了之后为什么反而错了」
                try:
                    shutil.copy2(cpp, os.path.join(SRC, f"p{pid}.a{attempt - 1}.cpp"))
                except Exception:
                    pass
            with open(cpp, "w", encoding="utf-8") as f:
                f.write(code)
            self.problem(pid, title=title, status="编译中", attempt=attempt, code=code, verdict="")
            self.log(f"[{pid}] 出码 {raw_len} 字节 / {time.time()-t0:.1f}s"
                     + (f"，去注释后 {len(code)} 字节" if len(code) != raw_len else ""))

            exe, err = compile_cpp(cpp)
            if not exe:
                self.log(f"[{pid}] 编译失败", "err")
                feedback = "上一版编译失败，请修正：\n" + err
                self.problem(pid, title=title, status="编译失败", attempt=attempt,
                             code=code, verdict="", detail=err[:800])
                continue
            self.log(f"[{pid}] 编译通过", "ok")

            ok, msg, kind = check_samples(exe, samples)
            self.log(f"[{pid}] 样例自检：{msg.splitlines()[0]}",
                     "ok" if ok else "warn")
            if not ok:
                feedback = ("上一版代码与题目样例不符：\n" + msg
                            + "\n请修正逻辑。若你认为样例本身有误，就严格按题面文字描述实现。")
                self.problem(pid, title=title, status="样例不符", attempt=attempt,
                             code=code, verdict="", detail=msg[:800])
                if attempt <= retry:
                    continue
                if strict and not dry:
                    # 明知样例都过不了还交，只会浪费提交次数、还可能把错代码钉在记录里
                    self.log(f"[{pid}] 多轮仍与样例不符，按设置不提交", "err")
                    self._record(pid, title, attempt, samples, "样例不符", kind,
                                 time.time() - t_start, msg)
                    return {"pid": pid, "title": title, "verdict": "样例不符",
                            "attempts": attempt, "cpp": cpp, "samples": len(samples)}

            if dry:
                self.log(f"[{pid}] 仅生成模式，不提交")
                self._record(pid, title, attempt, samples, "已生成", kind, time.time() - t_start, msg)
                return {"pid": pid, "title": title, "verdict": "已生成", "attempts": attempt,
                        "cpp": cpp}

            # 2. 提交
            self.problem(pid, title=title, status="提交中", attempt=attempt, code=code, verdict="")
            before = oj.max_sub_id()
            r = oj.submit(pid, cpp)
            if r.get("code") not in (0, "0", None):
                self.log(f"[{pid}] 提交被拒：{r}", "err")
                feedback = "提交被平台拒绝：" + str(r.get("info"))
                self.problem(pid, title=title, status="提交被拒", attempt=attempt,
                             code=code, verdict="", detail=str(r)[:400])
                continue
            self.log(f"[{pid}] 已提交，等待判定…")

            # 3. 轮询判定
            self.problem(pid, title=title, status="判定中", attempt=attempt, code=code, verdict="")
            got = None
            time.sleep(wait)
            for _ in range(poll + 1):
                if self.stop_flag.is_set():
                    return {"pid": pid, "title": title, "verdict": "已停止", "attempts": attempt}
                try:
                    rs = oj.results(rows=10)
                except Exception as e:
                    self.log(f"[{pid}] 取结果失败：{e}", "warn")
                    rs = []
                fresh = [x for x in rs if isinstance(x["sub_id"], int) and x["sub_id"] > before]
                if fresh:
                    x = max(fresh, key=lambda y: y["sub_id"])
                    got = x
                    if x["verdict"] and x["verdict"] not in PENDING_WORDS:
                        break
                time.sleep(gap)

            if not got:
                self.log(f"[{pid}] 没有等到判定结果", "err")
                self._record(pid, title, attempt, samples, "无判定", kind,
                             time.time() - t_start, msg)
                return {"pid": pid, "title": title, "verdict": "无判定", "attempts": attempt,
                        "cpp": cpp}

            last = got
            v = got["verdict"] or "未判定"
            if v in PENDING_WORDS:
                # 平台在排队，不是代码错；照实记录，别伪装成「无判定」
                self.log(f"[{pid}] 等待 {wait + (poll + 1) * gap} 秒仍是「{v}」，记为排队超时", "warn")
                self.problem(pid, title=title, status="排队超时", attempt=attempt, code=code,
                             verdict=v, sub=got["sub_id"])
                self._record(pid, title, attempt, samples, "排队超时", kind,
                             time.time() - t_start, msg)
                return {"pid": pid, "title": title, "verdict": "排队超时", "attempts": attempt,
                        "sub": got["sub_id"], "cpp": cpp}

            self.log(f"[{pid}] 判定：{v}（提交号 {got['sub_id']}）",
                     "ok" if v == VERDICT_OK else "warn")
            self.problem(pid, title=title, status=v, attempt=attempt, code=code,
                         verdict=v, sub=got["sub_id"])

            if v == VERDICT_OK:
                self._record(pid, title, attempt, samples, "正确", kind,
                             time.time() - t_start, msg)
                return {"pid": pid, "title": title, "verdict": "正确", "attempts": attempt,
                        "sub": got["sub_id"], "cpp": cpp}
            feedback = (f"上一版提交判定为「{v}」，逻辑或输出格式有错。"
                        f"请重新审题，注意边界情况；输出格式严格按题目的文字要求"
                        f"（分隔符、空格数量、每行几个数），不要照抄样例里可能不规范的空白。")

        self._record(pid, title, retry + 1, samples, (last or {}).get("verdict") or "未通过",
                     kind, time.time() - t_start, "")
        return {"pid": pid, "title": title,
                "verdict": (last or {}).get("verdict") or "未通过",
                "attempts": retry + 1, "cpp": os.path.join(SRC, f"p{pid}.cpp")}

    # ---------- 批量 ----------
    def run(self, contest, pids, retry=1, dry=False, delay=5, wait=10, poll=25,
            user=None, pwd=None, skip_ac=True, strict=True):
        cfg = load_env()
        try:
            if self.oj is not None and self.oj.contest == int(contest):
                oj = self.oj
            else:
                oj = self.connect(contest, user, pwd)
            ps = oj.problems()
            by_id = {p["pid"]: p for p in ps}
            self.log(f"考试 {contest} 共 {len(ps)} 题")
        except Exception as e:
            self.log(f"连接失败：{e}", "err")
            self.done(total=0, ac=0, results=[], error=str(e))
            return []

        done = set()
        if skip_ac:
            try:
                done = oj.solved_pids()
            except Exception:
                done = set()

        todo = []
        for pid in pids:
            p = by_id.get(pid) or {"pid": pid, "title": "(不在该考试题目列表中)", "tl": "", "ml": ""}
            if skip_ac and pid in done:
                self.log(f"[{pid}] 已是正确状态，跳过", "warn")
                self.problem(pid, title=p["title"], status="跳过", attempt=0,
                             code="", verdict="已通过")
                continue
            todo.append(p)

        self.log(f"待做 {len(todo)} 题：" + ", ".join(str(p["pid"]) for p in todo))
        if not strict:
            self.log("已关闭「样例不符不提交」，样例过不了也会照旧提交", "warn")
        results = []
        auth_failed = False
        for i, p in enumerate(todo, 1):
            if self.stop_flag.is_set():
                self.log("已停止", "warn")
                break
            self.log(f"—— 进度 {i}/{len(todo)} ——", "head")
            try:
                r = self.solve_one(p, cfg, retry=retry, dry=dry, wait=wait, poll=poll,
                                   strict=strict)
            except Exception as e:
                self.log(f"[{p['pid']}] 出错：{e}", "err")
                r = {"pid": p["pid"], "title": p["title"], "verdict": "出错", "detail": str(e)}
            results.append(r)
            if r.get("verdict") == "Key 失效":
                auth_failed = True
                self.log("Key 无效或额度用尽，剩余题目不再执行", "err")
                break
            if i < len(todo) and not self.stop_flag.is_set():
                time.sleep(delay)

        ac = sum(1 for r in results if r["verdict"] in ("正确", "已通过"))
        left = len(todo) - len(results)
        self.log(f"结束：共 {len(results)} 题，正确 {ac} 题"
                 + (f"，{left} 题未执行" if left else ""),
                 "ok" if ac == len(results) and results else "warn")
        self.done(total=len(results), ac=ac, results=results,
                  stopped=self.stop_flag.is_set(), pending=left,
                  error="Key 无效或额度用尽" if auth_failed else "")
        return results
