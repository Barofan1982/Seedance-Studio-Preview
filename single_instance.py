"""One Studio window per data directory, with local activation on repeat launch."""
from __future__ import annotations

import ctypes
import hashlib
import os
from pathlib import Path
import time

from PySide6.QtCore import QObject, QLockFile, Signal, Qt
from PySide6.QtNetwork import QLocalServer, QLocalSocket


def activate_window(window):
    """Restore a minimized window without resizing an already visible window."""
    if window.isMinimized():
        window.setWindowState(window.windowState() & ~Qt.WindowState.WindowMinimized)
        window.show()
    else:
        window.show()
    window.raise_()
    window.activateWindow()
    if window.windowHandle():
        window.windowHandle().requestActivate()


class SingleInstance(QObject):
    activation_requested = Signal()

    def __init__(self, directory, parent=None):
        super().__init__(parent)
        self.directory = Path(directory).resolve()
        identity = os.path.normcase(str(self.directory))
        self.server_name = 'SeedanceStudio-' + hashlib.sha256(identity.encode()).hexdigest()[:32]
        self.lock = QLockFile(str(self.directory / 'studio.instance.lock'))
        # A slow or long-running generation never makes a live owner stale.
        # QLockFile still detects and removes a dead process's stale lock.
        self.lock.setStaleLockTime(0)
        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self.server.newConnection.connect(self._accept)
        self._buffers = {}
        self._owned = False

    def acquire_or_activate(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.lock.tryLock(0):
            self._owned = True
            # Only the lock owner can clean up an abandoned local endpoint.
            QLocalServer.removeServer(self.server_name)
            if not self.server.listen(self.server_name):
                detail = self.server.errorString()
                self.close()
                raise RuntimeError('无法创建工作室启动保护：' + detail)
            return True
        if self.lock.error() != QLockFile.LockError.LockFailedError:
            raise RuntimeError('无法锁定工作室数据目录，请确认该目录可以写入。')
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            socket = QLocalSocket()
            socket.connectToServer(self.server_name)
            if socket.waitForConnected(200):
                self._allow_foreground()
                socket.write(b'activate\n')
                written = socket.waitForBytesWritten(500) or socket.bytesToWrite() == 0
                if written:
                    # The first window may still be initializing its controller.
                    # Its listener retains the activation until the event loop starts.
                    socket.waitForReadyRead(700)
                    socket.disconnectFromServer()
                    return False
            socket.abort()
            time.sleep(.05)
        raise RuntimeError('工作室已在启动或运行，暂时无法唤醒。请切换到已有窗口后重试。')

    def _allow_foreground(self):
        if os.name != 'nt':
            return
        try:
            valid, pid, _host, _app = self.lock.getLockInfo()
            if valid:
                ctypes.windll.user32.AllowSetForegroundWindow(int(pid))
        except (AttributeError, OSError, TypeError, ValueError):
            pass

    def _accept(self):
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            self._buffers[socket] = b''
            socket.readyRead.connect(lambda socket=socket: self._read(socket))
            socket.disconnected.connect(lambda socket=socket: self._discard(socket))
            self._read(socket)

    def _read(self, socket):
        if socket not in self._buffers:
            return
        data = self._buffers[socket] + bytes(socket.read(64))
        self._buffers[socket] = data
        if b'\n' in data:
            if data.split(b'\n', 1)[0] == b'activate':
                self.activation_requested.emit()
                socket.write(b'ok\n')
                socket.flush()
            socket.disconnectFromServer()
        elif len(data) >= 64:
            socket.abort()

    def _discard(self, socket):
        self._buffers.pop(socket, None)
        socket.deleteLater()

    def close(self):
        for socket in list(self._buffers):
            socket.abort()
        self.server.close()
        if self._owned:
            self.lock.unlock()
            self._owned = False
