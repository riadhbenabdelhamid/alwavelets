"""Agent control channel: let an external agent drive a *running* viewer.

The MCP server (mcp_server.py) is headless — it parses a file of its own and
cannot see what you are looking at. This is the other half: a live session
opts in with `--control`, and an authorized agent connects over a Unix socket
to inspect and steer that exact view — group signals, jump the cursor to when
an expression holds, zoom, restyle — while the user watches it happen.

Design notes:

* **Opt-in.** No socket exists unless the viewer was started with --control.
* **User-scoped + token.** The socket and its session file are 0600, and every
  request must carry the token from the session file. An agent that can read
  that file already runs as you; the token exists so an unrelated process that
  merely guesses the socket path cannot drive your viewer.
* **Main-thread application.** The socket thread only parses requests and
  queues them; the curses run loop drains the queue and applies each one, so
  viewer state is never mutated from two threads.
* **The command vocabulary is the one that already exists** — the `;` command
  line (see ui_commands.execute_command) plus raw keystrokes. Nothing about a
  capability is duplicated here, so the channel cannot drift from the UI.

Wire protocol: newline-delimited JSON, one request object per line, one
response object per line. See SKILL.md for the agent-facing contract.
"""
import json
import os
import queue
import secrets
import socket
import stat
import sys
import threading
import time

PROTOCOL_VERSION = 1

# Ops that only read state; handy for an agent that wants to be sure it is not
# about to change anything (and for a future read-only token).
READ_OPS = frozenset({"ping", "state", "render", "capabilities"})
WRITE_OPS = frozenset({"command", "key"})


def sessions_dir():
    """Directory holding one descriptor file per live controllable session."""
    base = (os.environ.get("XDG_RUNTIME_DIR")
            or os.environ.get("XDG_CACHE_HOME")
            or os.path.join(os.path.expanduser("~"), ".cache"))
    return os.path.join(base, "alwavelets", "sessions")


def list_sessions():
    """Return the live sessions an agent could attach to, newest first.

    Each entry is the session descriptor plus 'stale' when its process is
    gone (a viewer killed with SIGKILL cannot clean up after itself).
    """
    out = []
    try:
        names = os.listdir(sessions_dir())
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(sessions_dir(), name)
        try:
            with open(path) as f:
                info = json.load(f)
        except (OSError, ValueError):
            continue
        info["descriptor"] = path
        info["stale"] = not _pid_alive(info.get("pid", -1))
        out.append(info)
    out.sort(key=lambda i: i.get("started", 0), reverse=True)
    return out


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError, TypeError):
        return False
    return True


def prune_stale_sessions():
    """Remove descriptors whose viewer is gone. Best-effort."""
    for info in list_sessions():
        if info.get("stale"):
            for p in (info.get("socket"), info.get("descriptor")):
                try:
                    os.unlink(p)
                except (OSError, TypeError):
                    pass


class ControlServer:
    """Accepts agent connections and queues their requests for the UI thread."""

    def __init__(self, viewer, trace_path=None):
        self.viewer = viewer
        self.trace_path = trace_path or ""
        self.token = secrets.token_hex(16)
        self.requests = queue.Queue()       # (request dict, reply queue)
        self.sock = None
        self.socket_path = None
        self.descriptor_path = None
        self.clients = 0                    # connected agents, for the status bar
        self.last_activity = 0.0            # monotonic; drives the UI indicator
        self._started = time.time()         # reported on errors: staleness clue
        self._stop = threading.Event()
        self._thread = None

    # ── lifecycle ────────────────────────────────────────────────────

    def start(self):
        """Bind the socket, publish the descriptor, and serve. Returns True on
        success; on failure the viewer runs perfectly well without control."""
        try:
            prune_stale_sessions()
            d = sessions_dir()
            os.makedirs(d, exist_ok=True)
            os.chmod(d, stat.S_IRWXU)                      # 0700
            pid = os.getpid()
            self.socket_path = os.path.join(d, f"{pid}.sock")
            self.descriptor_path = os.path.join(d, f"{pid}.json")
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.bind(self.socket_path)
            os.chmod(self.socket_path, stat.S_IRUSR | stat.S_IWUSR)   # 0600
            self.sock.listen(4)
            self.sock.settimeout(0.5)                      # so stop() is prompt
            self._write_descriptor(pid)
            self._thread = threading.Thread(target=self._serve, daemon=True)
            self._thread.start()
            return True
        except OSError:
            self.stop()
            return False

    def _write_descriptor(self, pid):
        info = {
            "protocol": PROTOCOL_VERSION,
            "pid": pid,
            "socket": self.socket_path,
            "token": self.token,
            "trace": os.path.abspath(self.trace_path) if self.trace_path else "",
            "started": time.time(),
        }
        # Create 0600 before writing: the token must never exist world-readable,
        # not even for the instant between open() and chmod().
        fd = os.open(self.descriptor_path,
                     os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(info, f, indent=2)

    def stop(self):
        self._stop.set()
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        for path in (self.socket_path, self.descriptor_path):
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass

    # ── socket thread ────────────────────────────────────────────────

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, conn):
        self.clients += 1
        try:
            conn.settimeout(30)
            with conn.makefile("rwb") as stream:
                for raw in stream:
                    if self._stop.is_set():
                        break
                    try:
                        req = json.loads(raw.decode("utf-8", "replace"))
                        if not isinstance(req, dict):
                            raise ValueError("request must be a JSON object")
                    except ValueError as e:
                        self._send(stream, {"ok": False, "error": f"bad request: {e}"})
                        continue
                    resp = self._dispatch(req)
                    self._send(stream, resp)
        except (OSError, socket.timeout):
            pass
        finally:
            self.clients = max(0, self.clients - 1)
            try:
                conn.close()
            except OSError:
                pass

    @staticmethod
    def _send(stream, obj):
        stream.write((json.dumps(obj) + "\n").encode("utf-8"))
        stream.flush()

    def _dispatch(self, req):
        """Authenticate, then hand the request to the UI thread and wait."""
        if not secrets.compare_digest(str(req.get("token", "")), self.token):
            return {"ok": False, "error": "unauthorized: bad or missing token"}
        op = req.get("op")
        if op not in READ_OPS and op not in WRITE_OPS:
            return {"ok": False,
                    "error": f"unknown op {op!r}; expected one of "
                             + ", ".join(sorted(READ_OPS | WRITE_OPS))}
        reply = queue.Queue(1)
        self.requests.put((req, reply))
        try:
            # Generous but finite: a wedged UI must not hang the agent forever.
            return reply.get(timeout=15)
        except queue.Empty:
            return {"ok": False, "error": "viewer did not respond in time"}

    # ── UI thread ────────────────────────────────────────────────────

    def pump(self):
        """Apply every queued request. Called from the curses run loop, so all
        viewer mutation happens on one thread. Returns True if anything ran
        (the caller redraws)."""
        did = False
        while True:
            try:
                req, reply = self.requests.get_nowait()
            except queue.Empty:
                break
            try:
                resp = handle(self.viewer, req)
            except Exception as e:                 # never kill the UI thread
                # An internal crash (rather than a refusal) usually means this
                # viewer process is running older code than what is on disk —
                # a long-lived session does not pick up fixes. Say so, or an
                # agent reads one traceback as a permanent capability gap and
                # gives up instead of asking for a restart.
                resp = {"ok": False,
                        "error": f"{type(e).__name__}: {e}",
                        "hint": "internal viewer error — this session may be "
                                "running older code than the installed version. "
                                "Ask the user to restart the viewer (quit with "
                                "'q' and relaunch with --control), then retry. "
                                "Call 'capabilities' to see what this build "
                                "actually supports.",
                        "started": self._started}
            reply.put(resp)
            did = True
            self.last_activity = time.monotonic()
        return did


# ── op handlers (run on the UI thread, with the viewer in hand) ───────

def handle(v, req):
    op = req.get("op")
    if op == "ping":
        return {"ok": True, "protocol": PROTOCOL_VERSION,
                "trace": getattr(v.vcd, "_filename", ""),
                "signals": len(v.vcd.signal_order)}
    if op == "capabilities":
        return {"ok": True, "capabilities": capabilities(v)}
    if op == "state":
        return {"ok": True, "state": snapshot_state(v)}
    if op == "render":
        return _op_render(v, req)
    if op == "command":
        return _op_command(v, req)
    if op == "key":
        return _op_key(v, req)
    return {"ok": False, "error": f"unknown op {op!r}"}


def _op_command(v, req):
    """Run one `;` command line — the same surface the user types."""
    text = str(req.get("text", "")).strip()
    if not text:
        return {"ok": False, "error": "command: 'text' is required"}
    v._remote_depth += 1          # makes prompt_input a no-op; see ui.py
    try:
        result = v.execute_command(text)
    finally:
        v._remote_depth -= 1
    v._dirty = True
    return {"ok": True, "quit": result == "QUIT",
            "message": v._message or "", "state": snapshot_state(v)}


def _op_key(v, req):
    """Feed keystrokes for actions that only exist as keys (Tab, x, w, …)."""
    keys = req.get("keys", "")
    if not isinstance(keys, str) or not keys:
        return {"ok": False, "error": "key: 'keys' must be a non-empty string"}
    if len(keys) > 64:
        return {"ok": False, "error": "key: at most 64 keystrokes per request"}
    named = {"tab": 9, "enter": 10, "esc": 27, "space": 32,
             "backspace": 263, "up": 259, "down": 258,
             "left": 260, "right": 261, "home": 262, "end": 360}
    # A named key is the WHOLE string ("space"), not a character within it —
    # testing per-character meant {"keys": "space"} pressed s,p,a,c,e, which
    # on this keymap toggles the panel, register panel, analog, cursor and
    # edge mode. Match the whole token first, then fall back to per-char.
    token = keys.strip().lower()
    if token in named:
        seq = [(token, named[token])]
    else:
        seq = [(ch, ord(ch)) for ch in keys]

    applied = []
    v._remote_depth += 1
    try:
        for label, code in seq:
            handler = v.keymap.get(code)
            if handler is None:
                continue
            handler()
            applied.append(label)
    finally:
        v._remote_depth -= 1
    v._dirty = True
    return {"ok": True, "applied": " ".join(applied) if len(seq) == 1
                        else "".join(applied),
            "message": v._message or "", "state": snapshot_state(v)}


def _op_render(v, req):
    """Text render of what the viewer is showing right now."""
    from .snapshot import render_text
    width = int(req.get("width", 100) or 100)
    width = max(20, min(400, width))
    sigs = [s for s in v.display_order if v.get_signal(s)]
    names = [v.get_signal(s).full_name for s in sigs]
    if not names:
        return {"ok": True, "text": "(no signals displayed)"}
    text = render_text(v.vcd, names, v.time_start, v.time_end, width=width)
    return {"ok": True, "text": text,
            "window": [v.time_start, v.time_end]}


def snapshot_state(v):
    """What the viewer is currently showing — the agent's eyes."""
    focused = v.focused_signal_id()
    fsig = v.get_signal(focused) if focused else None
    return {
        "trace": getattr(v.vcd, "_filename", ""),
        "timescale": f"{v.vcd.timescale_number}{v.vcd.timescale_unit}",
        "max_time": v.vcd.max_time,
        "window": [v.time_start, v.time_end],
        "cursor": {"a": v.cursor_a, "b": v.cursor_b,
                   "active": v.active_cursor,
                   "on": v.cursor_on, "b_on": v.cursor_b_on,
                   "delta": v.cursor_delta},
        "focused": fsig.full_name if fsig else None,
        "displayed": [v.get_signal(s).full_name
                      for s in v.display_order if v.get_signal(s)],
        "selected": [v.get_signal(s).full_name
                     for s in v.selected_signals if v.get_signal(s)],
        "groups": [{"name": g["name"], "parent": g.get("parent"),
                    "collapsed": bool(g["collapsed"]),
                    "members": sorted(v.get_signal(m).full_name
                                      for m in g["members"] if v.get_signal(m))}
                   for g in v.groups],
        "markers": [{"time": m.time, "label": m.label} for m in v.markers],
        "radix": v.radix.value,
        "message": v._message or "",
    }


def capabilities(v):
    """Self-description an agent can read instead of hard-coding a list."""
    return {
        "protocol": PROTOCOL_VERSION,
        "ops": sorted(READ_OPS | WRITE_OPS),
        "read_ops": sorted(READ_OPS),
        "write_ops": sorted(WRITE_OPS),
        "commands": sorted(v.command_verbs()),
        "notes": [
            "op 'command' takes the same text as the in-app ';' prompt.",
            "Commands that would open an interactive prompt are no-ops over "
            "the control channel (they return with the view unchanged) — "
            "pass their argument inline instead, e.g. 'goto 1200'.",
            "op 'key' feeds keystrokes for actions bound only to keys.",
        ],
    }


# ── client side (for agents, tests, and the `alwavelets-ctl` helper) ─────

class ControlClient:
    """Thin client for a live session's control socket.

    Usage:
        from alwavelets.control import ControlClient, list_sessions
        s = [x for x in list_sessions() if not x['stale']][0]
        with ControlClient(s) as c:
            c.command("when valid && ready")   # cursor lands on the match
            print(c.render(width=100)["text"])
    """

    def __init__(self, session, timeout=20):
        if isinstance(session, str):            # a descriptor path
            with open(session) as f:
                session = json.load(f)
        self.session = session
        self.timeout = timeout
        self._sock = None
        self._stream = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    def connect(self):
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.settimeout(self.timeout)
        self._sock.connect(self.session["socket"])
        self._stream = self._sock.makefile("rwb")
        return self

    def close(self):
        for obj in (self._stream, self._sock):
            try:
                if obj is not None:
                    obj.close()
            except OSError:
                pass
        self._stream = self._sock = None

    def request(self, op, **kw):
        if self._stream is None:
            self.connect()
        payload = {"token": self.session["token"], "op": op}
        payload.update(kw)
        self._stream.write((json.dumps(payload) + "\n").encode("utf-8"))
        self._stream.flush()
        line = self._stream.readline()
        if not line:
            raise ConnectionError("viewer closed the control connection")
        return json.loads(line.decode("utf-8", "replace"))

    # convenience wrappers
    def ping(self):
        return self.request("ping")

    def capabilities(self):
        return self.request("capabilities")

    def state(self):
        return self.request("state")

    def render(self, width=100):
        return self.request("render", width=width)

    def command(self, text):
        return self.request("command", text=text)

    def key(self, keys):
        return self.request("key", keys=keys)
