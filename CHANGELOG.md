# 更新日志

本项目所有值得记录的变更。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

> **关于版本号**：`0.x` 为开发阶段里程碑，未单独打 tag；`1.0.0` 为当前代码中的版本（`core/__init__.py` 与 `/api/health` 均返回 `1.0.0`）。全部版本在同一开发日内完成，故日期相同。

---

## [未发布]

### 计划中

- `createV2` 真实成单验证（当前仅验证到 `prepare` 拿 token）
- 支付链接自动获取（现在依赖手动点开）
- 二维码扫码登录（纯标准库生成 QR 图）
- 蹲票模式的多票档轮询（现在一次只蹲一个 SKU）

---

## [1.0.0] - 2026-10-29

首个正式版本。功能冻结，文档完备，可直接分发。

### 变更

- 版本号统一为 `1.0.0`：`core/__init__.py` 的 `__version__` 与 `server.py` 的 `/api/health` 响应一致
- `README.md` 重写为完整文档（功能、环境、登录、流程、模式、参数、原理、下单链路、HTTP 接口、文件结构、FAQ、注意事项）
- 新增 `.gitignore`，优先挡住 `data/`（含明文 Cookie 与浏览器配置）

### 已知限制

- `createV2` 会真实生成订单，未做自动化成单验证，只验证到 `prepare` 返回 token
- 手填明文身份证与打码值（如 `441************010`）的匹配只覆盖等长 18 位场景
- 代码审查列出的 8 类坏味道（重复的错误码解包、重复的致命类别元组、`(project_id, screen_id, sku_id, count)` 数据泥团等）尚未重构

---

## [0.5.1] - 2026-10-29

### 新增

- `README.md` 初版：快速开始、Cookie 获取、三种模式表、参数表、性能原理、下单链路、错误码、文件结构
- 清理开发期临时脚本与 pip 残留目录

---

## [0.5.0] - 2026-10-29

两轴代码审查（Standards / Spec）后的 8 处修复。

### 修复

- **蹲票模式吃满整个窗口**：`_fire` 原本共用 `max_seconds`，一轮就打 1800 秒回不到轮询。新增独立 `fire_seconds`，默认 定时 `300` / 立刻 `120` / 蹲票 `60`
- **任务结束后又被改回 monitoring**：`_finish` 落盘后 `_run_monitor` 仍继续写状态。`_fire(..., finish_on_fail=False)` + 按 `stop_flag` 与 `task.status` 判断是否继续
- **并发可能下出两张票**：`winner.set()` 拦不住已在途的 `createV2`。新增 `single_order`（默认开）+ `order_lock`，锁内复查 `winner`/`fatal`，抢到即放弃在途请求
- **`errno=0` 但无订单号也判成功**（日志会打印「订单号 None」）：改为当失败重试并输出原始返回
- **`engine.start` 不校验 `screen_id`**：补校验，缺失时提示「请选择场次」
- **`timestamp` 用本机时钟**：新增 `_ts_ms()`，`prepare` / `createV2` / `pay` 三处改用对时校准后的服务器时间
- **手填实名匹配不上打码值**：新增 `_id_match(plain, masked)`（长度一致 + `*` 位放行 + 其余位必须相同），`resolve_buyer` 两处比较改用它
- **`run.bat` 只写死 `python`**：改为 `where python` → `py -3` 回退 → 都没有则打印安装提示

### 变更

- `README.md` 参数表补充「单轮开抢 秒」「单订单锁」两行

---

## [0.4.0] - 2026-10-29

真实账号联调 + 两个 Windows 平台 bug。

### 新增

- 端口被占用时打印已有实例地址并退出，不再静默失败
- 启动时自动打开浏览器（可用 `--no-browser` 关闭）

### 修复

- **`run.bat` 双击报错**：文件用 LF 换行，`cmd.exe` 只认 CRLF，把 `title bilibili 会员购 · 自动抢票` 从空格处撕开，后半截被当成命令执行 → `'会员购' is not recognized`。改为 UTF-8 无 BOM + CRLF（与编码无关，纯粹是换行符）
- **Windows 上双实例抢同一端口**：`allow_reuse_address = True` 让第二个进程也能绑上 8765，表现为「有时打得开有时打不开」。改为 `allow_reuse_address = os.name != "nt"`

### 变更

- 实名自动填充改为「选观演人 ID」：服务端返回的 `personal_id` 本来就是打码的，官方页面也是这个做法，不需要明文身份证

### 验证

- 真实账号 `buyer/list` 返回 4 个观演人
- `prepare` 真实成功拿到 token，证明 auth + `bili_jct` CSRF + payload 结构都被服务端接受

---

## [0.3.0] - 2026-10-29

一键浏览器登录。

### 新增

- `core/ws.py`：手写 RFC6455 WebSocket 客户端（帧掩码、126/127 长度分支、分片重组、自动回 pong、按 id 配对响应），零第三方依赖
- `core/browser_login.py`：通过 Chrome DevTools 协议抓 Cookie
  - 独立 `--user-data-dir`（`data/browser-profile`），不碰用户日常浏览器数据
  - `--remote-debugging-port=0`，从 `DevToolsActivePort` 读实际端口
  - 每秒 `Storage.getCookies`，出现 `SESSDATA` + `bili_jct` 即抓取、校验、存盘、关窗口
  - 校验失败自动还原原登录态，不会把已登好的账号搞掉
  - 10 分钟超时，支持随时取消
- 三条接口：`POST /api/login/browser`、`GET /api/login/browser/status`、`POST /api/login/browser/cancel`
- 前端登录区改造：一键登录按钮 + 进度指示，手动粘贴收进 `<details>` 折叠区

### 变更

- 放弃二维码登录方案：纯标准库无法生成 QR 图，不交付半成品

---

## [0.2.0] - 2026-10-29

接口结构对齐与解析层重写。这一版修掉的三个 bug 都会导致功能完全不可用。

### 修复

- **票档解析读错位置**：原实现读顶层 `sku_list`（真实返回是空数组），改为走 `screen_list[].ticket_list[]`
- **中文实名直接崩**：`http.client` 把 str body 按 latin-1 编码，中文姓名触发 `UnicodeEncodeError: 'latin-1' codec can't encode ...`，但外层把它当「接口繁忙」重试了 220 次。改为统一 `body.encode("utf-8")` + `Content-Type: application/json; charset=utf-8`
- **致命错误不终止**：`create_order` 抛 `BiliError` 后无条件 `sleep + continue`。改为先 `classify_error`，auth / buyer / limit / closed 立即 `fatal.set()` 并记录「下单终止」
- **`prepare` 静默吞异常**：`except Exception` 不留痕迹。改为累加 attempts 并在前 3 次打 warn
- **未登录误判**：`errno=2`（请先登录）未被归类，落入可重试分支。`classify_error` 的 auth 码表补上 `2`

### 变更

- `normalize_project` / `normalize_sku` 按真实字段重写，兼容 `saleStart`(int) 与 `sale_start`(str) 双份时间字段
- 新增 `money(cents)` 金额格式化
- 选中票档后自动把该票档的 `saleStart` 填入「开抢时间」，用户手动改过则不再覆盖
- 场次下拉显示「日期 · N 档 · 售卖状态」，票档显示价格 / 余票 / 限购 / 售卖状态

### 验证

- 真实项目 `1007085`：解析出 5 个场次、5 个票档
- 定时开抢全链路：对时偏移 `-1190 ms`、RTT `62 ms`、预热 `6/6`、开抢时刻偏差 **0 ms**
- API 回归 8 项全通过

---

## [0.1.0] - 2026-10-29

项目骨架与主链路打通。

### 新增

- **零第三方依赖架构**：`pip install` 在本机全部失败（`OSError: [Errno 13] Permission denied`），改为只用 Python 标准库，同时规避版本兼容问题与用户安装成本
- `core/util.py`：时区（UTC+8）、时间解析与格式化、脱敏、`EventBus` 事件总线
- `core/store.py`：JSON 持久化，`os.replace` 原子写
- `core/httpc.py`：`HostClient` keep-alive 连接池、`prewarm()` 连接预热、`TimeSync` 基于 HTTP `Date` 头的整秒边界对时
- `core/bili.py`：`extract_project_id`（支持 `detail.html?id=`、mall 链接、纯数字）、`normalize_project`、`classify_error`、`BiliClient` 全链路封装
- `core/engine.py`：`Task` / `Engine`，三种模式分派，`_sleep_until` 分级精确等待（粗睡 → 细睡 → 纯自旋），`_fire` 多线程并发下单
- `server.py`：`ThreadingHTTPServer` 手写路由 + SSE 实时日志推送，支持 `--port` / `--host` / `--no-browser`
- `web/`：原生 HTML/CSS/JS 单页，深色主题，B 站粉 `#fb7299`，五段式布局（账号 / 场次票档 / 实名观演人 / 抢票模式 / 运行状态）
- `run.bat`：Windows 双击启动
- Cookie 粘贴登录：调 `/x/web-interface/nav` 校验，Cookie 存本地

### 说明

- 放弃 Playwright / requests：抢票瓶颈是延迟，自建连接池 + 对时 + 预热比通用 HTTP 库更可控
- 放弃二维码登录：纯标准库无法生成 QR 图片
