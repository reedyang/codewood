---
name: stock-daily-analysis
description: 【股票分析必经本技能】凡用户要「分析个股/ETF/指数/大盘表现、技术面、买卖点、复盘、盯盘、仓位与止损建议」等，必须通过本 skill 调用 `scripts/analyzer.py` 完成；禁止仅用联网摘录、闲聊或复述行情代替本技能的分析管线。股票/基金/指数专用；支持 A/H/美股、技术面（MA/MACD/RSI 等）、趋势与 AI 操作建议。强触发：股票分析、个股分析、行情分析、技术分析、买点卖点、复盘、盯盘、自选股等。
---

# Daily Stock Analysis

面向股票相关任务的高优先级 Skill，覆盖 A/H/美股、ETF 与指数场景，提供技术面分析、趋势研判与结构化交易建议。

## Bundle 与脚本路径（可移植）

本技能以 **bundle** 形式分发：`scripts/analyzer.py` 与本 `SKILL.md` 同属该 bundle。占位符 **`<skill_root>`** 表示**本 bundle 根目录的绝对路径**，须由**运行环境在调用前**解析并填入（勿自行猜测目录层级）。与上游行情脚本管道联调时，上游 bundle 的根目录记为 `<stock_data_skill_root>`，同样以环境提供的绝对路径为准。

## 强制路由（必须先读本段）

- **任何**涉及「对股票/基金/指数做分析、诊断、操作建议」的用户目标，**必须**通过本 bundle 内的 `scripts/analyzer.py` 产出结论；若运行环境支持按需加载完整 `SKILL.md`，应先加载再按下文调用脚本（通常与上游行情抓取脚本管道串联，或由上游注入 JSON）。
- **禁止**用仅联网检索摘录、纯自然语言臆测、或不经 `analyzer.py` 的输出，冒充「股票分析已完成」；摘录可以来自搜索或行情接口，但**分析结论与技术面要点必须来自本技能管线**。
- 若编排里同时出现「搜索/拉行情」与「分析」，搜索/取数只是**前置步骤**，**不能**在取数后直接结束；必须再执行 `analyzer.py` 并按「输出模板」回复用户。

## 功能特性

1. **多市场支持** - A股、港股、美股
2. **技术面分析** - MA5/10/20、MACD、RSI、乖离率
3. **趋势交易** - 多头排列判断、买入信号评分
4. **AI 决策** - 由AI分析决策
5. **数据源集成** - 通过标准多-skill 编排由上游注入行情快照（推荐上游为 `stock-data` 或 `baidu` skill）

本技能的分析管线**依赖**行情快照（`data_fetcher` 不单独拉网），但**不要求**也**不应**让 AI 在工作区创建临时 JSON 文件再喂给脚本。优先用 **标准输入输出管道** 或 **命令行内联 JSON**，见下文。

## 快速使用

```python
from scripts.analyzer import analyze_stock, analyze_stocks

# 单只分析
result = analyze_stock('600519')
print(result['ai_analysis']['operation_advice'])

# 批量分析
results = analyze_stocks(['600362', '601318', '159892'])
```

### 直接命令行调用

优先直接调用 skill 内置脚本，避免在 workspace 里创建临时 Python 脚本。**仅股票代码、无行情快照时，分析会因缺数据失败**——须按下面「协调调用」注入快照。

```bash
python "<skill_root>/scripts/analyzer.py" 600519 601318 00700
```

或输出 JSON：

```bash
python "<skill_root>/scripts/analyzer.py" 600519,601318 --json
```

> 其中 `<skill_root>` 为本技能目录绝对路径。

### 标准 skill 协调调用（推荐：管道，不落盘）

不要在本 skill 内通过 `subprocess` 去调其他 skill。应由编排侧 **先跑上游脚本、再管道接入本 skill**，或 **单行内联 `--quote-json`**（数据量很小时）。

**首选（A 股 + `stock-data`）：一条管道，无需中间文件**

```bash
python "<stock_data_skill_root>/scripts/fetch_realtime_snapshot.py" 688795 --compact | python "<skill_root>/scripts/analyzer.py" 688795 --quote-stdin --json
```

多代码时两边代码列表保持一致，例如：

```bash
python "<stock_data_skill_root>/scripts/fetch_realtime_snapshot.py" 600519 601318 --compact | python "<skill_root>/scripts/analyzer.py" 600519 601318 --quote-stdin --json
```

**次选：内联 `--quote-json`**（适合 JSON 很短、避免管道时）

```bash
python "<skill_root>/scripts/analyzer.py" 688795 --quote-json "{\"688795\":{\"name\":\"摩尔线程-U\",\"price\":548.81,\"change_pct\":8.61,\"change_amount\":43.49,\"open_price\":515.99,\"high\":555.00,\"low\":506.03,\"volume\":2666188,\"amount\":1422998000,\"pre_close\":505.32,\"turnover_rate\":9.07,\"pb_ratio\":65.50}}" --json
```

由 `baidu` 等拿到结构化行情时，同样优先 **拼进 `--quote-json`** 或 **经 stdin 传入**（可把 JSON 作为 here-string/管道上游脚本的 stdout），**不要**先 `text_file` 写入工作区再 `--quote-file`。

支持单标的简写（自动绑定到第一个 code）：

```bash
python "<skill_root>/scripts/analyzer.py" 688795 --quote-json "{\"name\":\"摩尔线程-U\",\"price\":548.81,\"change_pct\":8.61}" --json
```

**不推荐：`--quote-file`** — 仅当运行环境无法使用管道且命令行长度不足以容纳 JSON 时再考虑从路径读入；**禁止**为走 `--quote-file` 而在工作区主动创建临时 JSON 文件，除非用户明确要求生成文件。

## 配置

本内建 skill 无需配置文件，开箱即用。

## 执行约束（路由）

- **分析类任务必经本技能**：当任务是「分析股票/ETF/指数」或同义需求时，**必须**通过本 bundle 的 `scripts/analyzer.py` 产出结论；不得省略本技能仅用其它渠道凑答案。优先通过命令行/子进程直接调用 `analyzer.py`（与上游行情管道或 `--quote-json` 组合见上文）。
- 上游实时行情优先使用 `stock-data` skill 的抓取脚本；若不可用可退回其它已约定的行情来源。
- 禁止为此 skill 在 workspace 额外创建临时 Python 脚本做二次封装，除非用户明确要求“生成脚本文件”。
- 禁止为注入行情而在 workspace **创建中间 JSON 文件**（不要用 `text_file` 写快照再 `--quote-file`），除非用户明确要求落盘。编排时应 **上游抓取脚本 stdout 管道接入 `analyzer.py --quote-stdin`**，或 **单行 `--quote-json`**。
- 禁止在本 skill 代码内再嵌套调用其它 bundle 的脚本；应由编排侧分步执行上游，将数据经 **管道 stdin** 或 **`--quote-json`** 交给 `analyzer.py`。
- 当 `analyzer.py` 返回结构化结果（尤其是 `--json`）后，必须先输出「自然语言分析结论」，再结束面向用户的任务；禁止只回传原始 JSON 后直接结束。
- 对用户的最终可见回复必须是“结论优先 + 关键依据 + 风险提示 + 操作建议”的摘要形式，禁止粘贴大段原始日志/原始 JSON。

## 输出模板（强制）

当拿到 `analyzer.py` 结果后，AI 必须按下列模板组织自然语言输出（可多标的逐个展开）：

```markdown
【结论】
<1-2 句直接回答用户：当前趋势/是否偏强或偏弱/建议动作（买入-持有-减仓-观望）>

【关键信号】
- 趋势：<trend_status>；均线：<ma_alignment>
- 动量：MACD=<macd_status>，RSI=<rsi_status>
- 量能：<volume_status 或 volume_trend>

【风险与不确定性】
- 风险点：<risk_warning / risk_factors>
- 置信度：<confidence_level>（若低，说明原因）

【操作建议（仅供参考）】
- 建议：<operation_advice>
- 若有：目标价 <target_price>；止损位 <stop_loss>
```

补充约束：

- 若 `confidence_level` 为“低”或信号冲突，必须明确提示“观望/轻仓/等待确认”。
- 若字段缺失（如 `target_price`、`stop_loss` 为空），应明确写“暂无有效目标价/止损位”，不得编造。
- 输出模板完成后，若任务目标已满足，再由编排侧按运行环境的约定结束本轮（勿在本技能文档中假设具体宿主 API）。

## 返回数据

```python
{
    'code': '600519',
    'name': '贵州茅台',
    'technical_indicators': {
        'trend_status': '强势多头',
        'ma5': 1500.0, 'ma10': 1480.0, 'ma20': 1450.0,
        'bias_ma5': 2.5,
        'macd_status': '金叉',
        'rsi_status': '强势买入',
        'buy_signal': '买入',
        'signal_score': 75
    },
    'ai_analysis': {
        'sentiment_score': 75,
        'operation_advice': '买入',
        'confidence_level': '高',
        'target_price': '1550',
        'stop_loss': '1420'
    }
}
```

## 项目信息

- **开源协议**: MIT
- **项目地址**: https://github.com/yourusername/stock-daily-analysis
- **原项目**: https://github.com/ZhuLinsen/daily_stock_analysis

---

⚠️ **免责声明**: 本项目仅供学习研究，不构成投资建议。股市有风险，投资需谨慎。
