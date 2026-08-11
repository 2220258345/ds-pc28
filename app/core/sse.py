# -*- coding: utf-8 -*-
"""SSE (Server-Sent Events) 客户端管理模块。

维护一个全局客户端队列集合, 提供 register/unregister/broadcast 三个原子操作。
server.py 采集到新数据时调用 broadcast 推送, api_server.py 轮询数据库检测到
新数据时也调用 broadcast 推送, 两个入口共享同一套客户端池。
"""
import json
import queue
import threading

_sse_clients = set()
_sse_lock = threading.Lock()


def sse_register():
    """注册一个 SSE 客户端, 返回其消息队列。"""
    q = queue.Queue()
    with _sse_lock:
        _sse_clients.add(q)
    return q


def sse_unregister(q):
    """注销 SSE 客户端。"""
    with _sse_lock:
        _sse_clients.discard(q)


def sse_broadcast(event, data):
    """向所有 SSE 客户端推送事件, 自动清理已满/失效的队列。"""
    msg = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
    dead = []
    with _sse_lock:
        clients = list(_sse_clients)
    for q in clients:
        try:
            q.put_nowait(msg)
        except queue.Full:
            dead.append(q)
    if dead:
        with _sse_lock:
            for q in dead:
                _sse_clients.discard(q)


def client_count():
    """返回当前已注册的客户端数量 (用于日志)。"""
    with _sse_lock:
        return len(_sse_clients)
