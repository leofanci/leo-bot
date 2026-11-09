"""terminal_session.py — a shell or agent CLI attached to a PTY.

The bottom integrated terminal runs it here (Settings, Engines picks which); bytes are bridged to the
browser's xterm.js over the /ws/terminal WebSocket. Content writes must still
go through osctl — see `dashboard/ai_rules.py` and CLAUDE.md.
"""
import fcntl
import os
import pty
import signal
import struct
import termios
import threading

# The user's own umask, saved by the server before it goes owner-only: the
# full-trust terminal is the user's shell, so files made there stay normal.
USER_UMASK = None


class TerminalSession:
    def __init__(self, cmd, cwd):
        self.cmd = cmd
        self.cwd = cwd
        self.pid = None
        self.fd = None
        self._reader = None
        self._alive = False

    def start(self, on_output):
        self.pid, self.fd = pty.fork()
        if self.pid == 0:  # child
            try:
                os.chdir(self.cwd)
                if USER_UMASK is not None:
                    os.umask(USER_UMASK)
                # Finder-launched GUI apps inherit no TERM, so claude detects no
                # color support and renders monochrome. Advertise a color terminal.
                env = {**os.environ, "TERM": "xterm-256color", "COLORTERM": "truecolor"}
                os.execvpe(self.cmd[0], self.cmd, env)
            except Exception:
                os._exit(1)
        self._alive = True

        def pump():
            while self._alive:
                try:
                    data = os.read(self.fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                on_output(data)
            self._alive = False

        self._reader = threading.Thread(target=pump, daemon=True)
        self._reader.start()

    def write(self, data):
        if self.fd is not None:
            try:
                os.write(self.fd, data)
            except OSError:
                pass

    def resize(self, cols, rows):
        if self.fd is None:
            return
        try:
            cols, rows = int(cols), int(rows)
        except (TypeError, ValueError):
            return
        cols, rows = max(1, min(cols, 1000)), max(1, min(rows, 1000))  # struct "H" overflows past 65535
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, winsize)
            if self.pid:
                os.kill(self.pid, signal.SIGWINCH)
        except OSError:
            pass

    @staticmethod
    def _reap(pid):
        try:
            os.waitpid(pid, 0)
        except OSError:
            pass

    def close(self):
        self._alive = False
        if self.pid:
            try:
                os.kill(self.pid, signal.SIGTERM)
            except OSError:
                pass
            # Reap the child so it never lingers as a zombie.
            threading.Thread(target=self._reap, args=(self.pid,), daemon=True).start()
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.fd = None
