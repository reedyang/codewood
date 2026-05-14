---
name: stock-data
description: 使用新浪财经实时行情 API 获取 A 股快照，并输出可直接注入 stock-daily-analysis 的 JSON（stdout；可与 analyzer --quote-stdin 管道联动）。触发词：stock-data、实时行情、A股实时价格、获取股票快照、行情注入、quote-json。适用于先取数再分析的场景。
license: MIT
---

# Stock Data Snapshot

基于新浪财经实时行情接口（`hq.sinajs.cn`）的内建 skill，用于获取 A 股实时行情，并生成 `stock-daily-analysis` 可直接消费的快照 JSON。

## Bundle 与脚本路径（可移植）

本技能以 **bundle** 形式分发：与本 `SKILL.md` 同级的 `scripts/` 目录下为可执行脚本。占位符 **`<skill_root>`** 表示**该 bundle 的根目录绝对路径**，须由**运行环境在调用前**解析并填入（常见做法是在提示中列出检测到的 `scripts/*.py` 绝对路径）；**不要**凭猜测拼接父目录名。命令形如：

`python "<skill_root>/scripts/fetch_realtime_snapshot.py" ...`

## 用途

- 获取单只或多只 A 股实时行情。
- 输出标准字段：`name/price/change_pct/change_amount/open_price/high/low/volume/amount/pre_close`。
- 作为上游 skill，为 `stock-daily-analysis/scripts/analyzer.py` 提供输入（**推荐**管道：`fetch_realtime_snapshot.py ... --compact | analyzer.py ... --quote-stdin`）。

## 依赖

- Python：标准库即可（无需额外依赖包）
- 凭证：默认无需 token（取决于上游数据源可用性）

## 命令行

```bash
python "<skill_root>/scripts/fetch_realtime_snapshot.py" 600519 601318
```

逗号分隔也支持：

```bash
python "<skill_root>/scripts/fetch_realtime_snapshot.py" 600519,601318
```

输出紧凑 JSON（推荐给下游 **`analyzer.py --quote-stdin`** 管道或 `--quote-json`）：

```bash
python "<skill_root>/scripts/fetch_realtime_snapshot.py" 600519 --compact
```

网络不稳定时可加重试参数：

```bash
python "<skill_root>/scripts/fetch_realtime_snapshot.py" 600519 --compact --retries 5 --retry-delay 1.5
```

## 与 stock-daily-analysis 联动

**推荐一条 stdin/stdout 管道**（不在工作区写中间文件）：

```bash
python "<stock_data_skill_root>/scripts/fetch_realtime_snapshot.py" 600519 601318 --compact | python "<stock_skill_root>/scripts/analyzer.py" 600519 601318 --quote-stdin --json
```

若不能管道，再将上一步 stdout 整段作为 `analyzer.py` 的 **`--quote-json` 参数**（短 JSON 时），**避免**先写入临时文件再 `--quote-file`，除非环境限制必须读路径。

## 代码与交易所（易错点）

- **仅写 6 位数字**时，脚本按首位规则推断 `sh` / `sz`（如 `600519`→`sh600519`，`000001`→`sz000001` **平安银行**）。
- **上证指数**与深交所 **000001 个股** 在新浪侧符号不同：指数行情为 **`sh000001`**。若需上证指数，请在参数中**显式写出** `sh000001`（或 `SH000001`），不要只写 `000001`。
- 显式写出 `sh` / `sz` 前缀时，**不会**再被剥掉；JSON 顶层 key 与入参一致（如 `sh000001`），便于与下游 `analyzer.py` 传入的代码参数对齐。

## 约束

- 本 skill 只负责行情抓取与标准化，不做交易建议。
- 不在本 skill 内调用其他 skill 脚本，由上层 AI 进行编排。
- 若用户只给出公司/标的自然语言称呼而未在句中写出本脚本所需的交易所代码，编排侧应**先**取得可靠代码（例如经会话内已确认的信息或运行环境提供的记忆检索），再调用本脚本；**禁止**臆测代码。
- 若脚本出现网络错误，建议提高 `--retries` 与 `--retry-delay` 后重试。
