r"""One running copy of the application, and a way to raise its window.

Two mechanisms are combined because each covers a gap in the other:

* A Windows **named mutex** is the authority on "am I the first copy?". The
  kernel creates it atomically, so two copies launched in the same instant
  cannot both believe they won. A lock file cannot promise that, and a stale
  lock file after a crash would lock the user out of their own application.
* A **local socket** (a named pipe on Windows) carries a short message from
  the second copy to the first, which is how a second launch raises the
  existing window instead of silently doing nothing.

The mutex is taken first. If it already exists, this copy is the second one:
it sends its message and exits without ever creating a window.

Both names are per-user. Named pipes and mutexes in the ``Local\`` namespace
live in a session-wide namespace shared by everything that session runs, and
on a machine with several accounts two people running the application must not
collide or be able to reach each other's window. The user name is hashed
rather than embedded so the name never exposes an account name.
"""

from __future__ import annotations

import getpass
import hashlib
import sys
import time

from PySide6.QtCore import QCoreApplication, QEventLoop, QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from app.branding import BRAND
from app.diagnostics.logger import get_logger

logger = get_logger("winint.single_instance")

#: ``CreateMutexW`` sets this as the last error when the name already exists.
ERROR_ALREADY_EXISTS = 183

#: Longest message accepted from another copy. A payload arrives from outside
#: this process, so it is treated as untrusted input: it is length-capped and
#: only ever used to decide whether to show the window.
MAX_PAYLOAD_CHARS = 4096

#: Bounded waits, in milliseconds. A hung first copy must never leave the
#: second copy blocked with no window and no message.
CONNECT_TIMEOUT_MS = 500
WRITE_TIMEOUT_MS = 500
READ_TIMEOUT_MS = 500

#: One slice of the bounded wait while the message is pushed out.
_DRAIN_SLICE_MS = 25


def per_user_key(base: str = BRAND.single_instance_key) -> str:
    """``base`` with a short hash of the current user name appended.

    The hash keeps two accounts on one machine independent without putting a
    user name into a name that other processes can enumerate.
    """
    try:
        user = getpass.getuser()
    except Exception:  # pragma: no cover - only when the environment is broken
        user = "unknown"
    digest = hashlib.sha1(user.encode("utf-8", "replace")).hexdigest()[:12]
    return f"{base}.{digest}"


class SingleInstance(QObject):
    """Guard that admits one copy of the application and relays activations.

    Emits :attr:`activate_requested` when another copy asks for the window to
    be brought to the front. The signal is delivered on the thread that owns
    this object, so it is safe to connect straight to a widget slot.
    """

    #: The payload sent by the other copy, already length-capped.
    activate_requested = Signal(str)

    def __init__(self, key: str | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._key = key if key is not None else per_user_key()
        self._server: QLocalServer | None = None
        self._mutex_handle: int | None = None
        self.last_error: str | None = None
        """Plain-English reason the guard could not take ownership.

        Set when the first copy answered the pipe but never read the message,
        which is what a hung or frozen first copy looks like. The caller uses
        it to say "appears to be running but not responding" rather than
        simply exiting with no explanation.
        """

    @property
    def key(self) -> str:
        """The resolved per-user name used for the mutex and the socket."""
        return self._key

    def acquire(self, payload: str = "activate") -> bool:
        """True when this copy owns the application; False when another does.

        On False the existing copy has been asked to show its window (or
        :attr:`last_error` explains why that request could not be delivered).
        """
        self.last_error = None
        if self._server is not None:
            return True

        if not self._take_mutex():
            self._notify_existing(payload)
            return False

        return self._start_server(payload)

    def release(self) -> None:
        """Give up ownership. Safe to call more than once."""
        if self._server is not None:
            self._server.close()
            QLocalServer.removeServer(self._key)
            self._server.deleteLater()
            self._server = None
        self._release_mutex()

    # --- mutex ------------------------------------------------------------

    def _take_mutex(self) -> bool:
        """Claim the kernel mutex. True when this copy created it.

        On platforms without the Windows API there is no mutex to take and the
        socket alone decides, which is enough for the test suite and for a
        developer running on another operating system.
        """
        if sys.platform != "win32":
            return True

        import ctypes

        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.restype = ctypes.c_void_p
            kernel32.CreateMutexW.argtypes = (
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_wchar_p,
            )
            handle = kernel32.CreateMutexW(None, 0, rf"Local\{self._key}")
            error = ctypes.get_last_error()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Could not use the Windows lock, relying on the socket: %s", exc)
            return True

        if not handle:
            logger.warning("Windows refused to create the lock (error %s)", error)
            return True

        if error == ERROR_ALREADY_EXISTS:
            self._close_handle(handle)
            return False

        self._mutex_handle = int(handle)
        return True

    def _release_mutex(self) -> None:
        if self._mutex_handle is None:
            return
        handle, self._mutex_handle = self._mutex_handle, None
        self._close_handle(handle)

    @staticmethod
    def _close_handle(handle: int) -> None:
        if sys.platform != "win32":  # pragma: no cover - never reached
            return
        import ctypes

        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
            kernel32.CloseHandle(ctypes.c_void_p(handle))
        except Exception:  # pragma: no cover - defensive
            logger.debug("Closing the Windows lock handle failed", exc_info=True)

    # --- socket -----------------------------------------------------------

    def _start_server(self, payload: str) -> bool:
        server = QLocalServer(self)
        # A no-op on Windows, but on POSIX a socket file survives a crash and
        # would make every later launch think a copy is already running.
        QLocalServer.removeServer(self._key)
        server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        if not server.listen(self._key):
            reason = server.errorString()
            server.deleteLater()
            self._release_mutex()
            logger.info("Could not listen for other copies: %s", reason)
            self._notify_existing(payload)
            return False

        server.newConnection.connect(self._on_new_connection)
        self._server = server
        return True

    def _notify_existing(self, payload: str) -> None:
        """Ask the copy that is already running to show its window."""
        socket = QLocalSocket()
        socket.connectToServer(self._key)
        if not socket.waitForConnected(CONNECT_TIMEOUT_MS):
            self.last_error = "The application is already running but did not answer."
            logger.info("Existing copy did not answer: %s", socket.errorString())
            return

        socket.write(payload[:MAX_PAYLOAD_CHARS].encode("utf-8"))
        socket.flush()
        if not self._drain(socket):
            self.last_error = (
                "The application appears to be running but is not responding."
            )
            logger.warning("Existing copy accepted a connection but not the message")
        socket.disconnectFromServer()

    @staticmethod
    def _drain(socket: QLocalSocket) -> bool:
        """Push the queued bytes out within :data:`WRITE_TIMEOUT_MS`.

        Qt hands a local socket write to the event loop, and the copy sending
        this message has not started one yet: it is still deciding whether it
        is allowed to run at all. Without pumping events here the message
        would sit in Qt's buffer until the process exited and be lost, so the
        wait is interleaved with short, bounded event processing.
        """
        deadline = time.monotonic() + WRITE_TIMEOUT_MS / 1000.0
        application = QCoreApplication.instance()
        while socket.bytesToWrite() > 0:
            if socket.waitForBytesWritten(_DRAIN_SLICE_MS):
                continue
            if socket.state() == QLocalSocket.LocalSocketState.UnconnectedState:
                return False
            if application is not None:
                application.processEvents(
                    QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents,
                    _DRAIN_SLICE_MS,
                )
            if time.monotonic() >= deadline:
                return False
        return True

    def _on_new_connection(self) -> None:
        if self._server is None:  # pragma: no cover - defensive
            return
        while (socket := self._server.nextPendingConnection()) is not None:
            self._read_payload(socket)

    def _read_payload(self, socket: QLocalSocket) -> None:
        if socket.bytesAvailable() == 0:
            socket.waitForReadyRead(READ_TIMEOUT_MS)
        raw = bytes(socket.readAll().data())
        socket.disconnectFromServer()
        socket.deleteLater()
        payload = raw.decode("utf-8", "replace")[:MAX_PAYLOAD_CHARS]
        logger.info("Another copy asked for the window to be shown")
        self.activate_requested.emit(payload)
