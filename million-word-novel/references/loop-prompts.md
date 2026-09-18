# 百万字长篇·轮回提示词模板

本文件提供可直接复制的提示词。**每次转世开新上下文时使用**。

---

## A. 首轮启动提示词

```
用 million-word-novel 技能写一部长篇小说。

书名：《        》
题材：
主角：
核心设定 / 金手指：
基调与叙事人称：
目标字数：1000000
项目目录：C:\work up\<书名>

流程：
1. init 建立项目
2. 把 state/bible.md 填成真设定（世界规则编号化，人物写清欲望+缺陷，规划 ≥12 条跨卷伏笔并写明计划回收章节）
3. 把 state/outline.md 每卷填上「卷末不可逆变化」
4. 跑 next 取简报，写本轮章节，一章一 commit
5. 每章跑 audit_quality.py --chapter N --strict，不合格就重写
6. 一轮结束跑 round-done
7. 若返回 REINCARNATE：写 state/last-context.md，开新上下文重新加载本技能，从第 4 步继续
   若返回 STOP：跑 export_novel.py --by-volume --zip，然后停止

铁律：
- 停止条件只有 total_chars >= target_chars
- 只读 ledger_tail 和 previous_chapter_tail，禁止通读旧章
- 每章提交后回写 bible.md
- 上下文用到 70% 就收轮转世，不许硬撑
```

---

## B. 转世续写提示词（第 N 轮，最重要的一个）

```
继续用 million-word-novel 技能续写《        》。

项目目录：C:\work up\<书名>
当前是第 N 轮。你不记得之前写过什么，也不需要记得——所有记忆都在 state/ 里。

按顺序执行：
1. python scripts/novel_state.py next --root "C:\work up\<书名>"
   这个 JSON 是你这一轮唯一的信息来源。
2. 如果 action == "STOP"，直接执行导出并结束，不要写新章。
3. 如果 action == "WRITE"：
   a. 只写 chapters_to_write 里列出的章节
   b. 每章字数落在 chars_tolerance 区间内
   c. 参考 previous_chapter_tail 衔接语感，但禁止复述其内容
   d. 遵守 bible_digest 里所有硬设定
   e. 一章写完立刻 commit，然后 audit --strict，不合格重写
   f. 有新设定/伏笔/人物变化 → 回写 state/bible.md
4. 本轮章节全部提交后：
   python scripts/novel_state.py round-done --root "C:\work up\<书名>"
5. verdict == "REINCARNATE" → 更新 state/last-context.md（未解悬念+下一步计划），本轮结束
   verdict == "STOP" → 导出并永久停止
```

---

## C. subagent 起草提示词（并行加速用）

```
你在为长篇小说《        》起草第 K 章。这是全新上下文，你不记得前文，这很正常。

硬设定（必须遵守）：
<bible_digest 原文粘贴>

上一章结尾（只用于衔接语感，禁止复述）：
<previous_chapter_tail 原文粘贴>

最近章节台账：
<ledger_tail 原文粘贴>

你的任务：
- 写出第 K 章，字数 KKKK–KKKK 字（非空白字符口径）
- 本章必须发生一件改变人物状态或局势的事
- 对话占比要够（审计要求 ≥ 字数/900 处）
- 禁止使用：瞳孔骤缩、五味杂陈、嘴角勾起、心中一沉、深吸一口气、沉默良久 等套话
- 不许与上一章有超过 12% 的 8 字重复片段

只输出正文，不要解释，不要标题之外的前言。写完后把正文写入：
<项目目录>\drafts\ch-KKKK.txt
```

---

## D. 质检返工提示词

```
第 K 章质检未通过，问题如下：
<audit_quality.py 输出的 issues 逐条粘贴>

请重写这一章，逐条解决问题。注意：
- 字数不足要靠「增加场景/冲突」补，不许用排比句和空话注水
- 自我重复的句子必须换掉，不许换同义词敷衍
- 与上一章重复率过高说明在复述前文，直接删掉复述部分，从新事件开始
- 全章无对话说明写成了说明文，加入人物之间的交锋

重写后重新 commit 并再次 audit，直到通过。
```

---

## E. 收尾导出提示词

```
《        》已达到目标字数，轮回结束。

1. python scripts/novel_state.py status --root "<项目目录>"   # 确认 complete: true
2. python scripts/audit_quality.py --root "<项目目录>" --all --json   # 全量质检，记录不合格章节
3. python scripts/export_novel.py --root "<项目目录>" --by-volume --zip
4. 报告：总字数、章节数、卷数、完成轮次、质检不合格章节清单、导出文件路径

不要写番外，不要继续加字数。达标即停止。
```
