# best-2026-11-09 — 目前效果最好的一版

`root_scout.py` 快照（498,368 字节，sha256 `ee6da5ad8fc123c45e0dff224d72c5f9c52f8e6762102cda80131a56929e688c`），
与仓库根目录的 `root_scout.py` 内容一致，随仓库版本化。`validation/` 下的运行结果与 case_state 不进仓库。

## 这一版相对老基线改动四处

| # | 改动 | 位置 |
|---|---|---|
| 1 | 每案 schema 澄清上限 1 → 2 | `V2_PROTOCOL["limits"]["parse_clarifications_per_case"]` |
| 2 | 做过一次澄清后，`stop=true` 不再被本轮 issue 拦下（不再被迫多跑一轮） | `diagnose_v2` 循环内的 break 判据（两处） |
| 3 | 收尾轮残留的格式问题只记账、不改变结论；从未澄清过的残留仍判 `model_protocol_incomplete` | `v2_incomplete_protocol` / `v2_tolerated_issue_turns` |
| 4 | final review 的触发改用同一判据；报告给容忍项加标注 | `pending_results` / `markdown_v2` |

提示词未改动（`ROOT_SCOUT_PROMPT_PROFILE` 默认 `baseline`）。提升来自控制器把"格式噪音"和"结论"分开，而不是加指令。

## 运行配置

```text
OPENAI_MODEL=deepseek-v4.1-flash
OPENAI_REASONING_EFFORT=low
ROOT_SCOUT_REASONING_POLICY=fixed
ROOT_SCOUT_OUTPUT_POLICY=adaptive
ROOT_SCOUT_MODEL_OUTPUT_LIMIT=128000
ROOT_SCOUT_MAX_WALL_SECONDS=600
ROOT_SCOUT_EXEC_WORKERS=1
ROOT_SCOUT_SCHEMA_CLARIFY=1
ROOT_SCOUT_IMAGE=sha256:5105aa6bf38d3517dc4a0c6d52ac1904bf957761fc79c6f394fab7eda650837e
```

协议 v5，`--engine v2 --strategy causal`，2 并发跑 `balanced-100-v1`。

复现：

```powershell
python validation\bench-100-full-2026-11-09\run.py     # 断点续跑，已有 case_state 的会跳过
python validation\bench-100-full-2026-11-09\analyze.py # 与 bench-100-2026-11-08 逐例配对对比
```

## 结果（balanced-100-v1，100 题，单跑）

| 指标 | 老基线 `bench-100-2026-11-08` | 本版 `bench-100-full-2026-11-09` |
|---|---:|---:|
| token | 6,008,838 | **5,942,272**（−1.1%）|
| 调用 | 336 | 330 |
| 单例均值 | 60,088 | 59,422 |
| 批墙钟（2 并发）| — | 4,961s（83 分钟）|
| 协议失败 | 10 | **1**（p035）|
| 被拒动作 | 5 | 1 |
| 协议局部问题 | 43 | 34 |
| 澄清使用 | 3 | 25（23 例）|
| bug 严格 `defect_candidate` | 47/50 | **49/50** |
| bug 宽松（+`unclassified`）| 48/50 | **50/50** |
| control 警报 | 2/50（0 真 / 2 凭空猜测）| 2/50（**2 真 / 0 凭空猜测**）|

按 tier（本版）：easy bug 12/12、control 0/12；medium bug 24/25、control 2/25；hard bug 13/13、control 0/13。

## 两份 control 警报都是真缺陷

- **p060**：固定 `1e-10` 绝对容差，`N=76, P=0.6750` 精确最小 K=37（概率恰好 27/40），双精度差值约 −1e-10 跨过阈值 → 输出 38。
- **p061**：`vector<vector<char>> GRID`，首末两行只 push 了 `m+1` 个元素，`COUNTDIR/COUNTZ` 取 `GRID[X-1][Y+1]`，当 DFS 走到第 m 列时读到 `GRID[0][m+1]` —— 真实的单字节堆越界读，但不改变输出。

## 已知短板（未修）

1. **p035 稳定失手**：复跑 A/B 与全量四次采样都触发 `model_protocol_incomplete`；需要一个"被拒动作"的恢复通道，与 schema 澄清分开。
2. **kind 标签抖动**：同一份内容会在 `defect_candidate` / `resource_concern` / `unclassified` 之间漂移（p037 写成 `unclassified` 但内容是硬反例；p089 复跑中写成 `resource_concern`）。严格口径对标签极敏感，读结果时必须同时看内容。
3. **"真 UB 但不改输出"无维度**：p061 这类只能记成 control 警报；要分清需要给候选加"是否改变输出"的字段。
4. **单跑噪声**：逐例 token 中位数 0.92×、p10 0.47×、p90 2.46×、max 4.49×；单例级别的检出翻转不可归因，只有总体指标可归因。
