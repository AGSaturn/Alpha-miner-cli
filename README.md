# Alpha Miner CLI

QuantGPT 因子研究的命令行工具。将 [QuantGPT](https://github.com/Miasyster/QuantGPT) MCP Server 的全部能力暴露为终端子命令，支持 A 股回测、因子评分、诊断、WQ BRAIN 提交等。

## 安装

```bash
pip install -e .
```

依赖 [quantgpt](https://github.com/Miasyster/QuantGPT) >= 2.5.0。

## 子命令一览

### 信息查询

| 命令 | 说明 |
|---|---|
| `alpha-miner list-operators` | 列出所有支持的因子表达式操作符 |
| `alpha-miner list-universes` | 列出可用股票池和基准 |

### 因子验证

```bash
alpha-miner validate --expression "rank(close/ts_mean(close,20))"
alpha-miner validate --expression "rank(close/open)" --mode wq
```

### 回测

```bash
alpha-miner backtest \
  --expression "rank(close/ts_mean(close,20))" \
  --universe hs300 \
  --start 2023-01-01 \
  --end 2025-12-31 \
  --groups 5 \
  --holding 5 \
  --benchmark hs300
```

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--expression` | (必填) | 因子表达式 |
| `--universe` | `hs300` | 股票池: `small_scale` / `hs300` / `csi500` / `csi1000` / `csi2000` |
| `--start` | `2023-01-01` | 起始日期 |
| `--end` | `2025-12-31` | 结束日期 |
| `--groups` | `5` | 分组数量 |
| `--holding` | `5` | 持仓周期(交易日) |
| `--benchmark` | `hs300` | 基准: `hs300` / `zz500` / `sz50` / `csi1000` |
| `--no-neutralize-industry` | | 禁用行业中性化 |
| `--no-neutralize-cap` | | 禁用市值中性化 |

输出 JSON: `report_path`（HTML 报告路径）、`metrics`、`backtest_summary`、`anti_overfit`。

### 因子评分

```bash
alpha-miner score --expression "rank(close/ts_mean(close,20))" --universe small_scale
```

参数同 backtest，但不生成 HTML 报告，专注输出评分(0-100)和等级(A/B/C/D)。

### 因子诊断

```bash
alpha-miner diagnose \
  --expression "rank(ts_mean(close,20))" \
  --score 45 \
  --ic-mean 0.03 \
  --ic-ir 0.5 \
  --monotonicity 0.6
```

输出: `strategy`（推荐变异策略）、`reason`、`mutation_prompt`（定向 LLM 提示词）。

### 反过拟合检测

```bash
alpha-miner anti-overfit --expression "rank(close/ts_mean(close,20))" --universe small_scale
```

4 项测试: IC 稳定性、子样本压力、安慰剂检验、半衰期估计。

### 滚动验证

```bash
alpha-miner rolling --expression "rank(close/ts_mean(close,20))" --universe hs300 --start 2020-01-01
```

Walk-Forward 滚动窗口，评估样本外衰减。

### WQ BRAIN 模拟

```bash
alpha-miner wq-submit \
  --expression "rank(close/open)" \
  --tag my-agent \
  --region USA \
  --wq-universe TOP3000 \
  --delay 1 \
  --decay 0 \
  --neutral SUBINDUSTRY \
  --auto-submit
```

需要配置 `.env`:
```
WQ_BRAIN_EMAIL=your_email
WQ_BRAIN_PASSWORD=your_password
```

### WQ BRAIN 批量扫描

```bash
alpha-miner wq-batch \
  --expression "rank(close/open)" \
  --tag my-agent \
  --regions USA CHN \
  --delays 0 1 \
  --wq-universes TOP3000 TOP500 \
  --auto-submit
```

在 region × delay × universe × neutralization 网格上逐一模拟，返回最优组合。

### WQ Alpha 管理

```bash
# 通过 alpha_id 批量提交
alpha-miner wq-submit-ids --ids alpha_001 alpha_002

# 列出平台 alpha
alpha-miner wq-list --limit 50 --min-fitness 1.0 --status-filter UNSUBMITTED

# 批量查询状态
alpha-miner wq-check --ids alpha_001 alpha_002

# 查询最终 SC 检查结果
alpha-miner wq-finalize --ids alpha_001 alpha_002
```

### 服务器

```bash
alpha-miner serve --transport http --host 0.0.0.0 --port 8003
alpha-miner serve --transport streamable-http --port 8003
```

### 数据预下载

```bash
alpha-miner prefetch hs300 csi500 small_scale
```

## 输出格式

所有命令输出 JSON 到 stdout，错误输出到 stderr：

```json
{
  "error": "错误信息"
}
```

## 架构

```
alpha-miner <subcommand>
  → __main__.py: argparse 解析
    → commands.py: async 业务逻辑（直接调用 quantgpt 核心函数）
      → quantgpt: 回测 / 评分 / 诊断 / WQ BRAIN
        → stdout: JSON
```

CLI 和 QuantGPT MCP Server 共享同一套核心业务代码（`_run_backtest_in_process`、`generate_report`、`MutationEngine` 等），只是入口不同 —— CLI 走 argparse，MCP 走 JSON-RPC。

## License

MIT — 继承自 QuantGPT 项目。
