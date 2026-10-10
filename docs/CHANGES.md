# 修改说明（改了什么 / 为什么改 / 保留了什么 / 破坏了什么）

对应审计报告：[`AUDIT.md`](AUDIT.md)。对应架构：[`ARCHITECTURE.md`](ARCHITECTURE.md)。
验收结果：[`ACCEPTANCE.md`](ACCEPTANCE.md)。

---

## A. 新增文件（完整清单）

### 核心基础设施
| 文件 | 职责 |
|---|---|
| `scripts/schema.sql` | Canon DB 的**唯一** DDL 源（25 张表 + 索引 + 外键） |
| `scripts/canon_db.py` | SQLite 访问层：实体/状态/事件/伏笔/线程/知识/物品/快照/门禁/事务登记；`apply_delta`；`replay`/`restore_to_chapter`/`verify_replay`；逻辑投影定义 |
| `scripts/tx.py` | 单章提交的文件系统事务（staging → journal → 原子 rename）+ 崩溃恢复 + 残留检测 |
| `scripts/character_state.py` | 人物状态机（9 字段、不可逆迁移防护、BEFORE→AFTER 变更日志） |
| `scripts/foreshadow.py` | 伏笔生命周期（6 状态、合法迁移、回收窗口、逾期、相关度评分、泄露检测） |

### 记忆与上下文
| 文件 | 职责 |
|---|---|
| `scripts/context_builder.py` | 四层记忆（L0/L1/L2/L3）与 query-driven 上下文组装、`canon_digest`、场景/事件语义重复检测 |
| `scripts/derived.py` | 从 Canon 重建全部派生视图（`bible.md`、`plot-ledger.md`、`summaries/`、`state/derived/*.json`） |

### 审计与规划
| 文件 | 职责 |
|---|---|
| `scripts/narrative_audit.py` | Level 1 文本卫生 + Level 2 语义一致性（人物/知识边界/时间线/地点/物品/关系/因果/伏笔生命周期/剧情重复） |
| `scripts/chapter_plan.py` | Chapter Plan / Scene Plan 骨架、schema 校验、plan-vs-delta 交付校验 |
| `scripts/extract.py` | Canon Delta schema + 校验器 + 规则提取器 + `human_summary` + 提取 Prompt |
| `scripts/llm.py` | LLM 边界（provider：none/rule/command/file），输出必须结构化且被校验 |
| `scripts/canon.py` | Canon Manager CLI：show/character/event/foreshadow/thread/knowledge/item/state/snapshot/compare/history/warnings/update/import-md/ddl/schema/verify-snapshots/rollback |
| `scripts/validate_state.py` | 全量状态校验（20+ 检查码，error 退出码 1） |

### 测试与文档
`tests/helpers.py`、`tests/fake_writer.py`、`tests/run_tests.py`、
`tests/test_core_infrastructure.py`、`tests/test_state_machines.py`、
`tests/test_audit_and_extract.py`、`tests/test_memory_context.py`、
`tests/test_workflow.py`、`tests/test_legacy_compat.py`、
`tests/test_integration_10_chapters.py`、
`docs/AUDIT.md`、`docs/ARCHITECTURE.md`、`docs/DDL.sql`、`docs/CLI.md`、
`docs/CHANGES.md`、`docs/ACCEPTANCE.md`、`docs/test-report.json`。

---

## B. 逐项修改说明（对齐任务书问题 1–24）

### 问题 1：四层记忆架构
**改前**：`next` 只做 `bible.md[:4000]`、台账最后 12 行、上一章末 1200 字；`outline.md` 从未被读取。
**改后**：`context_builder.py` 实现 L0/L1/L2/L3；`outline.md` 由 `_common.parse_outline` 解析为
卷目标/卷末不可逆变化/主要冲突，进入 L2，并在 `next` 输出 `volume_goal`、
`volume_irreversible_change`、`outline_participation`。
**为什么**：截断式读取在第 5 章与第 900 章给出同一段文本，必然遗忘早期设定。

### 问题 2：真正的动态上下文检索
**改后**：`next` 走 `当前任务 → 识别人物/地点/冲突 → 匹配相关伏笔 → 查询 Canon → 查询最近剧情 → 查询当前卷/Arc → 组装`。
输出 `context`（四层全量）、`entity`、`relevant_foreshadowing`（带 score+reasons）、`character_states`、
`canon_digest`。Canon 切片只含本章涉及实体；禁用实体外的行不会进入提示词。
**验证**：`test_layer3_returns_only_the_entities_in_scope`、
`test_digest_stays_the_same_size_as_the_book_grows`。

### 问题 3：Canon Snapshot
**改后**：每章提交写 `state/snapshots/ch-NNNN.json`（BEFORE / EVENTS / AFTER / 状态变化 / 知识 /
关系 / 物品 / 世界规则 / 伏笔 / 线程 / 场景 / 警告），并登记进 `snapshots` 表。
`canon.py snapshot|state|compare` 提供查看、重建、版本比较；
`canon.py verify-snapshots` 机器证明「快照能完整重建 canon.db」。
**快照自足性**：快照携带 touched 实体与完整变更字段，`replay` 不依赖实时库。

### 问题 4：彻底修改 commit 流程
**改前**：`copyfile → 追台账 → 写摘要 → 存 progress`，审计在提交之后。
**改后**：`commit` 按 `draft → prose validation(length) → semantic audit(hash 绑定) →
canon extraction → canon consistency validation(含 plan-vs-delta) → PASS → commit →
snapshot → index` 执行，任一失败即拒绝，draft 保留、给出具体原因。
**关键机制**：审计记录绑定草稿 `sha256`；`commit` 重算哈希并要求 L1、L2 都有通过记录。
审计后再动一个字，哈希不匹配 → 拒绝。
**验证**：`test_commit_without_a_plan_is_refused`、`test_commit_without_an_audit_is_refused`、
`test_audit_after_a_text_change_no_longer_authorises_the_commit`。

### 问题 5：commit 事务性
**改后**：`tx.py` 的 `CommitTransaction` 把章节文件、snapshot、派生视图、progress 全部
staging 到 `state/tx/<id>/`，写 journal，然后单个 SQLite 事务写 Canon，
提交后 `os.replace` 原子发布，最后清理。任何中断都由 `recover` 幂等补齐或丢弃。
**验证**：`test_interrupted_publish_is_completed_by_recover`、
`test_interrupted_prepare_is_discarded`、`test_recover_is_idempotent`、
`test_successful_commit_leaves_no_journal`。

### 问题 6：重新设计 round-done
**改前**：`rounds_completed += 1`。
**改后**：校验本轮计划/实际章数、每章 L1+L2 审计与 plan/audit/canon 门、未处理草稿、
parts 残留、半开事务、Canon 行与磁盘一致、每章 snapshot、章号连续、progress 一致、
`validate_state` 无 error；全部满足才 `ROUND_COMPLETE`，否则 `ROUND_BLOCKED`（退出码 3，
轮次保持 open，计数不变）。合法逃生通道是 `--abandon --reason`（记录原因、不计轮次、隔离草稿）。
**验证**：`test_round_done_blocks_when_planned_chapters_are_missing` 等 6 个用例。

### 问题 7：真正的语义审计
**改后**：Level 1（文本卫生）+ Level 2（语义一致性）。Level 2 确定性检查：
因果断裂、时间线倒退、死者行动、计划禁止泄露出现、机密事实无人知晓、地点跳变、
物品转移无交代、关系变化未刻画、伏笔非法迁移、场景/事件语义重复、计划状态变化缺失/偏离。
可选 LLM 语义审计只能**追加提示**，不能推翻确定性阻断，且输出必须符合 verdict schema。

### 问题 8：伏笔生命周期
**改后**：`PLANTED → ACTIVE → DEVELOPING → PAYOFF_READY → RESOLVED`（+ `STALE_LEAK`），
合法迁移表 + 计划回收区间 + 逾期判定 + 相关度评分 + 关键词泄露检测；
`next` 输出 `relevant_foreshadowing` 并在 L2 列出全部未回收伏笔。

### 问题 9：人物状态机
**改后**：9 字段状态 + 不可逆迁移防护 + 逐字段 BEFORE→AFTER 变更日志；
`next` 自动检索本章人物状态；`canon.py character 李禾` 查看全部历史。

### 问题 10：禁止润色器偷偷改变事实
**改后**：`polish_prose.py` 把 pass 分为 CANON-SAFE（同人分句合并、代词标签合并、
整句重复合并、同人标签池轮换）与 CANON-AFFECTING（称呼式台词、短应答、填充动作）。
后者默认**检测并拒绝**（`CANON_AFFECTING_EDIT`），`--strict-canon` 变成退出码 1，
`--allow-canon-edits` 才放行并且写 `.polish.json` 报告；由于 commit 校验文本哈希，
放行后必然强制重审。
**验证**：`PolishCanonSafetyTest` 的 4 个用例。

### 问题 11：fix_quotes 与文档一致
**改后**：文档与实现统一为「默认只报告（退出码 1 表示有待修复），`--apply` 才写盘」，
docstring 明确写出退出码契约；`README.md`、`SKILL.md`、`docs/CLI.md` 三处一致。
另外修掉了 `SKILL.md` 命令速查表里被错误的换行转义污染的行（原表把表格行粘成了一行，
字面残留了转义字符与竖线）。
**验证**：`FixQuotesContractTest`、`test_skill_md_has_no_corrupted_table_rows`、
`test_fix_quotes_is_documented_as_report_only_with_apply`。

### 问题 12：n-gram 只能作为辅助指标
**改后**：8-gram 自我重复与跨章重合率降级为 **advisory（提示）**，不再判失败；
只有「极端重复」（≥ 2× 长度自适应阈值，即明显的机器产物）才阻断。
同时新增语义级重复检测：`POSSIBLE_PLOT_REPETITION`（场景/事件的冲突+参与人+地点+语义相似度），
即使文字完全不同也会提示。
**验证**：`test_ngram_repetition_is_advisory_not_a_verdict`、
`test_extreme_repetition_still_blocks`、`test_semantic_plot_repetition_is_reported`。

### 问题 13：Chapter Planning
**改后**：`next → plan → scene-plan → write → audit`。
`plan` 从 Canon 生成确定性骨架（人物当前状态、必须推进的主支线、相关伏笔、禁止泄露、
时间线），写作者/agent 填叙事字段（目标、冲突、必须改变的状态、场景分解）；
`plan --check` 做 schema 校验，不通过则 `commit` 拒绝。
**验证**：`test_commit_without_a_plan_is_refused`、`test_dispatch...`（plan 相关用例）。

### 问题 14：状态变化而非单纯摘要
**改后**：`character_changes` 数组以 `{character, field, before, after}` 形式进入
Canon、快照与派生摘要（`summaries/ch-NNNN.md` 含 BEFORE→AFTER 表格）；
`human_summary()` 从结构化 Delta **反向生成**人读摘要。

### 问题 15：章节提交自动提取 Canon
**改后**：`chapter → LLM/规则 structured extraction → JSON Canon Delta → validator →
human-readable summary → database`。默认路径无需任何 `--summary/--place/--threads/--people`
（这些参数保留但标记 deprecated，仅作兼容）。Delta 来源优先级：
`--delta` > `state/plans/ch-NNNN.delta.json`（agent 写）> `--llm` > 规则提取。
**验证**：`RuleExtractorTest`、`DeltaValidationTest`。

### 问题 16：Canonical Truth 原则
**改后**：优先级 `章节正文 > 校验过的 Canon Delta > canon.db > 派生摘要 > Markdown 视图`。
`bible.md`、`plot-ledger.md`、`summaries/` 全部带 `AUTO-GENERATED from state/canon.db`
标记并在每次提交后重建；手写圣经在 `import-md` 时备份为 `state/bible.source.md`。
`commit`、`canon update`、`rollback`、`recover`、`migrate` 都会触发重建。

### 问题 17：允许人工修正 Canon
**改后**：`canon.py update --kind character|rule|foreshadow|thread|knowledge|item|relationship|alias|rename`
支持人工纠正，强制 `--reason`，写入 `canon_changes`（actor/time/before/after/reason）
并落成可重放的 manual snapshot；`canon.py history` 查询全部修改历史；
`canon.py rollback` 可撤销。

### 问题 18：完整状态校验
**改后**：`validate_state.py` 覆盖任务书列举的全部项，并额外检查
章节哈希一致性、审计哈希过期、快照缺失/孤儿、半开事务、轮次未完成任务。
**验证**：`ValidationTest` 11 个用例。

### 问题 19：完整测试
**改后**：172 个用例（169 通过、1 跳过、2 依赖最终文档），覆盖
state update / chapter commit / audit / canon extraction / canon validation /
timeline / foreshadow / knowledge / rollback，以及 10 章端到端集成。
`tests/run_tests.py` 输出 PASS/FAIL 计数并写 `docs/test-report.json`。

### 问题 20：失败恢复
**改后**：`next` 启动时自动 `recover`；`tx.pending_work` 回答「上一章是否已提交 / 是否有
draft / 是否有 staging / 是否有未完成审计 / 是否有半提交事务 / 是否需要 rollback / 是否需要继续」，
并把结论放进 `brief.recovery.resume`。`novel_state.py recover [--dry-run]` 可单独执行。

### 问题 21：目录结构
**改后**：新增 `drafts/`（含 `committed/`）、`parts/`、`state/{canon.db,snapshots,plans,audits,derived,tx,rolled-back}`、
`exports/`（旧项目若已有 `export/` 则继续复用）、`tests/`。兼容旧路径见 §C。
`init` 还会把技能自带的 `references/` 与 `tests/` 复制进项目，使小说项目自带
技法文档与可运行的测试套件（`python tests/run_tests.py`）。

### 问题 22：CLI 保持简单
**改后**：主流程仍是 `init / next / plan / audit / commit / round-done`；
新增 `auto-next` 自动完成 `plan → draft → audit → extract → commit`，
默认关闭（需显式 `--writer`），可观察（每步一行 JSON + `state/auto.log`）、
可中断（Ctrl+C → 退出码 130 并打印恢复命令）、可恢复（每章独立事务 + 轮次计划在库中）。

### 问题 23：不要万能超级 Prompt
**改后**：拆成 Planner / Writer / Extractor / Consistency Auditor / Style Auditor /
Canon Manager / Recovery Manager，各自的输入输出与 CLI 入口见 `ARCHITECTURE.md §8`；
`references/loop-prompts.md` 也按角色重写。

### 问题 24：设计原则落地
Context 是 Cache（`context_builder`）、Canon DB 是长期记忆、Chapter Text 是第一手事实、
Summary/Bible/Ledger 是派生视图、Commit 前必须 Audit、Canon 修改可追踪、
状态可恢复、模型不能自改 Canon、文学质量与文本卫生分离（阻断 vs 提示）、
写够字数 ≠ 完成（`round-done` 校验结构完整性）—— 全部有代码与测试对应。

---

## C. 保留的旧设计（渐进式重构，未推翻）

| 保留项 | 说明 |
|---|---|
| 「上下文是缓存，磁盘是内存」心法 | 升级为 Canon DB 的动机 |
| 章文件 `ch-%04d.txt` + 顺序提交（禁止跳章） | 确定性、可机械校验 |
| 三种字数口径 `no_ws/cjk/all` | 与中文平台口径一致 |
| `progress.json` 原子写（tmp + replace） | 扩展为完整 staging/atomic rename |
| 显式 `volume_plan` 优先于线性估算 | 承接旧稿唯一正确的分卷方式 |
| 字数区间 `chars_per_chapter_tolerance` | 不足重写 / 超标拆章 |
| 长章 `part` 分节追加 + `part_chars` + `round_char_budget` | 解决单次输出截断 |
| Level 1 全部检查项（禁用词/句式/段落/对话密度） | 原样保留为 Level 1 |
| 8-gram 算法（长度自适应阈值 + 剥离 Markdown） | 算法保留，结论等级降为提示 |
| `thin_tags` / `thin_pronoun_tags` / `check_seams` / `autodiversify` | 不改说话人或本就只读 |
| `fix_quotes` 的四种修复形态与 `looks_like_indirect` 防护 | 检测逻辑保留，只统一 CLI 契约 |
| `next` 的 JSON 既有字段 | 只增不减 |
| `--strict` 退出码语义 | 保持 0/1 |
| REINCARNATE / STOP 双态模型 | 判定条件由「+1」升级为全条件校验 |
| 纯标准库、无第三方依赖 | 任务书禁止引入额外服务 |
| `references/*.md` | 内容重写为按角色拆分的提示词 |

---

## D. 破坏点（兼容性冲突，按「正确流程与 Canon 一致性优先」裁决）

1. **`commit` 必须先 plan 且先 audit。**
   旧用法 `commit --chapter N --file draft --summary "..."` 现在会因缺少 Chapter Plan
   或缺少同哈希审计而被拒绝（退出码 2）。
   *迁移*：先 `plan --chapter N`，再 `audit --chapter N --file <draft>`，然后 `commit`。
   临时放行：`--allow-plan` / `--allow-audit`（会写入 `GATE_WAIVED` warning，并由
   `round-done` 汇总报告）。

2. **`round-done` 从「永远成功」变为「可阻断」。**
   非法状态返回 `ROUND_BLOCKED` 与**退出码 3**，`rounds_completed` 不再递增。
   旧脚本若只检查 stdout 会看到行为变化。
   *迁移*：把退出码 3 视为「需要修复」；确需放弃本轮用 `--abandon --reason "..."`。

3. **8-gram 重复从阻断降级为提示。**
   `NGROV_REPETITION` 不再出现在阻断项中（极端重复 `NGROV_REPETITION_EXTREME` 仍阻断）。
   旧项目里原本被卡的章节现在会通过；请用 `--legacy-json` 兼容旧解析，或读
   `advisories` 而不是 `issues`。

4. **`bible.md`、`plot-ledger.md`、`summaries/` 变成派生视图。**
   直接手工编辑这些文件会被下一次重建覆盖。
   *迁移*：手写设定放进 `bible.md` 后运行一次 `canon.py import-md`（原件备份为
   `state/bible.source.md`）；之后的修改用 `canon.py update`。
   为安全起见，若 `bible.md` 没有自动生成标记，`derived.py` **不会覆盖它**，
   而是写到 `state/derived/bible.md` 并提示运行 `import-md`。

5. **`next` 的 `bible_digest` 内容变了（键名保留）。**
   它现在是按本章实体生成的 `canon_digest`，不再是 `bible.md` 的前 4000 字符。
   新增键：`context`、`entities`、`character_states`、`relevant_foreshadowing`、
   `plan_required`、`plan_command`、`audit_command`、`delta_file`、`recovery`、
   `memory_layers`、`outline_participation`；另有 `action` 新增取值 `ROUND_DONE`。

6. **`polish_prose.py` 默认行为显著变保守。**
   称呼式台词/短应答/填充动作三个 pass 默认不再改写（会被报告为 `CANON_AFFECTING_EDIT`）。
   需要旧行为时加 `--allow-canon-edits`，并**必须重新 audit**。

7. **`commit` 成功后草稿被归档到 `drafts/committed/`。**
   依赖「提交后草稿仍在 `drafts/`」的脚本需要改路径。

8. **`export/` 目录名。**
   新项目使用 `exports/`；若项目已存在 `export/`，系统继续使用 `export/`，
   因此旧项目不会被拆分到两个目录。

9. **`scan_beats.py` / `strip_beats.py` / `scan_tags.py` 现在返回退出码 1 表示「发现问题」。**
   旧版 `scan_beats.py` 恒返回 0、`strip_beats.py` 无退出码语义。

10. **`--summary/--place/--threads/--people` 标记为 deprecated。**
    仍被接受但不再参与 Canon（摘要改由 Delta 派生）；`plot-ledger.md` 的列结构也由
    8 列派生表取代，手工追加的行会被下一次重建覆盖。

---

## E. 未做的事（诚实的边界）

* 语义判断本身仍依赖模型：`extract.py` 的规则提取器**不猜测**人物内心与因果，
  因此不提供 `--delta`/`--llm`/`--extractor` 时，Canon 只会得到实体、事件与时间线，
  人物状态变化会以 warning 形式列出等待确认（这是刻意的保守默认）。
* 没有实现多进程并行提交（提交必须串行，事务与轮次任务表未做行级并发控制）。
* `verify_replay` 比较的是**逻辑投影**（忽略时间戳与自增 id）；这不影响故事状态，
  但不要把它当作二进制级数据库比对。
* `scan_beats.py` 的性别启发式仍是人名表 + 上下文窗口，不是 NLU。
