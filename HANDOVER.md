# root-scout 交接文档（2026-11-10）

读者：接手这个仓库的人。目标是一小时内能跑起来、看懂边界、知道哪些结论有数据支撑、哪些是坑。

---

## 1. 一页速览

- **做什么**：给一份算法题的提交（题面 + 源码），指出**哪一段代码有问题、为什么、用什么输入能证明**。产物是给人看的定位卡，不是补丁。
- **形态**：单进程 CLI，一个模型（`deepseek-v4.1-flash`，本地代理 `127.0.0.1:8788`）+ 一个严格控制器 + 一个 Docker 沙箱。
- **核心资产**：`CaseState` 账本。控制器校验一切，模型写的 `expected` 永不采信，全状态可哈希重算、可重放。
- **现状**：能稳定指出问题区域并给机制说明；**不能**稳定承诺"具体某一行"（同题复跑会漂）。
- **成本**：由**题目难度**决定，不由输入格式决定。首轮推理占输出 93–96%。

---

## 2. 产品形态

### 两种模式

| 模式 | 输入 | 场景 | 实测 |
|---|---|---|---|
| **A：有失败用例** | 题面 + 代码 + 一个 WA 的用例 | 真实刷题者来求助 | 复现 10/10；行号可用 9/10；成本与 B 持平 |
| **B：只有代码** | 题面 + 代码 | 提交前自查 | 100 题：bug 严格 49/50、宽松 50/50 |

**重要**：本数据集里题面自带的 `known_failing_tests` **全部通过**（`mismatches=0`）。真正失败的用例在 `evaluation/*.json` 的 private 测试里，模型看不到。所以 B 模式是"盲找"，A 模式才是用户真实场景。

### 一张卡片应该长这样

```
反例     输入 4 ... → 你的输出 X，正确 Y
参照     精确枚举（独立实现，非模型断言）
位置     第 20–25 行（区域级，不承诺单行）
机制     循环漏了 t=1，分母用 n-i 而非 n-i+1
改法方向 i <= n-1；den = n-i+1；max(0, a[0]-y)
证据等级 受信不符 / 沙箱观测 / 仅静态推理
```

**反例是主语，行号是定语。**

---

## 3. 架构

```
CLI (diagnose / replay / doctor)
      │
      ▼
控制器  root_scout.py  ~5,650 行 / 145 个顶层函数
 ├─ 协议   V2_PROTOCOL（schema v5）→ v2_protocol_spec() 生成机器可读协议
 ├─ 校验   validate_v2_response（逐条 candidate / update / action）
 ├─ 摄入   v2_process_response → v2_register_candidate（先落账，再校验动作）
 ├─ 执行   v2_execute_action：read_source / run_original / run_bash / run_variant
 ├─ 证据   v2_execute_source → T 记录（trusted_expected 只来自 caller 测试或 controller Oracle）
 ├─ 修复   response_repairs ｜ clarify(≤2) ｜ edit_range_repairs(±1 行) ｜ 控制器去重
 └─ 收尾   stop_reason + v2_localization
      │
      ├──► 执行层  Docker 镜像 5105aa6b：run_case / docker_bash_run / BashSession / BuildCache
      ├──► 状态层  CaseState JSON：v2_integrity(19 个 sha) + verify_saved_case_state + replay
      └──► 呈现层  markdown_v2（审计式）｜ localize_report.py（定位式）
```

### 一次诊断的生命周期

1. `docker_preflight` 确认镜像。
2. 组 `test_plan`（请求里的测试 + Oracle 用例）。
3. 原程序跑 baseline（**A 模式下这一步就会抓到受信不符**）。
4. 循环 ≤4 轮：模型返回一个 JSON → 控制器校验 → 执行动作 → 产生证据 → 更新候选生命周期。
5. 可选收尾轮（跑过动作的 case 强制一轮只下结论）。
6. 判定：`stop_reason` + `localization`。

### 五个不变量（改代码时不要破坏）

1. 模型的 `expected` 永不采信。
2. 候选先落账，再校验动作；坏动作不能抹掉 claim。
3. 候选只能引用**已存在**的证据 ID。
4. 被拒动作/更新/被合并候选全部留在账本；生命周期可从 `candidate_update_decisions` 重算。
5. 全状态哈希覆盖；`verify_saved_case_state` 与 `replay` 必须始终通过。

---

## 4. 代码地图

| 位置 | 作用 |
|---|---|
| `root_scout.py` | 全部逻辑（控制器、协议、报告、CLI） |
| `v2_protocol_spec()` / `v2_protocol_text()` | 协议的机器版与自然语言版（**改提示词会改变缓存前缀和行为**） |
| `v2_process_response()` / `v2_register_candidate()` | 候选摄入、去重（控制器生成的 merge 决策） |
| `v2_execute_action()` / `v2_apply_edits()` | 动作执行与变体编辑（含 ±1 行区间纠正） |
| `v2_response_repairs()` | 顶层键补齐、`kind` 推断、`arguments` 信封解包、`ref`/`question`/`reason` 补齐 |
| `v2_incomplete_protocol()` / `v2_tolerated_issue_turns()` | 协议失败与"收尾容忍"的判定 |
| `v2_site_check()` / `v2_quote_span()` / `v2_resolve_locations()` | 位置校验与"引用原文→行号"解析 |
| `v2_localization()` | 定位状态、站点排序、主指针、证据等级 |
| `verify_saved_case_state()` | 全量重算校验（改动状态结构后必须加测试覆盖） |
| `native_transport.py` / `native_adapter.py` | SSE 传输、tool call、reasoning 透传；`ROOT_SCOUT_TRACE_DIR` 可落盘原始 reasoning |
| `localize_report.py` | 定位式报告渲染（只读 CaseState，不调模型） |
| `test_root_scout.py` 等 9 个测试文件 | 243 passed / 10 skipped |

---

## 5. 实测能力边界

### B 模式（100 题，2026-11-09 全量）

| 指标 | 数值 |
|---|---|
| 规模 | 100 题（50 bug + 50 control） |
| 成本 | 5,942,272 token、4,961s（2 并发）、单例均值 59,422 |
| bug 严格 / 宽松 | **49/50 / 50/50** |
| control 警报 | 2/50，且**两个都是真缺陷**（p060 浮点容差、p061 堆越界读） |
| `model_protocol_incomplete` | 1（p035，行区间差一行被拒） |
| 逐例 token vs 老基线 | 中位数 0.92×、p10 0.47×、p90 2.46×、max 4.49× |

### A 模式（10 题，2026-11-10，`validation/amode-2026-11-10`）

| 指标 | A 模式 | B 模式（同 10 题） |
|---|---:|---:|
| token | 784,236 | 821,008（−4.5%）|
| 账单 | 837,365 | 842,516（−0.6%）|
| 墙钟 | 1,457s | 1,421s（+2.6%）|
| 基线受信不符 | **10/10** | 0/10 |
| 行号可用 | 9/10 | 10/10 |

**结论：知道失败用例不会省钱，但把证据从"模型自建对照"升级为"受信不符"。**

### 定位精度与稳定性

- 要求"给行号"后：6 份里 3 份给整个函数（25/39/48 行）。
- 允许"引用原文"后：6 份全部收敛到**单行**。
- **但复跑会漂**：p091 从"第 78 行"漂到"38–84 行"，p095 775→782，p100 409→404–405。
  → **在拿到同题多次采样的分布前，不能对外承诺单行。**

### 成本结构（关键）

- 计价：未命中输入 ×1、命中输入 ×0.02、输出 ×4。
- 首轮推理占输出 93–96%（p095 单次 60,180 字符推理 / 0 字符可见）。
- 推理内容是**重建标准解 + 对样例验算 + 映射回代码**，段落零重复，结尾都落到具体探针——**这是产品价值本身，不是浪费**。

---

## 6. 已知问题（按优先级）

| # | 问题 | 证据 | 影响 |
|---|---|---|---|
| 1 | **位置漂移**：同题复跑指到不同行 | p091 78 → 38–84 | 无法承诺单行定位 |
| 2 | **受信不符被判成"未决"** | `matched is False` → `oracle_unresolved` | A 模式 8/10 落在这个名字上，语义反了 |
| 3 | **被拒动作没有认领通道** | p035/p025 的 `run_variant` 行区间差一行；p049/p095 的 A0007/A0001 | 一次单点错误直接毁掉整条结论 |
| 4 | kind 标签抖动 | p037 硬反例写成 `unclassified`；p089 写成 `resource_concern` | 严格口径对标签过敏 |
| 5 | "真 UB 但不改输出"无维度 | p061 | 只能记成 control 警报 |
| 6 | 题面自带测试全通过 | 100 题 `mismatches=0` | B 模式下没有可复现的失败锚点 |

---

## 7. 数据索引（`validation/`，不进 git）

| 目录 | 内容 |
|---|---|
| `codecontests/balanced-100-v1/` | 冻结数据集：`inputs/`（100 份）、`evaluation/`（50 组，含 private 测试）、`manifest.json` |
| `bench-100-2026-11-08/` | 老基线全量（6.0M token） |
| `bench-100-full-2026-11-09/` | **当前 B 模式基线全量**，含 `analyze.py` |
| `bench-100-linecheck-2026-11-09/` | 6 题：只要求行号 |
| `bench-100-quotecheck-2026-11-09/` | 6 题：允许引用原文（3 份复跑）|
| `amode-2026-11-10/` | **A 模式实验**（10 题 + 真实失败用例）|
| `trace-reasoning-2026-11-10/` | 带 `ROOT_SCOUT_TRACE_DIR` 的 3 题，`trace/*/native/*/assistant.json` 里有完整 reasoning |
| `RETROSPECTIVE-2026-11-10.md` | 定位改造全过程复盘 |
| 其余 ~140 个目录 | 历史实验，与当前形态无关，不要当基线用 |

`snapshots/best-2026-11-09/` 是产出 100 题数字的那一版引擎快照（**未跟随后续改动**）。

---

## 8. 运行手册

```powershell
# 单题诊断
python root_scout.py diagnose validation\codecontests\balanced-100-v1\inputs\b100-p035.json `
       --out <输出目录> --engine v2 --strategy causal

# 重放已保存的状态（不调模型）
python root_scout.py replay <同一请求> --out <输出目录> --engine v2

# 重渲染定位报告（只读 CaseState）
python localize_report.py b100-p035 b100-p076
```

必需环境变量（缺一不可）：

```text
OPENAI_MODEL=deepseek-v4.1-flash
OPENAI_BASE_URL=http://127.0.0.1:8788/v1
OPENAI_REASONING_EFFORT=low
ROOT_SCOUT_REASONING_POLICY=fixed
ROOT_SCOUT_OUTPUT_POLICY=adaptive
ROOT_SCOUT_MODEL_OUTPUT_LIMIT=128000
ROOT_SCOUT_MAX_WALL_SECONDS=600
ROOT_SCOUT_EXEC_WORKERS=1
ROOT_SCOUT_IMAGE=sha256:5105aa6bf38d3517dc4a0c6d52ac1904bf957761fc79c6f394fab7eda650837e
ROOT_SCOUT_SCHEMA_CLARIFY=1
ROOT_SCOUT_TRACE_DIR=<可选：落盘原始 reasoning 与请求>
```

**注意端点限制**：`reasoning_effort` 只支持 `low/high/max`，`minimal` 无效（实测按 low 处理）；`response_format json_schema strict` 与 tool `strict:true` 被接受但不执行。

---

## 9. 测试

```powershell
python -m pytest test_root_scout.py test_protocol_delivery.py test_delivery_boundaries.py `
  test_context_compaction.py test_evaluation.py test_focused_prompt.py test_repo_tasks.py `
  test_session_affinity_dsml.py test_system_reliability.py -q      # 243 passed / 10 skipped

$env:ROOT_SCOUT_INTEGRATION=1; python -m pytest test_sandbox_integration.py -q   # 需要 Docker
```

`test_evidence_probe.py` 依赖缺失的 `mcp_server.py`，**无法运行**，属于已知状态。

---

## 10. 决策记录（为什么是这样）

| 决定 | 理由 |
|---|---|
| 交付物从"缺陷诊断"改为"定位指针" | 用户是刷题者，需要"哪错了"，不是"审计报告" |
| 拒绝"交补丁让它一次通过" | 本数据集没有失败用例可验证；且用户要定位不要改代码 |
| 拒绝"首轮简短"这类提示词软约束 | 读过 reasoning：首轮是**解题推导**，砍它等于砍产品价值 |
| 删除"引用不比数行更贵" | 实测证伪：账单 +38.9%，`read_source` 5→5 没减少 |
| 澄清上限 1 → 2 | 25 次澄清里 19 次本是必填键缺失，第二次修复机会能救回来 |
| 收尾容忍残留格式问题 | 避免"分析已完成却因格式判失败" |
| 引用原文 → 控制器解析行号 | 模型已有源码文本，数行号需要额外一轮 |

---

## 11. 下一步（建议顺序）

1. **修 verdict 词汇**：受信不符 → `defect_reproduced`，别再说"未决"。改动小、语义明确。
2. **测位置稳定性**：同 2 题各 5 次采样，统计"指到的函数"是否一致（而不是行号）。这是能否承诺单行定位的前提。
3. **被拒动作认领通道**：第二次机会或显式 acknowledge，与 schema 澄清分开。
4. **卡片以反例为主语**：把"你的程序在 X 上输出 Y，正确 Z"放第一行。
5. 若要做 A 模式产品化：把"用户提供失败用例"变成一等输入，并按难度给出成本/时延预估。

---

## 12. 坑清单（踩过的）

- **单例方差 2–4×**：任何 <30% 的差异都不能当结论，包括我自己报过的"便宜 28.8%""贵 27.6%"。
- **token 不是账单**：输出 4 倍、命中输入 0.02 倍。曾出现过 token +27.6% 而账单 −11.7%。
- **改提示词 = 换基线**：system prompt 哈希变，缓存前缀和行为都变，与历史结果不可比。
- **`validation/` 不在 git 里**：所有实验数据只在本地磁盘；要版本化请放 `snapshots/`。
- **`logging`/编码**：PowerShell 控制台显示中文会乱码，写文件用 UTF-8，读文件用 `read` 工具。
- **`git commit -m` 带中文 here-string 会产生 BOM**：用多个 `-m` 参数，不要用 `Out-File` 写消息。
- **Docker 容器泄漏**：被中断的批次可能留容器，`docker ps` 定期清。
