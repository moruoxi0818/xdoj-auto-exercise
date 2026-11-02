# XDOJ 自动刷题 · 开发说明

> 面向要读源码、改行为、重新打包的人：讲模块边界、接口契约、策略取舍和踩过的坑。
> 只想知道怎么用，看 [使用说明.md](使用说明.md)。

## 一、设计取舍

三条基础决定，改动前先理解，否则容易改回去：

1. **全程走 HTTP 接口，不开浏览器、不截屏、不做 OCR。** 页面截图会受窗口焦点、系统缩放、DPI 影响，换台机器结果就变；接口版可复现、可重试，也不怕前端改版。
2. **题面同时以「文字 + 图片」喂给模型。** 纯文字链路会丢公式结构（详见第七节），纯图片链路读样例和格式说明很吃力、token 还贵。两者同发，让模型以图校公式、以文校样例。
3. **本地必须编译并跑样例后才提交。** 把明显错误的代码拦在本机，减少无效提交；但它只是过滤器，不是正确性证明（见第八节）。

## 二、技术栈

| 部分 | 选型 | 备注 |
| --- | --- | --- |
| 后端 | Python 3.9+、FastAPI、uvicorn | 单进程；任务另起线程 |
| HTTP 客户端 | requests | 会话保持 Cookie 登录态 |
| PDF | PyMuPDF（`import fitz` / `import pymupdf`） | 取文字 + 页面渲染 |
| 模型 | 智谱开放平台 v4（OpenAI 兼容） | 视觉模型主出码，文本模型兜底 |
| 编译 | 本机 gcc（C11），回退 g++ | 不内置编译器 |
| 前端 | `static/index.html` 单文件，原生 JS | 日志用 SSE |

依赖清单见 `requirements.txt`，只有四个包。

## 三、模块划分

```
xdoj/
├── 启动.bat            源码版启动器：找 Python → 建 venv → 装依赖 → 起服务 → 开浏览器
├── server.py           控制台后端：REST + SSE，把任务丢到线程里跑
├── engine.py           刷题引擎：题面获取、编译、样例校验、提交、轮询、落盘
├── paths.py            路径解析（源码 / 打包两种模式）与 .env 生成
├── auto.py             命令行入口
├── desktop.py          桌面壳：Edge --app 窗口 + 心跳退出；自带 --selftest
├── oj/client.py        OJ 接口封装：登录、竞赛、题库、题面、提交、判题
├── oj/pdftext.py       PDF 取文字、公式修复、样例切段配对
├── ai/solver.py        模型调用、提示词、截断重试、.env 读写
└── static/index.html   控制台界面
```

`engine.Engine` 是唯一主流程，`server.py` 和 `auto.py` 都只是它的调用方，所以两条入口行为必然一致。改流程只改 `engine.py`。

## 四、主链路与 OJ 契约

```
登录 → 选竞赛 → 拉题目列表 → 下载题面 PDF → 抽文字 + 解析样例
→ 页面渲染成图 → 视觉模型出码(C) → gcc 编译 → 样例校验 → 提交 → 轮询判定 → 失败重做
```

`oj/client.py` 里 `BASE = http://47.92.139.74/xdoj-ssm`，主要端点：

| 环节 | 请求 | 关键参数 |
| --- | --- | --- |
| 登录 | `POST /user/login.do` | `userId`、`password`、`preferLanguage` |
| 选竞赛 | `POST /team/selectContest.do` | `contestId` |
| 题目列表 | `GET /contest/problems.do` | `contestId`、`userId`、`rows`、`page` |
| 题面 | `GET /problem/downloadTextFile.do` | `problemId`，返回 PDF 字节 |
| 提交 | `POST /submission/submit.do` | multipart，`sourceFile`、`languageId=2` |
| 判题 | `POST /submission/results.do` | `contestId`、`userId`，返回 `totalCount` / `data` |

**判定识别**：提交前记下当前最大提交号，之后每 `poll` 秒查一次，只有当出现更大的提交号、且状态不在「尚未评测/排队/正在编译」之列时才认结果。这样历史提交不会污染本轮判定，代价是首次提交若被系统合并也可能错过，此时按超时处理。

## 五、编译与语言策略

编译器**不打包**，查找逻辑在 `engine._find_tool()`，顺序为：环境变量 `CXX` / `GXX` → `PATH` 里的同名可执行文件 → 常见安装目录候选表 → 盘符浅层扫描。找不到时 `GCC` / `GXX` 为空串，`compile_cpp()` 直接返回可读的安装提示，而不是抛异常。

编译参数：

| 源码后缀 | 命令 |
| --- | --- |
| `.c` | `gcc -std=c11 -O2 -Wall -o exe src.c -lm` |
| `.c`（无 gcc） | `g++ -x c -std=c11 -O2 -Wall -o exe src.c -lm` |
| `.cpp` | `g++ -std=c++17 -O2 -Wall -o exe src.cpp` |

三个刻意的约束：

- **提示词锁定 C11 且禁注释。** 输出不带注释能省 token、少一类编译错误来源。
- **要求 `malloc` 显式强转、不用 C 专有扩展。** 评测机按 `languageId=2`（GNU C/C++）走哪个编译器，从接口无法确认；同一份代码让 gcc 和 g++ 都能过，是最省事的兼容方式。
- **提交前必过 `strip_comments()`。** 一是压长度，二是评测机可能按 GBK 读源码，UTF-8 中文注释会变成非法字节直接 CE——这条是踩过的真实故障。

源码落在 `src/p<题号>.c`，但提交时文件名仍按 `.cpp` 上传（`client.submit()` 的 `filename` 参数），保持与旧版本行为一致，避免因扩展名变化引出新问题。

## 六、配置与路径

`paths.py` 用 `sys.frozen` 分流，这是打包后最容易出问题的地方：

| 模式 | `APP_DIR`（可写） | `BUNDLE_DIR`（只读） |
| --- | --- | --- |
| 源码运行 | 项目目录 | 项目目录 |
| PyInstaller 打包 | `%APPDATA%\XDOJAuto` | `sys._MEIPASS` 解包目录 |

`STATIC_DIR` 先找解包目录、找不到再回落到 `APP_DIR`，因为 `--add-data "static;static"` 的资源位置在打包后会变。

`ensure_env()` 首次运行从 `.env.example`（缺失时用内置最小模板）复制出 `.env`，已存在则不动。`ENV_FILE` 永远指向 `APP_DIR`，所以源码版和 exe 版各有一份配置，互不干扰。

| 键 | 含义 | 默认 |
| --- | --- | --- |
| `XDOJ_USER` / `XDOJ_PASS` | OJ 账号 | 空 |
| `AI_API_KEY` | 智谱 Key | 空 |
| `AI_BASE_URL` | 接口地址 | `https://open.bigmodel.cn/api/paas/v4` |
| `AI_MODEL` | 文本主模型（兜底） | `glm-4.5-flash` |
| `AI_FALLBACK_MODEL` | 文本备用模型 | `glm-4-flash` |
| `AI_VISION_MODEL` | 视觉模型（主出码） | `glm-4.5v` |
| `AI_MAX_TOKENS` | 单次输出上限 | `4096` |

读写走 `ai/solver.py` 的 `load_env()` / `save_env()`。`GET /api/config` 只回传布尔状态和 Key 的位数提示（`key_hint`），**绝不回传 Key 与密码原文**；`POST /api/config` 中密码/Key 为空表示保持原值。

## 七、题面解析

### 7.1 文字抽取

`oj/pdftext.pdf_to_text()` 两种模式：

- **纯文本**（默认）：逐页 `page.get_text("text")`，叠加一次 `flags=_INHIBIT` 的「不补空格」取法，逐行比对后只丢掉 PyMuPDF 凭空插入的空格；再做 `repair_fractions()`。
- **始终给图**（`always_image=True`）：取文字的同时按 `Matrix(zoom, zoom)` 渲染页面 PNG，默认 `zoom=3.0`（约 216 dpi）、最多前 2 页。9pt 的公式小字在这个倍率下笔画不粘连；再高只是徒增 token。

文字少于 60 字视为扫描版，只返回图片、不返回文本——这类题只能靠视觉链路。

### 7.2 公式结构丢失与 `repair_fractions()`

PDF 里的公式不是字符串：分数线是矢量线段，上下标是更小字号的独立文本块。PyMuPDF 只能按坐标吐文字，于是

```
(x + 1)^2 + 2x + 1/x  →  (x + 1)2 + 2x +     ← 行尾停在运算符
                          1                    ← 分子
                          x                    ← 分母
```

模型读到的公式少了 `/x`，写出来自然少一项。若样例又刚好不覆盖该分支，就成了「样例全过、提交结果错误」。

`repair_fractions()` 做保守修复：只在第一个样例标题之前动手，只认「行尾是二元运算符 `+ - × ÷ * /`、紧跟两行都是 1～4 字符且不含空白的纯算式碎片」这一种确定形态，拼成 `a/(b)`。等号、逗号、左括号结尾**不做**合并——那种位置后面跟的短碎片通常是上标（如 `x` 的 `1/2` 次方），拼成分式反而更错。

上标依旧是普通数字（`(x+1)2`），代码层不猜，改由系统提示词要求模型按数学含义还原，并在出码前逐项核对。

### 7.3 样例配对

按「输入样例 / 输出样例」这类标题切段，配对的优先级依次是：

1. 标题带序号时按序号对齐；
2. 输入段数与输出段数相等时整批一一对应；
3. 以上都不成立才按相邻顺序硬配。

顺序配对放最后，因为它最容易把「输入示例 3」和「输出示例 1」凑成一对，造成期望值张冠李戴，而这类错误在日志里表现为「模型写错了」，极难排查。

## 八、样例校验语义

`check_samples(exe, samples)` 返回 `(可否提交, 说明, 类别)`：

| 类别 | 判据 | 处理 |
| --- | --- | --- |
| `exact` | 数值全部一致 | 提交 |
| `format` | 只有空格数量、换行位置或小数尾零不同 | 提交，日志注明 |
| `fail` | 数值不一致、运行崩溃、超时（单组 6 秒） | `strict=True` 时不提交 |
| `none` | 题面里解析不出样例 | 跳过自检，直接提交 |

比较用 `norm` → `_squeeze` → `_tokens_eq` 三级降级：先比规范化后的整串，再比去掉空白的串，最后逐 token 比数值（`6.00` 与 `6.000000` 视为相等）。这样「题面要求一个空格、样例给了两个」不会被误判为错。

**必须记住的盲区**：本地只跑题面给的样例。分段函数题若只给一组输入（例如 x=4，只走 x≥0 分支），另一分支写错本地查不出来，只能靠 OJ 判定暴露。这条不是实现缺陷，是信息不足，任何本地自检都跨不过去。

## 九、模型调用与重做

入口 `ai/solver.solve_with_fallback()`，优先级：

```
有图 → AI_VISION_MODEL（默认 glm-4.5v）看图出码
     失败（无权限/截断/超时）→ AI_MODEL 纯文字通道
无图 → AI_MODEL → AI_FALLBACK_MODEL
```

几个策略要点：

- **401 / Key 无效抛 `AuthError`，中止整批**，不逐题重试——那只会把额度耗在必然失败的请求上。
- **输出被截断时先翻倍 `max_tokens` 重发**（`HARD_CAP=8192`），而不是立刻换模型：备用模型的上限通常更低，换过去只会连续截断。
- **重做温度 0 → 0.3 → 0.7**，避免模型反复产出同一个错误版本。
- **失败重做时把具体差异喂回去**：附上题面里所有含运算符的公式行，并明确「若认为样例有误，就严格按题面文字实现」，避免模型在样例与文字之间摇摆。
- **`fix_broken_strings()`**：GLM 系列会把 `printf` 里的 `\n` 输出成真实换行，C 里直接 `missing terminating " character`。编译前统一把字符串字面量内部的换行还原成转义。这是接视觉链路时第一个炸掉的地方，也是最高频的编译失败原因。
- 视觉链路实测 25～85 秒/题（含一次 `max_tokens` 翻倍重发），比纯文字慢 2～3 倍，属预期成本。

`extract_code()` 认 `c` / `cpp` / `cc` 等多种代码块标注，无标注时退回整段文本，避免模型少写一个 ``` 就整题失败。

## 十、服务端与事件流

- **端口**：`PORT` 环境变量优先，否则从 8000 起向后试 30 个；实际端口写进 `port.txt`，`启动.bat` 靠它拼出地址。
- **并发模型**：`/api/run` 起一个 daemon 线程跑 `engine.run()`，uvicorn 事件循环不被阻塞；`BUS.running` 做互斥，同一时刻只允许一个任务。
- **事件流** `GET /api/events`（SSE）：类型 `hello` / `log` / `problem` / `done`；日志环形缓冲保留最近 800 条，断线重连会补发。
- **停止**：`/api/stop` 只置 `stop_flag`，引擎在每个步骤边界检查，正在编译或提交的题不会被腰斩。
- **心跳**：前端定时 `GET /api/ping`；`desktop.py` 在「连续 180 秒无心跳且当前未在刷题」时才退出，避免窗口关掉就把批量任务腰斩。

| 接口 | 作用 |
| --- | --- |
| `GET /api/state` | 是否在跑、进度、每题状态与代码 |
| `GET /api/problems` | 载入题目列表（含 ac 标记） |
| `POST /api/run` | 开始，body：`contest`、`pids`、`retry`、`dry`、`delay`、`wait`、`poll`、`skip_ac`、`strict` |
| `POST /api/stop` | 置停止标志 |
| `GET/POST /api/config` | 读/写配置（读接口一律脱敏） |
| `GET /api/ping` | 心跳 |
| `POST /api/open_dir` | 打开数据目录 |

## 十一、运行产物

| 路径 | 内容 | 可否删 |
| --- | --- | --- |
| `data/shots/p<题号>.pdf` | 题面 PDF，下载后复用 | 可，题库更新时删 |
| `data/shots/p<题号>.txt` | 抽取出的题面文字，便于排查公式问题 | 可 |
| `data/shots/*.png` | 渲染出的题面页图 | 可 |
| `src/p<题号>.c` | 生成的代码；`p123.a1.c` 是上一版留档 | 建议留 |
| `data/run.log` | 运行日志（打包后同名文件在 APP_DIR） | 可 |
| `data/runs.jsonl` | 每题一行：尝试次数、样例数、判定、耗时 | 建议留 |
| `port.txt` | 当前服务端口 | 可 |

排查「模型为什么写错」时，`data/shots/p<题号>.txt` 是最有价值的一份数据——它才是模型真正读到的题面。

## 十二、命令行

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
| `--list` | 只打印列表（序号、题号、是否已通过、标题） |
| `--dry` | 只生成不提交 |
| `--retry` | 单题最多重做次数，默认 1 |
| `--delay` | 题间间隔秒数，默认 5 |
| `--wait` | 单题最长等待秒数，默认 10 |
| `--poll` | 判定轮询间隔秒数，默认 15 |
| `--include-ac` | 不跳过已通过的题 |

退出码：全部通过返回 0，有失败返回 2，接口异常返回 1，可直接用于脚本判断。

## 十三、打包、自检与回归

打包（项目根目录执行）：

```bash
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --icon app.ico --name XDOJ刷题 --add-data "static;static" --add-data ".env.example;." ^
    --exclude-module pymupdf.layout --exclude-module onnxruntime ^
    --hidden-import uvicorn.logging --hidden-import uvicorn.loops.auto ^
    --hidden-import uvicorn.protocols.http.auto --hidden-import uvicorn.protocols.websockets.auto ^
    --hidden-import uvicorn.lifespan.on desktop.py
```

两个 `--exclude-module` 不能省：pymupdf 2.0 导入时会连带加载 `pymupdf.layout`，它自带 49 MB 的 onnx 模型资源；不排除就必须把整个 `layout/resources` 打进去，体积翻倍且容易丢文件。排除后 `import pymupdf` 走它自身的 `except ImportError` 分支，核心接口不受影响。四个 `--hidden-import` 覆盖 uvicorn 动态导入的协议实现，缺了会在解包环境里报 `No module named uvicorn.protocols...`。

打包后必须自检，`--selftest` 把 PDF 解析、页面渲染、编译器探测的结果写进 `%APPDATA%\XDOJAuto\data\app.log`：

```bash
dist\XDOJ刷题.exe --selftest 任意题面.pdf
```

三处回归建议在改动后各跑一遍：

1. **公式修复回归**：拿 `data/shots/*.txt` 的历史题面全部过一遍 `repair_fractions()`，要求文本零改动、`parse_samples()` 结果完全一致。误改历史题面的代价远高于漏改。
2. **视觉链路冒烟**：挑一道带平方/分式的题跑 `dry=True`，确认图渲染清晰、代码能编译、样例通过。
3. **打包自检**：`--selftest` 输出里 `pymupdf` 与 `gcc` 两行都必须是 OK。

## 十四、已知局限

- **公式还原不彻底**：`repair_fractions()` 只处理分式的一种确定形态，上下标仍靠提示词引导模型猜测；遇到矩阵、积分、上下标多层嵌套的题面，抽取结果依旧不可靠。
- **样例覆盖不足时无法本地兜底**：见第八节，属于信息不足，不是可修的 bug。若要缓解，可让模型额外产出 2～3 组覆盖各分支的自测输入，只检查崩溃/NaN，不比对输出——成本低但尚未实现。
- **视觉链路无逐题降级记录**：退回纯文字时只在日志里留一行，不落库，事后统计成功率需要翻 `run.log`。
- **判定轮询是轮询**：间隔 `poll` 秒，短时任务有最多一个周期的额外延迟；接口没有推送能力，只能如此。
- **单进程单任务**：`BUS.running` 互斥，没有任务队列，批量只能串行。多开实例会争用同一份 `.env` 与 `src/`，不建议。
- 提交有频率限制，`delay` 不要低于 2 秒；本工具仅用于本地练习和调试，编程作业请遵守学校规定。
