#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
xiaomi-moviepilot-bridge
=======================

小爱音箱 -> SongLoft MIoT 插件 Webhook -> MoviePilot v2 订阅 桥接服务

链路说明：
  用户对小爱音箱说「订阅电影XXX」
  -> SongLoft MIoT 插件轮询到新对话消息
  -> 插件向本服务配置的 Webhook URL 发送 POST(JSON)
  -> 本服务解析语音文本、提取电影/剧集名
  -> 调用 MoviePilot v2 API 搜索媒体并新增订阅

零依赖实现：仅使用 Python 标准库，可直接 Docker 部署（python:3.12-alpine）。

环境变量：
  MP_BASE_URL   MoviePilot 地址，例如 http://192.168.1.10:3000 （默认 http://127.0.0.1:3000）
  MP_TOKEN      MoviePilot API Token（设置 -> 安全 -> API Token）
  PORT          本服务监听端口（默认 9080）
  WEBHOOK_KEY   可选，Webhook URL 访问密钥（防局域网内他人滥用），
                配置后请求需为 /webhook?key=你的密钥
  TV_SEASON     剧集订阅默认季数（默认 1）
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
MP_BASE_URL = os.environ.get("MP_BASE_URL", "http://127.0.0.1:3000").rstrip("/")
MP_TOKEN = os.environ.get("MP_TOKEN", "").strip()
PORT = int(os.environ.get("PORT", "9080"))
WEBHOOK_KEY = os.environ.get("WEBHOOK_KEY", "").strip()
TV_SEASON = int(os.environ.get("TV_SEASON", "1"))

MAX_BODY = 2 * 1024 * 1024  # 请求体上限 2MB
REQUEST_TIMEOUT = 15        # 调用 MoviePilot 超时（秒）

# --------------------------------------------------------------------------
# 语音文本解析规则（按优先级匹配）
# --------------------------------------------------------------------------
# 说明：小爱转写文本通常是「订阅电影沙丘2」「帮我订阅电视剧XXX」之类，
# 前缀（帮我/我想/请…）与书名号均为可选项，正则做了容错。
# 类型词后的 `\s*[《\[]?\s*` 允许「订阅电影 沙丘2」这类带空格/书名号的说法
TV_PATTERNS = [
    re.compile(r"订阅(?:一部|个)?(?:电视剧|剧集|连续剧|美剧|英剧|韩剧|日剧|动漫|动画|剧)\s*[《\[]?\s*(?P<name>[^》\],，。！？!?、\s]{1,20})[》\]]?"),
]
MOVIE_PATTERNS = [
    re.compile(r"订阅(?:一部|个)?电影\s*[《\[]?\s*(?P<name>[^》\],，。！？!?、\s]{1,20})[》\]]?"),
    re.compile(r"订阅\s*[《\[]\s*(?P<name>[^》\]]{1,30})\s*[》\]]"),
]
GENERIC_PATTERN = re.compile(r"订阅(?!(?:[ \t]*一[部个]|[ \t]*(?:电影|电视剧|剧集|连续剧|美剧|英剧|韩剧|日剧|动漫|动画)))[ \t]*(?P<name>[^，。！？!?、\s]{1,16})")


def parse_subscribe_intent(text: str):
    """从一句语音转写文本中提取 (媒体标题, 类型 movie/tv)。找不到返回 None。"""
    if not text:
        return None
    t = text.strip()
    for p in TV_PATTERNS:
        m = p.search(t)
        if m:
            return clean_title(m.group("name")), "tv"
    for p in MOVIE_PATTERNS:
        m = p.search(t)
        if m:
            return clean_title(m.group("name")), "movie"
    # 兜底：任何「订阅XXX」
    m = GENERIC_PATTERN.search(t)
    if m:
        return clean_title(m.group("name")), "movie"
    return None


def clean_title(name: str) -> str:
    t = re.sub(r"[\s《》【】\"'“”‘’]", "", name)
    # 去掉口语尾巴：「沙丘2这部」「流浪地球2这部电影」
    t = re.sub(r"(?:这部|这部电影|这部剧|这部片|这部片子|这部动画|这部动漫)?$", "", t)
    return t


# --------------------------------------------------------------------------
# MoviePilot API 调用
# --------------------------------------------------------------------------
def mp_request(method: str, path: str, body: dict = None):
    """调用 MoviePilot API，返回 (http_code, json)。"""
    url = MP_BASE_URL + path
    headers = {"Accept": "application/json"}
    if MP_TOKEN:
        # MoviePilot v2 会把 Authorization: Bearer 当作 JWT 解析；
        # API Token 必须通过 X-API-KEY 头传递，不能用 Bearer。
        headers["X-API-KEY"] = MP_TOKEN
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
            code = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        code = e.code
    except Exception as e:  # 网络错误等
        return 0, {"error": str(e)}
    try:
        return code, json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return code, {"raw": raw[:500]}


def search_media(keyword: str):
    """按标题搜索媒体，返回最佳匹配条目或 None；鉴权/接口失败时抛出异常。"""
    q = urllib.parse.quote(keyword)
    code, data = mp_request("GET", f"/api/v1/media/search?title={q}&type=media&count=10")
    if code not in (200, 201):
        raise RuntimeError(describe_mp_error(code, data, keyword))
    if not isinstance(data, list) or not data:
        return None
    kw = keyword.strip().lower()
    for r in data:
        for field in ("title", "original_title"):
            if (r.get(field) or "").strip().lower() == kw:
                return r
    for r in data:
        title = (r.get("title") or "").strip().lower()
        original = (r.get("original_title") or "").strip().lower()
        if kw in title or kw in original:
            return r
    return data[0]


TYPE_MAP = {"movie": "电影", "tv": "电视剧"}


def subscribe_media(title: str, mtype: str, media: dict):
    """构造订阅参数并调用新增订阅接口。"""
    # MoviePilot v2 期望 type 为中文枚举：电影 / 电视剧
    mp_type = media.get("type") or TYPE_MAP.get(mtype, mtype)
    payload = {
        "name": title,
        "type": mp_type,
        "tmdbid": int(media["tmdb_id"]) if media.get("tmdb_id") else None,
        "doubanid": media.get("douban_id"),
        "year": str(media["year"]) if media.get("year") else None,
    }
    if mtype == "tv" or mp_type == "电视剧":
        payload["season"] = TV_SEASON
    # 去掉值为 None 的字段
    payload = {k: v for k, v in payload.items() if v is not None}
    return mp_request("POST", "/api/v1/subscribe/", payload)


# --------------------------------------------------------------------------
# Webhook 处理
# --------------------------------------------------------------------------
def handle_webhook(payload: dict):
    """
    SongLoft MIoT 插件推送格式：
    {
      "account_id": "...", "device_id": "...", "device_name": "...",
      "messages": [ { "message": { "response": { "answer": [
          { "question": "用户说的话", "intention": {"query": "..."}, ... }
      ] } } } ]
    }
    """
    messages = payload.get("messages") or []
    results = []
    for msg in messages:
        inner = msg.get("message") or {}
        answers = ((inner.get("response") or {}).get("answer")) or []
        for ans in answers:
            text = ans.get("question") or ((ans.get("intention") or {}).get("query")) or ""
            if not text:
                continue
            intent = parse_subscribe_intent(text)
            if not intent:
                continue
            title, mtype = intent
            results.append({"spoken": text, "title": title, "type": mtype})

    # 去重（同一句或同一标题只处理一次）
    seen, todo = set(), []
    for r in results:
        key = (r["title"], r["type"])
        if key in seen:
            continue
        seen.add(key)
        todo.append(r)

    output = []
    for item in todo:
        out = {"spoken": item["spoken"], "title": item["title"], "type": item["type"]}
        try:
            media = search_media(item["title"])
            if not media:
                out["status"] = "not_found"
                out["message"] = f"未搜索到「{item['title']}」，请尝试更准确的中文或英文片名"
            else:
                code, resp = subscribe_media(media.get("title") or item["title"], item["type"], media)
                out["matched"] = {
                    "title": media.get("title"),
                    "year": media.get("year"),
                    "type": media.get("type"),
                    "tmdb_id": media.get("tmdb_id"),
                }
                if code in (200, 201):
                    out["status"] = "ok"
                    out["message"] = f"订阅成功：{media.get('title')}"
                else:
                    out["status"] = "error"
                    out["code"] = code
                    out["message"] = describe_mp_error(code, resp, media.get("title"))
        except Exception as e:
            out["status"] = "exception"
            out["message"] = str(e)
        output.append(out)
    return output


def describe_mp_error(code: int, resp, title: str) -> str:
    """把 MoviePilot 的错误响应转成人话。"""
    msg = resp.get("message") or resp.get("detail") or resp.get("msg") or (json.dumps(resp, ensure_ascii=False)[:200] if resp else "")
    if isinstance(msg, list):  # FastAPI 校验错误
        msg = "; ".join(str(e.get("msg", e)) for e in msg)
    if code == 0:
        return f"无法连接 MoviePilot（{MP_BASE_URL}），请检查地址与网络"
    if "已存在" in str(msg) or "exist" in str(msg).lower():
        return f"「{title}」已在订阅列表中"
    if code in (401, 403):
        return "MoviePilot 鉴权失败：请检查 MP_TOKEN 是否与 设置 -> 系统 -> 基础设置 -> API令牌 中的值一致"
    return f"MoviePilot 返回错误({code})：{msg}"


# --------------------------------------------------------------------------
# HTTP 服务
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "XiaoAiMPBridge/1.0"

    def log_message(self, fmt, *args):  # 走统一日志
        log("[http] " + fmt % args)

    def _json(self, code: int, obj: dict):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            return b""
        return self.rfile.read(length)

    def do_GET(self):
        if self.path.split("?")[0] == "/health":
            return self._json(200, {"status": "ok", "ts": int(time.time())})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if WEBHOOK_KEY:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if qs.get("key", [""])[0] != WEBHOOK_KEY:
                return self._json(403, {"error": "invalid webhook key"})
        raw = self._read_body()
        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return self._json(400, {"error": "invalid json"})
        started = time.time()
        output = handle_webhook(payload)
        log(f"[webhook] messages={len(payload.get('messages') or [])} "
            f"subscribe_attempts={len(output)} cost={int((time.time()-started)*1000)}ms")
        self._json(200, {"code": 0, "results": output})


def log(msg: str):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def main():
    if not MP_TOKEN:
        log("警告: 未配置 MP_TOKEN，MoviePilot 调用将使用无鉴权请求")
    log(f"MoviePilot: {MP_BASE_URL}")
    log(f"Webhook 地址: http://0.0.0.0:{PORT}/webhook" + (f"?key={WEBHOOK_KEY}" if WEBHOOK_KEY else ""))
    log(f"TV_SEASON 默认季: {TV_SEASON}")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log("服务已启动，等待 SongLoft Webhook 推送...")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
