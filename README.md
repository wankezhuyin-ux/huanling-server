# 幻灵归虚 / 水墨灵兽 · 服务端

单进程 Python 3 HTTP 服务，替代原来的线上服，实现客户端用到的 **177 个接口**。
**客户端一行代码不用改**——所有玩法都长在 HTTP 协议上，协议不变，逻辑集中在一个能直接调试的进程里。

- API **3000**，聊天 **3001**
- **只用标准库，零第三方依赖**（`http.server` + `threading` + `json`），Python 3.9+ 装了就能跑
- 存档结构直接沿用客户端的 `saveData`，老档可以直接读
- 接口覆盖 **177/177**，一致性回归 **177/177 通过**

```bash
python3 server.py                 # 起服务（3000 API / 3001 聊天）
python3 tools/conformance.py      # 177 个接口一致性回归
```

## 三个仓库的关系

| 仓库 | 是什么 | 语言 |
|---|---|---|
| `huanling-client` | Godot 4.6.1 客户端，**主交付物** | GDScript |
| `huanling-server` ← **你在这里** | 自建服务端，177 个接口 | Python 3 标准库 |
| `huanling-ui-kit` | 概念图拆出的 UI PNG + 九宫格 + 装配验证台 | 素材 / Python |

本仓库替代的是原线上服；客户端 `scripts/game_server_config.gd` 里的 API 地址已经是 `127.0.0.1:3000`。

## 先读什么

| 顺序 | 文件 | 讲什么 |
|---|---|---|
| 1 | [`交接说明.md`](交接说明.md) | 接手先看：目录职责、**排除项**、部署到云、当前状态 |
| 2 | 本文下文 | 各 handler 干什么、数据从哪来、机制怎么实现、怎么扩展 |
| 3 | [`contract.json`](contract.json) | 177 个端点的契约（方法 / 请求字段 / 客户端读的响应字段） |

## 不要提交的东西（重要）

| 排除项 | 原因 |
|---|---|
| `state/` | **真实玩家存档 + 登录令牌**（`accounts/`、`tokens.json`、`gm.json`）。属于用户数据，`.gitignore` 已排除 |
| `__pycache__/` `*.pyc` | 编译缓存，可重建 |
| `*.log` `*.bak-*` | 运行日志与手改前备份 |

克隆下来第一次跑 `server.py` 会自动生成一份**空白** `state/`，不带任何真实账号。

## 已知边界（坦白）

1. **单账号**：`state/state.json` 只存一个玩家。测多账号就复制一份换 `--port` 起第二个进程。
2. **战斗是近似复刻**：伤害公式按真实样本反推（`atk - def×0.5`，8% 暴击 ×1.6），胜负、经验、金币口径已对齐，细节可能有偏差。
3. **没有反外挂**：本地服不校验客户端上报数值（原服有 `authorityPhase=enforce` 校验链）。**不能直接对外开服。**
4. **数据含真实玩家昵称**：`ranking*.json` / `save_seed.json` 来自线上抓包，**仅本机自用**。
5. **没有自动化集成测试**，只有一致性回归（HTTP 200 + 合法 JSON）；逻辑正确性靠人工走闭环。

---

## 为什么是这个方案

客户端所有玩法都长在 HTTP 协议上（`scripts/network.gd`，10083 行，178 个 `server_url + "..."`）。
与其在 GDScript 里重写 178 个本地分支，不如把服务端做出来：协议不变、客户端不改、
逻辑集中在一个能直接调试的进程里。存档结构完全沿用客户端的 `saveData`，所以老档可以直接用。

## 目录

| 文件 | 作用 |
|---|---|
| `server.py` | HTTP 入口、路由、鉴权、`/api` 前缀归一化、请求日志 |
| `core.py` | 全局配置（Rules）、玩家状态（State）、上下文（Ctx）、路由注册 |
| `combat.py` | 战斗结算：伤害/命中/连击/暴击，输出与线上一致的事件流 |
| `handlers_account.py` | 注册登录、存档上传下载、用户信息、邮件、公告、离线、排行榜 |
| `handlers_battle.py` | 野外战斗、天关战斗、战斗结算掉落、扫荡奖励 |
| `handlers_play.py` | 天关、套装/五行/图鉴副本、竞技场、世界 Boss、生死塔、藏宝洞、兄弟天关、仙魔战场、签到、贵族、养宠赛 |
| `handlers_growth.py` | 商店、抽奖与保底、经济结算、灵兽、装备、宠装、玩家属性、武学、打造、礼包 |
| `handlers_extra.py` | 批量接口、兄弟天关/生死塔实时交互、仙魔补充 |
| `applog.py` | 请求日志写入 + 按大小轮转（`requests.log` → `requests-<时间>.log.gz`，默认 32 MB × 8 份） |
| `contract.json` | 从 `network.gd` 静态抽出的 177 个端点契约（方法、请求字段、客户端读取的响应字段） |
| `data/` | 线上真实下发过的配置与样本（见下） |
| `state/` | 运行期状态：`state.json`（存档+计数）、`requests.log`、`conformance.json`、`coverage.json` |
| `tools/conformance.py` | 全接口回归测试 + 与真实抓包响应的字段对比 |
| `tools/fix_mail_placeholder_items.py` | 一次性数据修复：把邮箱里「普通技能书之一」这类占位附件换成具体道具（默认只预演，`--apply` 才写盘并自动备份） |
| `tools/test_lottery_mail_invest.py` | 抽奖占位奖励展开、灵兽投图鉴（服务端真删 + 消耗名单）、存量邮件修复的回归测试 |

## 数据从哪来

| 数据 | 来源 |
|---|---|
| `runtime-config.json` | 线上 `GET /api/gameplay/runtime-config`（73.7 KB，21 个玩法模块：次数、时间窗、权重、门槛、85 技能、8 阶法宝…） |
| `drop-config.json` | 线上 `/api/economy/drop-config`（17 条真实掉率） |
| `shop-catalog.json` | 线上 `/api/shop/catalog`（48 件商品，价格/限购/VIP 门槛） |
| `ranking*.json` | 线上真实榜单（玩家榜 / 进化宠 / 非进化宠），当"其他玩家"用来做排行榜与竞技场对手 |
| `save_seed.json` | 线上 `/api/save/download` 的真实存档（14 个区块、39 件道具、10 只灵兽、装备/武学/套装/任务） |
| `monsters.json` | 从 125 场真实野外战斗响应里反推的怪物图鉴与地图表 |
| `maps.json` | **279 张野外图的真实刷怪表**：怪物池（攻防敏血/等级）、刷怪数量分布、`exp_reward`/`gold_reward`、单场收益、掉落、零收益标记。由 `sweep/` 对线上跑 837 场 `wild-run + battle-loot` 实测得到 |
| `festival.json` / `xianmo-*.json` / `mail-seed.json` | 线上对应端点原样落盘 |

## 机制是怎么实现的

**存档权威**：服务端持有 `saveData`，`/save/upload` 收客户端整份存档，`/save/download` 发回去，
带 revision 递增。所有发奖都改这份存档，客户端下次下载就是这个结果。

**时间闸门**：`core.State.daily_try()` 统一管每日次数，跨天（北京时间 00:00）自动归零。
时间窗用 `in_window()` 按 `runtime-config` 里的 时/分 判断，客户端看到的 `inChallengeWindow` 就是真判定。

**战斗**：`combat.py` 在服务端逐回合结算，输出 14 种事件（`battle_start / info / round / combo_trigger /
attack / combo_attack / kuihua_hit / kuihua_point_success|fail / enemy_dead / miss / acupoint_skip / victory`），
字段结构与线上抓包逐字段对齐，客户端只负责播放。奖励口径用真实样本反推并已核对：

```
玩家经验 = Σ monsters.exp_reward × wildBattle.fieldExpMultiplier(0.5)
金币     = Σ monsters.gold_reward
宠物经验 = 与玩家经验同额
怪物经验 = 90 × 等级     金币 = 1 × 等级
```

实测：3 只 Lv1500 拂叶鸠 → 金币 +4500、经验 +202500，与线上样本（6000 金 / 270000 经验 @ 4 只）同比例。

**刷怪是查表的**：`spawn_wild_monsters()` 直接读 `data/maps.json` 里该地图的怪物池，
数量按线上实测的 `spawnDist` 加权抽。服务端不认识的地图（线上实测 `冰封雪地`）返回通用「怪物」，
结算回 `code: UNKNOWN_MAP` 且零奖励；`九曲回廊` 全系列线上同样是零收益，本地照抄。

**发奖出口**：需要"发到背包之外"的奖励（百关礼包、世界 Boss 礼包、生死塔参与礼包）一律走
`State.send_mail()`，和线上行为一致，客户端邮件页能直接看到。

**多人玩法**：世界 Boss / 竞技场 / 排行榜用真实榜单数据当机器人；兄弟天关、生死塔是"单人 + 事件流"，
`/brother-tower/battle-events` 按 `sinceSeq` 增量返回，客户端能正常进入并打完一波。

## 覆盖情况

```
接口覆盖：177/177（100%）
一致性测试：177/177 通过（HTTP 200 + 合法 JSON，无 5xx）
```

分域分布见 `state/coverage.json`，逐端点明细见 `state/conformance.json`。

## 怎么扩展

新增一个端点只需要三步：

```python
# 1. 在对应 handlers_*.py 里
@route("POST", "/your/endpoint")
def your_endpoint(ctx: Ctx) -> dict:
    state = ctx.state
    if not state.daily_try("your_counter", 3):      # 每日 3 次
        return fail("今日次数已用完")
    state.add_item("奖励名", 1, "item")              # 改存档
    state.add_gold(1000)
    state.bump()                                    # 版本号 +1
    return ok(items=[{"name": "奖励名", "count": 1}])

# 2. 数值从配置读，不要写死
RULES.v("tower", "maxFloor", 2000)

# 3. 跑回归
python3 tools/conformance.py
```

## 已知边界

- **单账号**：`state/state.json` 只存一个玩家。要测多账号，复制一份 `state.json` 换 `--port` 起第二个进程。
- **战斗是近似复刻**：伤害公式按样本反推（`atk - def×0.5`，8% 暴击 ×1.6），
  与原服可能存在细微偏差；胜负、经验、金币口径已与真实样本对齐。
- **没有反外挂**：本地服不校验客户端上报数值（原服有 `authorityPhase=enforce` 校验链）。
- **数据含真实玩家昵称**：`ranking*.json` / `save_seed.json` 来自线上抓包，**仅本机自用，不要外发或提交公开仓库**。
