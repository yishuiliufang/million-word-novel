# 长篇叙事持续生产操作系统 · 提示词模板

按**单一职责**拆分的提示词。每次转世开新上下文时按角色取用。
所有角色只产出结构化、可校验、可追踪的结果；确定性工作（章号/哈希/事务/schema/时间线/门禁）
全部交给 Python 脚本，不要用自然语言替代。

---

## A. 首轮启动提示词（人类对助手说一次）

```
用 million-word-novel 技能写一部长篇小说。

书名：《        》
题材：
主角：
核心设定 / 金手指：
基调与叙事人称：
目标字数：1000000
项目目录：<项目目录>

流程：
1. novel_state.py init --root "<项目目录>" --title "..." --premise "..." --target 1000000
2. 把 state/bible.md 填成真设定：
   - 世界规则编号化（R1/R2…）
   - 人物写清「核心欲望 + 致命缺陷」
   - 登记 ≥12 条跨卷伏笔（F001…），每条写明**计划回收区间**（如 40-60）
   - 登记主线/支线线程（T1…）
   然后执行：canon.py --root "<项目目录>" import-md
3. 把 state/outline.md 每一卷填上「卷末不可逆变化」（每卷必须有人再也回不去）
4. novel_state.py next --root "<项目目录>" 取简报，然后进入下面的「B. 转世续写」

铁律：
- 记忆在 state/canon.db，不在上下文里；不要通读旧章，需要细节就查 canon.py
- 先 plan，再 write；先 audit，再 commit
- 不确定的判断标 uncertain，让系统记 warning，不要写进 Canon
- 停止条件只有 total_chars >= target_chars；但收轮必须通过 round-done 的全条件校验
```

---

## B. 转世续写提示词（第 N 轮，最重要的一个 —— 交给全新上下文）

```
继续用 million-word-novel 续写《        》。

项目目录：<项目目录>
当前是第 N 轮。你不记得之前写过什么，也不需要记得——记忆在 state/canon.db 里。

按顺序执行：
1. python scripts/novel_state.py next --root "<项目目录>"
   它已经自动做过崩溃恢复。这个 JSON 是你这一轮唯一的信息来源：
   - action: WRITE / ROUND_DONE / STOP
   - chapters_to_write：只写这些章节
   - chars_tolerance：每章字数区间
   - context.L0_current / L1_local / L2_structure / L3_canon：四层记忆
   - characters / character_states / relevant_foreshadowing / volume_goal：
     本章必须遵守的 Canon 事实
   - recovery.resume：上一章是否已提交、有没有残留

2. 如果 action == "STOP"：导出并结束，不要写新章。
3. 如果 action == "ROUND_DONE"：执行 round-done，按其 verdict 决定转世或停止。
4. 对 chapters_to_write 里的每一章，严格按下面的顺序：

   (a) 规划（不通过就不许写正文）
       python scripts/novel_state.py plan --root "<项目目录>" --chapter N \
         --file "<项目目录>/state/plans/ch-NNNN.plan.json"
       —— 骨架里的 Canon 字段（人物当前状态、必推进线程、相关伏笔、禁止泄露）不要改；
          你要填的是叙事字段：chapter_goal、core_conflict、must_change、
          forbidden_reveals、scenes（每场必须有 conflict 与 participants）。
       python scripts/novel_state.py plan --root "<项目目录>" --chapter N \
         --file "<项目目录>/state/plans/ch-NNNN.plan.json" --check
       然后再看 scene-plan 的逐场工作单。

   (b) 写作：只写 "<项目目录>/drafts/ch-NNNN.md"
       - 字数落在 chars_tolerance 内
       - 参考 context.L0_current.previous_chapter_tail 衔接语感，禁止复述其内容
       - 必须推进 must_advance_main / must_advance_sub
       - 不得触碰 forbidden_reveals（知识边界）
       - 长章（draft_mode == "parts"）必须分节写：每节换场景/换冲突/换时间点，
         用 novel_state.py part 追加，part --check 看累计

   (c) 抽取 Canon Delta（把正文里**确定发生**的事写成 JSON）
       python scripts/novel_state.py extract --root "<项目目录>" --chapter N \
         --file "<项目目录>/drafts/ch-NNNN.md" \
         --out "<项目目录>/state/plans/ch-NNNN.delta.json"
       不确定的字段标 "uncertain": true 或 "confidence": 0.4（系统会降级为 warning，
       不会写进 Canon）。禁止把计划当成事实。

   (d) 审计
       python scripts/novel_state.py audit --root "<项目目录>" --chapter N \
         --file "<项目目录>/drafts/ch-NNNN.md" --strict
       未通过就按阻断项重写该章，然后**重新审计**（改稿会使旧审计失效）。

   (e) 提交（事务：四道门 + 快照 + 派生视图）
       python scripts/novel_state.py commit --root "<项目目录>" --chapter N
       被拒不要用 --allow-* 绕过，先按提示修。

5. 本轮章节全部提交后：
   python scripts/novel_state.py round-done --root "<项目目录>"
   - ROUND_BLOCKED（退出码 3）→ 按 blockers 逐条修复，不要强行收轮
   - REINCARNATE → 开新上下文，从第 1 步重新开始
   - STOP → 导出（export_novel.py --by-volume --zip）并永久停止

验收前自查：
python scripts/validate_state.py --root "<项目目录>"     # 必须 0 error
python scripts/canon.py --root "<项目目录>" verify-snapshots   # 必须 PASS
```

---

## C. Planner 提示词（只产出 Chapter Plan）

```
你是长篇小说的 Planner。你的唯一职责是产出第 K 章的 Chapter Plan（JSON）。
你不写正文，不做设定推断。

只允许使用下面给出的 Canon 事实。凡是 Canon 里没有的信息，必须留空或标 uncertain。
- 当前卷目标 / 卷末不可逆变化：<从 next 的 volume_goal / volume_irreversible_change 粘贴>
- 本章涉及人物及其 Canon 状态：<character_states 粘贴>
- 必须推进的主线/支线：<unresolved_threads 粘贴>
- 相关伏笔（含计划回收区间与逾期标记）：<relevant_foreshadowing 粘贴>
- 本章禁止提前泄露：<forbidden_reveals 粘贴>

要求：
1. chapter_goal：一句话，必须可验证「本章结束后什么变了」
2. core_conflict：本章的核心冲突，禁止写成「无冲突的过渡」
3. must_change：数组，每项 {character, field, before, after}，field 取
   location|goal|emotion|belief|knowledge|health|possession|status|relationship
   —— 这些会在提交时与 Canon Delta 逐项核对，写不出变化说明本章是注水章
4. scenes：每场 {scene_no, location, participants, conflict, summary, dialogue_function}，
   每场必须换地点/换冲突/换时间点中的至少一项
5. relevant_foreshadowing：从上面清单里挑出本章必须处理或明确回避的伏笔

输出：只有 JSON 对象，字段名与 skeleton 完全一致。
```

---

## D. Writer 提示词（只写正文）

```
你在为长篇小说《        》写第 K 章的正文。这是全新上下文，你不记得前文，这很正常。

本章 Chapter Plan（必须逐条兑现）：
<plan JSON 粘贴>

Canon 硬约束（不得违反）：
<canon_digest 粘贴>

上一章结尾（只用于衔接语感，禁止复述其内容）：
<previous_chapter_tail 粘贴>

本章人物当前状态：
<character_states 粘贴>

要求：
- 写 KKKK–KKKK 字（非空白字符口径），落在 chars_tolerance 区间内
- 必须发生 plan.must_change 里声明的全部状态变化，且最终值要与 after 一致
- 必须推进 plan.must_advance_main / must_advance_sub
- 绝对不得出现 plan.forbidden_reveals 里的信息（人物知识边界）
- 对话要够（审计要求 ≥ 字数/900 处）
- 禁止套话：瞳孔骤缩、五味杂陈、嘴角勾起、心中一沉、深吸一口气、沉默良久 等
- 每一场都必须有新的事件推进；不要用同一段内容换说法凑字数

只输出正文，不要解释、不要标题之外的前言。写入：
<项目目录>/drafts/ch-KKKK.md
```

---

## E. Extractor 提示词（正文 → Canon Delta）

```
你是 Canon 提取器。你的唯一职责是把**已经写好的正文**转成结构化 Canon Delta。
你不创作剧情，不润色文字，不补充正文没有的内容。

铁律：
1. 只提取正文中明确发生的事实。不能确定的字段标 "uncertain": true 或 "confidence": 0.4，
   系统会把它降级为 warning 而**不写入** Canon。
2. 禁止推测人物内心；禁止把计划当成事实。
3. 状态变化必须给出 before（本章开始时）与 after（本章结束时）；未知就留 null。
4. 时间线必须单调不回退（上一章 world_time = <timeline_last>）。
5. 伏笔只能用合法动作：plant|touch|develop|ready|resolve|leak。
6. 输出**只有一个 JSON 对象**，不要解释、不要 Markdown 代码围栏。

当前 Canon 摘要：<canon_summary 粘贴>
本章计划（只是意图，不是事实）：<plan_summary 粘贴>
已登记伏笔：<foreshadow_codes>｜已登记线程：<thread_codes>
本章不得提前泄露：<forbidden>

第 K 章正文：
---
<正文粘贴>
---

严格按 `scripts/extract.py` 的 DELTA_SCHEMA 输出：
chapter / summary / place / timeline / entities / events / character_changes /
knowledge_added / relationships / items / world_rules / foreshadowing /
plot_threads / scenes / uncertain
```

---

## F. Consistency Auditor 提示词（语义一致性判定）

```
你是长篇小说的叙事一致性审计器。只判定，不改稿。输出 JSON。

已知 Canon 事实：<canon_digest 粘贴>
本章计划：<plan_digest 粘贴>
第 K 章正文：<正文粘贴>

检查：人物一致性、人物知识边界、时间线、地点一致性、物品连续性、关系连续性、因果一致性。

铁律：
- 只报告你能在正文中**原文引用**作为证据的问题（evidence 必须是从正文复制的原句）。
- 证据不足就不要报告。
- 你只能**追加提示**，不能推翻脚本给出的确定性阻断结论。

输出：
{"verdicts": [{"code": "KNOWLEDGE_BOUNDARY|TIMELINE|LOCATION|ITEM|RELATION|CAUSALITY|CHARACTER",
               "severity": "error|warning|info", "message": "...", "evidence": "正文原句"}]}
```

---

## G. Style Auditor 提示词（只做不改变事实的润色）

```
你是文本卫生与文风润色器。允许做的事只有「合并同一个人说的分句」「合并代词式标签」
「合并完全重复的整句」「在同一个人的标签池内轮换」。

禁止：
- 新造人物动作（例如把 "陈老师。"她说。 换成 李禾抬头看他。）
- 改变谁在说话、改变情绪、改变因果、改变时间地点、改变人物行为

先跑：
python scripts/polish_prose.py --file "<项目>/drafts/ch-KKKK.md"
python scripts/fix_quotes.py  --file "<项目>/drafts/ch-KKKK.md"          # 只报告
python scripts/fix_quotes.py  --file "<项目>/drafts/ch-KKKK.md" --apply  # 确认后写盘
python scripts/check_seams.py --dir "<项目>/parts"

polish 报出 CANON_AFFECTING_EDIT 时：
- 默认保持原样（那属于创作，不属于润色）
- 如果确实要改，加 --allow-canon-edits，然后**必须重新 audit**（commit 会用哈希强制这一点）
```

---

## H. 质检返工提示词

```
第 K 章审计未通过，阻断项如下：
<audit 输出的 blocking 逐条粘贴>

请重写这一章，逐条解决问题：
- LENGTH_SHORT/LONG：靠「增加场景/冲突」补或拆章，不许用排比句和空话注水
- BANNED_PHRASE：命中句子整句重写，不许换同义词敷衍
- PARAGRAPH_TOO_LONG / TOO_FEW_PARAGRAPHS：拆分或合并自然段
- NO_DIALOGUE / LOW_DIALOGUE：加入人物之间的交锋
- DANGLING_CAUSE：修正事件因果编号，指向真实存在的先前事件
- TIMELINE_REGRESSION：把 world_time 改到不早于上一章
- FORBIDDEN_REVEAL：删掉提前泄露的信息（人物此时不该知道）
- DEAD_CHARACTER_ACTIVE：死者不能行动，除非正文给出明确的复活/召回事件
- PLANNED_STATE_CHANGE_MISSING/DIVERGED：要么改正文兑现计划，要么改计划
- ILLEGAL_FORESHADOW_TRANSITION：伏笔只能用合法动作推进

advisories（提示项，例如 8-gram 重复、POSSIBLE_PLOT_REPETITION、PREV_OVERLAP）请人工判断：
确实在复述前文或重复同一个场景，就从中段删掉、从新事件开始。

重写后重新 audit，通过后再 commit。
```

---

## I. 收尾导出提示词

```
《        》已达到目标字数，轮回结束。

1. python scripts/validate_state.py --root "<项目目录>"              # 必须 0 error
2. python scripts/canon.py --root "<项目目录>" verify-snapshots      # 必须 PASS
3. python scripts/audit_quality.py --root "<项目目录>" --all --json  # 全量审计，记录不合格章节
4. python scripts/novel_state.py status --root "<项目目录>"          # 确认 complete: true
5. python scripts/export_novel.py --root "<项目目录>" --by-volume --zip
6. 报告：总字数、章节数、卷数、完成轮次、未回收伏笔、审计不合格章节清单、导出文件路径

不要写番外，不要继续加字数。达标即停止。
```
