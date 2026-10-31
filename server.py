# -*- coding: utf-8 -*-
"""网页控制台：输入考试编号 + 题目编号，点「执行」即可自动刷题"""

import json
import os
import queue
import threading
import time

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from engine import Engine
from ai.solver import load_env, save_env

# 桌面壳用它判断「窗口还开着吗」
LAST_SEEN = {"t": time.time()}

try:
    import paths
    STATIC = paths.STATIC_DIR
    APP_DIR = paths.APP_DIR
except Exception:
    STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(title="XDOJ Auto")


class Bus:
    def __init__(self):
        self.lock = threading.Lock()
        self.history = []
        self.subs = []
        self.problems = {}
        self.running = False
        self.meta = {}

    def publish(self, ev):
        with self.lock:
            if ev.get("type") == "problem":
                pid = ev["pid"]
                cur = self.problems.setdefault(pid, {})
                cur.update(ev)
            if ev.get("type") == "log":
                self.history.append(ev)
                if len(self.history) > 800:
                    self.history = self.history[-600:]
            for q in list(self.subs):
                try:
                    q.put_nowait(ev)
                except Exception:
                    pass

    def subscribe(self):
        q = queue.Queue()
        with self.lock:
            self.subs.append(q)
            replay = list(self.history)
            probs = [dict(v) for v in self.problems.values()]
        return q, replay, probs

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subs:
                self.subs.remove(q)

    def reset(self):
        with self.lock:
            self.history = []
            self.problems = {}


BUS = Bus()
ENGINE = {"cur": None}


class RunReq(BaseModel):
    contest: int = 310
    pids: list[int] = []
    retry: int = 1
    dry: bool = False
    delay: int = 5
    wait: int = 10
    poll: int = 25
    skip_ac: bool = True
    strict: bool = True      # 样例不符就不提交
    user: str | None = None
    pwd: str | None = None


def _worker(req: RunReq):
    eng = Engine(BUS.publish)
    ENGINE["cur"] = eng
    BUS.running = True
    BUS.meta = {"contest": req.contest, "pids": req.pids, "total": len(req.pids)}
    try:
        eng.run(req.contest, req.pids, retry=req.retry, dry=req.dry, delay=req.delay,
                wait=req.wait, poll=req.poll, user=req.user, pwd=req.pwd,
                skip_ac=req.skip_ac, strict=req.strict)
    except Exception as e:
        BUS.publish({"type": "log", "msg": f"任务异常：{e}", "level": "err"})
        BUS.publish({"type": "done", "total": 0, "ac": 0, "results": [], "error": str(e)})
    finally:
        BUS.running = False
        ENGINE["cur"] = None


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"),
                        headers={"Cache-Control": "no-store, must-revalidate",
                                 "Pragma": "no-cache"})


@app.get("/api/state")
def state():
    return {"running": BUS.running, "meta": BUS.meta,
            "problems": list(BUS.problems.values())}


@app.get("/api/ping")
def ping():
    LAST_SEEN["t"] = time.time()
    return {"ok": True}


@app.get("/api/events")
def events():
    def gen():
        q, replay, probs = BUS.subscribe()
        try:
            yield "data: " + json.dumps({"type": "hello", "running": BUS.running,
                                         "problems": probs}, ensure_ascii=False) + "\n\n"
            for ev in replay:
                yield "data: " + json.dumps(ev, ensure_ascii=False) + "\n\n"
            while True:
                try:
                    ev = q.get(timeout=15)
                    yield "data: " + json.dumps(ev, ensure_ascii=False) + "\n\n"
                except queue.Empty:
                    yield ": ping\n\n"
        finally:
            BUS.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/problems")
def problems(contest: int = 310, user: str | None = None, pwd: str | None = None):
    try:
        ps = Engine().list_problems(contest, user, pwd)
        return {"ok": True, "problems": ps, "contest": contest}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)


@app.post("/api/run")
def run(req: RunReq):
    if BUS.running:
        return JSONResponse({"ok": False, "error": "已有任务在运行，先停止或等它跑完"},
                            status_code=200)
    if not req.pids:
        return JSONResponse({"ok": False, "error": "题目编号为空"}, status_code=200)
    BUS.reset()
    threading.Thread(target=_worker, args=(req,), daemon=True).start()
    return {"ok": True}


@app.post("/api/stop")
def stop():
    eng = ENGINE.get("cur")
    if not eng:
        return {"ok": False, "error": "当前没有任务"}
    eng.stop_flag.set()
    BUS.publish({"type": "log", "msg": "收到停止指令，等当前步骤结束…", "level": "warn"})
    return {"ok": True}


# ---------------- 配置 ----------------

MASK = "******"


class ConfigReq(BaseModel):
    user: str | None = None
    pwd: str | None = None
    api_key: str | None = None
    model: str | None = None
    base_url: str | None = None


@app.get("/api/config")
def get_config():
    cfg = load_env()
    key = cfg.get("AI_API_KEY") or ""
    try:
        import engine as _eng
        cxx = _eng.GXX
    except Exception:
        cxx = ""
    return {
        "user": cfg.get("XDOJ_USER") or "",
        "has_pwd": bool(cfg.get("XDOJ_PASS")),
        "has_key": bool(key),
        # 不回传 Key 的任何片段，只告诉前端「有没有配过」
        "key_hint": ("已保存（%d 位）" % len(key)) if key else "",
        "model": cfg.get("AI_MODEL") or "",
        "base_url": cfg.get("AI_BASE_URL") or "",
        # 只回传是否找到编译器，不回传完整路径
        "has_gxx": bool(cxx),
        "gxx_name": os.path.basename(cxx) if cxx else "",
        "data_dir": APP_DIR,
    }


@app.post("/api/config")
def set_config(req: ConfigReq):
    cfg = load_env()
    if req.user is not None:
        cfg["XDOJ_USER"] = req.user.strip()
    if req.pwd and req.pwd != MASK:
        cfg["XDOJ_PASS"] = req.pwd.strip()
    if req.api_key and req.api_key != MASK:
        cfg["AI_API_KEY"] = req.api_key.strip()
    if req.model is not None:
        cfg["AI_MODEL"] = req.model.strip() or cfg.get("AI_MODEL", "")
    if req.base_url is not None:
        cfg["AI_BASE_URL"] = req.base_url.strip() or cfg.get("AI_BASE_URL", "")
    save_env(cfg)
    return {"ok": True}


@app.post("/api/open_dir")
def open_dir():
    try:
        os.startfile(APP_DIR)  # noqa: S606
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    import socket

    import uvicorn

    def pick_port(start=8000, tries=30):
        for p in range(start, start + tries):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
            finally:
                s.close()
        return start

    port = int(os.environ.get("PORT") or 0) or pick_port()
    # 一键启动脚本靠这个文件拿到真实端口
    try:
        with open(os.path.join(APP_DIR, "port.txt"), "w", encoding="utf-8") as f:
            f.write(str(port))
    except Exception:
        pass
    print(f"控制台: http://127.0.0.1:{port}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
