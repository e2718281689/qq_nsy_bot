# nsy/plugins/picmap/__init__.py
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Tuple, Optional

import base64
import httpx
from urllib.parse import urlparse, urlunparse, quote

from nonebot import on_message, on_command, logger
from nonebot.adapters.onebot.v11 import Message, MessageEvent, MessageSegment
from nonebot.params import CommandArg
from nonebot.rule import to_me
from nonebot.exception import FinishedException  # 仅用于确保不误捕获（现在不会捕到了）

# ========== 配置区域 ==========
# 仍然支持带 user:pass@ 的 URL；代码会先鉴权拉图，再以 base64:// 发出
URL_MAPPING: Dict[str, str] = {
    "天音miho": "http://xxx:xxx@192.168.1.177:5005/nsy/miiii_am/2021-04-18%2021-15-img_1305.jpg",
    "大森riya": "http://xxx:xxx@192.168.1.177:5005/nsy/nichika1015/2021-09-25 20-10-img_1513.jpg",  # 注意这个有空格
    # … 自行添加
}

# 如需本地文件（不推荐跨机），可改为绝对路径并用 file:// 协议
LOCAL_FILE_MAPPING: Dict[str, Path] = {
    # "王五": Path("/absolute/path/to/wangwu.jpg"),
}

# 支持的别名（可选）：一个名字对应多个触发词
ALIASES: Dict[str, List[str]] = {
    # "张三": ["老张", "zs"],
}

# ========== 工具函数 ==========
def normalize(text: str) -> str:
    return text.strip()

async def url_to_base64_file_spec(url: str) -> str:
    """
    支持 http(s)://user:pass@host/path 和普通 http(s)://host/path
    返回可用于 MessageSegment.image(...) 的 base64://... 字符串
    """
    p = urlparse(url)
    auth = None

    # 对 path 做安全编码（把空格等转成 %20；已有的 %xx 不会被二次编码）
    encoded_path = quote(p.path, safe="/%:@")  # 保留 / 和已存在的 % 转义

    # 如果 URL 里带了 user:pass@，清理掉并改用 httpx 的 auth 传递
    if p.username or p.password:
        netloc = p.hostname or ""
        if p.port:
            netloc += f":{p.port}"
        clean_url = urlunparse((p.scheme, netloc, encoded_path, p.params, p.query, p.fragment))
        auth = (p.username or "", p.password or "")
    else:
        clean_url = urlunparse((p.scheme, p.netloc, encoded_path, p.params, p.query, p.fragment))

    timeout = httpx.Timeout(10.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        r = await client.get(clean_url, auth=auth)
        r.raise_for_status()
        b64 = base64.b64encode(r.content).decode("ascii")
        return f"base64://{b64}"

def lookup(name: str) -> Optional[Tuple[str, object]]:
    """
    返回 ('url', str) 或 ('local', Path)；找不到返回 None
    """
    key = normalize(name)

    # 命中主键
    if key in URL_MAPPING:
        return ("url", URL_MAPPING[key])
    if key in LOCAL_FILE_MAPPING:
        return ("local", LOCAL_FILE_MAPPING[key])

    # 命中别名
    for main, alias_list in ALIASES.items():
        if key in alias_list:
            if main in URL_MAPPING:
                return ("url", URL_MAPPING[main])
            if main in LOCAL_FILE_MAPPING:
                return ("local", LOCAL_FILE_MAPPING[main])

    return None

# ========== 触发方式 1：/pic 名字 ==========
pic_cmd = on_command("pic", aliases={"/pic"}, priority=5, block=False)

@pic_cmd.handle()
async def _(arg: Message = CommandArg()):
    name = normalize(str(arg).strip())
    if not name:
        await pic_cmd.finish("用法：/pic 名字")

    res = lookup(name)
    if res is None:
        await pic_cmd.finish(f"没有找到：{name}")

    kind, value = res
    # 只把“下载/转换”包在 try；成功后再 finish（不要包住 finish）
    try:
        if kind == "url":
            file_spec = await url_to_base64_file_spec(str(value))
            seg = MessageSegment.image(file_spec)
        else:  # local
            seg = MessageSegment.image(f"file://{Path(value).as_posix()}")
    except Exception as e:
        logger.exception(f"fetch image failed: {e}")
        await pic_cmd.send("图片获取失败，请稍后再试～")
        return

    # 正常结束（这行会抛 FinishedException，属框架正常行为，外面不要捕获）
    await pic_cmd.finish(Message(seg))

# ========== 触发方式 2：直接发“名字”（精确匹配）=========
# 如果你希望只有被@时才触发，把 rule=to_me() 打开
name_hit = on_message(priority=10, block=False)  # , rule=to_me()

@name_hit.handle()
async def _(event: MessageEvent):
    text = normalize(str(event.get_message()))
    if not text:
        return

    res = lookup(text)
    if res is None:
        return

    kind, value = res
    try:
        if kind == "url":
            file_spec = await url_to_base64_file_spec(str(value))
            seg = MessageSegment.image(file_spec)
        else:  # local
            seg = MessageSegment.image(f"file://{Path(value).as_posix()}")
    except Exception as e:
        logger.exception(f"fetch image failed: {e}")
        await name_hit.send("图片获取失败，请稍后再试～")
        return

    await name_hit.finish(Message(seg))
