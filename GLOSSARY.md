# Auto-Purchase(会员购抢票)

本地 WebUI 的 bilibili 会员购自动抢票工具:对时、预热、开抢。当前设计方向为同一任务携带多个实名观演人、一次性下多票。

## Language

**观演人库 (Buyer Registry)**:
会员购账号下已实名的观演人集合,通过 `/api/ticket/buyer/list` 读取,身份证由接口打码返回。
_Avoid_: 买家池, 购买人列表, 观演人列表

**任务观演人 (Task Buyers)**:
挂在一次抢购任务上的观演人集合,有序,每个观演人对应一张票。
_Avoid_: 多买家, 购买人, 参战名单

**单订单多观演人 (Single-Order Multi-Buyer)**:
一次 createV2 下单携带 N 个任务观演人(count=N),整单原子,不存在部分成功。
_Avoid_: 批量下单, 多单并发, 一把梭

**拿齐语义 (All-or-Nothing)**:
任务成功 = 全部任务观演人同单出票;失败则整单重试,不保留部分结果。
_Avoid_: 部分成功, 能中几张算几张

**胜者 (Winner)**:
任务内第一份成功提交的订单;胜者出现即停止该任务其余 worker(现有 winner Event 语义)。
_Avoid_: 中签, 中标, 头马
