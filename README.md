# XDOJ 自动刷题 · 开发说明

> 这份文档是给要读源码、改功能的人看的，讲代码怎么组织、接口怎么调、参数怎么配。
> 只想把软件跑起来的人，请直接看 **[使用说明.md](使用说明.md)**。

## 一、这个程序在做什么

本地起一个小服务，按「考试编号 + 题目编号」把 XDOJ 的题面 PDF 拉下来，抽出文字和样例，交给大模型写 C++，用本机 g++ 编译、拿样例跑一遍，再提交、查判定，错了带着错误信息重做。

整条链路**只走 OJ 的 HTTP 接口**，不开浏览器、不截图、不做 OCR，所以速度稳定，也不受页面改版影响。

## 二、技术栈

| 部分 | 用了什么 |
| --- | --- |
| 后端 | Python 3.9+、FastAPI + uvicorn、requests |
| PDF | PyMuPDF（`import fitz`） |
| 模型 | 智谱开放平台，OpenAI 兼容的 v4 端点 |
| 编译 | 本机 MinGW-w64 的 g++（不内置） |
| 前端 | 单个 `static/index.html`，原生 JS，日志走 SSE |

## 三、目录与模块职责

```
xdoj/
├── 启动.bat            一键启动：找 Python → 建 venv → 装依赖 → 起服务 → 开浏览器
├── server.py           网页控制台（FastAPI + SSE）
├── engine.py           刷题引擎：编译、样例自检、提交、轮询、日志落盘
├── paths.py            路径与 .env 生成（区分源码运行 / 打包运行）
├── auto.py             命令行入口
├── desktop.py          桌面壳：Edge --app 窗口 + 心跳退出
├── oj/client.py        OJ 接口封装（登录、题库、题面、提交、判题）
├── oj/pdftext.py       PDF 取文字、按「输入样例/输出样例」切段配对
├── ai/solver.py        模型调用、提示词、.env 读写
├── static/index.html   控制台界面
├── app.ico             图标
├── requirements.txt    fastapi / uvicorn / requests / pymupdf
├── .env.example        配置模板
└── data/、src/         运行后生成（见第十节）
```

`engine.py` 是唯一的主流程，网页和命令行都调它，所以两条入口行为一致。

## 四、一次刷题的链路（接口级）

```
登录 → 选竞赛 → 拉题目列表 → 下载题面 PDF → 抽文字+样例
→ 模型出码 → g++ 编译 → 样例自检 → 提交 → 轮询判定 → 失败重做
```

对应的接口（`oj/client.py` 里 `BASE = http://47.92.139.74/xdoj-ssm`）：

| 环节 | 请求 | 关键参数 |
| --- | --- | --- |
| 登录 | `POST /user/login.do` | `userId`、`password`、`preferLanguage` |
| 选竞赛 | `POST /team/selectContest.do` | `contestId` |
| 题目列表 | `GET /contest/problems.do` | `contestId`、`userId`、`rows`、`page` |
| 题面 | `GET /problem/downloadTextFile.do` | `problemId`，返回 PDF |
| 提交 | `POST /submission/submit.do` | multipart，`sourceFile` 挂 `.cpp`，`languageId=2`（GNU C/C++） |
| 判题 | `POST /submission/results.do?contestId=&userId=` | 返回 `totalCount` / `data` |

判定的做法：提交前先记下当前最大提交号，之后每 `poll` 秒查一次，**出现更大的提交号且状态不是「尚未评测」**才算拿到结果——这样不会被历史提交干扰。

## 五、编译环境（换电脑必须改这里）

程序**不打包编译器**，用的是本机 g++。查找顺序在 `engine.py` 的 `_find_gxx()` 里，依次是：

1. 环境变量 `CXX`
2. 环境变量 `GXX`
3. `PATH` 里的 `g++`
4. 几个常见安装目录的候选路径
5. 盘符浅层扫描

编译参数固定为 `g++ -std=c++17 -O2 -Wall -o <exe> <cpp>`。

换到别人电脑上不匹配时，三种改法任选一种：

- 把 g++ 所在的 `bin` 目录加进系统 `PATH`；
- 设环境变量 `CXX=你的 g++.exe 完整路径`（`GXX` 也行）；
- 直接改 `_find_gxx()` 里的候选路径列表。

启动脚本会先 `g++ --version` 探一下，找不到会打红字提示，但不阻断启动——这时编译必然失败，等于刷不动。

另外两个和评测机有关的点：

- 提交语言固定 `languageId=2`；
- 提交前会调 `strip_comments()` 删掉注释。一是省长度，二是评测机可能按 GBK 读源码，UTF-8 中文注释容易变非法字节直接 CE。

## 六、配置项（`.env`）

| 键 | 含义 | 默认 |
| --- | --- | --- |
| `XDOJ_USER` | 学号 | 空 |
| `XDOJ_PASS` | 密码 | 空 |
| `AI_API_KEY` | 智谱 Key | 空 |
| `AI_BASE_URL` | 接口地址 | `https://open.bigmodel.cn/api/paas/v4` |
| `AI_MODEL` | 主模型 | `glm-4.5-flash` |
| `AI_FALLBACK_MODEL` | 备用模型 | `glm-4-flash` |
| `AI_MAX_TOKENS` | 输出上限 | `4096` |

首次运行由 `paths.ensure_env()` 从 `.env.example` 生成；网页「设置」通过 `/api/config` 写回。`GET /api/config` 只回传「有没有配过、Key 有几位」，不返回 Key 内容，密码同理。

`.env` 只在本机，不要提交、不要随包分发。

## 七、样例校验规则

`check_samples()` 返回三元组 `(能否提交, 说明, 类别)`，类别有四种：

| 类别 | 含义 | 处理 |
| --- | --- | --- |
| `exact` | 完全一致 | 提交 |
| `format` | 只有空格数量或换行位置不同 | 提交，日志注明 |
| `fail` | 数值对不上 | `strict=True` 时不提交 |
| `none` | 题面里没解出样例 | 跳过自检，直接提交 |

比对细节：`_num_eq` 让 `6.00` 与 `6.000000` 视为相等；空白差异只记日志不判错；单组样例超时 6 秒。

样例抽取在 `oj/pdftext.py`：按「输入样例 / 输出样例」这类标题切段，配对优先级是 **标题序号 → 输入输出数量相等就整批一一对应 → 最后才按相邻顺序配**。顺序配对放最后，是因为它最容易把「输入示例 3」和「输出示例 1」凑成一对，造成期望值张冠李戴。

## 八、模型调用与重做策略

- 入口 `ai/solver.py` 的 `solve_with_fallback()`，优先主模型，失败换备用模型。
- **鉴权错误（401/无效 Key）会抛 `AuthError` 并中止整批**，不逐题白试。
- 输出被截断时，先把 `max_tokens` 翻倍重发（上限 `HARD_CAP=8192`），而不是立刻换模型——换过去的模型往往上限更低。
- 多轮重做温度依次 0 → 0.3 → 0.7，避免模型反复写同一个错误版本。
- 失败重做时会把「样例不符」的具体差异喂回模型，并要求「若认为样例有误，就严格按题面文字实现」。

## 九、服务端与控制台

- 端口：`PORT` 环境变量优先，否则从 8000 起往后找 30 个；真实端口写入 `port.txt`，`启动.bat` 靠它拿到地址。
- 事件流：`GET /api/events`（SSE），事件类型 `hello` / `log` / `problem` / `done`；日志留最近 800 条，重连会补发。
- 任务跑在独立线程里（`/api/run` 起线程），因为 uvicorn 的事件循环不能被阻塞。
- 其它接口：

| 接口 | 作用 |
| --- | --- |
| `GET /api/state` | 当前是否在跑、进度、每题状态 |
| `GET /api/problems` | 载入题目列表（已通过的带 ac 标记） |
| `POST /api/run` | 开始，body 里带 `contest`、`pids`、`retry`、`dry`、`delay`、`wait`、`poll`、`skip_ac`、`strict` |
| `POST /api/stop` | 置停止标志，当前步骤结束后退出 |
| `GET/POST /api/config` | 读/写配置 |
| `POST /api/open_dir` | 打开数据目录 |

- 心跳：页面每几秒 `GET /api/ping`；`desktop.py` 在「连续 180 秒没有心跳、并且当前没在刷题」时才结束服务，避免把跑到一半的批量任务腰斩。

## 十、运行产物

| 路径 | 内容 |
| --- | --- |
| `data/shots/p<题号>.pdf` | 题面 PDF，下载后复用，不重复拉 |
| `data/shots/p<题号>.txt` | 从 PDF 抽出的题面文字 |
| `src/p<题号>.cpp` | 生成的代码；`p123.a1.cpp` 这种是上一版留档 |
| `data/run.log` | 运行日志，页面关掉也能翻 |
| `data/runs.jsonl` | 每题一行：尝试次数、样例数、判定、耗时 |
| `port.txt` | 当前服务端口 |

打包成 exe 后，只读资源在解包目录，**可写数据改落到 `%APPDATA%\XDOJAuto`**（`paths.py` 里的 `FROZEN` 分支），不污染桌面。

## 十一、命令行用法

```bash
.venv\Scripts\python.exe auto.py --contest 310 --list
.venv\Scripts\python.exe auto.py --contest 310 --problem 509
.venv\Scripts\python.exe auto.py --contest 310 --range 39-44
.venv\Scripts\python.exe auto.py --contest 310 --all --dry
```

| 参数 | 说明 |
| --- | --- |
| `--contest` | 考试编号，默认 310 |
| `--problem` | 单题题号 |
| `--range` | **题目列表里的序号区间**（如 39-44），不是题号区间 |
| `--all` | 列表里全部 |
| `--list` | 只打印题目列表（序号、题号、是否已通过、标题） |
| `--dry` | 只生成不提交 |
| `--retry` | 单题最多重做次数，默认 1 |
| `--delay` | 题与题之间间隔秒数，默认 5 |
| `--poll` | 判定轮询间隔秒数，默认 15 |
| `--include-ac` | 不跳过已通过的题 |

退出码：全部判定为「正确 / 已通过」返回 0，否则 2，接口出错返回 1。

## 十二、开发注意

- 提交有频率限制，`delay` 别调到 2 秒以下，批量时更要留余量。
- 不要把 `.env`、`.venv` 提交进仓库；`.venv` 体积大且绑定本机路径。
- 仓库里的 `xdoj.zip` 是分发包，改动代码后需要重新打包才会同步。
- 本工具只用于本地练习和调试，编程作业请遵守学校规定。
