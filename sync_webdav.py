# tools/sync_webdav.py
from __future__ import annotations
import os, time, sqlite3, xml.etree.ElementTree as ET
from urllib.parse import urlparse, urlunparse, urljoin, quote
import httpx
from typing import List
from pathlib import Path
from dotenv import load_dotenv

# ========= 内置配置（自行修改） =========


# 首次运行时可用的“人员清单种子”，可为空；后续你也能直接往 person 表插入
PERSONS_SEED = [
    # ("主名", "WebDAV目录或单文件URL", ["别名1","别名2"])
    ("天音美穗", "http://192.168.1.177:5005/nsy/miiii_am/", ["miiii_am"]),
    ("大森日雅", "http://192.168.1.177:5005/nsy/nichika1015/", ["nichika"]),
]

# ========= 公用工具 =========
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

load_dotenv(Path(__file__).with_name(".env"))

def _getint(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except Exception:
        return default

CONFIG = {
    "DB_PATH": os.getenv("PICMAP_DB_PATH", "./nsy/picmap.db"),
    "DAV_USER": os.getenv("PICMAP_DAV_USER", ""),
    "DAV_PASS": os.getenv("PICMAP_DAV_PASS", ""),
    "TIMEOUT": _getint("PICMAP_TIMEOUT", 20),
}

def _enc(url: str) -> str:
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc, quote(p.path, safe="/%:@"), p.params, p.query, p.fragment))

def _abs(base: str, href: str) -> str:
    if href.startswith("http://") or href.startswith("https://"):
        return href
    bp = urlparse(base)
    if href.startswith("/"):
        return f"{bp.scheme}://{bp.netloc}{href}"
    return urljoin(base if base.endswith("/") else base + "/", href)

def _is_image(u: str) -> bool:
    path = urlparse(u).path.lower()
    return any(path.endswith(ext) for ext in IMAGE_EXTS)

def get_client() -> httpx.Client:
    auth = (CONFIG["DAV_USER"], CONFIG["DAV_PASS"]) if (CONFIG["DAV_USER"] or CONFIG["DAV_PASS"]) else None
    return httpx.Client(timeout=CONFIG["TIMEOUT"], follow_redirects=True, auth=auth)

# ========= WebDAV 列目录 =========
def list_dir(dir_or_file_url: str) -> List[str]:
    u = dir_or_file_url.strip()
    if not u:
        return []
    # 单文件：校验后返回
    if _is_image(u):
        with get_client() as c:
            r = c.head(_enc(u))
            if r.status_code == 405:
                r = c.get(_enc(u), headers={"Range":"bytes=0-0"})
            r.raise_for_status()
        return [u]

    # 目录：PROPFIND Depth:1
    body = """<?xml version="1.0" encoding="utf-8"?>
<D:propfind xmlns:D="DAV:">
  <D:prop>
    <D:displayname/><D:getcontenttype/><D:resourcetype/>
  </D:prop>
</D:propfind>"""
    with get_client() as c:
        r = c.request("PROPFIND", _enc(u), content=body.encode("utf-8"),
                      headers={"Depth":"1","Content-Type":"application/xml; charset=utf-8"})
        r.raise_for_status()
        root = ET.fromstring(r.text)

    ns = {"D":"DAV:"}
    out: List[str] = []
    for resp in root.findall("D:response", ns):
        href = resp.findtext("D:href", default="", namespaces=ns)
        if not href:
            continue
        absu = _abs(u, href)
        # 跳过目录本身
        if _enc(absu).rstrip("/") == _enc(u).rstrip("/"):
            continue
        if _is_image(absu):
            out.append(absu)
    return out

# ========= DB 基础 =========
def ensure_schema(cur: sqlite3.Cursor):
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS person (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT NOT NULL UNIQUE,
      alias_json TEXT NOT NULL DEFAULT '[]',
      dav_url TEXT NOT NULL,
      enabled INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS image (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      person_id INTEGER NOT NULL,
      url TEXT NOT NULL,
      ext TEXT,
      bytes INTEGER,
      etag TEXT,
      last_modified TEXT,
      created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      active INTEGER NOT NULL DEFAULT 1,
      UNIQUE (person_id, url),
      FOREIGN KEY (person_id) REFERENCES person(id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_image_person_active_id
    ON image (person_id, active, id);
    CREATE TABLE IF NOT EXISTS person_stats (
      person_id INTEGER PRIMARY KEY,
      img_count INTEGER NOT NULL DEFAULT 0,
      min_id INTEGER,
      max_id INTEGER,
      updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      FOREIGN KEY (person_id) REFERENCES person(id) ON DELETE CASCADE
    );
    """)

def seed_persons(conn: sqlite3.Connection):
    if not PERSONS_SEED:
        return
    cur = conn.cursor()
    for name, url, aliases in PERSONS_SEED:
        cur.execute("""
        INSERT INTO person(name, alias_json, dav_url, enabled)
        VALUES (?, json(?), ?, 1)
        ON CONFLICT(name) DO UPDATE SET alias_json=excluded.alias_json, dav_url=excluded.dav_url, enabled=1
        """, (name, __import__("json").dumps(aliases or []), url))
    conn.commit()

def upsert_images(conn: sqlite3.Connection, person_id: int, urls: List[str]):
    cur = conn.cursor()
    cur.execute("UPDATE image SET active=0 WHERE person_id=?", (person_id,))
    for u in urls:
        ext = os.path.splitext(urlparse(u).path)[1].lower()
        cur.execute("""
        INSERT INTO image (person_id, url, ext, active)
        VALUES (?, ?, ?, 1)
        ON CONFLICT(person_id, url) DO UPDATE SET active=1, ext=excluded.ext
        """, (person_id, u, ext))

    cur.execute("""
    INSERT INTO person_stats (person_id, img_count, min_id, max_id)
    SELECT ?, COUNT(*), MIN(id), MAX(id)
    FROM image WHERE person_id=? AND active=1
    ON CONFLICT(person_id) DO UPDATE SET
      img_count=excluded.img_count,
      min_id=excluded.min_id,
      max_id=excluded.max_id,
      updated_at=CURRENT_TIMESTAMP
    """, (person_id, person_id))
    conn.commit()

def sync_person(conn: sqlite3.Connection, name: str):
    cur = conn.cursor()
    row = cur.execute("SELECT id, dav_url, enabled FROM person WHERE name=?", (name,)).fetchone()
    if not row:
        raise RuntimeError(f"person not found: {name}")
    pid, url, enabled = row
    if not enabled:
        return
    urls = list_dir(url)
    upsert_images(conn, pid, urls)

def sync_all():
    db_path = CONFIG["DB_PATH"]
    conn = sqlite3.connect(db_path)
    ensure_schema(conn.cursor())
    seed_persons(conn)
    names = [r[0] for r in conn.execute("SELECT name FROM person WHERE enabled=1").fetchall()]
    for name in names:
        try:
            sync_person(conn, name)
            print(f"[OK] {name}")
        except Exception as e:
            print(f"[ERR] {name}: {e}")
    conn.close()

if __name__ == "__main__":
    sync_all()
