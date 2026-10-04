# -*- coding: utf-8 -*-
"""kvserver —— 仅标准库的最小「数据库」服务，用于连接池练习。

协议（每行一条命令，CRLF 或 LF 分隔，UTF-8）：

    PING        -> PONG
    GET key     -> VALUE <text> | NULL
    SET key v   -> OK
    MULTI       -> OK            （开始事务）
    EXEC        -> COMMITTED     （提交；无事务时回 NOTXN）
    ROLLBACK    -> ROLLED BACK   （中止；无事务时回 NOTXN）
    SLEEP ms    -> SLEPT         （服务端阻塞 ms 毫秒，用于制造客户端超时；
                                  超时后这条迟到响应会残留在客户端读缓冲里）
    STATUS      -> TX=0|1        （该连接上是否还有未结束的事务）
    KILL        -> （直接关闭连接，不回任何内容）

服务端为每条 TCP 连接独立保存事务状态与 kv 视图，完全不区分连接
是否来自连接池——所以上一个使用者残留的事务 / 迟到响应 / socket
超时设置，下一个使用者都能通过 STATUS / GET / 读超时观察到。
"""

from __future__ import annotations

import socketserver
import threading
import time


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        tx = False
        kv = {}

        def reply(line: str) -> None:
            self.wfile.write((line + "\n").encode("utf-8"))
            self.wfile.flush()

        try:
            for raw in self.rfile:
                line = raw.decode("utf-8", errors="replace").strip()
                parts = line.split(" ", 2)
                cmd = parts[0] if parts else ""

                if cmd == "PING":
                    reply("PONG")
                elif cmd == "GET":
                    key = parts[1] if len(parts) > 1 else ""
                    reply("VALUE " + kv[key] if key in kv else "NULL")
                elif cmd == "SET":
                    key = parts[1] if len(parts) > 1 else ""
                    val = parts[2] if len(parts) > 2 else ""
                    kv[key] = val
                    reply("OK")
                elif cmd == "MULTI":
                    tx = True
                    reply("OK")
                elif cmd == "EXEC":
                    if tx:
                        tx = False
                        reply("COMMITTED")
                    else:
                        reply("NOTXN")
                elif cmd == "ROLLBACK":
                    if tx:
                        tx = False
                        reply("ROLLED BACK")
                    else:
                        reply("NOTXN")
                elif cmd == "SLEEP":
                    ms = int(parts[1]) if len(parts) > 1 else 0
                    time.sleep(ms / 1000.0)
                    reply("SLEPT")
                elif cmd == "STATUS":
                    reply("TX=%d" % (1 if tx else 0))
                elif cmd == "KILL":
                    return
                else:
                    reply("ERROR unknown command: " + cmd)
        except (ConnectionResetError, BrokenPipeError):
            return


class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """后台线程 TCP 服务；port=0 时自动分配端口。"""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        super().__init__((host, port), _Handler)
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.shutdown()
        self.server_close()
