"""
Persistent keep-alive HTTP client — kills the per-poll TLS stall.

Why not ``requests``: it opens a fresh connection per call, and on the
badge DNS, TCP connect and the TLS handshake all run **holding the GIL**
(measured: 350ms of mbedTLS handshake per poll against the Cloudflare
origin) — a visible whole-scheduler stall on every poll. This client
pays that cost once and reuses the connection: the steady-state poll is
just send/recv, whose socket waits release the GIL, so animations keep
running. The 5s poll cadence keeps the connection warm through
Cloudflare's idle timeout; if the server does drop it, get() retries
once on a fresh connection.

No certificate validation (CERT_NONE) — deliberate, matches the
documented threat model for this jacket.

MicroPython (badge/ESP32, ``tls``/``ussl`` module, stream sockets) and
CPython (tests, ``ssl``, recv/sendall sockets) are both supported.
"""

import socket

try:
    import json
except ImportError:
    json = None


def _wrap_tls(sock, host):
    try:
        import tls as _tls  # modern MicroPython (badge v1.6.0 firmware)
    except ImportError:
        try:
            import ussl as _tls  # older MicroPython
        except ImportError:
            import ssl as _tls  # CPython
    if hasattr(_tls, "SSLContext") and hasattr(_tls, "PROTOCOL_TLS_CLIENT"):
        ctx = _tls.SSLContext(_tls.PROTOCOL_TLS_CLIENT)
        try:
            ctx.check_hostname = False  # CPython: must precede CERT_NONE
        except AttributeError:
            pass
        ctx.verify_mode = _tls.CERT_NONE
        return ctx.wrap_socket(sock, server_hostname=host)
    return _tls.wrap_socket(sock, server_hostname=host)


class PersistentClient:
    def __init__(self, base_url, timeout=10):
        proto, rest = base_url.split("://", 1)
        rest = rest.rstrip("/")
        if ":" in rest:
            host, port = rest.split(":", 1)
            port = int(port)
        else:
            host = rest
            port = 443 if proto == "https" else 80
        self._tls = proto == "https"
        self._host = host
        self._port = port
        self._timeout = timeout
        self._sock = None
        self._buf = b""

    # -- connection lifecycle -------------------------------------------------

    def _connect(self):
        addr = socket.getaddrinfo(self._host, self._port)[0][-1]
        s = socket.socket()
        s.settimeout(self._timeout)
        s.connect(addr)
        if self._tls:
            s = _wrap_tls(s, self._host)
        # Normalise the two socket dialects once: MicroPython sockets are
        # streams (read/write); CPython raw+ssl sockets use recv/sendall.
        self._read = s.read if hasattr(s, "read") else s.recv
        self._write = s.sendall if hasattr(s, "sendall") else s.write
        self._sock = s
        self._buf = b""

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
        self._sock = None
        self._buf = b""

    # -- buffered reading -------------------------------------------------------

    def _read_until(self, marker):
        while self._buf.find(marker) < 0:
            chunk = self._read(256)
            if not chunk:
                raise OSError("connection closed")
            self._buf += chunk
        i = self._buf.find(marker) + len(marker)
        out = self._buf[:i]
        self._buf = self._buf[i:]
        return out

    def _read_n(self, n):
        while len(self._buf) < n:
            chunk = self._read(min(1024, n - len(self._buf)))
            if not chunk:
                raise OSError("connection closed")
            self._buf += chunk
        out = self._buf[:n]
        self._buf = self._buf[n:]
        return out

    def _read_chunked(self):
        body = b""
        while True:
            line = self._read_until(b"\r\n")
            size = int(line.strip().split(b";")[0], 16)
            if size == 0:
                self._read_until(b"\r\n")  # trailing CRLF, no trailers expected
                return body
            body += self._read_n(size)
            self._read_until(b"\r\n")  # chunk terminator

    # -- the two verbs we need ---------------------------------------------------

    def get(self, path, headers=None):
        """GET ``path``; returns (status:int, body:str). The kept-alive
        socket can die between polls (server idle timeout, watchdog worker
        respawn) — retry exactly once on a fresh connection."""
        return self._attempt("GET", path, headers, None)

    def post(self, path, obj, headers=None):
        """POST ``obj`` (a dict, sent as JSON) to ``path``; returns
        (status:int, body:str). Same single-retry contract as get() —
        safe here because score submission is idempotent enough (a rare
        double-submit just re-records the same score)."""
        return self._attempt("POST", path, headers, json.dumps(obj))

    def _attempt(self, method, path, headers, body):
        for attempt in (0, 1):
            try:
                if self._sock is None:
                    self._connect()
                return self._request(method, path, headers, body)
            except Exception:
                self.close()
                if attempt:
                    raise

    def _request(self, method, path, headers, body):
        req = "{} {} HTTP/1.1\r\nHost: {}\r\nConnection: keep-alive\r\n".format(
            method, path, self._host)
        if headers:
            for k in headers:
                req += "{}: {}\r\n".format(k, headers[k])
        if body is not None:
            body = body.encode() if not isinstance(body, bytes) else body
            req += "Content-Type: application/json\r\n"
            req += "Content-Length: {}\r\n".format(len(body))
        req += "\r\n"
        self._write(req.encode())
        if body is not None:
            self._write(body)

        head = self._read_until(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        status = int(lines[0].split(b" ", 2)[1])
        length = None
        chunked = False
        close_after = False
        for ln in lines[1:]:
            low = ln.lower()
            if low.startswith(b"content-length:"):
                length = int(ln.split(b":", 1)[1])
            elif low.startswith(b"transfer-encoding:") and b"chunked" in low:
                chunked = True
            elif low.startswith(b"connection:") and b"close" in low:
                close_after = True

        if chunked:
            body = self._read_chunked()
        elif length is not None:
            body = self._read_n(length)
        else:
            # No framing info: body runs to EOF and the connection dies.
            body = self._buf
            self._buf = b""
            while True:
                chunk = self._read(512)
                if not chunk:
                    break
                body += chunk
            close_after = True

        if close_after:
            self.close()
        return status, body.decode()
