# RootScout

**面向 AI 研发智能体（Coding Agent）与工程师的证据驱动型白盒质检探针**  
*Evidence-Driven Adversarial QA & Causal Diagnosis Probe*

RootScout 嵌入 Agent 开发流水线，负责构造合法域内的对抗测试、复现反例、执行隔离的因果对照，交付可追溯的诊断事实。修复由主 Agent 或工程师负责；探针不会覆写调用方源码。

具备两大维度的质检能力：
1. **单文件/算法代码**：通过 `diagnose` 和 `replay` 支持 Python 3 / C++17 / C++20 标准输入输出程序的对抗测试、反例复现和隔离对照；模型的病因解释和反例最小性需要另行验证。
2. **多文件仓库与 Git 变更**：`repo_tasks.py` 提供有限的符号、测试引用和 Diff 文本规则分析。结果是待复核线索，无法证明覆盖率、业务正确性或安全性。
控制器使用 Python 3.11+ 标准库。MCP 服务已废弃，目前使用 CLI 和 Python 模块。

## 工作流

1. 固定需求、原始源码、语言、可信测试及其来源。
2. 在明确的输入域内寻找边界、遮蔽和交互反例。
3. 在 Docker 中执行原版，用调用方审定的输出、题面样例或控制器 Oracle 比较。
4. 在临时派生版本上做局部干预，记录原版和变体的输出差异及回归。
5. 输出事实摘要和完整证据路径，交回调用方审查、修复、复测。

适用于隐藏边界缺陷、难以解释的测试失败和高风险逻辑复核，目前可通过 CLI 接入开发流水线。

## 证据与权限边界

| 项目 | 当前行为 |
|---|---|
| 原始源码 | 算法任务使用只读快照，保留 SHA-256 与原始行号；仓库检查只读挂载仓库 |
| 测试执行 | 仅 Docker；无网络、非特权用户、只读根目录、资源与输出限制；不可用即阻断 |
| 对抗脚本 | 仅在 Docker 临时工作区生成测试和计算观察值，不能把脚本自己的 expected 晋级为可信输出 |
| 可信预期 | 必须有调用方显式审定和来源、题面样例出处，或经过契约验证的内建 Oracle |
| 因果干预 | 只生成隔离的派生源码，返回对照事实，不应用生产补丁 |
| 行号定位 | 精确锚定原始版本的现存行；“该行存在”和“该行造成失败”分别表达 |
| 反例 | 真实比较失败才记为反例；未做缩减验证时标记 minimality=not_established |
| 语义确认 | 模型解释保持 unverified；单候选、单编辑对照也不自动证明自由文本因果解释 |
| 通过结论 | 仅表示已完成的有限测试没有观察到失败，不证明全域正确 |

Docker 缺失时不会退回宿主机执行。探针不提供修改仓库、应用补丁或提交代码的工具。

## 交付物

CLI 使用以下报告契约：

- **report.md**：面向人的定位卡，先展示可信反例，再列出明确标为未验证的模型线索及剩余缺口；无需额外模型调用。
- **probe.json**：主交付，包含状态、实际反例、关联诊断、原始行锚点、干预对照、验证缺口及审计材料路径。
- **case_state.json**：完整源码快照、模型可见响应、执行记录、变体及来源，可校验和重放。
- **result.json**：兼容旧候选视图，同时包含状态、停止原因和可信反例；没有真实反例关联的主张在这里与完整状态中保留，主报告只列其 ID。

主报告状态：

| status | 含义 |
|---|---|
| counterexample_found | 原版在合法、可信测试上成功执行，实际输出与可信预期不一致 |
| no_failure_observed | 当前完成的有限可信测试未观察到失败，且没有已记录的验证缺口 |
| inconclusive | 缺少可信比较、候选语义未验证、预算耗尽或其他分析缺口 |
| environment_blocked | Docker 沙箱不可用 |
| error | 控制器失败且没有优先报告的真实反例 |

失败发生后仍保留已经取得的反例；请同时检查 stop_reason、scope.gaps 和 protocol_status。findings 中的 description、trigger、location 是明确标注的模型主张；source_anchors 核对的是原件行号，causal_check 报告的是执行对照。

被工具协议拒绝的动作不会消失：账本保留提案与拒绝原因，候选与它的关联不产生任何证据。收尾前若有未修复的拒绝动作，控制器追加一次有界修复轮，把拒绝原因和声明的行区间回给模型，允许它按同一 schema 重提；重提成功时原记录标注已被替代执行，`scope.gaps` 用 `rejected_actions_replaced` 记明，判定不再因此变成协议失败；仍未修复时保持 `model_protocol_incomplete`。修复轮与 schema 澄清是两条通道，各自有独立上限。

为控制报告体积，摘要最多展示 8 条反例和每个候选 8 条对照。截断字段有显式标记，完整内容在 case_state.json，未完整展示的输入不能直接当作可执行复现材料。哈希用于一致性检查，不是签名认证。

`report.md` 默认显示首条受信反例和最多三条活动候选。没有受信反例时，候选仍会显示为待验证线索；模型描述、位置和触发条件不因此获得可信状态。报告列出的源码行只证明锚点存在，不能单独证明因果。

## 安装与运行

先安装并启动 Docker，再构建镜像：

~~~powershell
docker build -t root-scout:local .
python root_scout.py doctor
~~~

请求格式示例见 [examples/probe_max.json](examples/probe_max.json)。每条调用方测试都必须显式提供：

~~~json
{
  "input": "2\n-4 -2\n",
  "expected": "-2\n",
  "trusted": true,
  "expected_source": "人工核对：两个输入整数的最大值为 -2",
  "legal_input": true,
  "legal_input_source": "n=2，元素在需求给出的 [-100,100] 内"
}
~~~

没有可信预期的模型探针仍可执行，但只保存观察，matched 为 null。不得把模型编写的答案或暴力脚本未经审定地声明为 trusted。

如果评测器的输出比较规则已知，可在请求顶层明确声明并写明来源，例如：

~~~json
"output_comparison": {
  "kind": "whitespace_tokens",
  "source": "调用方核对：评测器按空白分隔的词比较"
}
~~~

支持 `exact_text`（原始文本）、`trimmed_text`（去除首尾空白）、`whitespace_tokens` 和 `case_insensitive_tokens`。未声明时，默认忽略整个输出的首尾空白；若剩余文本仅在内部空白或大小写上不同，结果保持未决，不把它当作已证实反例。对于允许多个合法答案、浮点容差或特殊评测器的题目，单一预期和这些简单比较规则不足以证明判题结果。历史 CaseState 仍按原来的记录规则校验；面向用户的结果会保守处理其中未声明规则的格式差异。

使用已配置的兼容 Chat Completions 服务诊断：

~~~powershell
$env:OPENAI_API_KEY = '...'
$env:OPENAI_MODEL = '服务实际提供的模型 ID'
$env:OPENAI_BASE_URL = 'https://api.openai.com/v1'
python root_scout.py diagnose examples/probe_max.json --out root-scout-report
~~~

现有本地代理设置保留在 run-local.ps1：

~~~powershell
.\run-local.ps1 diagnose examples/probe_max.json --out root-scout-report
~~~

可选的 `focused-v1` Prompt 保留相同的控制器、预算和证据规则，适合与默认 `baseline` 做对照：

~~~powershell
$env:ROOT_SCOUT_PROMPT_PROFILE = 'focused-v1'
.\run-local.ps1 diagnose examples/probe_max.json --out root-scout-focused
Remove-Item Env:ROOT_SCOUT_PROMPT_PROFILE
~~~

该选项仅影响 v2 探索和协议澄清提示，所选版本记录在 `case_state.json` 的 `run_configuration.prompt_profile`。候选版的质量与模型费用仍需实测，默认保持 `baseline`。

不调用模型，仅执行可信基线：

~~~powershell
python root_scout.py replay examples/probe_max.json --out root-scout-replay
~~~

新请求的 replay 只运行基线，stop_reason 为 replay_without_saved_actions，退出码为 1，表示诊断探索未执行；已观察到的反例仍进入 probe.json。保存的完整 CaseState 可用同一命令重放其动作：

~~~powershell
python root_scout.py replay root-scout-report/case_state.json --out replay-again
~~~

重放会重新执行已接受的动作，以及曾产生执行证据的阻断动作；其余被拒或阻断动作保留为带“未重执行”标记的历史记录，以保持后续证据编号。若重放产生的证据编号无法与原记录对齐，控制器会报告失败并保留本次基线，不会把两次运行的证据混在一起。重放不调用模型，历史模型调用单独保存在来源记录中，不计入本次用量。

MCP 服务已废弃；请使用 CLI。仓库检查由 Docker 在只读仓库挂载中运行，不继承宿主机环境，使用无网络、非 root 的容器。测试所需依赖必须已安装在镜像内；需要向仓库写入的检查不能在该模式下运行。

## 预算与适用范围

默认每题最多 4 轮探索，每轮最多 8 个动作，最多 64 个程序执行单元。另有有界协议澄清和只做结论审查的调用；它们共享整题预算。one_shot 只做首轮动作。默认墙钟预算 180 秒，可用 ROOT_SCOUT_MAX_WALL_SECONDS 设置 30–3600 秒。Docker 清理和网络读取可能产生少量余量。

默认没有整题 Token 硬上限；需要硬约束时运行 CLI 并设置 ROOT_SCOUT_MAX_TOKENS。run-local.ps1 会主动清除此变量，启用现有本地 adaptive 策略。模型调用会产生服务商费用。原生多轮工具传输可能在内存中保留并向服务商回传推理内容，默认不将原始传输轨迹写入磁盘。显式设置 `ROOT_SCOUT_TRACE_DIR` 会保存请求、响应、源码上下文和服务返回的推理内容，应仅在需要调试时启用。

例如，直接调用 CLI 时可为单题设置 100,000 Token 的预算准入上限；值只是示例，应按任务和服务商定价调整：

~~~powershell
$env:ROOT_SCOUT_MAX_TOKENS = '100000'
python root_scout.py diagnose examples/probe_max.json --out root-scout-report
Remove-Item Env:ROOT_SCOUT_MAX_TOKENS
~~~

预算不足时诊断可能提前停止并保留未决项；输入预留和服务商计费口径不同，这也不是金额账单上限。

设置整题 Token 上限时，当前 `native_delta` 适配器会转用普通的 Chat Completions 传输；其上下文与缓存行为可能不同于默认的无限额原生传输。对照不同预算时，应把这一变化一起记录。

只做首轮探索可显式使用 `--strategy one_shot`，适合先查看线索和已有反例：

~~~powershell
python root_scout.py diagnose examples/probe_max.json --strategy one_shot --out root-scout-one-shot
~~~

该模式不会继续根据首轮动作结果开展后续探索或结论复审；需要进一步验证时可使用默认的 `causal`。选择单轮可能省去调用，但不能保证诊断质量与多轮相同。

内建 Oracle 仅覆盖经过显式调用方契约审定的五类题型：range_sum.v1、range_min.v1、max.v1、increasing_xor.v1、two_sorts.v1。超出已配置工具域时保持未知。当前没有通用可信 Oracle、反例最小性证明、独立语义裁决、完整根因集证明或通用性能认证。

legacy 引擎和旧 CaseState 重放继续保留，CLI 默认使用 v2。历史数据集、实验成本和旧机制细节见 [README_LEGACY.md](README_LEGACY.md)，不作为本版本质量承诺。

## 离线质量与成本基线

已有结果可以不调用模型或重新执行程序，按冻结清单核对请求、CaseState、候选、位置锚点和用量：

~~~powershell
python baseline_audit.py `
  --manifest validation\codecontests\balanced-100-v1\manifest.json `
  --results validation\bench-100-full-2026-11-09\results `
  --out validation\bench-100-full-2026-11-09\audit-current.json
~~~

汇总保留清单和结果文件的哈希、缺失及校验失败数量；缺失状态和未知用量不会按零成本计算。默认成本指数使用未命中输入、命中输入、输出分别为 `1:0.02:4` 的历史权重，可通过 `--weights` 更改；它不是实际账单。候选数量和源码锚点可用性只是可复算的代理指标，不是根因或行号准确率；对照组也可能包含真实缺陷。该命令重新解读已保存的结果，不能证明当前代码或模型的新运行质量。历史数据保存在未纳入 Git 的 `validation/` 中。

本次离线复算的摘要见 [quality-baseline.json](snapshots/best-2026-11-09/quality-baseline.json)；完整明细需要保留对应的本地数据集和 CaseState。

仓库还提供 32 个人工构造、带缺陷行和触发输入标注的开发样本。可以先导出不含标注的诊断请求，再对已经产生的 v2 结果离线评分：

~~~powershell
python evaluation.py export-requests --out validation\v2-eval-requests
python root_scout.py diagnose validation\v2-eval-requests\max-init-python3.json --out validation\v2-eval-results\max-init-python3
python evaluation.py score-v2 --results validation\v2-eval-results --out validation\v2-location-score.json
~~~

导出和评分不调用模型；中间的 `diagnose` 会调用模型并产生费用。评分只统计首要候选的位置与标注行是否重合，以及是否关联完全相同的可信触发输入；其他有效反例可能被漏计。这些结果是定位代理指标，不能证明解释或根因在语义上正确，也不能代表真实题目的总体效果。未运行的样本保留在分母中；没有完成模型响应的结果单列为不可评分，不当作成功弃答。

2026-11-10 在这组样本上进行了受限预算的单轮与多轮对照；服务中途发生故障，20 组可比较，其余记录了失败或未运行。用量、局限和离线证据见[实验记录](experiments/2026-11-10-paired-v2/README.md)。

## 验证

~~~powershell
python -m pytest -q
$env:ROOT_SCOUT_INTEGRATION = '1'
python -m pytest -q
Remove-Item Env:ROOT_SCOUT_INTEGRATION
~~~

运行项目测试需要 pytest；默认排除 `validation/` 和 `demo_apps/` 等独立实验目录。部分测试依赖未纳入 Git 的 `validation/` 夹具；迁移仓库时需要一并保存。废弃 MCP 服务的历史检查会跳过。Docker 集成验证使用固定的模型响应夹具，不调用付费模型，不能代替真实模型诊断质量评测。
