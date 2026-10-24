"""
kabala:// deep links and single-instance handling.

After paying, the browser opens kabala://payment-success. Windows starts Kabala with that URL as an
argument; if Kabala is already running, the new process hands the URL to the running one over a
local socket and exits, so the open window updates to Pro immediately.
"""

from __future__ import annotations

import getpass
import logging
import sys

from PySide6.QtCore import QObject, QSettings, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

log = logging.getLogger(__name__)

SCHEME = "kabala"
_KEY = rf"HKEY_CURRENT_USER\Software\Classes\{SCHEME}"


def link_from_argv(argv: list[str]) -> str | None:
    return next((a for a in argv[1:] if a.lower().startswith(f"{SCHEME}://")), None)


def launch_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" "%1"'
    exe = sys.executable.replace("python.exe", "pythonw.exe")
    return f'"{exe}" -m kabala "%1"'


def register_protocol(settings: QSettings | None = None) -> bool:
    """Registers kabala:// for the current user (HKCU, no admin rights). The installer does the same."""
    if settings is None:
        if sys.platform != "win32":
            return False  # macOS registers URL schemes through Info.plist (installer stage)
        settings = QSettings(_KEY, QSettings.Format.NativeFormat)
    settings.setValue("Default", "URL:Kabala")
    settings.setValue("URL Protocol", "")
    settings.setValue("shell/open/command/Default", launch_command())
    settings.sync()
    return True


class SingleInstance(QObject):
    """First instance listens; later instances forward their message (a deep link or "activate")."""

    message = Signal(str)

    def __init__(self, name: str | None = None) -> None:
        super().__init__()
        self.name = name or f"kabala-{getpass.getuser()}"
        self.server: QLocalServer | None = None
        probe = QLocalSocket()
        probe.connectToServer(self.name)
        self.secondary = probe.waitForConnected(300)
        probe.abort()
        if not self.secondary:
            QLocalServer.removeServer(self.name)  # stale socket after a crash
            self.server = QLocalServer(self)
            self.server.newConnection.connect(self._on_connection)
            if not self.server.listen(self.name):
                log.warning("single-instance server failed: %s", self.server.errorString())

    def send(self, message: str) -> bool:
        """Sends one line and waits until the running instance has read it (it closes the connection)."""
        sock = QLocalSocket()
        sock.connectToServer(self.name)
        if not sock.waitForConnected(1000):
            return False
        sock.write((message.replace("\n", " ") + "\n").encode("utf-8"))
        sock.flush()
        sock.waitForBytesWritten(1000)
        # Closing first could drop unread bytes on Windows named pipes: let the receiver close.
        sock.waitForDisconnected(3000)
        sock.abort()
        return True

    def _on_connection(self) -> None:
        assert self.server is not None
        while self.server.hasPendingConnections():
            self._accept(self.server.nextPendingConnection())

    def _accept(self, sock: QLocalSocket) -> None:
        # One newline-terminated message per connection; we close the connection once it's read,
        # which tells the sender it can exit.
        buf = bytearray()

        def drain() -> None:
            buf.extend(bytes(sock.readAll()))
            if b"\n" in buf or len(buf) > 4096:
                line = bytes(buf).split(b"\n", 1)[0].decode("utf-8", errors="replace").strip()
                buf.clear()
                sock.disconnectFromServer()
                if line:
                    self.message.emit(line[:2048])

        sock.readyRead.connect(drain)
        sock.disconnected.connect(sock.deleteLater)
        if sock.bytesAvailable():
            drain()
