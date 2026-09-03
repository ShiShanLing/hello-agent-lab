"""东方财富指数/板块/ETF 实时行情快照（管理后台专用）。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_REFERER = "https://quote.eastmoney.com/"
_UT = "bd1d9ddb04089700cf9c27f6f7426281"
_HOSTS = [
    "https://82.push2.eastmoney.com",
    "https://39.push2.eastmoney.com",
    "https://48.push2.eastmoney.com",
    "https://push2delay.eastmoney.com",
    "https://push2.eastmoney.com",
]
_TIMEOUT = 15.0
_HEADERS = {"User-Agent": _UA, "Referer": _REFERER}

_ULIST_FIELDS = "f2,f3,f4,f6,f12,f13,f14,f62,f66,f72,f78,f84,f184,f104,f105,f106,f152"
_CLIST_FIELDS = _ULIST_FIELDS
_PAGE_SIZE = 100

# ── 置顶指数 ──
PINNED_INDICES = [
    {"name": "上证指数", "secid": "1.000001"},
    {"name": "深证成指", "secid": "0.399001"},
    {"name": "创业板指", "secid": "0.399006"},
    {"name": "科创50", "secid": "1.000688"},
    {"name": "沪深300", "secid": "1.000300"},
]

# ── 关注的 ETF ──
ETF_LIST = [
    {"name": "半导体ETF", "secid": "1.512480"},
    {"name": "科技ETF", "secid": "1.515000"},
    {"name": "医疗ETF", "secid": "1.512170"},
    {"name": "医药ETF", "secid": "0.159938"},
]

PINNED_SECIDS = {spec["secid"] for spec in PINNED_INDICES}


def _to_num(val: Any) -> float:
    if val is None or val == "-":
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def _to_yi(val: Any) -> float:
    return round(_to_num(val) / 1e8, 2)


def _direction_label(pct: float) -> dict[str, str]:
    if pct > 0.05:
        return {"direction": "涨", "label": f"涨 {abs(pct):.2f}%"}
    if pct < -0.05:
        return {"direction": "跌", "label": f"跌 {abs(pct):.2f}%"}
    return {"direction": "平", "label": f"平 {abs(pct):.2f}%"}


def _classify(secid: str) -> str:
    if secid.startswith("90."):
        return "board"
    parts = secid.split(".", 1)
    if len(parts) == 2:
        digits = parts[1]
        if digits.startswith("51") or digits.startswith("15"):
            return "etf"
    return "index"


def _parse_row(row: dict[str, Any], force_type: str | None = None) -> dict[str, Any]:
    secid = f"{row.get('f13', '')}.{row.get('f12', '')}"
    pct = _to_num(row.get("f3"))
    return {
        "name": row.get("f14", ""),
        "secid": secid,
        "type": force_type or _classify(secid),
        "price": _to_num(row.get("f2")),
        "pct": pct,
        "change": _to_num(row.get("f4")),
        "amount_yi": _to_yi(row.get("f6")),
        "main_net_yi": _to_yi(row.get("f62")),
        "main_pct": _to_num(row.get("f184")),
        "super_big_yi": _to_yi(row.get("f66")),
        "big_yi": _to_yi(row.get("f72")),
        "mid_yi": _to_yi(row.get("f78")),
        "small_yi": _to_yi(row.get("f84")),
        "up_count": int(_to_num(row.get("f104"))),
        "down_count": int(_to_num(row.get("f105"))),
        "flat_count": int(_to_num(row.get("f106"))),
        **_direction_label(pct),
    }


def _parse_diff(diff: Any, force_type: str | None = None) -> list[dict[str, Any]]:
    rows = diff if isinstance(diff, list) else list(diff.values())
    return [_parse_row(r, force_type) for r in rows]


async def _ulist_fetch(
    client: httpx.AsyncClient, secids: list[str]
) -> list[dict[str, Any]]:
    """ulist.np 接口拉取指数/ETF 行情。"""
    path = (
        f"/api/qt/ulist.np/get?fltt=2&invt=2"
        f"&secids={','.join(secids)}&fields={_ULIST_FIELDS}&ut={_UT}"
    )
    for host in _HOSTS:
        try:
            resp = await client.get(f"{host}{path}", headers=_HEADERS)
            data = resp.json()
            diff = (data.get("data") or {}).get("diff")
            if diff:
                return _parse_diff(diff)
        except Exception as exc:
            logger.warning("东财 ulist 节点 %s 失败: %s", host, exc)
    raise RuntimeError("ulist 接口所有节点不可用")


async def _clist_fetch_all(
    client: httpx.AsyncClient, fs: str, board_type: str
) -> list[dict[str, Any]]:
    """clist 接口翻页拉取全量板块。"""
    all_quotes: list[dict[str, Any]] = []
    for pn in range(1, 20):
        if pn > 1:
            await asyncio.sleep(0.3 + random.random() * 0.5)
        path = (
            f"/api/qt/clist/get?fltt=2&invt=2&np=1&pn={pn}&pz={_PAGE_SIZE}&po=1"
            f"&fs={fs}&fid=f3&stat=1&fields={_CLIST_FIELDS}&ut={_UT}"
        )
        fetched = False
        for host in _HOSTS:
            try:
                resp = await client.get(f"{host}{path}", headers=_HEADERS)
                data = resp.json()
                total = (data.get("data") or {}).get("total", 0)
                diff = (data.get("data") or {}).get("diff")
                if diff:
                    all_quotes.extend(_parse_diff(diff, board_type))
                fetched = True
                break
            except Exception as exc:
                logger.warning("东财 clist 节点 %s page %d 失败: %s", host, pn, exc)
        if not fetched or len(all_quotes) >= total:
            break
    return all_quotes


async def fetch_market_indices() -> dict[str, Any]:
    """拉取置顶指数 + 全量行业板块 + 全量概念板块 + ETF。"""
    ulist_secids = [s["secid"] for s in PINNED_INDICES] + [
        s["secid"] for s in ETF_LIST
    ]

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        index_quotes = await _ulist_fetch(client, ulist_secids)
        await asyncio.sleep(0.5 + random.random() * 0.5)
        industry_quotes = await _clist_fetch_all(
            client, "m%3A90+t%3A2", "industry"
        )
        await asyncio.sleep(0.5 + random.random() * 0.5)
        concept_quotes = await _clist_fetch_all(
            client, "m%3A90+t%3A3", "concept"
        )

    pinned = sorted(
        [q for q in index_quotes if q["secid"] in PINNED_SECIDS],
        key=lambda q: q["pct"],
        reverse=True,
    )
    etf = sorted(
        [q for q in index_quotes if q["secid"] not in PINNED_SECIDS],
        key=lambda q: q["pct"],
        reverse=True,
    )

    # 行业 + 概念按 secid 去重（行业优先）
    seen: set[str] = set()
    boards: list[dict[str, Any]] = []
    for q in industry_quotes + concept_quotes:
        if q["secid"] not in seen:
            seen.add(q["secid"])
            boards.append(q)
    boards.sort(key=lambda q: q["pct"], reverse=True)

    rest = boards + etf

    now = datetime.now(timezone(timedelta(hours=8)))
    result = {
        "date": now.strftime("%Y-%m-%d"),
        "generated_at": now.isoformat(),
        "count": len(pinned) + len(rest),
        "pinned": pinned,
        "rest": rest,
        "board_count": len(boards),
        "etf_count": len(etf),
    }
    _save_cache(result)
    return result


_CACHE_DIR = Path(os.environ.get("HELLO_AGENT_DATA_DIR", "/var/lib/hello-agent"))
_CACHE_FILE = _CACHE_DIR / "market_indices_cache.json"


def _save_cache(data: dict[str, Any]) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        logger.warning("保存行情缓存失败: %s", exc)


def load_cache() -> dict[str, Any] | None:
    try:
        if _CACHE_FILE.exists():
            return json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("读取行情缓存失败: %s", exc)
    return None


_CST = timezone(timedelta(hours=8))


def _is_trading_session(now: datetime) -> bool:
    """当前是否处于交易时段（工作日 9:30 ~ 15:05）。"""
    if now.weekday() >= 5:
        return False
    t = now.time()
    from datetime import time as _time
    return _time(9, 30) <= t <= _time(15, 5)


def _last_trading_date(now: datetime) -> date:
    """返回最近一个交易日的日期（不含节假日，仅排除周末）。"""
    from datetime import time as _time
    d = now.date()
    # 如果今天是工作日且已过 9:30，最近交易日就是今天
    if d.weekday() < 5 and now.time() >= _time(9, 30):
        return d
    # 否则往前找最近的工作日
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _cache_is_final(cached: dict[str, Any] | None) -> bool:
    """缓存是否已包含最近交易日收盘后的数据（该日 15:00 之后生成）。"""
    if not cached:
        return False
    generated = cached.get("generated_at", "")
    if not generated:
        return False
    try:
        gen_dt = datetime.fromisoformat(generated)
    except (ValueError, TypeError):
        return False
    now = datetime.now(_CST)
    last_td = _last_trading_date(now)
    from datetime import time as _time
    return gen_dt.date() == last_td and gen_dt.time() >= _time(15, 0)


def should_fetch(cached: dict[str, Any] | None) -> bool:
    """判断点击刷新时是否需要真正请求东财 API。"""
    now = datetime.now(_CST)
    if _is_trading_session(now):
        return True
    if _cache_is_final(cached):
        return False
    return True
