# nsy/plugins/picmap/__init__.py
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Tuple, Optional

import json
import base64
import httpx
from urllib.parse import urlparse, urlunparse, quote, unquote

from nonebot import on_message, on_command, logger
from nonebot.adapters.onebot.v11 import Message, MessageEvent, MessageSegment
from nonebot.params import CommandArg
from nonebot.rule import to_me
from nonebot.exception import FinishedException  # 仅用于确保不误捕获（现在不会捕到了）

import os, random, sqlite3
from nonebot import logger

# ========== 配置项 ==========
DB_PATH = "./picmap.db"  # SQLite 数据库路径，用于随机图功能


# ========== 工具函数 ==========
def normalize(text: str) -> str:
    return text.strip()

async def url_to_base64_file_spec(url: str, auth: tuple[str, str] | None = None) -> str:
    """
    支持:
    - http(s)://host/path
    - http(s)://user:pass@host/path
    - 通过参数 auth=('user','pass') 显式提供鉴权信息
    返回: base64://... 字符串，可直接传给 MessageSegment.image()
    """
    p = urlparse(url)

    # 1️⃣ 处理鉴权优先级
    if auth is None and (p.username or p.password):
        # 从 URL 中抽取 user:pass
        auth = (p.username or "", p.password or "")
        # 把 URL 中的 user:pass 去掉，防止重复
        netloc = p.hostname or ""
        if p.port:
            netloc += f":{p.port}"
        netloc = netloc.strip("@")
    else:
        netloc = p.netloc

    # 2️⃣ 安全编码路径
    encoded_path = quote(p.path, safe="/%:@")

    # 3️⃣ 重新拼装 URL
    clean_url = urlunparse((p.scheme, netloc, encoded_path, p.params, p.query, p.fragment))

    # 4️⃣ 发起请求
    timeout = httpx.Timeout(15.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        try:
            r = await client.get(clean_url, auth=auth)
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP {e.response.status_code} for {clean_url}")
            raise
        except Exception as e:
            logger.exception(f"fetch {clean_url} failed: {e}")
            raise

        b64 = base64.b64encode(r.content).decode("ascii")
        return f"base64://{b64}"

def lookup_db(name: str) -> Optional[Tuple[str, str]]:
    """
    从数据库查找人物目录。
    返回 ('webdav', dav_url)，找不到返回 None。
    """
    key = normalize(name)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        cur = conn.cursor()

        # 1) 先查主名
        row = cur.execute(
            "SELECT dav_url FROM person WHERE name=? AND enabled=1",
            (key,)
        ).fetchone()
        if row:
            return ("url", row[0])

        # 2) 再查别名（解析 JSON）
        rows = cur.execute(
            "SELECT name, alias_json, dav_url FROM person WHERE enabled=1"
        ).fetchall()
        for name_, alias_json, dav_url in rows:
            try:
                aliases = json.loads(alias_json or "[]")
            except Exception:
                aliases = []
            if isinstance(aliases, list) and key in aliases:
                return ("url", dav_url)
        return None
    finally:
        conn.close()

def _db():
    return sqlite3.connect(DB_PATH)

def find_person(conn, key: str):
    # 1) 主名精确
    row = conn.execute("SELECT id, name FROM person WHERE name=? AND enabled=1", (key,)).fetchone()
    if row:
        return row
    # 2) 别名匹配（JSON里存数组，如 ["miho","天音"]）
    row = conn.execute("""
        SELECT id, name FROM person
        WHERE enabled=1 AND EXISTS (
            SELECT 1
            FROM json_each(alias_json)
            WHERE json_each.value = ?
        )
    """, (key,)).fetchone()
    return row

def rand_image_rowid(conn, person_id: int):
    # 方案 A：OFFSET 抽签（简洁）
    stat = conn.execute("SELECT img_count FROM person_stats WHERE person_id=?", (person_id,)).fetchone()
    if not stat or stat[0] <= 0:
        return None
    n = stat[0]
    k = random.randrange(n)
    row = conn.execute("""
        SELECT id, url FROM image
        WHERE person_id=? AND active=1
        ORDER BY id LIMIT 1 OFFSET ?
    """, (person_id, k)).fetchone()
    return row

async def fetch_random_image_via_db(name: str) -> str | None:
    conn = _db()
    try:
        p = find_person(conn, name)
        if not p:
            return None
        pid, pname = p
        row = rand_image_rowid(conn, pid)
        if not row:
            return None
        _, url = row
        # 复用你已有的 base64 下载→发图逻辑
        return url
    finally:
        conn.close()

# ========== 触发方式 2：直接发“名字”（精确匹配）=========
# 如果你希望只有被@时才触发，把 rule=to_me() 打开
name_hit = on_message(priority=10, block=False)  # , rule=to_me()

@name_hit.handle()
async def _(event: MessageEvent):
    text = normalize(str(event.get_message()))
    if not text:
        return

    res = lookup_db(text)
    if res is None:
        return

    kind, value = res
    print(f"picmap: matched '{text}' -> ({kind}, {value})")

    try:
        if kind == "url":
            url = await fetch_random_image_via_db(text)
            path = unquote(urlparse(url).path)
            local_root = Path("/mnt")
            local_path = local_root / path.lstrip("/")
            print(local_path)
            uri = local_path.as_uri()
            print(uri)


            # print(f"picmap: fetched url '{url}'")
            # if not url:
            #     await name_hit.finish(f"没有可用图片或未收录：{text}")
            # file_spec = await url_to_base64_file_spec(
            #     url,
            #     auth=("fnos", "Ee_271828")
            # )
            seg = MessageSegment.image(uri)
        else:  # local
            seg = MessageSegment.image(f"file://{Path(value).as_posix()}")
    except Exception as e:
        logger.exception(f"fetch image failed: {e}")
        await name_hit.send("图片获取失败，请稍后再试～")
        return

    await name_hit.finish(Message(seg))
