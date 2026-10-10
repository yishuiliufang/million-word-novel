# 写作延续备忘（跨轮回读取）

## 项目
- 书名《草色三千里》，根目录 `novels/caoye`
- 目标 100 万字，5 卷，每章约 3000 字（no_ws，容差 2400–3750），每轮 1 章
- 用户要求：每章写完后**不呈现正文**，只给 3–4 个下一章走向选项由用户挑；
  正文持续追加到 `novels/caoye/manuscript.md`（第 1 章已写入），全书 100 万字达标后一次性展示。
  目标字数用户明确保持 100 万字，不降。

## 命令顺序（严格）
```
python3 scripts/novel_state.py next --root "$ROOT"
python3 scripts/novel_state.py plan --root "$ROOT" --chapter N --file <plan.json> --check
# 写 drafts/ch-NNNN.md → 写 state/plans/ch-NNNN.delta.json
python3 scripts/novel_state.py audit --root "$ROOT" --chapter N --file <draft.md> --strict
python3 scripts/novel_state.py commit --root "$ROOT" --chapter N --file <draft.md> --delta <delta.json>
python3 scripts/novel_state.py round-done --root "$ROOT"
```
脚本根目录：`C:\Users\Administrator\.dsh\skills\million-word-novel`；WSL 路径
`/mnt/c/Users/Administrator/.dsh/skills/million-word-novel`。WSL 只有 `python3`，没有 `python`。

## 关键约定（踩过的坑）
1. `canon.py` 的 `--root` 是**全局参数，必须放在子命令之前**：`canon.py --root X import-md`。
2. plan 的 `relevant_foreshadowing` 必须是对象数组
   `[{"code":"F001","status":"PLANTED","overdue":false}]`，写成字符串数组会让 `plan_digest` 崩。
3. `must_change[].field` 只能是
   location / goal / emotion / belief / knowledge / health / possession / status / relationship。
4. **不要把还没写的伏笔灌进 Canon。** `canon.py update` 的可写字段里没有
   `planted_chapter`（canon_db.py:454 只有 title/description/status/kind/planned_payoff_*/
   resolved_chapter），所以只能直接改 sqlite：
   `UPDATE foreshadowings SET planted_chapter=NULL WHERE code IN (...)`。
   第 1 轮已把 F004–F015 的 `planted_chapter` 置 NULL（保留回收区间与 PLANTED 状态）。
   后续**不要重跑 `import-md`** 整份 bible，新伏笔一律走 delta 的 `foreshadowing` 数组
   （`action: "plant"` + `planned_payoff_start/end`），由 commit 写入 planted_chapter。
5. `bible.md` / `outline.md` 是派生视图（bible.md 首次 import-md 后带派生标记）。
   真源是 `state/canon.db`；人写源备份在 `state/bible.source.md`。
6. delta 字段：`character_changes` 需要 character/field/before/after；`events` 至少 summary；
   `knowledge_added` 需要 knower/fact_code；`foreshadowing` 用 code + action + 回收区间；
   `plot_threads` 用 code + action + status。
7. 草稿里对白标签紧跟的词会被当说话人解析。避免"娜仁没有……"这类句式，
   改成"娜仁没接话"。
8. **JSON 字段必须是数组，不是字符串。** `canon_db.JSON_FIELDS = (belief, knowledge,
   possession, relationship)`，delta 的 `character_changes` 里这四项的 `before/after`
   写成字符串会被 `STATE_TRANSITION_REJECTED` 拒掉（commit 仍会通过，但状态没写进去，
   只能事后用 `canon.py update --field X --json-value '[...]'` 补）。第 1 章的
   苏牧.knowledge / 苏牧.possession / 娜仁.possession 已按此补正。
9. plan 的 `scenes[].location` 写 Canon 里已存在的地点名（苏家牛场 / 巴音乌拉草场 /
   塔布森旗草业技术推广站……），不要写成"苏家牛场 棚圈外"这种带子地点的串，
   否则会一直留 `PLAN_UNKNOWN_LOCATION` warning。
10. **时间线比较是纯字符串字典序**（narrative_audit.py:224、validate_state.py:266）。
    `"故事开始年 10月14日" < "故事开始年 9月下旬…"`，会报 TIMELINE_REGRESSION 阻断。
    统一用**补零**格式：`故事开始年 09月18日-10月04日`、`故事开始年 10月14日`。
    ch1 已用 `UPDATE timeline SET world_time=… WHERE chapter=1` 改成补零格式
    （正文不含该串，改库安全）。
11. 说话人解析正则是 `"([^"\n]{1,200})"([\u4e00-\u9fff]{2,4})(?:说|道|问|答|应|喊|叫)`
    （narrative_audit.py:63）。引号闭合后**紧跟 2–4 个汉字再跟语气词**就会被判成说话人。
    踩过的坑：`"我问了。"她说，"他不说。"` → 正则匹配到 `"她说，"`+`他不`+`说` →
    报 `UNKNOWN_SPEAKER: 他不`。修法：让引号后那 2–4 字不接语气词，或换成动作插入语
    （`娜仁把那叠钱推回灶台。` / `娜仁把剩下的半盆苞谷倒进桶里。`）。
12. delta 里 `foreshadowing[].status` 必须与 `action` 推出的状态一致：
    `touch/advance/develop → ACTIVE`，`plant → PLANTED`。写 `status:PLANTED` + `action:touch`
    会报 `FORESHADOW_STATUS_OVERRIDE`（非阻断，但会留 warning）。
13. WSL 里用 `python3` 时不要把脚本名存进变量再传（`$P` 不展开）。直接写
    `python3 canon.py --root "$ROOT" …`，或先 `cd` 到 scripts 目录。
14. **delta 里新建（Canon 中不存在的）伏笔必须带 `title` 和 `description`。**
    `upsert_foreshadow`（canon_db.py:454）只写这几个列，delta 用 `note` 不会变成 title。
    ch2 新增的 F017 被建成空标题，已用
    `canon.py --root X update --kind foreshadow --ref F017 --field title --value … --chapter 2 --reason …`
    补全（`update` 必须带 `--reason`）。已有的 F001–F016 来自 bible import，带标题。

## 第 2 章之后（第 3 章起点）
- 苏牧：在巴音乌拉草场；持有 父亲的扶手纸条 / 冬料手账 / 远山草业借款单副本 / 抵押合同副本
  （后两者锁在饭桌抽屉）；背包里是娜仁的四万八（他没说明用途，也没算给她说）
- 娜仁：库房钥匙**仍在她手里，还没交**；四万八已被苏牧取走；她说了"钱先"——
  账本救的是明年的牛，钥匙救的是这个冬天的钱；点破"那笔钱不是借的"
- 苏巴特尔：已死（累死在割晒机旁）
- 郝建（新人物）：远山草业风控部经理，管塔布森片区账款；只认合同不认人，从头到尾没看牛
- 硬数字（必须保持一致）：本金 23.8 万｜逾期 47 天｜本利 26.5 万｜期限 15 天｜
  抵押北边 3000 亩经营权 10 年｜附件只列 1500 亩｜79 元/亩 vs 开垦至少 120 元/亩｜
  补冬料 30 吨×1600 元 = 4.8 万｜合计需 31.3 万｜差 26.5 万｜无批文约 400 亩（≈26 公顷）
- 苏牧已算出但未说出口：400 亩无批文 + 另 1500 亩无借款对应。这是他手里的筹码，
  别让他提前打出去
- 未回收伏笔：F001(ACTIVE,4-6) F002(ACTIVE,8-12) F003(ACTIVE,3-5) F016(ACTIVE,30-40)
  F017(PLANTED,6-10)；F004–F015 尚未埋设
- 已埋事件 E1-1…E1-7、E2-1…E2-6；知识 K1-1…K1-7、K2-1…K2-9
- Canon 规模：人物 11（新增郝建）、事件 13、知识 16、物品 8、机构新增"远山草业"
- 卷 1 不可逆变化仍**未完成**：苏牧签下牛场债务与草场承包权 + 白灾预警把他钉在草原上
  （第 1 章完成"必须留下"，签字留给后面）
- 已追加进 manuscript.md：第 1、2 章（目标 100 万字后统一交付，中途不展示正文）

## 风格红线
- 冷峻、写实、克制；短句；草业术语与牧民口语混编；数字精确
- 不用"仿佛""宛如"堆砌；不写抒情散文段落；不写空洞行业赞歌
- 主角缺陷必须持续显影：苏牧用数据压制人情，算得出数字算不出人心
