# 历史实现说明（2026-11-01 Probe 改造前）

本文保留旧数据集与实验记录，不代表当前 MCP 的权限、报告或质量承诺。当前入口请阅读 [README.md](../README.md)；MCP 服务已废弃。

# root-scout（设计 2.0：候选—工具动作有界探索）

> 当前代码已于2026-10-31按用户要求精确回退至 `bdc54e3b00bfca5abd21e2218b8ea113fb02faf5288113e5c7256d7971865e39`。后续的派生版本空编辑复测、源码读取窗口反馈修复、SSE累计字节限制修复、编辑行号自动校正和自动补测均未叠加。语义记忆未启用。回退前完整文件和哈希保存在 `validation/bdc54e3b-full40-v1/before-rollback/`，回退记录为同目录上级的 `rollback-record.json`。本文按恢复后的行为更新，不是历史README的逐字恢复。

root-scout 用受控循环诊断单份 C++17/20 或 Python 3 算法提交。Python 3.11+ 标准库实现，无第三方包和 UI。默认 `diagnose` 使用 core v2：先保存可读候选，再独立验证工具动作；它不会把候选直接转换成旧版已确认 roots。scope6 因果引擎保留为显式 legacy。

> **数据集状态（2026-10-29）：RunBugRun 旧 `validation/external-expansion-v1/pilot/frozen` 和 `final/docker-qualified` 已标为 quarantined/not-ready。其测试池含未按题面域逐项核验的生成输入，不能作为合法基准使用。RunBugRun 样例子集 `validation/external-expansion-v1/final/sample-qualified-v2` 有39个 bug 请求和39个同测试 fixed 控制，覆盖19个题目组；20条开发 pilot 单列。所有入选测试与题面样例精确匹配并由 Docker 复验。另有 CodeContests 公共样例通过、私有测试失败的小型数据集 `validation/codecontests/sample-pass-private-fail-v1`：20个不同题目、20个独立参考解控制，所有请求仅含 public tests，private 失败证据与参考源码隔离。该数据集未运行模型诊断，也没有根因金标。两套数据都不是代表性质量评测集：RunBugRun 多缺陷审定数为0、根因金标未建立、性能未验证；其目标120+60+最多20尚未达到。详见各目录 `report.md`。**

## 当前主线：候选先行，动作逐项验证

`EXPLORE_V2` 使用一份 `V2_PROTOCOL` 对象同时生成 system 协议文本并驱动 action validator。可读候选先进入 `C_raw`，能无歧义保存的候选进入 `C_store`，用户报告列出 `C_deliver`；解析失败的完整模型可见响应仍写入账本。位置、触发和因果解释都是模型主张，尚未语义验证。`C_raw/C_store/C_deliver` 是接收追踪集合，不是语义召回率。

动作 `read_source`、`run_original` 和 `run_variant` 分开验证。运行与变体只通过 Docker 正式沙箱；变体对派生源码应用精确、有界编辑，不改原请求。模型不能提供 expected；请求中的可信测试或控制器重新计算的已配置 Oracle 才能提供 expected。没有可信 expected 的运行保留 stdout 观察且 `matched=null`。编译、运行、协议和预算失败都保留为状态，不能确认根因。core v2 暂不将任何候选自动投影为 legacy E2 root，资源性能和完整根因集仍未验证。



默认最多 4 轮、每轮最多 8 个动作、每次诊断最多 64 个正式执行单元；`one_shot` 在首轮工具动作后停止，`causal` 只在继续有用且总预算允许时反馈。模型要求继续探索且给出明确 `next_question` 时，即使当前没有动作也会继续到有界轮次上限。每轮提示都包含源码总行数和候选原位置；后续可用 `read_source` 按范围读取原始源码。JSON 解析失败时最多发起一次独立计费的局部协议澄清，不重试传输错误，也不重置 Token/墙钟预算；失败原文和用量账本均保留。Token 和墙钟预算不会按轮重置。完整状态可用 `verify_v2_case_state(state)` 或统一入口 `verify_saved_case_state(state)` 检查；哈希只检查内部一致性，不是签名认证。

## 安装与 Docker

需安装并运行 Docker Engine/Desktop。仓库提供可本机构建镜像：

```powershell
docker build -t root-scout:local .
python root_scout.py doctor
```

Doctor 检查 Docker daemon、镜像实际存在性、固定镜像 ID 和镜像内 g++/Python 版本，不会启动 Docker Desktop。当前 core v2 用正式沙箱执行基线与获准动作，不运行诊断配置。scope1–6 legacy 快照可能包含正式与诊断两类执行记录，并按创建时规则验证/重放。新动作若没有 caller expected 或可用控制器 Oracle，只记 stdout 观察，不把模型 expected 当证据；所有执行都使用正式配置。C++构建与运行分属独立容器阶段，build artifact 经隔离临时目录交接并按 (源码, 语言, 配置) 在一次诊断内复用，跨用例只编译一次；执行阶段用 `ROOT_SCOUT_EXEC_WORKERS`（默认 8，1 为串行）并行调度独立容器，报告同时给出实际 elapsed、并行倍数和累计墙钟，使用 Docker State/Error 判定工具状态，不使用提交 stderr 标记。容器使用非特权 UID、无网络、只读根文件系统、只读源码挂载、私有临时目录、512 MiB 内存、1 CPU、20 秒 CPU 时间、最多 64 个进程、最大单文件 64 MiB，以及 stdout 最多 64 KiB、stderr 最多 10 KB（合计最多 75,536 字节）。控制器按 180 秒请求调度预算检查剩余时间；单次执行受题目时限（默认 3 秒，最多 30 秒）限制，超时会移除容器。HTTP socket 读取及容器清理会带来少量余量，因此这不是严格的端到端墙钟中断保证。Docker/编译/程序错误单独记录，不会自动作为根因。Docker 不可用时状态为 `environment_blocked`。

当前固定使用镜像内 g++/Python 和文档列出的构建参数；请求里的 `compiler`、`compile_flags`、`memory_limit_mb` 会保留在快照，但不应用，报告会写明该假设。`time_limit_ms` 会采用并限制在 30 秒内。

## 请求格式

请求 JSON 需要 `statement`、`source` 和 `language`。可选 `environment`、`known_failing_tests`；每个测试须含 `input`、`expected`、`trusted: true`、`expected_source`、`legal_input: true` 和 `legal_input_source`。调用方必须为期望输出和输入合法性负责：模型生成的 expected 不会被当作可信 Oracle。系统也会从题面中紧邻“样例输入/样例输出”标题的围栏代码块抽取成对样例，并保存各自原文出处。模型定向探针没有可信 expected，只用于暴露行为，不能独立确认根因。示例：[examples/prefix_overflow.json](../examples/prefix_overflow.json)。

## 运行

当前本地服务配置：`http://127.0.0.1:8788/v1`，模型 `deepseek-v4.1-flash`，使用任意占位密钥。已保存到 `run-local.ps1`，在仓库目录执行：

```powershell
.\run-local.ps1 doctor
.\run-local.ps1 diagnose examples/prefix_overflow.json --out run
```

当前 `diagnose` 默认走 core v2；可用 `--strategy one_shot` 在首轮动作后停止：

```powershell
python root_scout.py diagnose examples/max_oracle_demo.json --strategy causal --out v2-causal-run
python root_scout.py diagnose examples/max_oracle_demo.json --strategy one_shot --out v2-one-shot-run
python root_scout.py diagnose examples/max_oracle_demo.json --engine legacy --strategy causal --out legacy-run
```

旧 scope1–6 的假设/实验/因果 finding 工作流只通过 `--engine legacy` 运行，历史 scope 快照会按原版本验证。旧的 `HYPOTHESIS_EXPERIMENT`、`CAUSAL_UPDATE`、root/finding 说明以下文仅描述兼容引擎，不代表默认 core v2 行为。

## Legacy 引擎历史说明

以下旧因果章节只描述 `--engine legacy` 和历史 scope1–6 快照。core v2 的报告与 `C_raw/C_store/C_deliver` 不能输入旧评分器后解释为根因质量分数；新候选追踪没有独立语义裁决，不构成语义召回率。

初始契约引用使用模型返回的原始 `ref` 命名空间；只有带题面精确引文的 `stated` 条款可绑定到控制器ID并用于假设/E2。caller tool contract 的精确题面锚点由控制器单独绑定为 `TOOL-semantics` / `TOOL-domain`；这类授权不是模型生成的statement事实。错误的 `stated` 引文逐条保留并标注拒绝原因，不会抛弃同一响应里其他有效条款，也不会授权根因；derived、assumed、unknown 和不存在的引用只作为上下文，且引用碰撞不会回退为控制器ID。反馈阶段才接受明确的控制器条款ID。编辑old_text若仅多出源码跨度外的空格或制表符，控制器会将其裁剪到跨度并在CaseState记录原始/规范化文本；非空白越界仍拒绝，原源码与题面引文不变。

因果范围 v5 要求显式 `kind`：`defect_candidate` 才进入 E2 根因候选；至少一条 stated/调用方工具契约引用授权，其他 derived/assumed/unknown 引用逐项作为未验证上下文保存。仅上下文不能授权根因。正向 `correctness_coverage` 记录原版可信测试的有限观测（通过、分歧、执行受阻或无证据），不会证明全域正确，也不能消除任何原版 E1；`resource_concern` 保持 `unverified`。实验的非目标上下文同样记录为未验证声明，不宣称已验证非干扰。缺失/未知kind按schema错误记账，不静默降级。

新版机制检查只识别少数源码改动类别（初始化、比较/更新、索引/边界、迭代次数和整数宽度），并核对可识别的机制描述别名是否与编辑类别一致。新 scope v5 按实际变化跨度和原行语法区域归类：完整 `for` 行与仅改相同字段的短 edit 应一致；循环计数/边界字段归为迭代次数，循环体比较仍按比较分类，带数组索引变量的左右端点偏移才归为索引映射。跨多个字段或无法判定的修改保持 `unknown`。它不验证模型自由文本 claim 的语义；报告保留原文供追溯并说明限制。同一 `single` 假设包含多种可识别改动时会被拒绝；多个相同类别的宽度编辑仍可作为一个机制，多个类别需分成假设或明确标为 `group`。对已启用的 max/range-min Oracle，字面初始化哨兵会按调用方声明值域及明确的 C++ 整数目标类型核对；方向/表示范围不匹配的补丁只保留输出变化证据，不投影成根因。数组元素初始化保留 singleton 补偿限制。无法由这些窄规则判断的改动保持未决，不把输出改善当作机制验证。联合机制只形成组支持；有限样例不会证明全域正确、独立根因集或E3。CaseState为旧评分接口派生`residual_status`和`known_explained_evidence_ids`：`complete`仅表示当前提出假设及已执行可信测试在有限因果账本内闭合，不表示执行过文字残余审计，也不证明全域完整；重放会重新计算并校验这些字段。scope v6 将原本通过的测试中“背景变体失败、完整目标变体通过”单独记录为背景/交互风险；它不把该控制测试计入根因支持，也不将背景声明为安全。完整目标变体若令原通过测试失败仍是 `regressed`。每个 finding/root 保留模型原始 claim，并标 `unverified_model_text`；编辑类别检查不验证 claim 的语义。旧 scope v1-v5 快照继续按创建时的派生与验证规则重算，scope v6 才采用上述风险区分。

`one_shot` 执行首轮实验后保留未决项并停止。`causal` 只在尚有未决假设/E1且存在已执行可信测试时反馈，最多两次 `CAUSAL_UPDATE`；反馈输入还包含控制器拒绝的实验 proposal ref 和具体原因，便于修正漏掉的 target/background edit；无新可区分实验就停止。每轮累计使用同一配置的墙钟、Token与最多64个实验执行单元，不重置预算。报告按stated契约列出关联假设状态，也显示可信测试数量和匹配、资源状态（始终 `unverified`）及未决覆盖；“已支持”只指当前提出假设和已执行有限测试。初始化若读取 `values[0]` 可能补偿索引/迭代缺陷；在精确值域和类型允许时，提示优先使用可表示的域外哨兵，否则保留交互为未决。该提示不自动补丁，也不放宽每个 background H 必须有 edit 的门控。调用账本包含各次实际请求、prompt和system的SHA-256，便于比较A/B首包。新CaseState用v5完整性字段重算假设、实验、证据矩阵与finding；旧快照仍按原版本验证并走legacy重放路径。

启动脚本向现有 CLI 传递全部参数；API 设置仅作用于运行它的进程环境，不修改系统环境变量。本地可设 `OPENAI_REASONING_EFFORT=low`。没有设置 `ROOT_SCOUT_MAX_TOKENS` 时，默认不启用整题Token硬上限，CaseState记录实际确认用量、未知用量和相对30,000 Token软目标的确认用量超额；软目标不停止调用。此时直接CLI和 `run-local.ps1` 默认使用adaptive v2单次输出cap；若明确设置 `ROOT_SCOUT_MAX_TOKENS`，默认恢复fixed单次阶段cap（v2探索默认20,000）及旧的完整UTF-8输入、最大输出和1,500报告余量准入。`run-local.ps1` 会清理继承的整题/单次Token cap并启用adaptive，避免遗留环境变量重新限制本轮设置；本地DeepSeek代理的单次cap使用其已广告的128,000 ceiling。若需旧的整题硬预算兼容行为，可直接运行CLI并设置 `ROOT_SCOUT_MAX_TOKENS`，例如30,000；`run-local.ps1` 会有意清除此硬预算变量。服务端最终usage达到12,000只作为观察阈值；unknown usage仍独立记账，不当作0或已确认。wall默认180秒，可单独设上限；此token策略不放宽墙钟、轮数、沙箱和工具限制。

`ROOT_SCOUT_OUTPUT_POLICY=adaptive`改变 v2 两个探索阶段的单次补全上限：单次上限不超过针对当前API模型显式配置的 `ROOT_SCOUT_MODEL_OUTPUT_LIMIT`、显式阶段/全局单次cap和调用方 `max_output`。本地代理 `/v1/models` 对 `deepseek-v4.1-flash` 广告128,000，但未在该上限实测成功生成；通用core不为其他model猜测provider ceiling。没有整题硬预算时adaptive不按剩余总预算、UTF-8请求字节或1,500报告预留缩减单次cap；显式设置 `ROOT_SCOUT_MAX_TOKENS` 时保留旧的完整UTF-8和报告预留准入逻辑。没有整题硬预算时adaptive也是直接CLI的默认v2策略；run-local另外清理继承的global/phase固定cap。服务端不会在同一流内动态修改已发送的cap，程序不读取隐藏推理，也不因截断自动重试。`OPENAI_REASONING_EFFORT`保持调用方设置，run-local为low。`ROOT_SCOUT_MAX_WALL_SECONDS`默认180秒，可设30–3600秒；墙钟deadline独立约束模型流读取与沙箱调用。

可选 `ROOT_SCOUT_REASONING_POLICY=none_low` 只对 v2 `causal` 生效：第1轮探索请求使用 `none`，随后强制安排一次完整源码的 `low` 独立复核；澄清请求使用 `none`，之后的探索请求使用 `low`。即使第1轮要求停止或没有候选，也会在原有最多4轮、同一Token和墙钟预算内尝试这次复核；预算不足会记录未复核并停止。`one_shot` 不增加调用，继续使用 `OPENAI_REASONING_EFFORT` 设置。此配置按阶段固定effort，不做难度判断，不会在同一响应流里调节推理，也不承诺节省Token或提高召回。模型输出仍是未验证候选；服务端报告的 reasoning token 会照常记账，程序不会获取隐藏思维文本。DeepSeek v4.1 Flash 经本地代理请求 `none` 时同时发送 `thinking.type=disabled`；其他模型不发送该附加字段。默认策略仍为 `fixed`。

```powershell
# 真实模型：三个变量需由调用环境预先设置；model 必须是服务商实际支持的 API ID
$env:OPENAI_API_KEY = '...'
$env:OPENAI_MODEL = '...'
# 可选，兼容 OpenAI Chat Completions 的服务地址根路径
$env:OPENAI_BASE_URL = 'https://api.openai.com/v1'
python root_scout.py diagnose examples/prefix_overflow.json --out run

# 离线重放：运行请求中的可信用例/题面样例，不调用模型
python root_scout.py replay examples/prefix_overflow.json --out replay
# 也可把已保存 CaseState 作为重放输入；会检查原始/证据哈希和镜像 ID
python root_scout.py replay run/case_state.json --out replay-again
```

API Key 只进入 Authorization 请求头，不写入日志或报告。未配置模型时仍保存请求、运行可信基线并记录模型配置错误。调用前按完整请求UTF-8字节数保守预留输入、按固定阶段cap或adaptive分配cap预留最大输出，并保留1,500 Token报告预算；真实分词与字节长度并不等价，因此预算预留是调度估算。账本分别记录服务端确认用量、未知部分的保留额度、`usage_complete`、服务端 reasoning usage（如有）、请求/输出上限及墙钟。v2流式账本记录可见增量、已收字节/事件、完成标记和服务端 reasoning token计数，不索取或保存隐藏推理文本；legacy仍使用原非流式路径。未收到 `[DONE]`、正常 `stop`，或服务端以 `length` 结束时，响应保留在账本但不会解析/执行JSON动作；缺失usage时按未知额度计入。连接受配置的剩余墙钟约束；此历史版本将累计原始SSE传输限制为16 MiB；超限会保留失败和未确认用量，不自动重试。非可见推理按事件处理后丢弃。预算准入拒绝不把预留量冒充实际服务端用量。实现使用OpenAI Chat Completions JSON object、`max_completion_tokens`与SSE，需服务商支持；API model必须显式配置，不能把Codex内部模型名当作API ID。

Legacy system schema约束 `HYPOTHESIS_EXPERIMENT` 与 `CAUSAL_UPDATE`。core v2 使用 `EXPLORE_V2`，候选与工具动作协议由 `V2_PROTOCOL` 统一驱动提示与 action validator；两套协议不混用。v1-v6 CaseState 按各自旧规则验证；旧 schema 测试不能代表新协议的真实模型质量。


core v2 每次运行默认写入 `result.json` 和完整的 `case_state.json`。轻量结果只有顶层 `findings`；每条 finding 仅含 `location`、`description`、`trigger`、`evidence`、`status` 五项。`description` 使用当前候选文本；新协议候选的 `evidence` 只列模型明确引用且在该轮可见的真实控制器证据 ID，未知/未来 ID 会在 CaseState 记为拒绝。工具动作关联单独保存在 CaseState；联合动作不等于每个候选的独立证据。历史 schema 1/2 结果仍按旧动作关联导出。`status` 是 `{id, kind, lifecycle, verification, merged_into?}` 对象，`verification` 保持 `unverified`，不会自动确认根因。原始可见模型响应、工具输出、请求/测试来源、引用决定、哈希、镜像/工具版本与调用账本仍保存在 `case_state.json`。当前写入的 `protocol_schema_version=3`、`case_state_schema_version=2` 承载显式引用和修订；旧 protocol schema 1/2 快照继续按原规则验证和重放，旧快照重放保留其来源协议版本。显式 legacy 运行和读取旧 legacy 快照继续写 `report.md`；`markdown()` 接口保留给旧验证脚本使用。

`result.json` 的五字段格式没有运行状态字段，因此 `findings: []` 本身不能区分 clean、未决或技术失败。请同时检查 `case_state.json` 的 `stop_reason`；CLI 技术失败以非零退出码结束，不会伪造 finding 表示错误。

执行中写原子更新的 `case_state.inprogress.json`；中断时保留现场。重放验证 request 中测试与执行证据的对应关系，并递归检查日志哈希；哈希只用于发现内部不一致，不提供认证或防篡改保证。没有完整性元数据的旧 CaseState 会明确拒绝重放，请从原始 request JSON 重新运行。若镜像 ID 改变会报告环境不匹配。重放读取原文件并写入新输出目录。请勿公开含私有源码/题面的结果目录。legacy 停止原因包括 `completed_with_evidence`、`no_bug_found_within_scope`、`oracle_unresolved`、`environment_blocked`、`budget_exhausted` 和 `interrupted`；core v2 另记录模型协议不完整与原始 request replay 等状态。测试未发现问题不等于证明正确。

真实 Docker 集成检查可选运行（需已有 `root-scout:local` 镜像）：

```powershell
$env:ROOT_SCOUT_INTEGRATION = '1'
python -m unittest -v
```

自动模型 HTTP 协议测试使用本地 mock API。2026-10-27 另通过 `http://127.0.0.1:8788/v1` 的 `deepseek-v4.1-flash` 完成真实服务联调，契约、裁决和残余审计均成功返回。端点漏报调查后，补全演示题面并修复引文门控、源码定位和探针换行协议，最新结果在 `validation/endpoint-investigation-lines/`，已确认两个已知根因；初次联调记录保留在 `validation/deepseek-live-final/`。单个演示不能作为诊断质量或平均成本达标的结论。Sandbox集成验证覆盖 stdin、编译错误、超时清理、输出上限、非特权/只读/无网络和完整模型证据流程。性能及目标 OJ 判定尚未验收。


## 旧阶段流程（仅历史 CaseState 兼容）

旧v1-v6 CaseState保留创建时的验证与重放规则。core v2 不走固定phase流水线；旧路径的局部干预/残余能力仅用于显式 legacy 和历史快照兼容。C++正式构建使用 `g++ -O2`，Docker隔离要求见前文。历史诊断模式的状态不得与新候选报告直接混为同一质量口径。

以下提示与裁决规则只描述旧v1-v4兼容路径，不属于当前因果循环：旧阶段提示共用 `statement/source/numbered_source/language/environment` 前缀，旧 ADJUDICATE 与 RESIDUAL 的公共前缀延伸到 `evidence`。旧裁决与残余审计向模型发送包含测试输入、可信期望值、实际输出、诊断日志及来源标识的证据摘要；重复的容器 State 和阶段元数据保留在完整 CaseState 中，以减少模型输入预算。追加裁决要把证据并入已有根因时只返回 `{existing_root_id,evidence_ids}`，原描述由控制器沿用，新 E1 仍逐条过原 E2 门控。相同精确源码片段和契约但修复机制文字不一致、且没有明确既有 ID 的候选记为 `needs_disambiguation`，保留候选与关联 root，不自动新增或合并；同一行上的不同源码片段仍可独立裁决。

`root_decisions` 保存每个裁决候选的原始字段、接受/拒绝状态和原因，报告展示未晋级项及残余审计状态。模型探针优先使用 `input_lines` 行数组，由程序拼接实际换行，防止把字面量 `\\n` 误当成换行；旧 `input` 字符串仍可用，但不会自动反转义。生成输入尚未经过通用语义合法性验证，不能独立作为确认根因的证据。

通用 Oracle Builder、模糊测试、反例缩减、资源根因判定及正式盲测验收仍递延。局部干预仅支持对已确认 E2 根因提出单行 edit：目标行须在原根因锚点前后 2 行内，old_text 必须是唯一精确子串，edit 只作用于临时源码副本并只在 Docker 执行。提示要求初始化修复优先使用由题面值域和源码类型支持的安全哨兵，不读取查询端点或顺带改变访问范围；若模型仍提出混杂修复，矩阵不得据此单独确认根因。每轮至多纳入 4 个根因，最多 64 个 variant×test 正式执行单元；k=2 时运行 PA/PB/PAB，k=3 时运行 PABC 与各 leave-one-out，k=4 时先运行完整 PABCD 再运行最多 4 个 leave-one-out，不枚举全部组合。超过根因、patch 或 cell 上限的项目会明确列为未覆盖，部分累计版本不会冒充完整累计修复。变体单独回归仍保留风险；只有完整累计版、单版/leave-one-out 和原始 E1 对照满足条件时，才可能记录有限的 `interaction_confounded` 条件支持，绝不清除单版回归，也不升级为 E3。当完整累计版与去掉某根因补丁的版本都修复同一原始 E1 时，该根因标记为 `cross_root_confounded`，不计入 `known_explained`；独立 patch 支持仍未建立。确认至少两个 E2 根因时，干预前只触发一次完整性 ADJUDICATE 复核，不依赖证据是否重叠；记录根因数、共享 E1 和调用状态，并检查全部可信原始 E1 与初步假设。该复核会消耗一次 ADJUDICATE 调用；空响应不证明根因列表完整。编译/运行失败、回归或预算/覆盖缺口均不能反证原根因。残余新例会追加到现存矩阵额度内，分类为 known_explained / unexplained / blocked；变体独有失败只记为 variant risk。若健康矩阵已经支持至少一个根因、仍有未解释的原版 E1，控制器最多再调用一次 ADJUDICATE；新候选仍须通过原始 E2 门控，且不会开启第二轮补丁循环。所有支持仍限于有限测试，不证明全域正确或根因全局独立。CaseState 会重算补丁、派生源码、测试绑定和矩阵结果；重放会重新执行变体。无 intervention metadata 的旧快照仍可读取并按本次原始重跑重新检查根因。下节只实现三个固定题型的小型内建 Oracle，不提供任意题面推断能力。在线模型分析不等同于质量验收。设计文档中的 AllRootExact/RootRecall 95%、precision 98%、误报率 1% 与平均 20k Token 均未验收；当前原型不能作这些质量承诺。

## 最小阶段 2：受限 Oracle 与评测

当前内建工具支持五个显式调用方契约：`range_sum.v1`、`range_min.v1`、`max.v1`，以及本轮新增的 `increasing_xor.v1` 和 `two_sorts.v1`。请求必须提供 `oracle_tool_contract`，字段包括：

```json
{
  "kind": "max.v1",
  "version": 1,
  "caller_attested": true,
  "reviewed_by": "题目审定者",
  "statement_sha256": "对完整 statement UTF-8 内容计算的 SHA-256",
  "statement_quote": "Given n integers, output their maximum.",
  "semantics": "output the maximum of the n input integers",
  "input_format": "n; n integer values",
  "domain": {"n_min": 1, "n_max": 200000, "value_min": -1000000000, "value_max": 1000000000},
  "domain_quote": "题面中精确出现的规模和值域出处"
}
```

系统要求题面含固定语义锚点、完整题面哈希一致、语义/输入格式与实现匹配、调用方显式声明审定和值域出处。既有 range/max 工具仅在 `n<=8`、`q<=8` 的小规模可信域生成测试。新增 XOR 工具把 `n` 限制在1..8、元素限制在0..5000；Two Sorts 只处理 `n` 在1..1000。前者用小n子序列穷举并与递增状态DP交叉检查，输出比较只对该工具按空白分隔token比较；后者直接字典序排序并与十进制前缀DFS核对，另验题面手算样例。每个工具只生成至多8条初始和8条残余输入；离线自检覆盖更大的结构化小域，但不会把整套自检输入都送入Docker。输入格式错误、索引非法或超出声明/小规模域时返回 `UNKNOWN`，绝不产生可信 expected。

新增两种工具的请求仍须由调用方提供完整题面SHA和精确引文：XOR契约同时绑定题面中的 `n` 与元素值域引文，Two Sorts绑定排序语义与 `n` 范围。契约审定标记是调用方声明，不是Oracle自动证明题意；模型不得给出expected或Oracle代码。域外输入保持 `UNKNOWN`。旧快照中的历史Oracle实现哈希按Oracle种类限制在重放兼容清单中（仅旧 `range_sum.v1`、`max.v1`、`range_min.v1`）；不得把旧哈希用于新增工具。新证据始终记录当前实现哈希。

小型审定夹具索引见 [`validation/oracle-expansion-v1/reviewed-fixtures.json`](../validation/oracle-expansion-v1/reviewed-fixtures.json)。它包含一个有限域 max 初始化触发、两种新增Oracle的独立算法校验样例，以及034/083/042/033/124五题的AI辅助源码/触发审查与历史证据指针。历史模型主张不是根因金标；有限样例和工具自检也不证明根因集合穷尽或性能达标。 八例一次性模型探针的请求、逐题结果、预检和费用限制见 [`validation/oracle-expansion-v1/acceptance-v1/live/report.md`](../validation/oracle-expansion-v1/acceptance-v1/live/report.md)；该探针不是根因准确率评测。

Oracle 来源记为 `builtin_oracle:<kind>`。E2 门控和 CaseState 重放都会调用 validator 重新检查输入合法性、expected、契约绑定、Oracle 版本及实现文件哈希；不靠 `trusted: true` 字段授权。请求中的 `caller_attested` 是调用方声明，哈希只绑定题面版本，不能证明调用方声明真实或语义等价；结论依赖其审定。模型不能创建或修改 Oracle，也不能将自己生成的 expected 晋级为可信。

可离线演示（请求没有 `known_failing_tests`，会靠内建 max Oracle 发现全负数组上的初始化问题）：

```powershell
python root_scout.py diagnose examples/max_oracle_demo.json --provider replay --out max-oracle-replay
```

`replay` 不调用模型，只执行可信测试；因此会保留 E1 分歧而不做根因归因。要完成模型裁决，设置真实 API 环境后使用 `diagnose`。示例文件中的工具契约是调用方审定声明的演示，不应不加检查地复制到其他题目。

旧v1-v4快照中的 RESIDUAL 可以请求一个固定格式的 `{"action":"oracle_batch","kind":"..."}`，只用于历史路径兼容。新因果主线让实验提案带合法输入，由控制器统一调用同一 `oracle_tools.evaluate` 重新计算expected并入账，再对原版和变体执行比较；模型不会执行Oracle代码、提交源码只在Docker中执行。新快照重放会重新验证这些输入和变体证据。

仓库包含 32 份固定开发评测样本，含正确、单根因、双根因、三根因、四根因、掩盖/交互、缺失题意，以及 Python、C++17/20。其中 10 份是后加的未参与提示词调试的样本：新题型 `range_min.v1`（clean、单根因、双根因、三根因，含 Python 三根因），`range_sum.v1` 三根因（端点、整型宽度、查询循环丢行）与四根因（再加输出无分隔符；历史上用于暴露旧三根因干预上限，当前策略支持四根因），以及 `max.v1` 的 C++20 掩盖组合。标注保存在 `evaluation_samples.jsonl` 的 `expected_roots`，运行请求本身不包含金标准。每个根因记录冻结 ID、解释、源码行/表达式、合法触发输入和独立性说明。样本是小型开发夹具，不是双盲正式裁决集，也不能支持 95% 或 20k 目标结论。

旧 `evaluation.py` 根因指标面向 legacy `roots`；core v2 刻意不生成 confirmed roots，因此其结果不能用于 core v2 候选的语义质量打分。不要把它的空 roots 解释为模型没有发现候选。core v2 当前只交付 C_raw/C_store/C_deliver 的接收账本，未接入独立语义裁决评测。

后续提示会在源码不超过16,000字符时继续附上完整源码，同时保留已接受 `read_source` 请求的有界原始行号窗口；重叠读取去重合并，拒绝的读取不进入窗口。较长源码提示保留已读/编辑窗口、未撤回候选位置附近最多80行和上一轮明确问题，并标注这些窗口可能不完整，缺少定义时可按原始版本调用 `read_source`。这些位置仅用于导航，不是证据。

Legacy 评测运行器可选 Docker 回放（不调模型）或使用已配置模型；模型模式可能产生 API 费用：

```powershell
python evaluation.py run --provider replay --out evaluation-runs
python evaluation.py score --results evaluation-runs --out evaluation-score.json
```

评分包含 RootPrecision/RootRecall、AllRootExact、CleanFPR、未决覆盖、全数据集分母上的 Token/墙钟汇总和停止原因。AllRootExact 分母只含有标注缺陷的样本，Recall 分母含这些样本中的全部冻结根因；clean 另计 CleanFPR，题意缺失样本另计未决覆盖。缺失结果、工具失败、预算失败和弃答不会从已标注缺陷样本的质量分母中剔除。默认的 `rule_proxy_line_and_trigger` 只要求预测根因的原始行号和所引用的合法触发输入精确命中标注；它是可检查的代理匹配，不是通用语义等价判定。需要更严格判断时可提供人工裁决 JSONL：每行 `{case_id, reviewer, matches:[{predicted_root_id, frozen_root_id}]}`；人工空匹配是明确否定，不会回退自动规则。报告逐项记录 `match_method`。

评分另保留每例原始 `stop_reason`、执行失败列表，并给出有限可信测试范围的 `functional_scope_status`、协议状态、环境阻断状态与资源状态。旧快照诊断配置的 `program_failed` 仍记录为诊断失败，但不会单独推翻已完成的正式可信测试/干预证据。缺题意样本单列 `expected_abstention`。性能始终标为 `unverified`，除非后续加入独立性能评测；小规模样例通过不能说明复杂度满足评测机时限，也不等同 Accepted。分类汇总按样本类别给出 Token、墙钟、停止原因和功能范围状态。这里的自动代理评分不是官方质量评分。

`trusted_test_status` 与 raw stop 独立，基于证据账本给出 `no_failure_observed`、`mismatch_observed`、`execution_blocked` 或 `insufficient`，并包含可用/已执行数量和不匹配数。因此原始状态仍是 `oracle_unresolved` 的 clean 案例，也能如实显示全部四个正式可信测试已运行且未见不匹配；这只描述有限测试事实，不自动把功能范围升级为完成。`protocol_status` 区分 schema 校验、响应解析、传输/模型调用和控制器阶段错误，同时保留失败异常类型及安全分类；HTTP 失败不会被归为 schema 错误。

十题历史切片保留了当时的原始结果：总计 166,226 Token，平均 16,623 Token/题；四个 bug 样本平均约 24,892 Token/题。该批次包含协议漂移和重复根因等失败，因此既不能作为修复后质量结论，也不能用整体均值声称 bug 诊断普遍达到 20k Token 目标。修复后的真实模型复测由独立验收记录追加，实验文件与冻结 gold 保持原样。

## 自检

```powershell
python -m unittest -v
```






## Bash实验库：历史命令上下文减负

2026-10-31，当前实验库对至少隔一轮的长Bash命令采用按需读取：提示保留用途、状态、结果和原文哈希，完整命令可从只读 `/work/history/Axxxx/command.sh` 获取，stdin在同目录 `stdin.txt`。首轮、最近一轮、题面、源码选择、候选及观察结果保持原展示。净字节收益不足时回退原格式。完整CaseState保留原文。

149个历史请求离线回放中，后续user内容减少6.68%，全部HTTP请求体减少2.41%；这不是实测Token或推理节省。177项检查通过，并完成一次本地只读容器冒烟，无模型API调用。详见 [上下文改动报告](../validation/context-compaction-v1/report.md)。生产目录 `F:/AI-Tools/root-scout` 保持 `bdc54e3b`。

2026-10-31，进一步把提示中的稳定字段、观察和工具历史排到版本摘要、候选与轮次之前，提高相同前缀的可复用性。149个历史请求的JSON值和UTF-8字节总量不变；95个后续分析请求的相同user前缀字节合计增加16.89%，实际缓存命中与费用尚未在线验证。历史Bash完整批次已观察到54.98%的输入缓存命中率。详见 [缓存前缀报告](../validation/cache-prefix-v1/report.md)。

同日修正每题稳定的 `X-Conversation-Id`、DSML 命令/stdin 中竖线和空白丢失，以及命名动作数组重复读取。183项检查和一次本地Docker夹具通过。代理当前只有1个账号，固定会话没有已证实的当前缓存或费用收益，因此未发付费A/B请求。详见 [会话与DSML验证](../validation/session-affinity-dsml-v1/report.md)。
