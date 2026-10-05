"""舞台提示执行台 —— FastAPI + SQLite 后端。

提示变更业务集中在共用模块 :mod:`app.cueflow`：手工确认（manual_issue）、
自动发出（auto_issue_due）、撤回末条（withdraw_last）、编辑
（edit_pending）、重排（reorder_pending）、接受改写提议
（accept_pending_suggestion）都调用其中的实现——手工与自动**共用同一个
发出实现**（issue_current：内容快照 + 推进 + 版本 + 换轮 + 计时安排，
末条完成并关闭自动），不是各自重复一份。端点保持薄 HTTP 层：在
``with lock, db() as conn`` 内先做控制席身份检查（受保护写操作须凭据
匹配），再调用 cueflow 完成"版本 / 资格检查 + 写入"，因此身份、版本
与发出资格检查始终与写入在同一把写锁、同一事务内；提示快照、待发队列、
版本号、提议状态、核验轮次与自动计时随该事务一起提交或回滚。cueflow
抛 Conflict / BadRequest 时端点映射为 409（附最新状态）/ 400，HTTP
方法、路径、请求 / 响应字段与状态码均不变。

并发模型：所有写操作（确认 / 撤回末条 / 暂停 / 恢复 / 编辑 / 重排 / 归档 /
设置 / 问题记录 / 重置 / 以档案开新场）都在同一把锁内串行完成；其中带版本号
的操作（确认 / 撤回末条 / 暂停恢复 / 编辑 / 重排 / 归档 / 问题记录 /
以档案开新场）在锁内完成"检查版本号 -> 修改数据"两个步骤，因此同一版本号上
并发的两个操作只会有一个生效，另一个收到 409 及最新状态。归档的"写档案
（含问题记录快照）+ 清空提示与问题记录"、以档案开新场的"写入新场提示 +
推进状态"、暂停恢复的"暂停标记 + 版本 +1"、问题记录的"插入记录 + 版本 +1"、
撤回末条的"末条已发退回待发队首 + 完成态回到演出中 + 版本 +1"都在同一事务
内提交，任一步失败整体回滚。

单控制席：默认未启用，所有写操作保持旧行为。任意页面可凭当前版本号启用
或接管控制席，"启用/接管 + 新凭据 + 版本号 +1"在同一事务内落库；启用后
全部写操作（手工开场 / 确认 / 撤回末条 / 编辑 / 重排 / 暂停恢复 / 归档 /
重置 / 以档案开新场 / 问题记录）须携带当前凭据（请求头 X-Control-Token）。身份
检查与业务写入在同一把锁、同一事务内生效——缺失或已被接管替换的旧凭据
返回 403 且数据不变，不会出现"旧请求先通过检查、接管后再落库"的交错。
凭据只随成功的启用/接管响应返回，状态查询不含凭据；查询接口始终可用。
GET /api/state 接受可选的 X-Control-Token 头：响应的 control_granted 字段
如实回报"本请求"是否具备控制资格（未启用时恒为 true），供页面刷新时核验
缓存凭据——被接管后的旧凭据查询即得 false，无需先触发一次 403；核验是
纯查询，不泄露凭据，也不改变业务状态或版本号。

本场核验：默认关闭，开场前可凭版本号启用（受控制席保护，版本 +1）。启用后
灯光（lighting）、音响（sound）两工种各自对"当前提示 + 当前轮次"回报就绪：
POST /api/verify/report 接受可选 X-Control-Token 头——无需凭据也可回报，缺失
或已被接管替换的凭据不返回 403、不获得控制权；成功响应与 409 附带状态中的
control_granted 一律按本次凭据如实回报（凭据永不泄露），当前控制席回报后本页
资格与缓存凭据不受影响，旧页则按真实资格清除失效缓存。回报不改全局版本号，
仅接受演出中（含暂停）当前提示与当前轮次，过期返回 409 及最新状态（不写入）；
同一工种同一轮次重复回报以最后一次为准（覆盖写）。确认发出要求两工种本轮均已
报就绪，缺任一返回 409 及最新状态。确认推进、撤回末条、改写当前提示、重排
更换当前提示都会开启新轮次并清空本轮就绪（与业务写入同一事务，轮次单调递增
不可复用）；编辑其他提示、暂停恢复、接管不清空。轮次与就绪记录随数据库持久化
（刷新、重启保留）；归档或重置会关闭核验并清空轮次与就绪，下一场不继承，档案
结构不变。核验关闭时所有旧接口行为不变。

自动发出：默认关闭。演出中可经 POST /api/auto 设置统一间隔（1～60 秒整数）
与开关（携带版本号、受控制席保护，成功版本 +1；非演出中或版本过期 409，
非法间隔 400）。开启或改间隔后从完整间隔重新计时；服务端后台任务在同一
写锁内重新核对开关、演出状态、暂停标记、计时到期与两工种就绪后才发出
一条（与手工确认共用 app.cueflow 的同一发出实现：快照、版本推进与换轮
规则，不补发积压），无需页面保持打开。暂停冻结剩余时间、恢复续计；到期未就绪继续等。手工确认、撤回、
改写当前提示或重排换头都会重新计时；队首不变的重排与编辑其他提示不重置。
接管、归档、重置、完成或服务重启都会关闭自动模式；写入失败整体回滚，
不推进、不重置计时、不改版本。

改写提议：无控制权的页面可经 POST /api/suggestions 为**待发提示**提交改写
建议（提交 cue_id、建议内容与所见 version；仅演出中含暂停可提交，建议内容
沿用编辑接口的内容校验）。提交只保存**原文快照**与建议，**不改提示内容、
不改版本号**，也不需要控制凭据（与工种回报一样不返回 403）。控制席凭最新
版本号经 /api/suggestions/{id}/accept 与 /reject 审核（受凭据保护）：接受时
目标提示必须仍是待发且当前内容**等于原文快照**——其他提示的编辑、暂停、
重排不使提议失效；只有该提示自身被改写（内容变化）、被发出（含撤回后再
处理等）才使接受失效，返回 409 且提议不变。"写入建议内容 + 标记已接受 +
版本 +1"在同一事务内提交（接受的是当前提示时同时重开核验轮次、重新自动
计时）；拒绝仅把提议标记为已拒绝，**不改版本号**。版本过期或提议已处理
返回 409 并附最新状态、提议不变；与确认、编辑、撤回、暂停恢复、归档及
手工/自动发出交错时，锁 + 版本 + 快照检查保证不重复处理、不覆盖已变内容、
不留半次结果，写入失败整体不变。提议随数据库持久化（刷新、重启保留），
归档、重置、开新场（手工录入或以档案开新场）一并清空，**不写入档案**。

观众字幕屏：观众可打开独立页面 /screen（只读，经 GET /api/screen 轮询），
画面只显示**另行设置的字幕**，不显示提示正文等任何后台信息；接口只返回
当前字幕与跨场不归零的递增画面序号，迟到响应不能让画面倒退。执行台经
POST /api/subtitles 为**演出中待发 cue_id** 设置字幕（携带 version、受
控制席保护，文本去空白后 1～200 字符，null 清除；非法文本 400；非演出
中 / 目标非待发 / 版本过期 409 附最新状态），设置与版本 +1 同事务保存，
**不推进、不换核验轮次、不重新计时**，也不改变当前画面。字幕绑定提示
身份：手工编辑或接受提议改写该提示（即使同文）即令字幕失效，重排不
影响；手工或自动发出时有效字幕替换观众画面、否则清屏，画面与发出同
事务保存，失败均不变、不额外阻挡发出。撤回清屏但保留设置；归档、重置、
开新场（手工录入或以档案开新场）清屏并清除设置，字幕不进档案。设置与
画面随数据库持久化（重启保留，旧库迁移默认无设置、画面为空、序号 0）。
"""
import json
import os
import secrets
import sqlite3
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI, Header
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import cueflow
from app.cueflow import VERIFY_DEPARTMENTS

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "data", "cues.db"))

MIN_CUES = 2
MAX_CUES = 12
ISSUE_MIN_LEN = 1
ISSUE_MAX_LEN = 200

# 自动发出：统一间隔的合法范围（秒，缺省 10 见 meta.auto_interval 列默认值）；
# 后台任务的唤醒周期
AUTO_MIN_INTERVAL = 1
AUTO_MAX_INTERVAL = 60
AUTO_POLL_SECONDS = 0.2

# 串行化所有写操作，保证版本检查与落库是原子的。
lock = threading.Lock()


def _clock() -> float:
    """自动计时使用的当前时间（epoch 秒）。

    与记录用的 datetime.now 分开：计时走这个可替换的时钟，回归测试用可控
    时钟精确驱动"等满间隔 / 暂停冻结 / 到期未就绪"等交错场景；生产环境
    即真实时间。
    """
    return time.time()


def _connect() -> sqlite3.Connection:
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def db():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                id      INTEGER PRIMARY KEY CHECK (id = 1),
                version INTEGER NOT NULL DEFAULT 0,
                status  TEXT NOT NULL DEFAULT 'setup',  -- setup | live | done
                paused  INTEGER NOT NULL DEFAULT 0,     -- 演出中是否暂停发出
                control_enabled INTEGER NOT NULL DEFAULT 0,  -- 是否启用单控制席
                control_token   TEXT,                   -- 当前控制席凭据（未启用为 NULL）
                verify_enabled  INTEGER NOT NULL DEFAULT 0,  -- 是否启用本场核验
                verify_round    INTEGER NOT NULL DEFAULT 0,  -- 当前核验轮次（单调递增）
                auto_enabled    INTEGER NOT NULL DEFAULT 0,  -- 是否开启自动发出
                auto_interval   INTEGER NOT NULL DEFAULT 10, -- 统一间隔（秒，1～60）
                auto_deadline   REAL,     -- 当前提示到期时刻（epoch 秒）；暂停/关闭为 NULL
                auto_remaining  REAL,     -- 暂停时冻结的剩余秒数；未暂停为 NULL
                screen_text     TEXT,     -- 观众字幕屏当前画面（清屏为 NULL）
                screen_seq      INTEGER NOT NULL DEFAULT 0  -- 画面序号：跨场递增、从不归零
            );
            CREATE TABLE IF NOT EXISTS cues (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                number         TEXT NOT NULL,           -- 唯一编号
                content        TEXT NOT NULL,           -- 当前（可改写的）内容
                position       INTEGER NOT NULL,        -- 待发顺序
                status         TEXT NOT NULL DEFAULT 'pending',  -- pending | issued
                issued_content TEXT,                    -- 发出那一刻的内容快照
                issued_at      TEXT,
                issued_seq     INTEGER,                 -- 发出顺序
                subtitle       TEXT,                    -- 绑定该提示的观众字幕设置（未设为 NULL）
                subtitle_valid INTEGER NOT NULL DEFAULT 0  -- 字幕设置是否仍有效（手工编辑/接受提议失效）
            );
            CREATE TABLE IF NOT EXISTS issues (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,  -- 同时即保存顺序
                cue_id     INTEGER NOT NULL,           -- 记录关联的本场提示
                cue_number TEXT NOT NULL,              -- 记录当时的编号快照
                cue_status TEXT NOT NULL,              -- 记录当时状态：pending | issued
                cue_content TEXT NOT NULL,             -- 编号指向内容：已发为发出快照
                text       TEXT NOT NULL,              -- 现场问题正文（已去除首尾空白）
                created_at TEXT NOT NULL               -- 服务端保存时间（UTC ISO）
            );
            CREATE TABLE IF NOT EXISTS verify_reports (
                round      INTEGER NOT NULL,           -- 所属核验轮次（仅当前轮次有记录）
                department TEXT NOT NULL,              -- lighting | sound
                cue_id     INTEGER NOT NULL,           -- 回报针对的当前提示
                ready      INTEGER NOT NULL,           -- 0 未就绪 | 1 就绪
                updated_at TEXT NOT NULL,              -- 服务端保存时间（UTC ISO）
                PRIMARY KEY (round, department)        -- 同工种同轮次重复回报覆盖写
            );
            CREATE TABLE IF NOT EXISTS suggestions (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,  -- 同时即保存顺序
                cue_id         INTEGER NOT NULL,        -- 提议针对的本场提示
                cue_number     TEXT NOT NULL,           -- 提交时编号快照（列表展示用）
                original_content TEXT NOT NULL,         -- 提交时原文快照（接受时比对）
                content        TEXT NOT NULL,           -- 建议内容（已去除首尾空白）
                status         TEXT NOT NULL DEFAULT 'pending',  -- pending | accepted | rejected
                created_at     TEXT NOT NULL,           -- 提交时间（UTC ISO）
                decided_at     TEXT                     -- 接受 / 拒绝时间（UTC ISO）
            );
            CREATE TABLE IF NOT EXISTS archives (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,              -- 场次名称
                archived_at TEXT NOT NULL,              -- 归档时间（UTC ISO）
                status      TEXT NOT NULL,              -- 归档当时状态：live | done
                version     INTEGER NOT NULL,           -- 归档时携带的页面版本
                issued      TEXT NOT NULL,              -- 已发：编号/快照/时间（按发出顺序）
                pending     TEXT NOT NULL,             -- 剩余：编号/内容（按待发顺序）
                issues      TEXT NOT NULL DEFAULT '[]' -- 现场问题记录（按保存顺序）
            );
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO meta (id, version, status) VALUES (1, 0, 'setup')"
        )
        # 旧库升级：meta 表缺少 paused 列时补上，已有数据默认未暂停。
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(meta)")}
        if "paused" not in columns:
            conn.execute(
                "ALTER TABLE meta ADD COLUMN paused INTEGER NOT NULL DEFAULT 0"
            )
        # 旧库升级：meta 表缺少控制席列时补上，已有数据默认未启用、无凭据。
        if "control_enabled" not in columns:
            conn.execute(
                "ALTER TABLE meta ADD COLUMN control_enabled INTEGER NOT NULL DEFAULT 0"
            )
        if "control_token" not in columns:
            conn.execute("ALTER TABLE meta ADD COLUMN control_token TEXT")
        # 旧库升级：meta 表缺少本场核验列时补上，已有数据默认未启用、轮次为 0。
        if "verify_enabled" not in columns:
            conn.execute(
                "ALTER TABLE meta ADD COLUMN verify_enabled INTEGER NOT NULL DEFAULT 0"
            )
        if "verify_round" not in columns:
            conn.execute(
                "ALTER TABLE meta ADD COLUMN verify_round INTEGER NOT NULL DEFAULT 0"
            )
        # 旧库升级：meta 表缺少自动发出列时补上，已有数据默认关闭、间隔取缺省值。
        if "auto_enabled" not in columns:
            conn.execute(
                "ALTER TABLE meta ADD COLUMN auto_enabled INTEGER NOT NULL DEFAULT 0"
            )
        if "auto_interval" not in columns:
            conn.execute(
                "ALTER TABLE meta ADD COLUMN auto_interval "
                "INTEGER NOT NULL DEFAULT 10"
            )
        if "auto_deadline" not in columns:
            conn.execute("ALTER TABLE meta ADD COLUMN auto_deadline REAL")
        if "auto_remaining" not in columns:
            conn.execute("ALTER TABLE meta ADD COLUMN auto_remaining REAL")
        # 旧库升级：meta 表缺少观众画面列时补上：旧库默认清屏、画面序号从 0 起。
        if "screen_text" not in columns:
            conn.execute("ALTER TABLE meta ADD COLUMN screen_text TEXT")
        if "screen_seq" not in columns:
            conn.execute(
                "ALTER TABLE meta ADD COLUMN screen_seq INTEGER NOT NULL DEFAULT 0"
            )
        # 旧库升级：cues 表缺少观众字幕列时补上：旧提示默认无字幕设置、无效。
        cue_columns = {r["name"] for r in conn.execute("PRAGMA table_info(cues)")}
        if "subtitle" not in cue_columns:
            conn.execute("ALTER TABLE cues ADD COLUMN subtitle TEXT")
        if "subtitle_valid" not in cue_columns:
            conn.execute(
                "ALTER TABLE cues ADD COLUMN subtitle_valid INTEGER NOT NULL DEFAULT 0"
            )
        # 旧库升级：archives 表缺少 issues 列时补上，旧档案默认无问题记录。
        archive_columns = {
            r["name"] for r in conn.execute("PRAGMA table_info(archives)")
        }
        if "issues" not in archive_columns:
            conn.execute("ALTER TABLE archives ADD COLUMN issues TEXT NOT NULL DEFAULT '[]'")


# ---------------------------------------------------------------- 状态组装

def verify_view(
    conn: sqlite3.Connection, meta: sqlite3.Row, current_id: Optional[int]
) -> dict:
    """本场核验视图：当前轮次与两工种（灯光/音响）对当前提示的就绪回报。

    工种字段取值为 None（未回报）/ False（已回报未就绪）/ True（已报就绪）；
    仅当前轮次有回报记录（开启新轮次时旧记录已清空）。核验关闭时返回固定
    关闭视图，旧客户端可整体忽略该字段。
    """
    if not meta["verify_enabled"]:
        return {
            "enabled": False,
            "round": 0,
            "cue_id": None,
            "lighting": None,
            "sound": None,
            "all_ready": False,
        }
    reports = {
        r["department"]: bool(r["ready"])
        for r in conn.execute(
            "SELECT department, ready FROM verify_reports WHERE round = ?",
            (meta["verify_round"],),
        ).fetchall()
    }
    lighting = reports.get("lighting")
    sound = reports.get("sound")
    return {
        "enabled": True,
        "round": meta["verify_round"],
        "cue_id": current_id,
        "lighting": lighting,
        "sound": sound,
        "all_ready": lighting is True and sound is True,
    }


def verify_reset_for_new_show(conn: sqlite3.Connection) -> None:
    """开场（手工录入 / 以档案开新场）时的核验初始化：启用则开第 1 轮，
    未启用则轮次归零；清空可能残留的就绪记录。与开场写入同一事务。"""
    meta = conn.execute("SELECT verify_enabled FROM meta WHERE id = 1").fetchone()
    conn.execute("DELETE FROM verify_reports")
    conn.execute(
        "UPDATE meta SET verify_round = ? WHERE id = 1",
        (1 if meta["verify_enabled"] else 0,),
    )


def verify_close(conn: sqlite3.Connection) -> None:
    """关闭核验并清空（归档 / 重置）：下一场不继承。与收场写入同一事务。"""
    conn.execute("DELETE FROM verify_reports")
    conn.execute(
        "UPDATE meta SET verify_enabled = 0, verify_round = 0 WHERE id = 1"
    )


# ---------------------------------------------------------------- 自动发出

def auto_view(meta: sqlite3.Row, all_ready: bool) -> dict:
    """自动发出视图：开关、间隔、剩余秒数或"等待就绪"标记。

    剩余秒数：未暂停时为到期时刻减去当前时间（冻结前不为负）；暂停时为
    冻结的剩余时间。到期而核验门槛未满足时 waiting_ready 为 True（继续等，
    不重置计时）。关闭时剩余秒数为 None，旧客户端可整体忽略该字段。
    """
    enabled = bool(meta["auto_enabled"])
    remaining = None
    waiting_ready = False
    if enabled:
        if meta["paused"]:
            frozen = meta["auto_remaining"]
            remaining = max(0.0, frozen) if frozen is not None else None
        elif meta["auto_deadline"] is not None:
            remaining = max(0.0, meta["auto_deadline"] - _clock())
            if (
                remaining <= 0
                and meta["verify_enabled"]
                and not all_ready
            ):
                waiting_ready = True
    return {
        "enabled": enabled,
        "interval": meta["auto_interval"],
        "remaining_seconds": round(remaining, 1) if remaining is not None else None,
        "waiting_ready": waiting_ready,
    }


# 计时变更（冻结 / 续计 / 重计 / 关闭）与自动发出动作均在共用模块
# app.cueflow 内实现，这里保留薄包装并注入服务端时钟：生产即真实
# time.time，回归测试可替换 main._clock 为可控时钟精确驱动。

def auto_restart_timer(conn: sqlite3.Connection) -> None:
    """当前提示更换（或开启 / 改间隔）后重新计时：委托 cueflow。"""
    cueflow.restart_timer(conn, _clock)


def auto_freeze(conn: sqlite3.Connection) -> None:
    """暂停发出冻结剩余时间：委托 cueflow（与暂停写入同一事务）。"""
    cueflow.freeze_timer(conn, _clock)


def auto_thaw(conn: sqlite3.Connection) -> None:
    """恢复发出按冻结剩余续计：委托 cueflow（同一事务）。"""
    cueflow.thaw_timer(conn, _clock)


def auto_disable(conn: sqlite3.Connection) -> None:
    """关闭自动模式并清空计时：委托 cueflow（接管 / 归档 / 重置 / 完成 /
    服务重启；间隔保留）。"""
    cueflow.disable_auto(conn)


def auto_tick() -> bool:
    """后台定时任务：委托共用模块自动发出（与手工确认共用发出实现）。

    与手工操作共用同一把写锁：cueflow.auto_issue_due 在锁 / 事务内重新
    核对自动开关、演出状态、暂停标记、计时到期与两工种就绪，任一不满足
    即不发出；通过检查后与手工确认走同一个 cueflow.issue_current
    （内容快照、版本推进、换轮、末条关自动、下一条重计时），不会重复
    发出或跳条，每次最多一条，下一条从完整间隔重新计时，不补发积压。
    返回本次是否发出了提示。
    """
    with lock, db() as conn:
        return cueflow.auto_issue_due(conn, clock=_clock)


# 后台任务：周期唤醒执行 auto_tick，无需页面保持打开。测试可换可控时钟后
# 直接调用 auto_tick 精确驱动；也可用 AUTO_WORKER=0 关闭后台线程。
_auto_worker: Optional[threading.Thread] = None
_auto_worker_stop = threading.Event()


def _auto_worker_loop() -> None:
    while not _auto_worker_stop.wait(AUTO_POLL_SECONDS):
        try:
            auto_tick()
        except Exception:
            # 后台任务失败不影响服务与页面：下一周期重试（写入失败已随
            # 事务回滚，不会推进、重置计时或改版本）
            pass


def start_auto_worker() -> None:
    global _auto_worker
    if _auto_worker is not None and _auto_worker.is_alive():
        return
    _auto_worker_stop.clear()
    _auto_worker = threading.Thread(
        target=_auto_worker_loop, name="cue-auto-tick", daemon=True
    )
    _auto_worker.start()


def stop_auto_worker() -> None:
    global _auto_worker
    _auto_worker_stop.set()
    if _auto_worker is not None:
        _auto_worker.join(timeout=2)
        _auto_worker = None


def issue_json(r: sqlite3.Row) -> dict:
    """一条现场问题记录；已发提示的 cue_content 取发出快照，不可再变。"""
    return {
        "id": r["id"],
        "cue_id": r["cue_id"],
        "cue_number": r["cue_number"],
        "cue_status": r["cue_status"],
        "cue_content": r["cue_content"],
        "text": r["text"],
        "created_at": r["created_at"],
    }


def issue_rows(conn: sqlite3.Connection) -> list[dict]:
    """本场现场问题记录，按保存顺序（自增 id）返回。"""
    return [
        issue_json(r)
        for r in conn.execute("SELECT * FROM issues ORDER BY id ASC").fetchall()
    ]


def suggestion_json(conn: sqlite3.Connection, r: sqlite3.Row) -> dict:
    """一条改写提议视图。

    target_valid 表示"现在接受能否生效"：目标提示仍是待发且当前内容等于
    提交时的原文快照。其他提示的编辑、暂停、重排不影响该值；只有目标提示
    自身被改写（内容变化）或已发出（撤回后再成为待发时内容是发出快照，
    通常也与原文不同）才会失效。该字段为派生值，不写库。
    """
    cue = conn.execute(
        "SELECT status, content FROM cues WHERE id = ?", (r["cue_id"],)
    ).fetchone()
    target_valid = (
        cue is not None
        and cue["status"] == "pending"
        and cue["content"] == r["original_content"]
    )
    return {
        "id": r["id"],
        "cue_id": r["cue_id"],
        "cue_number": r["cue_number"],
        "original_content": r["original_content"],
        "content": r["content"],
        "status": r["status"],
        "target_valid": target_valid if r["status"] == "pending" else False,
        "created_at": r["created_at"],
        "decided_at": r["decided_at"],
    }


def suggestion_rows(conn: sqlite3.Connection) -> list[dict]:
    """本场改写提议，按保存顺序（自增 id）返回。"""
    return [
        suggestion_json(conn, r)
        for r in conn.execute("SELECT * FROM suggestions ORDER BY id ASC").fetchall()
    ]


def clear_suggestions(conn: sqlite3.Connection) -> None:
    """归档 / 重置 / 开新场清空本场改写提议（不写入档案）。"""
    conn.execute("DELETE FROM suggestions")


def clear_subtitles_and_screen(conn: sqlite3.Connection) -> None:
    """归档 / 重置 / 开新场：清除全部字幕设置并清屏（不写入档案）。

    与收场 / 开场写入同一事务：删除本场提示（DELETE FROM cues）即连同
    绑定的字幕一起消失，这里再清画面、推进跨场递增的画面序号，使观众
    屏轮询能立刻看到清屏（旧帧不会因新场还未发出而残留）。
    """
    cueflow.clear_screen(conn)


def build_state(conn: sqlite3.Connection, token: Optional[str] = None) -> dict:
    meta = conn.execute(
        "SELECT version, status, paused, control_enabled, control_token, "
        "verify_enabled, verify_round, auto_enabled, auto_interval, "
        "auto_deadline, auto_remaining, screen_text, screen_seq "
        "FROM meta WHERE id = 1"
    ).fetchone()
    rows = conn.execute(
        "SELECT * FROM cues ORDER BY position ASC, id ASC"
    ).fetchall()
    pending = [r for r in rows if r["status"] == "pending"]
    issued = sorted(
        (r for r in rows if r["status"] == "issued"),
        key=lambda r: r["issued_seq"],
    )

    def pending_json(r):
        return {
            "id": r["id"],
            "number": r["number"],
            "content": r["content"],
            # 绑定该提示的观众字幕设置（未设置为 None）与当前有效性：
            # 手工编辑 / 接受提议改写该提示后即使同文也失效；重排不影响。
            "subtitle": r["subtitle"],
            "subtitle_valid": bool(r["subtitle_valid"])
            if r["subtitle"] is not None else False,
        }

    def issued_json(r):
        return {
            "id": r["id"],
            "number": r["number"],
            "content": r["issued_content"],
            "issued_at": r["issued_at"],
        }

    control_enabled = bool(meta["control_enabled"])
    verify = verify_view(conn, meta, pending[0]["id"] if pending else None)
    return {
        "version": meta["version"],
        "status": meta["status"],
        "paused": bool(meta["paused"]),
        # 是否启用单控制席；凭据永不随状态返回，只在启用/接管成功时下发一次。
        "control_enabled": control_enabled,
        # 本请求（凭其携带的 X-Control-Token）是否具备控制资格：未启用时任何
        # 调用可写（恒 true）；启用后须凭据与当前控制席一致。被接管后的旧
        # 凭据查询即得 false，页面据此只读，无需先触发一次 403。
        "control_granted": (not control_enabled)
        or (token is not None and meta["control_token"] == token),
        "current": pending_json(pending[0]) if pending else None,
        "next": pending_json(pending[1]) if len(pending) > 1 else None,
        "pending": [pending_json(r) for r in pending],
        "issued": [issued_json(r) for r in issued],
        "issues": issue_rows(conn),
        # 本场改写提议（无控制权页面提交、控制席审核）：原文/建议/处理状态
        "suggestions": suggestion_rows(conn),
        # 本场核验：当前轮次与两工种就绪状态；关闭时为固定关闭视图
        "verify": verify,
        # 自动发出：开关、间隔、剩余秒数或等待就绪；关闭时为固定关闭视图
        "auto": auto_view(meta, verify["all_ready"]),
        # 观众字幕屏当前画面：subtitle 为上屏字幕（清屏为 null），seq 为
        # 跨场递增、从不归零的画面序号（观众屏据此丢弃迟到的旧响应）。
        "screen": {"subtitle": meta["screen_text"], "seq": meta["screen_seq"]},
        "total": len(rows),
        "remaining": len(pending),
        "min_cues": MIN_CUES,
        "max_cues": MAX_CUES,
    }


def conflict(
    conn: sqlite3.Connection,
    token: Optional[str] = None,
    message: str = "操作已过期",
) -> JSONResponse:
    """409：请求已过期（或未满足前置条件），附带最新状态供页面刷新
    （含本请求的控制资格与本场核验状态）。"""
    return JSONResponse(
        status_code=409,
        content={
            "error": "conflict",
            "message": message,
            "state": build_state(conn, token),
        },
    )


def control_check(conn: sqlite3.Connection, token: Optional[str]) -> Optional[JSONResponse]:
    """启用控制席后，写操作须携带当前凭据（请求头 X-Control-Token）。

    必须在写锁内调用：身份检查与随后的业务写入在同一把锁、同一事务内生效，
    不会出现"旧请求先通过检查、接管后再落库"的交错。凭据缺失或已被接管
    替换时返回 403（附最新状态），数据不做任何修改；未启用时一律放行，
    旧调用不变。
    """
    meta = conn.execute(
        "SELECT control_enabled, control_token FROM meta WHERE id = 1"
    ).fetchone()
    if meta["control_enabled"] and meta["control_token"] != token:
        return JSONResponse(
            status_code=403,
            content={
                "error": "forbidden",
                "message": "本页面不是当前控制席：写操作未生效，可凭最新版本号接管",
                # 凭据缺失或失效，状态中的 control_granted 自然为 false
                "state": build_state(conn, token),
            },
        )
    return None


def bad_request(message: str) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": message})


def cue_list_error(cues: list[tuple[str, str]]) -> Optional[str]:
    """校验提示清单：2～12 条、编号唯一非空、内容非空。

    入参为 (number, content) 对；通过返回 None，失败返回中文错误说明。
    """
    if not (MIN_CUES <= len(cues) <= MAX_CUES):
        return f"提示数量需在 {MIN_CUES}～{MAX_CUES} 条之间"
    numbers = [n.strip() for n, _ in cues]
    if any(not n for n in numbers):
        return "编号不能为空"
    if len(set(numbers)) != len(numbers):
        return "编号必须唯一"
    if any(not c.strip() for _, c in cues):
        return "内容不能为空"
    return None


# ---------------------------------------------------------------- 档案组装

def archive_snapshots(conn: sqlite3.Connection) -> tuple[list[dict], list[dict], list[dict]]:
    """读取归档快照：已发按发出顺序、剩余按待发顺序、问题记录按保存顺序。"""
    rows = conn.execute("SELECT * FROM cues ORDER BY position ASC, id ASC").fetchall()
    issued = [
        {
            "number": r["number"],
            "content": r["issued_content"],
            "issued_at": r["issued_at"],
        }
        for r in sorted(
            (r for r in rows if r["status"] == "issued"),
            key=lambda r: r["issued_seq"],
        )
    ]
    pending = [
        {"number": r["number"], "content": r["content"]}
        for r in rows
        if r["status"] == "pending"
    ]
    return issued, pending, issue_rows(conn)


def archive_summary(row: sqlite3.Row) -> dict:
    issued = json.loads(row["issued"])
    pending = json.loads(row["pending"])
    issues = json.loads(row["issues"]) if row["issues"] is not None else []
    return {
        "id": row["id"],
        "name": row["name"],
        "archived_at": row["archived_at"],
        "status": row["status"],
        "issued_count": len(issued),
        "pending_count": len(pending),
        "issue_count": len(issues),
    }


def archive_detail(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "archived_at": row["archived_at"],
        "status": row["status"],
        "version": row["version"],
        "issued": json.loads(row["issued"]),
        "pending": json.loads(row["pending"]),
        # 旧档案（迁移前建立）该列为 NULL，视为无问题记录。
        "issues": json.loads(row["issues"]) if row["issues"] is not None else [],
    }


def archive_cues(row: sqlite3.Row) -> list[tuple[str, str]]:
    """档案转新场提示：先按已发顺序取编号与内容快照，再接按待发顺序的
    编号与内容；不继承发出时间或进度（全部作为新场待发提示）。"""
    issued = json.loads(row["issued"])
    pending = json.loads(row["pending"])
    return [(c["number"], c["content"]) for c in issued] + [
        (c["number"], c["content"]) for c in pending
    ]


# ---------------------------------------------------------------- 场次对照

MISSING_SIDE = {"status": "missing", "content": None, "offset_seconds": None}


def parse_time(iso_text: Optional[str]) -> Optional[datetime]:
    if not iso_text:
        return None
    return datetime.fromisoformat(iso_text.replace("Z", "+00:00"))


def archive_side_view(row: sqlite3.Row) -> dict[str, dict]:
    """档案 -> {编号: 一侧视图}：已发给内容快照与相对零点秒数，未发给内容。

    零点为该场首条已发提示的发出时间；相对秒数保留毫秒，暂停时长不扣除、
    直接计入时间差。未发条目、或已发但缺发出时间的条目秒数为 None。
    """
    view: dict[str, dict] = {}
    issued = json.loads(row["issued"])
    zero = parse_time(issued[0].get("issued_at")) if issued else None
    for cue in issued:
        at = parse_time(cue.get("issued_at"))
        offset = None
        if zero is not None and at is not None:
            offset = round((at - zero).total_seconds(), 3)
        view[cue["number"]] = {
            "status": "issued",
            "content": cue["content"],
            "offset_seconds": offset,
        }
    for cue in json.loads(row["pending"]):
        view[cue["number"]] = {
            "status": "pending",
            "content": cue["content"],
            "offset_seconds": None,
        }
    return view


def compare_rows(base_view: dict[str, dict], compare_view: dict[str, dict]) -> list[dict]:
    """按编号精确匹配取并集（编号字符串升序），逐条给出两侧状态与秒数差。

    仅两侧均已发出且内容完全相同时计算"对照减基准"的秒数差（正值表示
    对照场更晚），其余为 None 并附中文原因。
    """
    rows = []
    for number in sorted(set(base_view) | set(compare_view)):
        base = base_view.get(number)
        comp = compare_view.get(number)
        delta = None
        reason = None
        if (
            base is not None
            and comp is not None
            and base["status"] == "issued"
            and comp["status"] == "issued"
        ):
            if base["content"] != comp["content"]:
                reason = "两侧均已发出，但内容不一致"
            elif base["offset_seconds"] is None or comp["offset_seconds"] is None:
                reason = "缺少发出时间，无法计算秒数差"
            else:
                delta = round(comp["offset_seconds"] - base["offset_seconds"], 3)
        else:
            parts = []
            if base is None:
                parts.append("基准场无此编号")
            elif base["status"] != "issued":
                parts.append("基准场未发出")
            if comp is None:
                parts.append("对照场无此编号")
            elif comp["status"] != "issued":
                parts.append("对照场未发出")
            reason = "；".join(parts)
        rows.append(
            {
                "number": number,
                "base": base if base is not None else dict(MISSING_SIDE),
                "compare": comp if comp is not None else dict(MISSING_SIDE),
                "delta_seconds": delta,
                "delta_reason": reason,
            }
        )
    return rows


# ---------------------------------------------------------------- 应用

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    with db() as conn:
        # 服务重启关闭自动模式：开关与计时清空（间隔保留），下一场如需重开
        auto_disable(conn)
    if os.environ.get("AUTO_WORKER", "1") != "0":
        start_auto_worker()
    yield
    stop_auto_worker()


app = FastAPI(title="舞台提示执行台", lifespan=lifespan)


class CueIn(BaseModel):
    number: str
    content: str


class SetupIn(BaseModel):
    cues: list[CueIn]


class VersionedIn(BaseModel):
    version: int


class PauseIn(BaseModel):
    version: int
    paused: bool  # 目标状态：true 暂停发出，false 恢复发出


class ArchiveIn(BaseModel):
    version: int
    name: str


class EditIn(BaseModel):
    version: int
    cue_id: int
    content: str
    number: Optional[str] = None


class ReorderIn(BaseModel):
    version: int
    order: list[int]


class IssueIn(BaseModel):
    version: int
    cue_id: int
    text: str


class WithdrawIn(BaseModel):
    version: int
    cue_id: int  # 页面所见的最后一条已发提示 id


class StartFromArchiveIn(BaseModel):
    version: int


class VerifyReportIn(BaseModel):
    department: str  # lighting（灯光）| sound（音响）
    cue_id: int      # 回报针对的当前提示 id
    round: int       # 页面所见的当前核验轮次
    ready: bool      # 就绪布尔值


class AutoIn(BaseModel):
    version: int
    enabled: bool            # 目标开关：true 开启自动发出，false 关闭
    interval: Optional[Any] = None  # 统一间隔（秒）：1～60 整数，缺省沿用已存间隔


class SuggestionIn(BaseModel):
    version: int    # 页面所见版本号（提交不改版本，但仍做过期检查）
    cue_id: int     # 提议针对的待发提示 id
    content: str    # 建议内容（沿用编辑接口的内容非空校验，去除首尾空白）


class SuggestionDecisionIn(BaseModel):
    version: int    # 控制席审核时携带的最新版本号


class SubtitleIn(BaseModel):
    version: int               # 执行台所见版本号
    cue_id: int                # 目标待发提示 id（字幕绑定提示身份）
    # 用 Any 而非 str：非字符串（数字 / 数组 / 对象）不交给 pydantic 422，
    # 统一由业务校验返回 400；显式 null（Python None）表示清除字幕设置。
    subtitle: Optional[Any]    # 字幕文本（去空白 1～200 字符）；null 清除设置


def auto_interval_error(interval: object) -> Optional[str]:
    """校验自动间隔：1～60 的整数（布尔与带小数的数都不合法）。

    通过返回 None，失败返回中文错误说明（接口据此返回 400，先于版本检查）。
    """
    if isinstance(interval, bool):
        return f"自动间隔须为 {AUTO_MIN_INTERVAL}～{AUTO_MAX_INTERVAL} 秒的整数"
    if isinstance(interval, int):
        value = interval
    elif isinstance(interval, float) and interval.is_integer():
        value = int(interval)
    else:
        return f"自动间隔须为 {AUTO_MIN_INTERVAL}～{AUTO_MAX_INTERVAL} 秒的整数"
    if not (AUTO_MIN_INTERVAL <= value <= AUTO_MAX_INTERVAL):
        return f"自动间隔须为 {AUTO_MIN_INTERVAL}～{AUTO_MAX_INTERVAL} 秒的整数"
    return None


@app.get("/api/state")
def get_state(x_control_token: Optional[str] = Header(default=None)):
    """查询当前状态（只读，不改变业务状态或版本号）。

    接受可选的 X-Control-Token 头：响应的 control_granted 如实回报本请求的
    控制资格（未启用时恒 true；启用后须凭据匹配），供页面刷新时核验缓存
    凭据。凭据缺失或失效同样返回 200，响应永不包含凭据本身。
    """
    with db() as conn:
        return build_state(conn, x_control_token)


@app.get("/api/archives")
def list_archives():
    """档案列表：按归档时间倒序、同时间按档案 ID 倒序。"""
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM archives ORDER BY archived_at DESC, id DESC"
        ).fetchall()
        return {"archives": [archive_summary(r) for r in rows]}


@app.get("/api/archives/{archive_id}")
def get_archive(archive_id: int):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM archives WHERE id = ?", (archive_id,)
        ).fetchone()
        if row is None:
            return JSONResponse(status_code=404, content={"error": "archive_not_found"})
        return archive_detail(row)


@app.get("/api/compare")
def compare_archives(base: int, compare: int):
    """对照两场档案的提示发出节奏（只读，任意演出状态可用）。

    按编号精确匹配取两场提示的并集，按编号字符串升序返回；每条列出两侧
    内容快照、已发/未发/缺失状态与相对发出秒数。各场以首条已发提示的
    发出时间为零点，使用档案保存的发出时间，保留小数秒，暂停时长计入；
    未发、缺失或整场无已发记录时秒数为 null。仅两侧均已发出且内容完全
    相同时计算"对照减基准"的秒数差（正值表示对照场更晚），其余为 null
    并附中文原因。任一档案不存在返回 404。查询不修改档案、当前演出或
    版本号。
    """
    with db() as conn:
        sides = {}
        for key, archive_id in (("base", base), ("compare", compare)):
            row = conn.execute(
                "SELECT * FROM archives WHERE id = ?", (archive_id,)
            ).fetchone()
            if row is None:
                return JSONResponse(
                    status_code=404, content={"error": "archive_not_found"}
                )
            sides[key] = row

    views = {key: archive_side_view(row) for key, row in sides.items()}
    return {
        "base": archive_summary(sides["base"]),
        "compare": archive_summary(sides["compare"]),
        "rows": compare_rows(views["base"], views["compare"]),
    }


# ---------------------------------------------------------------- 单控制席

@app.post("/api/control/enable")
def enable_control(
    body: VersionedIn, x_control_token: Optional[str] = Header(default=None)
):
    """启用单控制席（仅当前未启用时）。

    任意页面凭当前版本号即可启用；成功则生成随机凭据，"启用标记 + 凭据 +
    版本号 +1"在同一事务内落库。凭据仅随本响应返回一次（状态查询永远不含
    凭据），此后所有写操作须携带该凭据（请求头 X-Control-Token）。已启用
    或版本过期返回 409 及最新状态，原控制席与版本不变；同版本的启用 /
    接管 / 业务写操作最多一个生效。启用不改变提示、进度、暂停标记与档案。
    """
    with lock, db() as conn:
        meta = conn.execute(
            "SELECT version, control_enabled FROM meta WHERE id = 1"
        ).fetchone()
        if meta["control_enabled"] or body.version != meta["version"]:
            return conflict(conn, x_control_token)
        token = secrets.token_urlsafe(32)
        conn.execute(
            "UPDATE meta SET control_enabled = 1, control_token = ?, "
            "version = version + 1 WHERE id = 1",
            (token,),
        )
        state = build_state(conn)
        state["control_token"] = token  # 凭据仅随成功的启用/接管响应返回
        state["control_granted"] = True  # 本响应的接收者即新控制席
        return state


@app.post("/api/control/takeover")
def takeover_control(
    body: VersionedIn, x_control_token: Optional[str] = Header(default=None)
):
    """接管控制席（仅已启用时）：旧凭据立即失效。

    任意页面凭当前版本号即可主动接管；成功则换发随机凭据，"新凭据 + 版本号
    +1"在同一事务内落库，旧席即使用最新版本号也会因凭据失效被 403 拒绝。
    未启用或版本过期返回 409 及最新状态，原控制席与版本不变；同版本的
    接管最多一个成功。接管不改变提示、进度、暂停标记与档案。
    """
    with lock, db() as conn:
        meta = conn.execute(
            "SELECT version, control_enabled FROM meta WHERE id = 1"
        ).fetchone()
        if not meta["control_enabled"] or body.version != meta["version"]:
            return conflict(conn, x_control_token)
        token = secrets.token_urlsafe(32)
        conn.execute(
            "UPDATE meta SET control_token = ?, version = version + 1 WHERE id = 1",
            (token,),
        )
        # 接管即关闭自动模式（同一事务）：自动发出只随原控制席会话存在
        auto_disable(conn)
        state = build_state(conn)
        state["control_token"] = token  # 凭据仅随成功的启用/接管响应返回
        state["control_granted"] = True  # 本响应的接收者即新控制席
        return state


# ---------------------------------------------------------------- 本场核验

@app.post("/api/verify/enable")
def enable_verify(
    body: VersionedIn, x_control_token: Optional[str] = Header(default=None)
):
    """启用本场核验（仅开场前，默认关闭）。

    凭当前版本号启用，受单控制席保护（启用控制席后须携带当前凭据）；
    "启用标记 + 版本号 +1"在同一事务内落库，轮次归零、清空可能残留的就绪
    记录。已启用、非开场前或版本过期返回 409 及最新状态，原设置与版本不变；
    同版本的启用与其他写操作最多一个生效。启用不改变提示与档案；开场（手工
    录入 / 以档案开新场）时从第 1 轮开始，归档或重置会关闭核验，下一场
    不继承。
    """
    with lock, db() as conn:
        forbidden = control_check(conn, x_control_token)
        if forbidden:
            return forbidden
        meta = conn.execute(
            "SELECT version, status, verify_enabled FROM meta WHERE id = 1"
        ).fetchone()
        if (
            meta["verify_enabled"]
            or meta["status"] != "setup"
            or body.version != meta["version"]
        ):
            return conflict(conn, x_control_token)
        conn.execute("DELETE FROM verify_reports")  # 防御：启用即清空旧就绪
        conn.execute(
            "UPDATE meta SET verify_enabled = 1, verify_round = 0, "
            "version = version + 1 WHERE id = 1"
        )
        return build_state(conn, x_control_token)


@app.post("/api/verify/report")
def verify_report(
    body: VerifyReportIn, x_control_token: Optional[str] = Header(default=None)
):
    """工种就绪回报：灯光 / 音响对当前提示、当前轮次报就绪或未就绪。

    接受可选 X-Control-Token：回报是工种操作，无需控制凭据（即使已启用
    单控制席）——缺失或已被接管替换的凭据同样可回报，绝不返回 403，只是
    不获得控制资格；但成功响应与 409 附带状态中的 control_granted 均按
    **本次请求凭据**如实回报（状态永不包含凭据本身）：当前控制席回报后
    本页资格与缓存凭据得以保留，被接管的旧页回报后按真实资格清除其失效
    缓存。回报不携带也不改变全局版本号。仅核验已启用且演出中（含暂停，
    暂停可回报但不能发出）时，接受对**当前提示 + 当前轮次**的回报；
    同工种同轮次重复回报以最后一次为准（覆盖写）。核验未启用、非演出中、
    轮次或提示已过期（确认推进 / 撤回 / 改写当前提示 / 重排换头都会开启
    新轮次）返回 409 及最新状态，不写入回报；工种取值非法返回 400。
    写入失败整体回滚，不留部分结果。
    """
    department = body.department.strip().lower()
    if department not in VERIFY_DEPARTMENTS:
        return bad_request("工种须为 lighting（灯光）或 sound（音响）")

    with lock, db() as conn:
        meta = conn.execute(
            "SELECT status, verify_enabled, verify_round FROM meta WHERE id = 1"
        ).fetchone()
        if (
            not meta["verify_enabled"]
            or meta["status"] != "live"
            or body.round != meta["verify_round"]
        ):
            return conflict(conn, x_control_token)
        current = conn.execute(
            "SELECT id FROM cues WHERE status = 'pending' "
            "ORDER BY position ASC, id ASC LIMIT 1"
        ).fetchone()
        if current is None or current["id"] != body.cue_id:
            return conflict(conn, x_control_token)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO verify_reports (round, department, cue_id, ready, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(round, department) DO UPDATE SET "
            "cue_id = excluded.cue_id, ready = excluded.ready, "
            "updated_at = excluded.updated_at",
            (meta["verify_round"], department, body.cue_id,
             1 if body.ready else 0, now),
        )
        # 回报不改全局版本号；返回最新状态供页面刷新轮次与就绪显示。
        # control_granted 按本次请求凭据回报：当前席保留资格，无凭据 / 旧凭据
        # 页面按真实资格处理（响应不含凭据本身）。
        return build_state(conn, x_control_token)


# ---------------------------------------------------------------- 自动发出

@app.post("/api/auto")
def set_auto(body: AutoIn, x_control_token: Optional[str] = Header(default=None)):
    """设置自动发出：统一间隔（1～60 秒整数）与开关（仅演出中）。

    携带页面版本号、受单控制席保护；成功版本 +1。开启或改间隔后从完整间隔
    重新计时（暂停中则冻结完整间隔，恢复后续计）；已开启时仅改间隔同样
    重计，开关与间隔都未变的调用不重置计时但仍推进版本。关闭后保留已设
    间隔便于再次开启。非演出中或版本过期返回 409 及最新状态；间隔非法
    返回 400（先于版本检查，不改数据）。"开启/关闭 + 间隔 + 计时 + 版本 +1"
    在同一事务内落库，写入失败整体不变。
    """
    if body.interval is not None:
        error = auto_interval_error(body.interval)
        if error:
            return bad_request(error)

    with lock, db() as conn:
        forbidden = control_check(conn, x_control_token)
        if forbidden:
            return forbidden
        meta = conn.execute(
            "SELECT version, status, paused, auto_enabled, auto_interval "
            "FROM meta WHERE id = 1"
        ).fetchone()
        if meta["status"] != "live" or body.version != meta["version"]:
            return conflict(conn, x_control_token)
        new_interval = (
            int(body.interval) if body.interval is not None
            else meta["auto_interval"]
        )
        was_enabled = bool(meta["auto_enabled"])
        if body.enabled:
            restart = not was_enabled or new_interval != meta["auto_interval"]
            conn.execute(
                "UPDATE meta SET auto_enabled = 1, auto_interval = ?, "
                "version = version + 1 WHERE id = 1",
                (new_interval,),
            )
            if restart:
                # 开启或改间隔：从完整间隔重新计时（暂停中冻结完整间隔）
                auto_restart_timer(conn)
        else:
            conn.execute(
                "UPDATE meta SET auto_enabled = 0, auto_interval = ?, "
                "auto_deadline = NULL, auto_remaining = NULL, "
                "version = version + 1 WHERE id = 1",
                (new_interval,),
            )
        return build_state(conn, x_control_token)


# ---------------------------------------------------------------- 改写提议

@app.post("/api/suggestions")
def add_suggestion(
    body: SuggestionIn, x_control_token: Optional[str] = Header(default=None)
):
    """无控制权页面为待发提示提交改写建议（控制席审核后才改提示）。

    仅演出中（live，含暂停）且目标提示仍为待发时接受提交；建议内容沿用
    编辑接口的内容校验（去除首尾空白后非空，否则 400）。保存**提交时编号
    与原文快照**及建议内容，**不改提示、不改版本号**。提交无需控制凭据
    （只读页面也可提交，永不返回 403），但与工种回报一样接受可选的
    X-Control-Token，成功 / 409 响应中的 control_granted 按本次凭据如实
    回报。非演出中、提示已发出（或撤回等造成的版本不符）返回 409 且不
    写入；提示不存在返回 404；建议为空返回 400。与其他写操作同锁串行，
    写入失败整体不变。
    """
    content = body.content.strip()
    if not content:
        return bad_request("内容不能为空")

    with lock, db() as conn:
        meta = conn.execute(
            "SELECT version, status FROM meta WHERE id = 1"
        ).fetchone()
        # 提交不改版本，但仍要求页面所见版本最新：保证页面看到的是当前演出，
        # 且 409 能把最新状态（含别人刚审核的结果）带回页面。
        if meta["status"] != "live" or body.version != meta["version"]:
            return conflict(conn, x_control_token)
        row = conn.execute(
            "SELECT * FROM cues WHERE id = ?", (body.cue_id,)
        ).fetchone()
        if row is None:
            return JSONResponse(status_code=404, content={"error": "cue_not_found"})
        if row["status"] != "pending":
            return conflict(conn, x_control_token)
        now = datetime.now(timezone.utc).isoformat()
        # 保存原文快照：接受时据此比对，期间目标提示被改写则接受失效（409）。
        conn.execute(
            "INSERT INTO suggestions "
            "(cue_id, cue_number, original_content, content, status, created_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?)",
            (row["id"], row["number"], row["content"], content, now),
        )
        # 不改提示、不改版本号：返回最新状态供页面展示新提议。
        return build_state(conn, x_control_token)


@app.post("/api/suggestions/{suggestion_id}/accept")
def accept_suggestion(
    suggestion_id: int,
    body: SuggestionDecisionIn,
    x_control_token: Optional[str] = Header(default=None),
):
    """控制席接受一条待审改写提议。

    HTTP 薄端点：身份检查后委托共用模块 cueflow.accept_pending_suggestion
    ——版本、提议状态与原文快照检查及"改写提示 + 标记已接受 + 版本 +1"
    都在同一锁 / 事务内。接受的是当前提示（待发队首）时即使建议文本与
    原文相同也重开核验轮次、清空就绪并重新自动计时（与 cueflow
    编辑当前提示同规则），其他提示不重置。目标被手工 / 自动发出或内容
    已变返回 409 且提议保持待审、数据不变。
    """
    with lock, db() as conn:
        forbidden = control_check(conn, x_control_token)
        if forbidden:
            return forbidden
        try:
            cueflow.accept_pending_suggestion(
                conn,
                suggestion_id=suggestion_id,
                version=body.version,
                clock=_clock,
            )
        except cueflow.Conflict as exc:
            return conflict(conn, x_control_token, exc.message)
        return build_state(conn, x_control_token)


@app.post("/api/suggestions/{suggestion_id}/reject")
def reject_suggestion(
    suggestion_id: int,
    body: SuggestionDecisionIn,
    x_control_token: Optional[str] = Header(default=None),
):
    """控制席拒绝一条待审改写提议：仅标记为已拒绝，不改提示、不改版本号。

    携带最新版本号、受单控制席保护。提议不存在或已处理（含并发下已被接受
    / 拒绝）返回 409 且数据不变；版本过期同样 409 并附最新状态。拒绝不
    校验目标快照（目标已变也可直接拒绝），**不改版本号**——与同版本的
    接受 / 业务写操作交错时，只有一个能生效，拒绝胜出时版本不变。写入
    失败整体不变。
    """
    with lock, db() as conn:
        forbidden = control_check(conn, x_control_token)
        if forbidden:
            return forbidden
        meta = conn.execute("SELECT version FROM meta WHERE id = 1").fetchone()
        if body.version != meta["version"]:
            return conflict(conn, x_control_token)
        sug = conn.execute(
            "SELECT * FROM suggestions WHERE id = ?", (suggestion_id,)
        ).fetchone()
        if sug is None or sug["status"] != "pending":
            return conflict(conn, x_control_token)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE suggestions SET status = 'rejected', decided_at = ? WHERE id = ?",
            (now, sug["id"]),
        )
        # 拒绝不改版本号：返回最新状态（提议状态已刷新为已拒绝）。
        return build_state(conn, x_control_token)


# ---------------------------------------------------------------- 观众字幕

@app.post("/api/subtitles")
def set_subtitle(body: SubtitleIn, x_control_token: Optional[str] = Header(default=None)):
    """为演出中的待发提示设置 / 清除观众字幕（执行台操作）。

    携带页面版本号、受单控制席保护（与编辑 / 重排等一致）；请求体
    ``subtitle`` 为字符串时去除首尾空白后须为 1～200 字符，为 ``null``
    时清除该提示的字幕设置。字幕**绑定提示 id**，与版本号 +1 在同一事务
    落库，新设置标记为有效：手工编辑或接受提议改写该提示（即使同文）会
    令其失效，重排不影响。本操作**不推进提示、不换核验轮次、不重新计时、
    不改变观众当前画面**（画面只随实际发出 / 撤回 / 收场变化）。

    非字符串 / 空白 / 超长返回 400（先于版本检查，不改数据）；非演出中
    （开场前 / 已完成）、目标不存在或非待发、版本过期返回 409 及最新
    状态，原有设置与有效性不变；凭据缺失 / 失效返回 403。
    """
    with lock, db() as conn:
        forbidden = control_check(conn, x_control_token)
        if forbidden:
            return forbidden
        try:
            cueflow.set_pending_subtitle(
                conn,
                version=body.version,
                cue_id=body.cue_id,
                subtitle=body.subtitle,
            )
        except cueflow.Conflict as exc:
            return conflict(conn, x_control_token, exc.message)
        except cueflow.BadRequest as exc:
            return bad_request(exc.message)
        return build_state(conn, x_control_token)


@app.get("/api/screen")
def get_screen():
    """观众字幕屏专用只读接口：只返回当前字幕与跨场递增的画面序号。

    无需凭据、不需要版本号，不返回提示正文、编号、进度、控制席等任何
    后台信息（字幕屏可能投影给观众，必须与执行台状态隔离）。``subtitle``
    为当前应显示的字幕（清屏时为 ``null``）；``seq`` **跨场单调递增、
    从不归零**，每次画面变化（有效字幕上屏 / 清屏）都 +1，观众屏轮询时
    只接受更大的序号，迟到的旧响应不能让画面倒退。
    """
    with db() as conn:
        meta = conn.execute(
            "SELECT screen_text, screen_seq FROM meta WHERE id = 1"
        ).fetchone()
        return {"subtitle": meta["screen_text"], "seq": meta["screen_seq"]}


@app.get("/screen")
def screen_page():
    """观众字幕屏独立页面（/screen）：只显示另行设置的字幕，无后台信息。"""
    return FileResponse(os.path.join(BASE_DIR, "static", "screen.html"))


@app.post("/api/setup")
def setup(body: SetupIn, x_control_token: Optional[str] = Header(default=None)):
    """开场前录入：2~12 条、编号唯一、内容非空。会清空旧进度。"""
    cues = [(c.number, c.content) for c in body.cues]
    error = cue_list_error(cues)
    if error:
        return bad_request(error)
    numbers = [n.strip() for n, _ in cues]
    contents = [c.strip() for _, c in cues]

    with lock, db() as conn:
        forbidden = control_check(conn, x_control_token)
        if forbidden:
            return forbidden
        conn.execute("DELETE FROM cues")
        conn.execute("DELETE FROM issues")  # 手工录入新场：清空上场遗留的问题记录
        clear_suggestions(conn)  # 开新场清空上场的改写提议（提议不跨场）
        # 开新场清屏并清除字幕设置（随 DELETE FROM cues 一起消失），
        # 画面序号 +1（跨场不归零），字幕不进档案。
        clear_subtitles_and_screen(conn)
        for pos, (number, content) in enumerate(zip(numbers, contents), start=1):
            conn.execute(
                "INSERT INTO cues (number, content, position, status) "
                "VALUES (?, ?, ?, 'pending')",
                (number, content, pos),
            )
        # 本场核验：开场前已启用则从第 1 轮开始，未启用保持关闭（不清除启用标记，
        # 使"开场前启用 → 开始演出"的核验生效；下一场不继承由归档/重置关闭保证）
        verify_reset_for_new_show(conn)
        conn.execute(
            "UPDATE meta SET version = version + 1, status = 'live', paused = 0 "
            "WHERE id = 1"
        )
        # 演出中直接录入新场时自动模式保持开启：新场首条从完整间隔重新计时
        auto_restart_timer(conn)
        return build_state(conn, x_control_token)


@app.post("/api/confirm")
def confirm(body: VersionedIn, x_control_token: Optional[str] = Header(default=None)):
    """确认发出当前提示：以服务端此刻的内容为准记入已发，并推进一条。

    HTTP 薄端点：在同一写锁 / 事务内先做控制席身份检查，随后调用共用模块
    cueflow.manual_issue 完成版本、暂停与两工种就绪检查及发出；自动发出
    （auto_tick -> cueflow.auto_issue_due）通过检查后走的是同一个
    cueflow.issue_current，因此手工与自动的快照、版本推进、换轮、末条
    关自动与重计时规则完全一致。暂停期间即使用最新版本号也返回 409 及
    最新状态，须先恢复发出；启用本场核验后两工种缺任一就绪同样 409。
    """
    with lock, db() as conn:
        forbidden = control_check(conn, x_control_token)
        if forbidden:
            return forbidden
        try:
            cueflow.manual_issue(conn, version=body.version, clock=_clock)
        except cueflow.Conflict as exc:
            return conflict(conn, x_control_token, exc.message)
        return build_state(conn, x_control_token)


@app.post("/api/withdraw")
def withdraw(body: WithdrawIn, x_control_token: Optional[str] = Header(default=None)):
    """撤回末条已发提示：把本场最后一条已发提示退回当前待发队首。

    供误点确认后补救：仅把该条提示退回待发，不撤销其他任何操作。提交页面
    所见的 cue_id 与版本号；仅演出中（live，含暂停时）或已完成（done）且
    该 cue_id 仍为最后一条已发提示时生效。退回时以其**发出快照**作为待发
    内容（保留 id 与编号），清除发出快照、发出时间与发出序号，插到**现有
    待发队首**（现有待发内容与相对顺序整体后移）；其他已发记录、待发内容
    与相对顺序、问题记录快照均不变。已完成态撤回后回到演出中；暂停标记
    保持不变；该提示再次确认时记新的服务端时间。

    "退回待发（位置 / 内容 / 清空发出字段）+ 状态推进 + 版本 +1"在同一事务
    内提交，写入失败则全部不变。与确认、编辑、重排、归档、暂停恢复、问题
    记录等同版本竞争最多一个成功；版本过期、目标不是最后已发提示（含已被
    撤回）或本场没有已发提示都返回 409 及最新状态。
    """
    with lock, db() as conn:
        forbidden = control_check(conn, x_control_token)
        if forbidden:
            return forbidden
        try:
            cueflow.withdraw_last(
                conn, version=body.version, cue_id=body.cue_id, clock=_clock
            )
        except cueflow.Conflict as exc:
            return conflict(conn, x_control_token, exc.message)
        return build_state(conn, x_control_token)


@app.post("/api/pause")
def pause(body: PauseIn, x_control_token: Optional[str] = Header(default=None)):
    """暂停或恢复发出（仅演出中）。

    仅当状态为演出中（live）且目标 paused 与当前不同时生效；暂停标记与
    版本号 +1 在同一事务内提交，写入失败两者都不变。暂停不清空提示、不
    推进进度；暂停期间确认被拒绝（409），编辑 / 重排 / 归档仍可用。
    非演出中、目标已达成（如已暂停时再暂停）或版本过期返回 409 及最新
    状态；同版本的暂停、恢复、确认、编辑最多一个生效。
    """
    with lock, db() as conn:
        forbidden = control_check(conn, x_control_token)
        if forbidden:
            return forbidden
        meta = conn.execute(
            "SELECT version, status, paused FROM meta WHERE id = 1"
        ).fetchone()
        if (
            meta["status"] != "live"
            or body.version != meta["version"]
            or bool(meta["paused"]) == body.paused
        ):
            return conflict(conn, x_control_token)
        conn.execute(
            "UPDATE meta SET paused = ?, version = version + 1 WHERE id = 1",
            (1 if body.paused else 0,),
        )
        # 暂停冻结自动计时的剩余时间，恢复按剩余时间续计（同一事务）
        if body.paused:
            auto_freeze(conn)
        else:
            auto_thaw(conn)
        return build_state(conn, x_control_token)


@app.post("/api/edit")
def edit(body: EditIn, x_control_token: Optional[str] = Header(default=None)):
    """改写一条尚未发出的提示（内容，可选编号）。

    HTTP 薄端点：身份检查后委托共用模块 cueflow.edit_pending——版本 / 提示
    状态检查与写入在同一锁 / 事务内；成功改写当前提示（待发队首）时即使
    新文本与原文相同也换轮并重计时，编辑其他提示不重置。
    """
    with lock, db() as conn:
        forbidden = control_check(conn, x_control_token)
        if forbidden:
            return forbidden
        try:
            cueflow.edit_pending(
                conn,
                version=body.version,
                cue_id=body.cue_id,
                content=body.content,
                number=None if body.number is None else body.number,
                clock=_clock,
            )
        except cueflow.Conflict as exc:
            return conflict(conn, x_control_token, exc.message)
        except cueflow.BadRequest as exc:
            return bad_request(exc.message)
        return build_state(conn, x_control_token)


@app.post("/api/reorder")
def reorder(body: ReorderIn, x_control_token: Optional[str] = Header(default=None)):
    """重排尚未发出的提示：order 必须恰好是全部待发提示的 id。

    HTTP 薄端点：身份检查后委托共用模块 cueflow.reorder_pending；仅换
    队首才换轮 / 重计时，队首不变（含完全相同的顺序）不重置。
    """
    with lock, db() as conn:
        forbidden = control_check(conn, x_control_token)
        if forbidden:
            return forbidden
        try:
            cueflow.reorder_pending(
                conn, version=body.version, order=body.order, clock=_clock
            )
        except cueflow.Conflict as exc:
            return conflict(conn, x_control_token, exc.message)
        return build_state(conn, x_control_token)


@app.post("/api/issues")
def add_issue(body: IssueIn, x_control_token: Optional[str] = Header(default=None)):
    """为当前场次任一提示追加一条现场问题记录。

    演出中（含暂停时）或已完成都可记录，开场前（无本场提示）返回 409。
    保存服务端时间、当时编号、内容快照与已发/待发状态：已发提示取其发出
    快照（编号与内容此后不可变），待发提示取当前编号与内容。"插入记录 +
    版本 +1"在同一事务内提交：不推进提示、不改变暂停状态；写入失败则记录
    与版本都不变。与确认、编辑、重排、暂停恢复、归档同版本竞争，最多一个
    成功，其余收到 409 及最新状态。

    正文去除首尾空白后须为 1～200 字符，不合规返回 400；提示不存在返回
    404；开场前或版本过期返回 409。任何失败都不改数据。
    """
    text = body.text.strip()
    if not (ISSUE_MIN_LEN <= len(text) <= ISSUE_MAX_LEN):
        return bad_request(
            f"问题内容去除首尾空白后需为 {ISSUE_MIN_LEN}～{ISSUE_MAX_LEN} 字符"
        )

    with lock, db() as conn:
        forbidden = control_check(conn, x_control_token)
        if forbidden:
            return forbidden
        meta = conn.execute(
            "SELECT version, status FROM meta WHERE id = 1"
        ).fetchone()
        if meta["status"] not in ("live", "done") or body.version != meta["version"]:
            return conflict(conn, x_control_token)
        row = conn.execute(
            "SELECT * FROM cues WHERE id = ?", (body.cue_id,)
        ).fetchone()
        if row is None:
            return JSONResponse(
                status_code=404, content={"error": "cue_not_found"}
            )
        # 已发提示保存其发出快照；待发提示保存当前编号与内容。
        if row["status"] == "issued":
            cue_number, cue_content = row["number"], row["issued_content"]
        else:
            cue_number, cue_content = row["number"], row["content"]
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO issues "
            "(cue_id, cue_number, cue_status, cue_content, text, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                row["id"],
                cue_number,
                row["status"],
                cue_content,
                text,
                now,
            ),
        )
        conn.execute("UPDATE meta SET version = version + 1 WHERE id = 1")
        return build_state(conn, x_control_token)


@app.post("/api/reset")
def reset(x_control_token: Optional[str] = Header(default=None)):
    """清空进度，回到开场前设置；一并关闭本场核验并清空轮次与就绪。"""
    with lock, db() as conn:
        forbidden = control_check(conn, x_control_token)
        if forbidden:
            return forbidden
        conn.execute("DELETE FROM cues")
        conn.execute("DELETE FROM issues")  # 重置一并清空当前场问题记录
        clear_suggestions(conn)  # 重置清空当前场改写提议
        clear_subtitles_and_screen(conn)  # 重置清屏并清除字幕设置（不进档案）
        verify_close(conn)  # 重置关闭本场核验并清空轮次与就绪，下一场不继承
        auto_disable(conn)  # 重置关闭自动模式并清空计时（间隔保留）
        conn.execute(
            "UPDATE meta SET version = version + 1, status = 'setup', paused = 0 "
            "WHERE id = 1"
        )
        return build_state(conn, x_control_token)


@app.post("/api/archive")
def archive(body: ArchiveIn, x_control_token: Optional[str] = Header(default=None)):
    """归档本场并收场。

    仅演出中（live）或已完成（done）可归档；携带页面所见版本号。
    快照（已发按发出顺序、剩余按待发顺序、问题记录按保存顺序）写入 archives
    与清空 cues/issues、回到 setup、版本号 +1 在同一事务内提交：写档案失败
    则整体回滚，当前提示与问题记录都不会被清空。同版本的重复归档因版本不再
    匹配而拿到 409，不会多建档案。
    """
    name = body.name.strip()
    if not name:
        return bad_request("场次名称不能为空")

    with lock, db() as conn:
        forbidden = control_check(conn, x_control_token)
        if forbidden:
            return forbidden
        meta = conn.execute("SELECT version, status FROM meta WHERE id = 1").fetchone()
        if meta["status"] not in ("live", "done") or body.version != meta["version"]:
            return conflict(conn, x_control_token)

        issued, pending, issues = archive_snapshots(conn)
        now = datetime.now(timezone.utc).isoformat()
        new_version = meta["version"] + 1
        conn.execute(
            "INSERT INTO archives (name, archived_at, status, version, issued, pending, issues) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                name,
                now,
                meta["status"],
                meta["version"],
                json.dumps(issued, ensure_ascii=False),
                json.dumps(pending, ensure_ascii=False),
                json.dumps(issues, ensure_ascii=False),
            ),
        )
        conn.execute("DELETE FROM cues")
        conn.execute("DELETE FROM issues")  # 已随档案保存，当前场记录清空
        clear_suggestions(conn)  # 改写提议不写入档案，归档时一并清空
        clear_subtitles_and_screen(conn)  # 字幕设置与画面不写入档案：清屏并清除设置
        # 归档关闭本场核验并清空轮次与就绪（不写入档案，档案结构不变），
        # 下一场不继承；与"写档案 + 清空"在同一事务内提交。
        verify_close(conn)
        auto_disable(conn)  # 归档关闭自动模式并清空计时（同一事务）
        conn.execute(
            "UPDATE meta SET version = ?, status = 'setup', paused = 0 WHERE id = 1",
            (new_version,),
        )
        return build_state(conn, x_control_token)


@app.post("/api/archives/{archive_id}/start")
def start_from_archive(
    archive_id: int,
    body: StartFromArchiveIn,
    x_control_token: Optional[str] = Header(default=None),
):
    """以档案开新场。

    按档案已发顺序取编号与内容快照，再接待发顺序的编号与内容，全部作为
    新场待发提示；不继承发出时间或进度，也不修改原档案。仅开场前
    （setup）允许生效，携带页面所见版本号。"写入新场提示 + 推进到演出中
    + 版本号 +1"在同一事务内提交：任一步失败整体回滚，当前场次与档案都
    不变。同版本的重复/并发提交只有一次生效，其余收到 409 及最新状态。

    档案不存在返回 404；拼出的提示不满足 2～12 条 / 编号唯一 / 内容非空
    返回 400；非开场前或版本过期返回 409。
    """
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM archives WHERE id = ?", (archive_id,)
        ).fetchone()
    if row is None:
        return JSONResponse(status_code=404, content={"error": "archive_not_found"})

    cues = archive_cues(row)
    error = cue_list_error(cues)
    if error:
        return bad_request(error)
    numbers = [n.strip() for n, _ in cues]
    contents = [c.strip() for _, c in cues]

    with lock, db() as conn:
        forbidden = control_check(conn, x_control_token)
        if forbidden:
            return forbidden
        meta = conn.execute("SELECT version, status FROM meta WHERE id = 1").fetchone()
        if meta["status"] != "setup" or body.version != meta["version"]:
            return conflict(conn, x_control_token)

        conn.execute("DELETE FROM cues")
        conn.execute("DELETE FROM issues")  # 以档案开新场不复制问题记录，原档案不变
        clear_suggestions(conn)  # 以档案开新场不复制改写提议，原档案不变
        clear_subtitles_and_screen(conn)  # 以档案开新场不复制字幕：清屏并清除设置
        for pos, (number, content) in enumerate(zip(numbers, contents), start=1):
            conn.execute(
                "INSERT INTO cues (number, content, position, status) "
                "VALUES (?, ?, ?, 'pending')",
                (number, content, pos),
            )
        # 本场核验：开场前已启用则从第 1 轮开始，未启用保持关闭（同手工开场）
        verify_reset_for_new_show(conn)
        conn.execute(
            "UPDATE meta SET version = version + 1, status = 'live', paused = 0 "
            "WHERE id = 1"
        )
        # 防御：开场前自动模式本已关闭（归档/重置/重启都会关闭），此调用为无操作
        auto_restart_timer(conn)
        return build_state(conn, x_control_token)


app.mount(
    "/",
    StaticFiles(directory=os.path.join(BASE_DIR, "static"), html=True),
    name="static",
)
