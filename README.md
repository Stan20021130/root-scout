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

为控制报告体积，摘要最多展示 8 条反例和每个候选 8 条对照。截断字段有显式标记，完整内容在 case_state.json，未完整展示的输入不能直接当作可执行复现材料。哈希用于一致性检查，不是签名认证。

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

MCP 服务已废弃；请使用 CLI。仓库检查由 Docker 在只读仓库挂载中运行，不继承宿主机环境，使用无网络、非 root 的容器。测试所需依赖必须已安装在镜像内；需要向仓库写入的检查不能在该模式下运行。

## 预算与适用范围

默认每题最多 4 轮探索，每轮最多 8 个动作，最多 64 个程序执行单元。另有有界协议澄清和只做结论审查的调用；它们共享整题预算。one_shot 只做首轮动作。默认墙钟预算 180 秒，可用 ROOT_SCOUT_MAX_WALL_SECONDS 设置 30–3600 秒。Docker 清理和网络读取可能产生少量余量。

默认没有整题 Token 硬上限；需要硬约束时运行 CLI 并设置 ROOT_SCOUT_MAX_TOKENS。run-local.ps1 会主动清除此变量，启用现有本地 adaptive 策略。模型调用会产生服务商费用。原生多轮工具传输可能在内存中保留并向服务商回传推理内容，默认不将原始传输轨迹写入磁盘。显式设置 `ROOT_SCOUT_TRACE_DIR` 会保存请求、响应、源码上下文和服务返回的推理内容，应仅在需要调试时启用。

内建 Oracle 仅覆盖经过显式调用方契约审定的五类题型：range_sum.v1、range_min.v1、max.v1、increasing_xor.v1、two_sorts.v1。超出已配置工具域时保持未知。当前没有通用可信 Oracle、反例最小性证明、独立语义裁决、完整根因集证明或通用性能认证。

legacy 引擎和旧 CaseState 重放继续保留，CLI 默认使用 v2。历史数据集、实验成本和旧机制细节见 [README_LEGACY.md](README_LEGACY.md)，不作为本版本质量承诺。

## 验证

~~~powershell
python -m unittest -q
$env:ROOT_SCOUT_INTEGRATION = '1'
python -m unittest test_evidence_probe test_sandbox_integration test_protocol_delivery.BashProgressTests test_system_reliability.BashWorkspaceTests test_delivery_boundaries.RepoDockerBoundaryTests -q
~~~

外部历史实验夹具未安装时，对应检查会明确跳过；废弃 MCP 服务的历史检查也会跳过。检查覆盖可信来源、原始行锚点、干预混杂、回归、报告截断、异常保留和原件不变。Docker 集成验证使用固定的模型响应夹具，不调用付费模型，不能代替真实模型诊断质量评测。
