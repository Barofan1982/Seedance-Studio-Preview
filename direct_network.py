"""Domestic services bypass system proxies; retry only before HTTP bytes are sent."""
import http.client
import socket
import urllib.request

CONNECT_TIMEOUT_SECONDS = 30
CONNECT_ATTEMPTS = 2


class BeforeSendConnectionError(OSError):
    request_sent = False


class _ConnectPolicy:
    def connect(self):
        read_timeout = self.timeout
        for attempt in range(CONNECT_ATTEMPTS):
            self.timeout = CONNECT_TIMEOUT_SECONDS
            try:
                super().connect()
                if self.sock:
                    self.sock.settimeout(read_timeout)
                self.timeout = read_timeout
                return
            except (TimeoutError, ConnectionError, socket.gaierror) as error:
                self.close()
                if attempt + 1 == CONNECT_ATTEMPTS:
                    raise BeforeSendConnectionError('直连建立连接失败；HTTP 请求尚未发送。') from error


class DirectHTTPConnection(_ConnectPolicy, http.client.HTTPConnection):
    pass


class DirectHTTPSConnection(_ConnectPolicy, http.client.HTTPSConnection):
    pass


class DirectHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, request):
        return self.do_open(DirectHTTPConnection, request)


class DirectHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        return self.do_open(DirectHTTPSConnection, request, context=self._context)


def direct_handlers():
    return (urllib.request.ProxyHandler({}), DirectHTTPHandler(), DirectHTTPSHandler())
