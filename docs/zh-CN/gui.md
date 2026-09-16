# 桌面 GUI

[English](../en/gui.md) | **简体中文**

Code Wood 在 `desktop/` 下提供桌面 GUI。终端 UI 与 GUI 共用 **同一个入口和同一个可执行文件**：正常启动 Code Wood 进入终端 UI，传入 `app` 命令则打开 GUI。GUI 通过 [pywebview](https://pywebview.flowspace.dev/) 在平台默认的 Web 引擎（Windows 上为 WebView2）中渲染 TypeScript（React）界面，并通过本地 HTTP + SSE 接口驱动 headless 的 `serve` 后端，复用既有的 agent。

功能：流式 AI 对话、全部 `/workspace` 与 `/chat` 命令、明暗主题、中英文界面，以及设置页（主题、语言、模型、执行策略）。

## 启动 GUI

```bash
# 开发环境
python cli/main.py app

# 打包版本
codewood app
```

启动 `app` 会打开桌面窗口且不带控制台窗口。在打包版本中，`codewood app` 以分离进程启动 GUI 并立即把控制权交还给命令行。GUI 进程通过以 `serve` 模式重新启动同一个可执行文件来拉起后端（开发环境为 `python cli/main.py serve`）。

打包产物是单个 **one-dir 目录**（`dist/codewood/`），内含 **两个可执行文件** 与共享的 `_internal/` 运行时：

- **`codewood.exe`** —— 完整控制台版本，包含 *全部* 终端 UI 与 GUI 功能。
  - 终端中不带参数 → 终端 UI。
  - `codewood.exe app` → 在任意位置打开桌面 GUI。
  - 双击 → 打开桌面 GUI（因为这是控制台子系统二进制，可能一闪控制台窗口）。
- **`codewood-gui.exe`** —— 与 `codewood.exe` 同目录的极小窗口启动器。双击它会在 **完全没有控制台窗口** 的情况下打开 GUI；它只是以无窗口进程运行 `codewood app`（同目录的 `codewood.exe`）。它只打包 Python 标准库，因此体积很小。想要最干净的 GUI 启动方式，就用这个可执行文件（或其快捷方式）。

在非 Windows 平台会生成同样结构的两个二进制（`codewood` 与 `codewood-gui`）。

构建采用 PyInstaller 的 **one-dir** 模式而非 one-file：one-file 二进制需要通过 bootloader 进程自解压，每次启动都会多一个常驻进程。one-dir 下每个可执行文件都是单进程运行，因此桌面 GUI 使用 **两个** 进程（GUI 宿主 + `serve` 后端），终端 UI 使用 **一个**。

## 架构

- GUI 进程启动一个后端进程，并通过 `127.0.0.1` 使用每次启动生成的 bearer token 与之通信。
- 后端的 `serve` 模式复用常规 agent 循环，因此每个 GUI 操作都对应终端中的同一套逻辑。

## 自动更新

GUI 在启动时以及随后每小时检查一次
[github.com/reedyang/codewood/releases](https://github.com/reedyang/codewood/releases)。
发现新版本后，会在后台**静默**下载对应的安装包到
`<配置目录>/cache/`：

| 平台 | 安装包 |
| --- | --- |
| Windows | `…-windows-x64-setup.exe` |
| macOS | `…-macos-<arch>.pkg` |
| Linux | `…-linux-<arch>.AppImage`（跨发行版、免 root 的通用格式，缺失时退回 `.deb`） |

下载支持**断点续传**：文件先写入 `<asset>.part`，并用 HTTP `Range` 从当前
偏移继续，因此中途退出 Code Wood、下次启动时会接着下载而不是重来。缓存中
只保留最新版本的安装包——开始新下载前会先删除旧安装包及其未完成文件。

每次下载都会用发布页公布的 **SHA-256**（GitHub asset 的 `digest` 字段）校验：
文件在变为可安装状态前先做哈希校验，校验不通过——或缓存中的旧包已不匹配——
都会被丢弃并重新下载。安装包字节数也必须与发布信息完全一致，否则不会出现
Update 按钮。

下载过程完全静默，**下载完成前界面上不会出现任何提示**。如果原地址下载失败
（部分网络无法连通 GitHub 的资源 CDN），会自动改用
`https://gh-proxy.com/<url>` 重试；设置 `CODEWOOD_UPDATE_PROXY=0` 可关闭该
回退。

下载完成后，标题栏最右侧（Windows/Linux 上位于最小化按钮左侧）会出现
**Update** 按钮并显示版本号。点击它会启动安装程序并退出 Code Wood，
以免安装时文件被占用。设置 `CODEWOOD_UPDATE=0` 可完全关闭该功能。

## 后端 serve 模式

```bash
# GUI 使用的 headless 服务（默认使用临时端口）
python cli/main.py serve
python cli/main.py serve --host 127.0.0.1 --port 8765
```

启动时它会在 stdout 打印一行 JSON 握手信息（`{"port": ..., "token": ...}`），GUI 据此建立连接。

## 开发 GUI

```bash
# 1. 安装依赖（包含 GUI 所需的 pywebview）
pip install -r requirements.txt

# 2. 安装并构建前端（或运行 Vite dev server）
cd desktop/frontend
npm install
npm run build           # 产出 desktop/frontend/dist，供宿主使用

# 3. 启动 GUI
cd ../..
python cli/main.py app
```

前端实时开发时，在 `desktop/frontend` 中运行 `npm run dev`，并在启动 `python cli/main.py app` 前通过环境变量 `CODEWOOD_GUI_URL` 指向它（例如 `http://localhost:5173`）。

## 打包

GUI 由标准构建脚本一并打包（前端会自动构建）。一次运行即产出包含 `codewood` 与 `codewood-gui` 的 `dist/codewood/` 目录：

```bash
# Windows
build\pack.bat

# macOS / Linux（自动识别）
bash build/pack.sh

```

`codewood` 默认运行终端 UI，以 `codewood app` 调用时运行桌面 GUI。`codewood-gui` 是一个轻量无窗口启动器，通过委托 `codewood app` 在无控制台窗口的情况下打开 GUI。分发时请携带整个 `dist/codewood/` 目录（可执行文件需要同级的 `_internal/`）。
