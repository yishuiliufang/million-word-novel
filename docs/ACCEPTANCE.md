# 阶段 6 · 端到端验收报告

本报告由 [`tests/acceptance_run.py`](../tests/acceptance_run.py) 机械生成：
它用 **subprocess 调用真实 CLI**（不是进程内调用），逐条记录**确切命令**、**退出码**、
**产物是否存在**，并写入 [`acceptance-report.json`](acceptance-report.json)。

复现命令：

```bash
python tests/run_tests.py                 # 单元 + 集成测试，输出 docs/test-report.json
python tests/acceptance_run.py --chapters 6   # 端到端验收，输出 docs/acceptance-report.json
python tests/check_docs_cli.py            # 文档与 CLI 一致性
```

---

## 0. 结论

| 项目 | 结果 |
|---|---|
| 端到端验收 | **PASS**（77 / 77 检查项通过，0 失败） |
| 测试套件 | **PASS**（172 / 172，0 失败，0 跳过） |
| Python 语法 | **PASS**（`compileall` 覆盖 `scripts/` 下全部 23 个模块） |
| 模拟章节 | **10 章**完整流程（另有一次 6 章运行；要求 5~10 章） |
| Canon DB | **25 张表 / 225 字段 / 35 索引** |
| 回滚 | 通过（被撤销的章节隔离而非删除；回滚后状态校验与快照回放均通过） |
| audit → commit 顺序 | 通过（未审计提交被拒 exit 2；审计后改稿被拒 exit 2） |
| round-done 阻断 | 通过（非法状态 exit 3，`ROUND_BLOCKED`，轮次计数不变） |
| README / CLI 一致性 | 通过（文档中出现的每个子命令与脚本都真实存在） |
| 旧 CLI 兼容性 | 通过（`status` 旧键齐全、`plan --volume-plan`、`import`、三种字数口径、`count_words --per-chapter`） |
| 运行环境 | Windows + Python 3.12.14（无第三方依赖） |

---

## 1. 逐项验收证据

### 1.1 实际运行测试

| # | 确切命令 | 退出码 | 结果 |
|---|---|---|---|
| 1 | `python tests/run_tests.py` | 0 | 172 通过 / 172 总计（本机约 80s） |
| 2 | `python tests/check_docs_cli.py` | 0 | 16 个子命令、23 个脚本全部存在 |
| 3 | `python -c "compileall scripts/"` | 0 | 23 个模块全部通过语法检查 |

测试用例分布（详见 [`test-report.json`](test-report.json)）：

| 模块 | 覆盖 |
|---|---|
| `test_core_infrastructure.py` | Canon schema/索引、实体与别名、人物状态与变更日志、快照、回放一致性、事务发布、中断恢复（prepared/db_committed）、回滚、人工修正的可重放性 |
| `test_state_machines.py` | 人物状态机（不可逆迁移、列表合并、形状校验）、伏笔生命周期（合法/非法迁移、窗口、逾期、相关度、泄露）、知识、时间线、物品、关系、世界规则不可变 |
| `test_audit_and_extract.py` | Level 1 全部检查项与「n-gram 只能作为提示」、Level 2 全部语义检查、Canon Delta schema 校验、不确定项剔除、规则提取器不臆造状态、LLM 输出解析边界 |
| `test_memory_context.py` | 四层记忆齐备、L2 由 outline 供给、L3 只取本章实体、digest 尺寸稳定、相关伏笔评分、泄露风险、`next` 新键与旧键、派生视图跟随 Canon |
| `test_workflow.py` | plan 门、audit 门（哈希绑定与失效）、commit 顺序与 waiver、round-done 六种阻断与成功路径、abandon、recover、validate_state 11 项、auto-next 全流程 |
| `test_legacy_compat.py` | 旧子命令与旧 JSON 键、`part`、`plan --volume-plan`、`count_words`、`audit_quality` 退出码与旧 JSON 形状、`export --zip` 含 canon.db、`import`、`migrate`（schema 1 → 2）、`fix_quotes` 契约、润色器 Canon 保护、辅助脚本、文档一致性 |
| `test_integration_10_chapters.py` | **10 章**端到端：文件/行/快照、四道门、状态历史、时间线单调、因果链、伏笔完整生命周期、知识边界、校验 PASS、快照回放 PASS、派生视图、rollback 与导出 |

### 1.2 实际运行 5~10 个模拟章节的完整流程

`tests/acceptance_run.py` 第 [6] 步用真实 CLI 连续跑 6 章（`auto-next` 内部同样调用真实
`plan → write → audit → extract → commit`）：

```
python scripts/novel_state.py auto-next --root <项目> \
  --writer "<python> tests/fake_writer.py write" \
  --extractor "<python> tests/fake_writer.py extract" --max-chapters 6
→ 退出码 0；已提交 6 章，章节号连续 1..6，每章均有 canonical snapshot
```

`fake_writer.py` 是确定性 Writer/Extractor 桩，用来在**没有模型/网络**的条件下跑通
Agent 协议（brief JSON on stdin → prose on stdout；prompt on stdin → Canon Delta on stdout）。

### 1.3 检查数据库是否正常

```
python scripts/canon.py --root <项目> schema --json      → 退出码 0
```

| 项目 | 数值 |
|---|---|
| 表数量 | 25 |
| 字段总数 | 225 |
| 索引数量 | 35 |

表清单（验收脚本逐张断言存在）：
`arcs, audits, canon_changes, chapters, character_state_changes, character_states,
entities, events, foreshadowings, gates, items, knowledge, meta, plans, plot_threads,
relationships, round_runs, round_tasks, scene_index, snapshots, timeline, transactions,
volumes, warnings, world_rules`。

`python scripts/canon.py --root <项目> verify-snapshots` → 退出码 0，**PASS**：
回放全部章节快照后与实时库在 15 张逻辑表上完全一致（含实体、人物状态、状态变更日志、
事件、关系、伏笔、线程、世界规则、知识、物品、卷、Arc、时间线、章节行、场景索引）。

### 1.4 检查 rollback 是否正常

```
python scripts/novel_state.py rollback --root <项目> --to-chapter 4 --reason "验收：回滚测试"
→ 退出码 0
```

| 断言 | 结果 |
|---|---|
| 被撤销的章节文件被隔离而非删除 | PASS（`state/rolled-back/` 保留原文） |
| 回滚后 `validate_state.py` | PASS（0 error） |
| 回滚后 `canon.py verify-snapshots` | PASS |
| 章节行、时间线、快照同步收缩到 keep 章 | PASS |
| 保留章节的审计/计划/门状态/变更日志不丢失 | PASS（`copy_log_tables`） |
| 人工 Canon 修正可重放 | PASS（manual patch 快照） |

### 1.5 检查 audit → commit 顺序

| # | 确切命令 | 期望 | 实测 | 结论 |
|---|---|---|---|---|
| 1 | `novel_state.py commit --chapter 1 --file drafts/ch-0001.md`（未审计） | 拒绝 | **exit 2**，stderr 含 `audit` | PASS |
| 2 | 同上 + 断言 `chapters/ch-0001.txt` 不存在 | 立 | **不存在**（被拒的提交不留痕） | PASS |
| 3 | `novel_state.py audit --chapter 1 --file <draft> --strict` | 0 | exit 0 | PASS |
| 4 | 审计后向草稿追加一句，再 `commit` | 拒绝 | **exit 2**，stderr 含 `审计` | PASS |
| 5 | 还原草稿 → 重新 `audit` → `commit` | 0 | exit 0，`gates={plan:pass, audit:pass, canon:pass, length:pass}` | PASS |

机制说明：审计记录写入 `audits(chapter, level, sha256, pass)`；`commit` 用
`canonical_text()` 重新计算草稿哈希并要求 L1、L2 均有通过记录。任何改稿（包括润色、
换行风格、行尾空格）都会使旧审计失效，从而在代码层面杜绝「先 commit 后 audit」。

### 1.6 检查 round-done 能否阻止非法状态

```
python scripts/novel_state.py round-done --root <项目>
→ 退出码 3，verdict=ROUND_BLOCKED
  blockers=["本轮计划 3 章，实际提交 4 章；缺少 ch-0005, ch-0006"]
→ progress.json 的 rounds_completed 保持不变（1 → 1）
```

单元测试覆盖的阻断条件（`test_workflow.RoundDoneTest`）：

| 条件 | 期望 | 结果 |
|---|---|---|
| 本轮计划章节缺失 | exit 3 / `ROUND_BLOCKED` | PASS |
| 某章带 waiver（审计被放行） | exit 3，且 `waivers` 汇总 | PASS |
| 存在未处理草稿 | exit 3（blockers 含「草稿」） | PASS |
| 半开事务 / parts 残留 / 快照缺失 / 章节号不连续 / progress 不一致 | 由 `validate_state` 汇总为 blocker | PASS |
| 合法轮次 | exit 0 / `ROUND_COMPLETE`，`rounds_completed+1`，生成 `last-context.md` | PASS |
| `--abandon --reason` | exit 0 / `ROUND_ABANDONED`，不计轮次，草稿隔离 | PASS |

### 1.7 检查 README 和实际 CLI 是否一致

```
python tests/check_docs_cli.py → 退出码 0
  已核对子命令 16 个：audit auto-next commit extract import init migrate next part
                      plan recover rollback round-done scene-plan status validate
  已核对脚本 23 个：audit_quality autodiversify canon canon_db chapter_plan character_state
                    check_seams context_builder count_words derived export_novel extract
                    fix_quotes foreshadow llm narrative_audit novel_state polish_prose
                    scan_beats scan_tags strip_beats tx validate_state
```

同时机器断言：

* `docs/DDL.sql` 与 `scripts/schema.sql` **逐字节一致**（13817 字节）——文档不会与代码漂移。
* `SKILL.md` 命令速查表不再包含被污染的转义行；`fix_quotes.py` 的命令行在
  `README.md`、`SKILL.md`、`docs/CLI.md` 三处都标注了「默认只报告，`--apply` 才写盘」。

### 1.8 检查旧功能是否被破坏

| 旧功能 | 实测命令 | 退出码 | 结论 |
|---|---|---|---|
| `status --json` 旧键 | `novel_state.py status --json` | 0 | 13 个旧键全部仍在 |
| `next` 旧键 | `novel_state.py next` | 0 | `chapters_to_write/chars_tolerance/bible_digest/ledger_tail/previous_chapter_tail/hard_rules` 均在 |
| 分卷规划 | `novel_state.py plan --volume-plan 10,10 --chars-per-chapter 1500` | 0 | `mode=structure`，卷边界生效 |
| 承接旧稿 | `novel_state.py import --group ... --start-chapter N` | 0 | 导入后 `validate_state` 0 error（imported 章以 INFO 提示） |
| 长章分节 | `novel_state.py part --chapter N --file p1` / `--check` | 0 | 累计字数与 `ready_to_commit` 正确 |
| 字数口径 | `count_words.py --per-chapter --json` | 0 | `no_ws > cjk`、`all > no_ws` |
| 单章质检 | `audit_quality.py --chapter N --strict` | 0 / 1 | 通过=0，不合格=1（与旧版一致） |
| 旧 JSON 形状 | `audit_quality.py --legacy-json` | 0 | `reports[].issues` 仍存在 |
| 引号修补 | `fix_quotes.py --file <draft>` / `--apply` | 1 / 0 | 默认不写盘且报 1；`--apply` 写盘并返回 0 |
| 导出打包 | `export_novel.py --by-volume --zip` | 0 | `book.txt`、`vol-N.txt`、`STATS.md`、zip 均生成 |
| 旧版迁移 | `novel_state.py migrate --root <schema 1 项目>` | 0 | schema 升 2、bible 入库、每章补快照、`verify-snapshots` PASS |

**兼容性破坏点**（已在 [`CHANGES.md`](CHANGES.md) §D 完整列出，共 10 项）：
`commit` 必须先 plan 先 audit；`round-done` 可阻断并返回退出码 3；8-gram 降级为提示；
`bible.md`/`plot-ledger.md`/`summaries/` 变为派生视图；`next.bible_digest` 内容变化；
润色器默认保守；提交后草稿归档到 `drafts/committed/`；新项目导出目录为 `exports/`；
`scan_beats/strip_beats/scan_tags` 增加退出码语义；`--summary/--place/...` 标记 deprecated。

### 1.9 检查 Python 语法

```
python -c "import compileall; compileall.compile_dir('scripts', force=True)"
→ 退出码 0
```

`scripts/` 下 23 个 `.py` 全部编译通过（Python 3.12.14）；测试套件本身也全部通过导入，
说明没有隐藏的语法/导入问题。

### 1.10 检查所有测试结果

```
python tests/run_tests.py
→ 总用例 172 / 通过 172 / 失败 0 / 跳过 0 / 结论 PASS
```

机器可读明细： [`docs/test-report.json`](test-report.json)（逐用例状态与失败堆栈）。

---

## 2. 验收报告文件

| 文件 | 内容 |
|---|---|
| [`docs/acceptance-report.json`](acceptance-report.json) | 77 个检查项（命令、退出码、期望值、stdout 尾、产物断言） |
| [`docs/test-report.json`](test-report.json) | 172 个测试用例的状态与失败详情 |
| `docs/DDL.sql` | Canon DB 规范 DDL（与 `scripts/schema.sql` 一致） |

---

## 3. 复现步骤（评审者视角）

```bash
# 1. 语法
python -c "import compileall,sys; sys.exit(0 if compileall.compile_dir('scripts', quiet=1, force=True) else 1)"

# 2. 测试
python tests/run_tests.py

# 3. 端到端验收（真实 CLI，自动清理临时项目）
python tests/acceptance_run.py --chapters 6

# 想要保留验收项目以人工检查：
python tests/acceptance_run.py --chapters 10 --keep
```

两个脚本都以退出码表达结论：0 = PASS，非 0 = FAIL。
