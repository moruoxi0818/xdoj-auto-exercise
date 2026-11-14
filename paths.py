# -*- coding: utf-8 -*-
"""统一路径：开发时在项目目录，打包后用户数据落到 %APPDATA%\\XDOJAuto"""

import os
import sys

import charset

FROZEN = bool(getattr(sys, "frozen", False))

if FROZEN:
    # 只读资源（static、模板）在 PyInstaller 解包目录里
    BUNDLE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    # 可写数据跟着用户走，不污染桌面
    _base = os.environ.get("APPDATA") or os.path.expanduser("~")
    APP_DIR = os.path.join(_base, "XDOJAuto")
else:
    BUNDLE_DIR = os.path.dirname(os.path.abspath(__file__))
    APP_DIR = BUNDLE_DIR

STATIC_DIR = os.path.join(BUNDLE_DIR, "static")
if not os.path.isdir(STATIC_DIR):
    STATIC_DIR = os.path.join(APP_DIR, "static")

DATA_DIR = os.path.join(APP_DIR, "data")
SHOTS_DIR = os.path.join(DATA_DIR, "shots")
SRC_DIR = os.path.join(APP_DIR, "src")
ENV_FILE = os.path.join(APP_DIR, ".env")

for _d in (DATA_DIR, SHOTS_DIR, SRC_DIR):
    os.makedirs(_d, exist_ok=True)


def ensure_env():
    """首次运行从模板生成 .env（按默认编码 GBK 落盘）"""
    if os.path.exists(ENV_FILE):
        return ENV_FILE
    for cand in (os.path.join(BUNDLE_DIR, ".env.example"),
                 os.path.join(BUNDLE_DIR, "default.env")):
        if os.path.exists(cand):
            try:
                tpl = charset.read_text(cand, charset.detect(cand))
                charset.write_text(ENV_FILE, tpl)
                break
            except Exception:
                pass
    else:
        charset.write_text(ENV_FILE,
                           "AI_API_KEY=\nAI_BASE_URL=https://open.bigmodel.cn/api/paas/v4\n"
                           "AI_MODEL=glm-4.5-flash\nXDOJ_USER=\nXDOJ_PASS=\n")
    return ENV_FILE
