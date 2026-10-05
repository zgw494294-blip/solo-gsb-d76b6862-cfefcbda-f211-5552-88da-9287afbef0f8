"""提示变更共用业务模块：手工确认 / 自动发出 / 撤回 / 编辑 / 重排 / 接受提议。

本模块集中"会改变本场提示（cues）及其联动状态"的业务逻辑，HTTP 细节
（请求模型、X-Control-Token 凭据头、响应序列化）留在 app/main.py：
每个写端点在 ``with lock, db() as conn``（同一把写锁、同一事务）内先做
控制席身份检查，再调用本模块函数，并把 Conflict / BadRequest 映射为
409 / 400。因此**身份、版本与发出资格检查始终与写入处于同一写锁、同一
事务内**。

共用发出：:func:`manual_issue`（手工确认）与 :func:`auto_issue_due`
（后台自动发出）各自只完成"本路径的资格检查"——手工查版本 / 暂停 /
两工种就绪（凭据由端点先查），自动查开关 / 演出状态 / 暂停 / 计时到期
/ 就绪，不要求任何客户端凭据——通过后都调用同一个
:func:`issue_current` 完成"内容快照入已发 + 推进一条 + 版本 +1 +
核验换轮 + 自动计时安排（末条完成并关闭自动）"。没有两份发出实现：
两条路径交错时由同一把锁串行化，配合版本号保证**不重复、不跳条、每次
只发一条**。

事务契约：本模块所有函数只接收调用方已打开的 sqlite 连接并在其上执行，
自身既不获取锁也不 commit；提示发出快照、待发队列（位置）、全局版本号、
提议状态、核验轮次与就绪、自动计时的全部写入随调用方事务**一起提交或
回滚**。前置条件不满足时抛出 :class:`Conflict`（映射 409）或
:class:`BadRequest`（映射 400），抛出前不提交任何语句。

时钟：计时相关函数显式接收 ``clock``（无参、返回 epoch 秒的可调用对象）：
生产环境传入真实时钟，回归测试传入可控时钟以精确驱动"等满间隔 / 暂停
冻结 / 到期未就绪 / 交错"等场景。

观众字幕画面：手工 / 自动发出的同一实现 :func:`issue_current` 在发出事务
内把"该提示的有效字幕（无则清屏）+ 画面序号 +1"一起落库；撤回末条在同一
事务内清屏（保留字幕设置）。字幕设置本身（绑定提示 id、失效标记）的读写
与收场清空在 app/main.py。
"""
import sqlite3
from datetime import datetime, timezone
from typing import Callable, Optional

# epoch 秒时钟：生产为 time.time，测试可替换为可控时钟
Clock = Callable[[], float]

# 本场核验工种：灯光 / 音响（API 使用稳定英文键）。就绪门槛为全部工种
# 对当前轮次回报就绪。
VERIFY_DEPARTMENTS = ("lighting", "sound")


class CueflowError(Exception):
    """提示变更业务异常基类：消息即返回给页面的中文说明。"""

    def __init__(self, message: str = "操作已过期"):
        super().__init__(message)
        self.message = message


class Conflict(CueflowError):
    """前置条件不满足（版本过期 / 状态不符 / 未就绪 / 快照已变等）→ 409。"""


class BadRequest(CueflowError):
    """请求内容非法（空内容 / 空编号 / 编号重复等）→ 400。"""


# ---------------------------------------------------------------- 内部读取

def _first_pending(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    """当前提示：待发队首（按位置、id 升序）。"""
    return conn.execute(
        "SELECT * FROM cues WHERE status = 'pending' "
        "ORDER BY position ASC, id ASC LIMIT 1"
    ).fetchone()


def _pending_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS c FROM cues WHERE status = 'pending'"
    ).fetchone()["c"]


def _departments_ready(conn: sqlite3.Connection, round_no: int) -> bool:
    """指定核验轮次是否两工种都已报就绪。"""
    ready_count = conn.execute(
        "SELECT COUNT(*) AS c FROM verify_reports "
        "WHERE round = ? AND ready = 1",
        (round_no,),
    ).fetchone()["c"]
    return ready_count >= len(VERIFY_DEPARTMENTS)


# ---------------------------------------------------------------- 核验轮次

def verify_advance(conn: sqlite3.Connection) -> None:
    """开启新轮次并清空本轮就绪（仅核验启用时）。

    必须在写锁内、与触发它的业务写入同一事务内调用：任一步失败整体回滚，
    不留部分结果。轮次单调递增、不可复用；旧轮次回报随清空删除，迟到的
    旧轮次回报因轮次不符被 409 拒绝。
    """
    meta = conn.execute("SELECT verify_enabled FROM meta WHERE id = 1").fetchone()
    if meta["verify_enabled"]:
        conn.execute("UPDATE meta SET verify_round = verify_round + 1 WHERE id = 1")
        conn.execute("DELETE FROM verify_reports")


# ---------------------------------------------------------------- 自动计时

def restart_timer(conn: sqlite3.Connection, clock: Clock) -> None:
    """当前提示更换（或开启 / 改间隔）后重新计时：从完整间隔计起。

    仅自动模式开启时有效；暂停中改为冻结完整间隔的剩余时间（恢复后续计），
    因此暂停中撤回 / 改写队首 / 接受队首提议同样保存**完整**剩余间隔。
    必须在写锁内、与触发它的业务写入同一事务内调用。
    """
    meta = conn.execute(
        "SELECT auto_enabled, auto_interval, paused FROM meta WHERE id = 1"
    ).fetchone()
    if not meta["auto_enabled"]:
        return
    if meta["paused"]:
        conn.execute(
            "UPDATE meta SET auto_deadline = NULL, auto_remaining = ? WHERE id = 1",
            (float(meta["auto_interval"]),),
        )
    else:
        conn.execute(
            "UPDATE meta SET auto_deadline = ?, auto_remaining = NULL WHERE id = 1",
            (clock() + meta["auto_interval"],),
        )


def freeze_timer(conn: sqlite3.Connection, clock: Clock) -> None:
    """暂停发出：冻结剩余时间（到期时刻 -> 剩余秒数）。与暂停写入同一事务。"""
    meta = conn.execute(
        "SELECT auto_enabled, auto_deadline FROM meta WHERE id = 1"
    ).fetchone()
    if not meta["auto_enabled"] or meta["auto_deadline"] is None:
        return
    remaining = max(0.0, meta["auto_deadline"] - clock())
    conn.execute(
        "UPDATE meta SET auto_deadline = NULL, auto_remaining = ? WHERE id = 1",
        (remaining,),
    )


def thaw_timer(conn: sqlite3.Connection, clock: Clock) -> None:
    """恢复发出：按冻结的剩余时间续计（剩余秒数 -> 到期时刻）。同事务。"""
    meta = conn.execute(
        "SELECT auto_enabled, auto_remaining FROM meta WHERE id = 1"
    ).fetchone()
    if not meta["auto_enabled"] or meta["auto_remaining"] is None:
        return
    conn.execute(
        "UPDATE meta SET auto_deadline = ?, auto_remaining = NULL WHERE id = 1",
        (clock() + meta["auto_remaining"],),
    )


def disable_auto(conn: sqlite3.Connection) -> None:
    """关闭自动模式并清空计时（接管 / 归档 / 重置 / 完成 / 服务重启）。

    保留已设间隔，便于下一场再次开启时沿用。与触发它的写入同一事务。
    仅在确实开启或留有计时时才落 UPDATE：已关闭时不触碰 meta 行。
    """
    conn.execute(
        "UPDATE meta SET auto_enabled = 0, auto_deadline = NULL, "
        "auto_remaining = NULL WHERE id = 1 AND (auto_enabled != 0 "
        "OR auto_deadline IS NOT NULL OR auto_remaining IS NOT NULL)"
    )


# ---------------------------------------------------------------- 观众画面

def screen_clear(conn: sqlite3.Connection) -> None:
    """观众字幕屏清屏：画面置空、画面序号 +1（撤回 / 收场类操作）。

    必须在写锁内、与触发它的业务写入同一事务内调用：任一步失败整体回滚。
    画面序号跨场单调递增、不归零，字幕屏据此丢弃迟到的旧响应。
    """
    conn.execute(
        "UPDATE meta SET screen_text = NULL, screen_seq = screen_seq + 1 "
        "WHERE id = 1"
    )


def _screen_show_for_issue(conn: sqlite3.Connection, cue_id: int) -> None:
    """发出一条提示时更新观众画面：有效字幕上屏，否则清屏；序号 +1。

    与发出写入同一事务：画面随发出一起提交或回滚；该提示没有（有效）字幕
    不阻挡发出，只是画面清空。字幕绑定提示 id，与提示内容快照无关。
    """
    sub = conn.execute(
        "SELECT text FROM subtitles WHERE cue_id = ? AND valid = 1",
        (cue_id,),
    ).fetchone()
    conn.execute(
        "UPDATE meta SET screen_text = ?, screen_seq = screen_seq + 1 WHERE id = 1",
        (sub["text"] if sub else None,),
    )


# ---------------------------------------------------------------- 共用发出

def issue_current(conn: sqlite3.Connection, clock: Clock) -> sqlite3.Row:
    """把待发队首按服务端此刻内容记入已发并推进一条（手工 / 自动共用）。

    本函数**不做任何资格检查**：调用方必须已在写锁内完成本路径全部检查
    （手工：凭据、版本、暂停、就绪；自动：开关、演出状态、暂停、到期、
    就绪），且已确认存在待发提示。一次调用恰好发出一条：

    - 该条以 ``issued_content = content`` 保存**发出那一刻的内容快照**，
      记录服务端发出时间，``issued_seq`` 取推进后的新版本号；
    - 全局版本 +1，同时开启核验新轮次并清空本轮就绪；
    - 观众画面同步更新：该提示设有**有效字幕**则字幕上屏，否则清屏，
      画面序号 +1（同一事务，无字幕不阻挡发出）；
    - 仍有待发：状态保持演出中，下一条从完整间隔重新自动计时（暂停中
      冻结完整间隔），不补发积压；
    - 末条发出：进入已完成并在同一事务内关闭自动模式、清空计时。

    提示快照、队列状态、版本、核验轮次、计时与观众画面在同一事务内落库，
    任一步失败由调用方事务整体回滚。返回被发出的提示行。
    """
    meta = conn.execute("SELECT version FROM meta WHERE id = 1").fetchone()
    row = _first_pending(conn)
    if row is None:  # 防御：契约要求调用方先确认有待发提示
        raise Conflict()

    new_version = meta["version"] + 1
    now_iso = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE cues SET status = 'issued', issued_content = content, "
        "issued_at = ?, issued_seq = ? WHERE id = ?",
        (now_iso, new_version, row["id"]),
    )
    # 观众画面与发出一起保存：有效字幕上屏、否则清屏（同一事务）
    _screen_show_for_issue(conn, row["id"])
    remaining = _pending_count(conn)
    # 与手工 / 自动任一路径一致：推进即开启新轮次并清空本轮就绪（同一事务）
    verify_advance(conn)
    if remaining == 0:
        # 全部发出：完成并关闭自动模式（同一事务）
        disable_auto(conn)
        conn.execute(
            "UPDATE meta SET version = ?, status = 'done' WHERE id = 1",
            (new_version,),
        )
    else:
        # 每次只发一条：下一条从完整间隔重新计时，不补发积压
        restart_timer(conn, clock)
        conn.execute(
            "UPDATE meta SET version = ?, status = 'live' WHERE id = 1",
            (new_version,),
        )
    return row


def manual_issue(conn: sqlite3.Connection, *, version: int, clock: Clock) -> None:
    """手工确认发出当前提示（``POST /api/confirm`` 的业务部分）。

    凭据（X-Control-Token）由 HTTP 端点在调用前于同一锁 / 事务内检查；
    本函数在同一写事务内完成"版本 -> 演出状态 -> 暂停 -> 核验就绪 ->
    存在待发"的检查后调用共用发出 :func:`issue_current`。暂停期间即使
    版本最新也 409；启用核验后两工种缺任一就绪 409；无待发提示 409。
    """
    meta = conn.execute(
        "SELECT version, status, paused, verify_enabled, verify_round "
        "FROM meta WHERE id = 1"
    ).fetchone()
    if (
        meta["status"] != "live"
        or meta["paused"]
        or version != meta["version"]
    ):
        raise Conflict()
    if meta["verify_enabled"] and not _departments_ready(
        conn, meta["verify_round"]
    ):
        raise Conflict("灯光、音响尚未全部就绪，不能确认发出")
    if _first_pending(conn) is None:
        raise Conflict()
    issue_current(conn, clock)


def auto_issue_due(conn: sqlite3.Connection, *, clock: Clock) -> bool:
    """后台自动发出发出一条（无需任何客户端凭据）；本次未发出返回 False。

    与手工操作共用同一把写锁：执行时重新核对自动开关、演出状态、暂停
    标记、计时到期与两工种就绪，任一不满足即不发出（**无任何写入**）——
    与手工确认竞争时，要么手工先生效（版本已推进、计时被重置，本次到期
    检查自然落空），要么自动先发出（手工确认因版本过期被 409 拒绝），
    不会重复发出或跳条。到期未就绪（核验门槛未满足）继续等，不重置计时；
    通过检查后与手工完全共用 :func:`issue_current`（快照、版本、换轮、
    末条关自动、下一条重计时规则一致），每次最多一条。
    """
    meta = conn.execute(
        "SELECT version, status, paused, verify_enabled, verify_round, "
        "auto_enabled, auto_interval, auto_deadline FROM meta WHERE id = 1"
    ).fetchone()
    if (
        not meta["auto_enabled"]
        or meta["status"] != "live"
        or meta["paused"]
        or meta["auto_deadline"] is None
    ):
        return False
    if clock() < meta["auto_deadline"]:
        return False  # 未到期
    if meta["verify_enabled"] and not _departments_ready(
        conn, meta["verify_round"]
    ):
        return False  # 到期未就绪：继续等，不重置计时
    if _first_pending(conn) is None:
        return False
    issue_current(conn, clock)
    return True


# ---------------------------------------------------------------- 撤回

def withdraw_last(
    conn: sqlite3.Connection, *, version: int, cue_id: int, clock: Clock
) -> None:
    """把本场最后一条已发提示退回当前待发队首（``POST /api/withdraw``）。

    仅演出中（live，含暂停）或已完成（done）、版本匹配且 cue_id 仍是最后
    一条已发提示时生效。退回时以**发出快照**作为待发内容（保留 id 与
    编号），清除发出快照 / 时间 / 序号，插到现有待发队首（原待发整体后
    移），随后对全部待发重新分配紧凑位置 1..n；完成态同时回到演出中，
    暂停标记不动。撤回更换了当前提示：开启新核验轮次、自动计时从完整
    间隔重新计起（暂停中冻结完整间隔）。版本过期 / 目标不是末条 / 无
    已发提示抛 Conflict；全部写入与版本 +1 同一事务，失败整体不变。
    撤回同时把观众画面清屏（画面序号 +1，同一事务）：退回待发的提示
    尚未重新发出，字幕屏不应继续显示旧画面；该提示的字幕**设置**保留，
    再次发出时若仍有效会重新上屏。
    """
    meta = conn.execute("SELECT version, status FROM meta WHERE id = 1").fetchone()
    if meta["status"] not in ("live", "done") or version != meta["version"]:
        raise Conflict()
    last = conn.execute(
        "SELECT * FROM cues WHERE status = 'issued' "
        "ORDER BY issued_seq DESC, id DESC LIMIT 1"
    ).fetchone()
    if last is None or last["id"] != cue_id:
        # 无已发提示，或页面指定的提示已不是最后一条（如已被先撤回）
        raise Conflict()

    # 撤回提示以发出快照插到现有待发队首（id 与编号保留；发出快照 /
    # 时间 / 序号清空），随后对**全部待发**按"撤回提示 + 原待发顺序"
    # 重新分配紧凑位置 1..n（每场最多 12 条）。不能用 MAX(position) 做
    # 基数平移：反复确认/撤回会让位置指数增长，既有库可能已逼近 SQLite
    # 整数上限（2^63-1），平移会溢出成浮点（同值位置退回按 id 排序，
    # 待发相对顺序被破坏）甚至直接 OverflowError。已发提示的位置不参与
    # 待发排序，与 1..n 重叠也没有影响。
    existing_pending_ids = [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM cues WHERE status = 'pending' "
            "ORDER BY position ASC, id ASC"
        ).fetchall()
    ]
    ordered_ids = [last["id"]] + existing_pending_ids
    conn.execute(
        "UPDATE cues SET status = 'pending', content = issued_content, "
        "issued_content = NULL, issued_at = NULL, issued_seq = NULL "
        "WHERE id = ?",
        (last["id"],),
    )
    for pos, pending_id in enumerate(ordered_ids, start=1):
        conn.execute(
            "UPDATE cues SET position = ? WHERE id = ?", (pos, pending_id)
        )
    # 撤回更换了当前提示：开启新轮次并清空本轮就绪（同一事务）。
    # 与确认同版本竞争时只有一个生效——撤回先完成则确认因版本过期失败，
    # 确认先完成则本撤回因版本过期被拒，双方都不会复用旧轮次。
    verify_advance(conn)
    # 撤回更换了当前提示：自动计时从完整间隔重新计起（同一事务；
    # 暂停中则冻结完整间隔，恢复后续计）。
    restart_timer(conn, clock)
    # 撤回即观众画面清屏（同一事务）；字幕设置保留，再次发出时仍生效
    screen_clear(conn)
    # 完成态退回一条即回到演出中；暂停标记不动。
    conn.execute(
        "UPDATE meta SET version = version + 1, status = 'live' WHERE id = 1"
    )


# ---------------------------------------------------------------- 编辑

def edit_pending(
    conn: sqlite3.Connection,
    *,
    version: int,
    cue_id: int,
    content: str,
    clock: Clock,
    number: Optional[str] = None,
) -> None:
    """改写一条尚未发出的提示（``POST /api/edit`` 的业务部分）。

    版本过期、提示不存在或已发出抛 Conflict；去除首尾空白后内容为空、
    编号为空或编号与其他提示重复抛 BadRequest（数据不变）。``number``
    为 None（请求未提供该字段）时沿用原编号；显式传入字符串则改编号。
    **改写的是当前提示（待发队首）时，即使新文本与原文相同也开启新
    核验轮次并从完整间隔重新自动计时**（按提示身份而非文本变化判定）；
    编辑其他待发提示不换轮、不重置计时。改写该提示同时使其观众字幕
    **失效**（即使同文；重排不影响字幕）——字幕绑定提示 id 与当时的
    内容语境，提示被改写后需执行台重新确认字幕。所有写入与版本 +1
    同一事务。
    """
    meta = conn.execute("SELECT version FROM meta WHERE id = 1").fetchone()
    if version != meta["version"]:
        raise Conflict()
    row = conn.execute(
        "SELECT * FROM cues WHERE id = ?", (cue_id,)
    ).fetchone()
    if row is None or row["status"] != "pending":
        raise Conflict()

    content = content.strip()
    if not content:
        raise BadRequest("内容不能为空")
    if number is None:
        new_number = row["number"]
    else:
        new_number = number.strip()
        if not new_number:
            raise BadRequest("编号不能为空")
        dup = conn.execute(
            "SELECT 1 FROM cues WHERE number = ? AND id != ?",
            (new_number, row["id"]),
        ).fetchone()
        if dup:
            raise BadRequest("编号必须唯一")

    conn.execute(
        "UPDATE cues SET content = ?, number = ? WHERE id = ?",
        (content, new_number, row["id"]),
    )
    # 该提示被改写：其观众字幕即使同文也失效（设置保留、可重新保存恢复）
    conn.execute(
        "UPDATE subtitles SET valid = 0 WHERE cue_id = ?", (row["id"],)
    )
    # 改写的是当前提示（待发队首）才开启新轮次并清空就绪；编辑其他待发
    # 提示不影响当前轮次，本轮就绪保留。按提示 id 判定：即使文本相同，
    # 成功改写当前提示也换轮并重计时。
    first = _first_pending(conn)
    if first is not None and first["id"] == row["id"]:
        verify_advance(conn)
        # 改写当前提示：自动计时从完整间隔重新计起（编辑其他提示不重置）
        restart_timer(conn, clock)
    conn.execute("UPDATE meta SET version = version + 1 WHERE id = 1")


# ---------------------------------------------------------------- 重排

def reorder_pending(
    conn: sqlite3.Connection, *, version: int, order: list[int], clock: Clock
) -> None:
    """重排全部待发提示（``POST /api/reorder`` 的业务部分）。

    order 必须恰好是全部待发提示的 id（集合一致、长度一致），否则
    Conflict。按新顺序对待发分配紧凑位置 1..n（已发提示不参与，位置
    重叠无影响；不使用 MAX(position)+1 以免位置溢出）。**仅当重排更换
    了待发队首**才开启新核验轮次并从完整间隔重新计时；队首不变（仅
    后排调整）不换轮、不重置。所有写入与版本 +1 同一事务。
    """
    meta = conn.execute("SELECT version FROM meta WHERE id = 1").fetchone()
    if version != meta["version"]:
        raise Conflict()
    pending_ids = {
        r["id"] for r in conn.execute(
            "SELECT id FROM cues WHERE status = 'pending'"
        )
    }
    if len(order) != len(pending_ids) or set(order) != pending_ids:
        raise Conflict()

    first_before = _first_pending(conn)
    for pos, cue_id in enumerate(order, start=1):
        conn.execute(
            "UPDATE cues SET position = ? WHERE id = ?", (pos, cue_id)
        )
    # 仅当重排更换了当前提示（待发队首）才开启新轮次并清空就绪；
    # 队首不变的重排不影响当前轮次，本轮就绪保留。
    old_head = first_before["id"] if first_before is not None else None
    new_head = order[0] if order else None
    if old_head != new_head:
        verify_advance(conn)
        # 换队首：自动计时从完整间隔重新计起（队首不变的重排不重置）
        restart_timer(conn, clock)
    conn.execute("UPDATE meta SET version = version + 1 WHERE id = 1")


# ------------------------------------------------------------ 接受改写提议

def accept_pending_suggestion(
    conn: sqlite3.Connection, *, suggestion_id: int, version: int, clock: Clock
) -> None:
    """控制席接受一条待审改写提议（``/api/suggestions/{id}/accept`` 业务）。

    凭据与版本由端点在同锁 / 事务内先查（凭据 403、版本过期 409）。
    接受时目标提示必须**仍是待发**且当前内容**等于原文快照**：其他提示
    的编辑、暂停 / 恢复、重排（含把该提示移到别处）不使提议失效；只有
    该提示自身被改写（内容不同于快照）或已被手工 / 自动发出（撤回后再
    成为待发时内容为发出快照，同样按快照比对）才失效——此时抛
    Conflict 且**提议保持待审、数据不变**。提议不存在或已处理同样
    Conflict（不重复处理）。"写入建议内容 + 标记已接受 + 版本 +1"在
    同一事务提交；接受的是当前提示（待发队首）时，**即使建议文本与
    原文相同**也重开核验轮次、清空就绪并重新自动计时（与编辑当前提示
    同规则），接受其他提示不重置。接受改写该提示同时使其观众字幕
    **失效**（即使同文；与手工编辑同规则）。
    """
    meta = conn.execute("SELECT version FROM meta WHERE id = 1").fetchone()
    if version != meta["version"]:
        raise Conflict()
    sug = conn.execute(
        "SELECT * FROM suggestions WHERE id = ?", (suggestion_id,)
    ).fetchone()
    if sug is None or sug["status"] != "pending":
        # 不存在或已处理（含并发下另一请求先接受 / 拒绝）：409，不重复处理
        raise Conflict()
    row = conn.execute(
        "SELECT * FROM cues WHERE id = ?", (sug["cue_id"],)
    ).fetchone()
    if (
        row is None
        or row["status"] != "pending"
        or row["content"] != sug["original_content"]
    ):
        # 目标已发出 / 被改写成别的内容：接受会覆盖已变内容，拒绝生效，
        # 提议保持待审不变（可由控制席改走拒绝）。
        raise Conflict()

    now_iso = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE cues SET content = ? WHERE id = ?",
        (sug["content"], row["id"]),
    )
    # 该提示被改写：其观众字幕即使同文也失效（与手工编辑同规则）
    conn.execute(
        "UPDATE subtitles SET valid = 0 WHERE cue_id = ?", (row["id"],)
    )
    conn.execute(
        "UPDATE suggestions SET status = 'accepted', decided_at = ? WHERE id = ?",
        (now_iso, sug["id"]),
    )
    # 接受的是当前提示（待发队首）才重开核验轮次 / 重新自动计时；
    # 接受其他待发提示不影响当前轮次与计时。
    first = _first_pending(conn)
    if first is not None and first["id"] == row["id"]:
        verify_advance(conn)
        restart_timer(conn, clock)
    conn.execute("UPDATE meta SET version = version + 1 WHERE id = 1")
