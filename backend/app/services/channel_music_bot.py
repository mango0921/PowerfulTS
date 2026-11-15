"""频道级音乐机器人解析。

规则（产品约定）：
- 机器人只能由管理员在 TSMusicBot 后台创建/删除/派遣；
- 普通用户与游客只能控制**自己当前所在频道**的那台音乐机器人；
- 当前频道没有机器人时：从 TS 默认频道（cid=1）的机器人克隆一台过来；
  默认频道也没有，就先创建一台默认音乐机器人再复制到当前频道。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import get_settings
from ..models import Account, VoiceBot
from .ts3_query import TS3QueryClient
from .tsmusic_client import TSMusicClient

logger = logging.getLogger(__name__)

DEFAULT_CHANNEL_CID = 1
DEFAULT_BOT_NAME = "音乐机器人"
CLONE_WAIT_SECONDS = 25

# 频道快照缓存：clientlist 每次请求都查一次太重，5 秒足够。
_SNAPSHOT_TTL = 5.0
_snapshot_cache: tuple[float, list[dict[str, Any]], dict[int, str]] | None = None


def _ts_snapshot() -> tuple[list[dict[str, Any]], dict[int, str]]:
    """同步读取 TS 的 clientlist + channellist（在 to_thread 里跑）。"""
    settings = get_settings()
    conn = TS3QueryClient(
        settings.ts3_host,
        settings.ts3_query_port,
        transport=getattr(settings, "ts3_query_transport", "raw"),
        username=settings.ts3_query_user,
        password=settings.ts3_query_password,
        ssh_known_hosts=getattr(settings, "ts3_query_ssh_known_hosts", "") or None,
    )
    try:
        conn.connect()
        conn.authenticate(settings.ts3_query_user, settings.ts3_query_password)
        conn.send("use", sid=settings.ts3_sid)
        clients = conn.send("clientlist", uid=True)
        channels = conn.send("channellist")
    finally:
        try:
            conn.close()
        except Exception:  # pragma: no cover - 关闭失败不影响结果
            pass
    names: dict[int, str] = {}
    for ch in channels:
        try:
            cid = int(ch.get("cid"))
        except (TypeError, ValueError):
            continue
        names[cid] = str(ch.get("channel_name") or f"频道{cid}")
    return clients, names


async def _snapshot(refresh: bool = False) -> tuple[list[dict[str, Any]], dict[int, str]]:
    global _snapshot_cache
    now = time.monotonic()
    if not refresh and _snapshot_cache and now - _snapshot_cache[0] < _SNAPSHOT_TTL:
        return _snapshot_cache[1], _snapshot_cache[2]
    clients, names = await asyncio.to_thread(_ts_snapshot)
    _snapshot_cache = (now, clients, names)
    return clients, names


def invalidate_snapshot() -> None:
    global _snapshot_cache
    _snapshot_cache = None


def _clid_of(client: dict[str, Any]) -> int:
    try:
        return int(client.get("clid") or 0)
    except (TypeError, ValueError):
        return 0


def _cid_of(client: dict[str, Any]) -> int:
    try:
        return int(client.get("cid") or 0)
    except (TypeError, ValueError):
        return 0


async def _voice_client_id(db: AsyncSession, tsmusic: TSMusicClient, account: Account) -> int:
    """当前账号在网页通话里的 TS 客户端 id（没有则 0）。"""
    row = await db.execute(select(VoiceBot.bot_id).where(VoiceBot.account_id == account.id))
    voice_bot_id = row.scalar_one_or_none()
    if not voice_bot_id:
        raise HTTPException(status_code=409, detail="请先在网页通话中进入频道")
    clid = await tsmusic.get_bot_client_id(voice_bot_id)
    if not clid:
        status = await tsmusic.get_bot_brief(voice_bot_id)
        if status is None:
            # 映射指向的语音 bot 已被删除（例如后台清理过机器人）：清掉脏映射，
            # 让下一次进入网页通话时重新创建。
            await db.execute(delete(VoiceBot).where(VoiceBot.account_id == account.id))
            await db.commit()
            raise HTTPException(status_code=409, detail="网页通话身份已失效，请重新进入网页通话")
        # bot 存在但还没连上：不要删映射，稍后重试即可。
        raise HTTPException(status_code=409, detail="网页通话正在连接，请稍后再试")
    return int(clid)


def _bots_in_channel(
    clients: list[dict[str, Any]], cid: int, bots: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_clid = {_clid_of(c): c for c in clients if _clid_of(c)}
    found: list[dict[str, Any]] = []
    for bot in bots:
        if str(bot.get("purpose") or "music") == "voice":
            continue
        try:
            clid = int(bot.get("clientId") or 0)
        except (TypeError, ValueError):
            clid = 0
        client = by_clid.get(clid)
        if client is None:
            # TSMusicBot 上报的 clientId 可能滞后（重连后 clid 变化）；
            # 退回到昵称匹配：播放时昵称形如 "♪ 歌名 - 机器人名"。
            name = str(bot.get("name") or "").strip()
            if name:
                client = next(
                    (
                        c
                        for c in clients
                        if str(c.get("client_nickname") or "").strip().endswith(name)
                    ),
                    None,
                )
        if client and _cid_of(client) == cid:
            found.append(bot)
    found.sort(key=lambda b: str(b.get("name") or ""))
    return found


async def channel_context(
    db: AsyncSession, tsmusic: TSMusicClient, account: Account
) -> dict[str, Any]:
    """解析账号当前所在频道：{cid, channel_name, candidates}。"""
    clid = await _voice_client_id(db, tsmusic, account)
    clients, names = await _snapshot()
    me = next((c for c in clients if _clid_of(c) == clid), None)
    if me is None or _cid_of(me) == 0:
        raise HTTPException(status_code=409, detail="无法确定你当前所在频道，请重新进入网页通话")
    cid = _cid_of(me)
    bots = await tsmusic.list_bots()
    candidates = _bots_in_channel(clients, cid, bots)
    return {
        "cid": cid,
        "channel_name": names.get(cid, f"频道{cid}"),
        "candidates": candidates,
        "clients": clients,
        "bots": bots,
    }


async def _wait_connected(tsmusic: TSMusicClient, bot_id: str, seconds: int = CLONE_WAIT_SECONDS) -> bool:
    for _ in range(seconds):
        status = await tsmusic.get_bot_brief(bot_id)
        if status and status.get("connected"):
            return True
        await asyncio.sleep(1)
    return False


async def _move_into_channel(tsmusic: TSMusicClient, bot_id: str, cid: int) -> str | None:
    """让机器人进入目标频道，返回 None 表示成功，否则返回失败原因。"""
    result = await tsmusic.join_channel(cid, "", bot_id)
    if result.get("ok"):
        return None
    return str(result.get("detail") or "切换频道失败")


async def _clone_from_template(
    tsmusic: TSMusicClient, template: dict[str, Any], name: str, cid: int
) -> str:
    """复制一台机器人（新身份、不继承音源登录）并进入目标频道。

    进入频道失败时会删掉刚创建的机器人，避免默认频道里堆积只创建不生效的克隆。
    """
    template_id = str(template.get("id"))
    config = await tsmusic.get_bot_config(template_id)
    server_address = str(config.get("serverAddress") or "").strip()
    if not server_address:
        raise HTTPException(status_code=500, detail="模板机器人缺少服务器地址，无法克隆")
    # TS 昵称必须唯一：同频道多台克隆同名时后连上的会被踢（id=513）。
    nickname = f"{name}-{secrets.token_hex(2)}"
    payload = {
        "name": name,
        "nickname": nickname,
        "serverAddress": server_address,
        "serverPort": int(config.get("serverPort") or 9987),
        "serverPassword": config.get("serverPassword") or "",
        "channelPassword": config.get("channelPassword") or "",
        "purpose": "music",
        "autoStart": True,
    }
    async with _clone_semaphore:
        created = await tsmusic.create_bot(payload)
        bot_id = str(created.get("id") or "")
        if not bot_id:
            raise HTTPException(status_code=500, detail="创建音乐机器人失败")
        try:
            await tsmusic.start_bot(bot_id)
            if not await _wait_connected(tsmusic, bot_id):
                raise HTTPException(status_code=504, detail="音乐机器人连接超时，请稍后再试")
            failure = await _move_into_channel(tsmusic, bot_id, cid)
            if failure:
                _join_failure[cid] = (time.time() + JOIN_FAILURE_COOLDOWN_SECONDS, failure)
                raise HTTPException(
                    status_code=502,
                    detail=f"音乐机器人无法进入该频道：{failure}（检查频道密码/权限）",
                )
        except Exception:
            # 失败即清理，避免残留一台停在默认频道的克隆
            try:
                await tsmusic.delete_bot(bot_id)
            except Exception:  # pragma: no cover - 清理失败不影响原始异常
                logger.warning("failed to clean up clone %s", bot_id, exc_info=True)
            invalidate_snapshot()
            raise
    _join_failure.pop(cid, None)
    invalidate_snapshot()
    logger.info("cloned music bot %s from %s into cid=%s", bot_id, template_id, cid)
    return bot_id


async def _find_stray_clone(bots: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    """同名的克隆若已存在（之前进频道失败/被踢回默认频道），优先复用而不是再建一台。"""
    for bot in bots:
        if str(bot.get("name") or "") == name and str(bot.get("purpose") or "music") != "voice":
            return bot
    return None


_create_locks: dict[int, asyncio.Lock] = {}
# 同一时刻只允许一个克隆在连接：TS 同时握手多个客户端会互相拖到超时
_clone_semaphore = asyncio.Semaphore(1)
# 进频道失败后的冷却：避免每次状态轮询都重复建/删机器人
_join_failure: dict[int, tuple[float, str]] = {}
JOIN_FAILURE_COOLDOWN_SECONDS = 120


async def ensure_channel_bot(
    db: AsyncSession, tsmusic: TSMusicClient, account: Account
) -> dict[str, Any]:
    """确保账号所在频道有一台可控制的音乐机器人，返回解析结果。"""
    ctx = await channel_context(db, tsmusic, account)
    if ctx["candidates"]:
        ctx["created"] = False
        return ctx

    cid = ctx["cid"]
    until, reason = _join_failure.get(cid, (0.0, ""))
    if time.time() < until:
        raise HTTPException(
            status_code=502,
            detail=f"音乐机器人无法进入该频道：{reason}（{int(until - time.time())} 秒后重试）",
        )
    lock = _create_locks.setdefault(cid, asyncio.Lock())
    async with lock:
        # 拿到锁后重新判定：并发轮询可能已经有人建好了
        ctx = await channel_context(db, tsmusic, account)
        if ctx["candidates"]:
            ctx["created"] = False
            return ctx

        name = f"{DEFAULT_BOT_NAME}-{ctx['channel_name']}"
        stray = await _find_stray_clone(ctx["bots"], name)
        if stray is not None:
            stray_id = str(stray.get("id"))
            failure = await _move_into_channel(tsmusic, stray_id, cid)
            if failure is None:
                invalidate_snapshot()
                ctx = await channel_context(db, tsmusic, account)
                ctx["created"] = False
                return ctx
            # 复用也进不去（频道密码/权限），删掉重新按模板克隆
            logger.warning("stray clone %s cannot join cid=%s: %s", stray_id, cid, failure)
            try:
                await tsmusic.delete_bot(stray_id)
            except Exception:
                logger.warning("failed to delete stray clone %s", stray_id, exc_info=True)
            invalidate_snapshot()
            ctx = await channel_context(db, tsmusic, account)

        template_bots = _bots_in_channel(ctx["clients"], DEFAULT_CHANNEL_CID, ctx["bots"])
        if template_bots:
            await _clone_from_template(tsmusic, template_bots[0], name, cid)
            ctx = await channel_context(db, tsmusic, account)
            ctx["created"] = True
            return ctx

        # 默认频道也没有：先造一台默认音乐机器人，再克隆到当前频道。
        settings = get_settings()
        seed = {
            "name": DEFAULT_BOT_NAME,
            "nickname": DEFAULT_BOT_NAME,
            "serverAddress": str(getattr(settings, "ts3_host", "") or "").strip(),
            "serverPort": 9987,
            "purpose": "music",
            "autoStart": True,
        }
        if not seed["serverAddress"]:
            raise HTTPException(status_code=500, detail="未配置 TS 服务器地址，无法创建音乐机器人")
        created = await tsmusic.create_bot(seed)
        seed_id = str(created.get("id") or "")
        if not seed_id:
            raise HTTPException(status_code=500, detail="创建默认音乐机器人失败")
        await tsmusic.start_bot(seed_id)
        await _wait_connected(tsmusic, seed_id)
        invalidate_snapshot()
        ctx = await channel_context(db, tsmusic, account)
        template_bots = _bots_in_channel(ctx["clients"], DEFAULT_CHANNEL_CID, ctx["bots"])
        if not template_bots:
            raise HTTPException(status_code=504, detail="默认音乐机器人尚未就绪，请稍后再试")
        await _clone_from_template(tsmusic, template_bots[0], name, cid)
        ctx = await channel_context(db, tsmusic, account)
        ctx["created"] = True
        return ctx
