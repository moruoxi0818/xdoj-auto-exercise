# -*- coding: utf-8 -*-
"""XDOJ（SSM 版）纯 HTTP 客户端

接口全部来自实际抓包:
  登录      POST /user/login.do                 userId password preferLanguage
  选竞赛    POST /team/selectContest.do         contestId
  题目列表  GET  /contest/problems.do           contestId userId rows page  -> {totalCount,data:[...]}
  题面      GET  /problem/downloadTextFile.do   problemId                   -> PDF
  提交      POST /submission/submit.do          multipart
  判题      POST /submission/results.do?contestId=&userId=   -> {totalCount,data:[...]}
"""

import re

import requests

BASE = "http://47.92.139.74/xdoj-ssm"

# 单次刷题能拉到的题目上限（原为 200）。服务端若限制单页条数，会按 totalCount 自动续页。
MAX_PROBLEMS = 1000

VERDICT_OK = "正确"
VERDICTS = ("正确", "结果错误", "答案错误", "编译错误", "运行错误", "格式错误",
            "运行超时", "内存超限", "输出超限", "部分正确", "系统错误", "预判错误")
PENDING = ("尚未评测", "等待评测", "正在评测", "排队", "正在编译", "正在运行", "等待")


class OJError(RuntimeError):
    pass


def _as_int(v, default=0):
    """服务端 totalCount 有时是字符串，统一转成整数"""
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


class OJ:
    def __init__(self, timeout=30):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        self.timeout = timeout
        self.uid = ""
        self.class_id = ""
        self.contest = 0
        self._class_cache = {}

    # ---------------- 会话 ----------------
    def login(self, user, pwd):
        r = self.s.post(BASE + "/user/login.do",
                        data={"userId": user, "password": pwd, "preferLanguage": "zh_CN"},
                        timeout=self.timeout)
        try:
            js = r.json()
        except Exception:
            raise OJError("登录返回非 JSON: " + r.text[:200])
        if js.get("error") != 0:
            raise OJError(f"登录失败: {js}")
        self.uid = str(user)
        return js

    def logged_in(self):
        r = self.s.get(BASE + "/team/dashboard.do", allow_redirects=False,
                       timeout=self.timeout)
        return r.status_code < 400

    # ---------------- 竞赛 ----------------
    def select_contest(self, cid):
        r = self.s.post(BASE + "/team/selectContest.do",
                        data={"contestId": str(cid)}, timeout=self.timeout)
        try:
            js = r.json()
        except Exception:
            raise OJError("选竞赛返回异常: " + r.text[:200])
        if js.get("code") != 0:
            raise OJError(f"选竞赛 {cid} 失败: {js}")
        self.contest = int(cid)
        # 让服务端把当前竞赛写进 session
        self.s.get(BASE + "/team/problems.do", timeout=self.timeout)
        return js

    def _problems_page(self, cid, rows, page):
        """取一页题目列表，原样返回服务端 JSON"""
        r = self.s.get(BASE + "/contest/problems.do", params={
            "contestId": str(cid), "userId": self.uid, "_search": "false",
            "nd": "1", "rows": str(rows), "page": str(page), "sidx": "", "sord": "asc",
        }, headers={"Referer": f"{BASE}/team/problems.do"}, timeout=self.timeout)
        try:
            return r.json()
        except Exception:
            raise OJError("题目列表非 JSON: " + r.text[:200])

    def problems(self, cid=None, rows=MAX_PROBLEMS, page=1):
        """题目列表，默认可取到 1000 题

        只发一次大 rows 请求，服务端多半会截断成单页上限；所以按 totalCount 续页，
        直到拿满 rows 条或服务端不再返回新数据为止（用 pid 去重，防止分页参数被忽略时死循环）。
        """
        cid = cid or self.contest
        limit = max(1, int(rows or MAX_PROBLEMS))
        out, seen = [], set()
        total = 0
        cur = max(1, int(page or 1))
        for _ in range(60):                       # 页数硬上限，兜底防死循环
            if len(out) >= limit:
                break
            js = self._problems_page(cid, limit, cur)
            total = _as_int(js.get("totalCount"), total)
            data = js.get("data") or []
            if not data:
                break
            added = 0
            for p in data:
                pid = p.get("id")
                if pid in seen:
                    continue
                seen.add(pid)
                added += 1
                out.append({
                    "pid": pid,
                    "title": p.get("title") or "",
                    "category": p.get("categoryName") or "",
                    "tl": p.get("timeLimit"),
                    "ml": p.get("memoryLimit"),
                })
            if not added:                          # 服务端忽略 page，已经到底
                break
            if total and len(seen) >= total:
                break
            cur += 1
        return out[:limit]

    # ---------------- 题面 ----------------
    def statement_pdf(self, pid):
        r = self.s.get(BASE + "/problem/downloadTextFile.do",
                       params={"problemId": str(pid)}, timeout=60)
        if r.status_code != 200 or not r.content[:4] == b"%PDF":
            raise OJError(f"题面 {pid} 不是 PDF: {r.status_code} {r.headers.get('content-type')}")
        return r.content

    # ---------------- 提交 ----------------
    def class_id_for(self, pid):
        if pid in self._class_cache:
            return self._class_cache[pid]
        r = self.s.get(BASE + "/team/submit.do", params={"problemId": str(pid)},
                       timeout=self.timeout)
        m = re.search(r'id="classId"[^>]*value="([^"]*)"', r.text)
        cls = m.group(1) if m else ""
        self._class_cache[pid] = cls
        return cls

    def submit(self, pid, cpp_path, filename=None, language_id=2):
        import os
        cls = self.class_id_for(pid)
        name = filename or os.path.basename(cpp_path)
        if not name.lower().endswith(".cpp"):
            name = os.path.splitext(name)[0] + ".cpp"
        with open(cpp_path, "rb") as f:
            files = {"sourceFile": (name, f, "text/plain")}
            data = {
                "solutionType": "0",
                "contestId": str(self.contest),
                "userId": self.uid,
                "classId": cls,
                "problemId": str(pid),
                "languageId": str(language_id),
                "sourceCode": "",
            }
            r = self.s.post(BASE + "/submission/submit.do", data=data, files=files,
                            headers={"Referer": f"{BASE}/team/submit.do?problemId={pid}",
                                     "X-Requested-With": "XMLHttpRequest"},
                            timeout=120)
        try:
            js = r.json()
        except Exception:
            raise OJError(f"提交返回非 JSON: HTTP {r.status_code} {r.text[:200]}")
        return js

    # ---------------- 判题 ----------------
    def results(self, cid=None, rows=20, page=1):
        cid = cid or self.contest
        r = self.s.post(f"{BASE}/submission/results.do?contestId={cid}&userId={self.uid}",
                        data={"_search": "false", "nd": "1", "rows": str(rows),
                              "page": str(page), "sidx": "", "sord": "asc"},
                        headers={"Referer": f"{BASE}/team/results.do"},
                        timeout=self.timeout)
        try:
            js = r.json()
        except Exception:
            raise OJError("结果列表非 JSON: " + r.text[:200])
        out = []
        for x in js.get("data") or []:
            prob = x.get("problem") or {}
            out.append({
                "sub_id": x.get("id"),
                "pid": prob.get("id"),
                "verdict": (x.get("judgementText") or "").strip(),
                "judge": x.get("judgement"),
                "lang": (x.get("language") or {}).get("name"),
                "time": x.get("createTime") or x.get("submitTime") or "",
            })
        return out

    def max_sub_id(self, cid=None):
        return max([x["sub_id"] for x in self.results(cid, rows=10)
                    if isinstance(x["sub_id"], int)], default=0)

    def solved_pids(self, cid=None):
        """已经 AC 的题号集合

        提交记录按每页 200 条翻，最多看 1000 条：题量提到 1000 之后，只取第一页会漏掉
        早期 AC 的题，导致重复提交。服务端若忽略 page 参数，第二轮就取不到新记录，直接收工。
        """
        done, seen = set(), set()
        per = 200
        for page in range(1, max(1, MAX_PROBLEMS // per) + 1):
            batch = self.results(cid, rows=per, page=page)
            fresh = [x for x in batch if x["sub_id"] not in seen]
            if not fresh:
                break
            for x in fresh:
                seen.add(x["sub_id"])
                if x["verdict"] == VERDICT_OK and x["pid"]:
                    done.add(x["pid"])
        return done
