"""A fake IMAP server over real TLS, in-process, for the mailbox tests: no network, a test certificate made at run time.

It speaks enough IMAP4rev1 for ws.minters.imap (CAPABILITY, LOGIN, LIST, SELECT, EXAMINE, UID SEARCH/FETCH/STORE,
APPEND with a literal, NOOP, LOGOUT) and answers BAD to anything else. Every command it is sent goes to `log` as
(name, rest of line), so a test can assert what a client sent, not only what it got back. `gmail=True` adds X-GM-EXT-1,
X-GM-THRID and X-GM-LABELS. A failed LOGIN echoes the password it was given, to prove a client never passes server text
on. Names and hosts are example.com only.

`dial` patches socket.create_connection so a client that dials HOST:993 reaches this server; every address dialled is
recorded in `dialled`.
"""

from __future__ import annotations

import re
import shutil
import socket
import socketserver
import ssl
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

HOST = "imap.example.com"
USER = "someone@example.com"
_REAL_CONNECT = socket.create_connection  # taken at import, so a second patch never wraps the first

CNF = """[req]
distinguished_name = dn
x509_extensions = v3
prompt = no
[dn]
CN = {host}
[v3]
basicConstraints = critical,CA:TRUE
keyUsage = critical,keyCertSign,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = DNS:{host}
"""


def make_cert(folder: Path, host: str = HOST, stem: str = "imap") -> tuple[Path, Path]:
    """A self-signed certificate for `host` (its own CA) and its key, made with the openssl command."""
    if shutil.which("openssl") is None:
        import pytest

        pytest.skip("needs the openssl command to make a test certificate")
    cnf, cert, key = folder / f"{stem}.cnf", folder / f"{stem}.pem", folder / f"{stem}.key"
    cnf.write_text(CNF.format(host=host), encoding="utf-8")
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert),
         "-days", "2", "-config", str(cnf)],
        check=True, capture_output=True, timeout=60,
    )  # fmt: skip
    return cert, key


@dataclass
class Message:
    uid: int
    raw: bytes
    flags: set[str] = field(default_factory=set)
    labels: set[str] = field(default_factory=set)
    thread: int = 0


@dataclass
class Folder:
    name: str
    attrs: str = ""
    uidvalidity: int = 7
    messages: list[Message] = field(default_factory=list)

    def next_uid(self) -> int:
        return max((m.uid for m in self.messages), default=0) + 1


TOKEN_RE = re.compile(r'"(?:[^"\\]|\\.)*"|\([^()]*(?:\([^()]*\)[^()]*)*\)|\S+')


def _unquote(t: str) -> str:
    return t[1:-1].replace('\\"', '"').replace("\\\\", "\\") if t.startswith('"') else t


class FakeImap(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, cert: Path, key: Path, password: str, gmail: bool = True, drafts: bool = True):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(str(cert), str(key))
        self.password = password
        self.gmail = gmail
        self.refusal: str | None = None  # set: every LOGIN gets `NO <refusal>` (a server's own wording)
        self.log: list[tuple[str, str]] = []
        self.dialled: list[tuple[str, int]] = []
        self.lock = threading.Lock()
        drafts_name = "[Gmail]/Drafts" if gmail else "Drafts"
        self.folders: dict[str, Folder] = {
            "INBOX": Folder("INBOX", "\\HasNoChildren"),
            drafts_name: Folder(drafts_name, "\\HasNoChildren" + (" \\Drafts" if drafts else ""), uidvalidity=3),
            "Sent": Folder("Sent", "\\HasNoChildren \\Sent", uidvalidity=4),
            "Trash": Folder("Trash", "\\HasNoChildren \\Trash", uidvalidity=5),
        }
        self.drafts_name = drafts_name
        threading.Thread(target=self.serve_forever, args=(0.05,), daemon=True).start()

    @property
    def port(self) -> int:
        return self.server_address[1]

    def add(self, raw: bytes, folder: str = "INBOX", thread: int = 99) -> Message:
        f = self.folders[folder]
        m = Message(f.next_uid(), raw, thread=thread)
        f.messages.append(m)
        return m

    def verbs(self) -> list[str]:
        """The command names sent, UID commands as `UID FETCH` etc."""
        out = []
        for name, rest in self.log:
            out.append(f"UID {rest.split()[0].upper()}" if name == "UID" and rest else name)
        return out

    def close(self) -> None:
        self.shutdown()
        self.server_close()

    def dial(self, monkeypatch) -> None:
        real = _REAL_CONNECT

        def fake(address, *a, **kw):
            self.dialled.append((address[0], address[1]))
            if tuple(address) == (HOST, 993):
                return real(("127.0.0.1", self.port), *a, **kw)
            raise ConnectionRefusedError(f"the test allows {HOST}:993 only")

        monkeypatch.setattr(socket, "create_connection", fake)


class _Handler(socketserver.StreamRequestHandler):
    def setup(self):
        self.request = self.server.ctx.wrap_socket(self.request, server_side=True)
        super().setup()

    def send(self, line: str | bytes) -> None:
        self.wfile.write((line.encode() if isinstance(line, str) else line) + b"\r\n")
        self.wfile.flush()

    def handle(self):
        srv: FakeImap = self.server
        self.selected: Folder | None = None
        self.send("* OK fake IMAP ready")
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            parts = line.split(" ", 2)
            if len(parts) < 2:
                continue
            tag, name, rest = parts[0], parts[1].upper(), parts[2] if len(parts) > 2 else ""
            literal = None
            m = re.search(r"\{(\d+)\}$", rest)
            if m:
                self.send("+ go ahead")
                literal = self.rfile.read(int(m.group(1)))
                self.rfile.readline()
                rest = rest[: m.start()].rstrip()
            with srv.lock:
                srv.log.append((name, rest))
                if self.dispatch(tag, name, rest, literal) == "bye":
                    return

    def dispatch(self, tag: str, name: str, rest: str, literal: bytes | None):
        srv: FakeImap = self.server
        args = TOKEN_RE.findall(rest)
        if name == "CAPABILITY":
            self.send("* CAPABILITY IMAP4rev1 SPECIAL-USE UIDPLUS" + (" X-GM-EXT-1" if srv.gmail else ""))
            return self.send(f"{tag} OK done")
        if name == "NOOP":
            return self.send(f"{tag} OK done")
        if name == "LOGIN":
            if srv.refusal is not None:
                return self.send(f"{tag} NO {srv.refusal}")
            if len(args) == 2 and _unquote(args[0]) == USER and _unquote(args[1]) == srv.password:
                return self.send(f"{tag} OK logged in")
            return self.send(f"{tag} NO [AUTHENTICATIONFAILED] Invalid credentials {_unquote(args[-1])}")
        if name == "LOGOUT":
            self.send("* BYE")
            self.send(f"{tag} OK bye")
            return "bye"
        if name == "LIST":
            for f in srv.folders.values():
                self.send(f'* LIST ({f.attrs}) "/" "{f.name}"')
            return self.send(f"{tag} OK done")
        if name in ("SELECT", "EXAMINE") and args:
            f = srv.folders.get(_unquote(args[0]))
            if f is None:
                return self.send(f"{tag} NO no such folder")
            self.selected = f
            self.send(f"* {len(f.messages)} EXISTS")
            self.send(f"* OK [UIDVALIDITY {f.uidvalidity}] ok")
            return self.send(f"{tag} OK [{'READ-ONLY' if name == 'EXAMINE' else 'READ-WRITE'}] done")
        if name == "APPEND" and literal is not None and args:
            f = srv.folders.get(_unquote(args[0]))
            if f is None:
                return self.send(f"{tag} NO no such folder")
            flags = set(args[1].strip("()").split()) if len(args) > 1 and args[1].startswith("(") else set()
            msg = Message(f.next_uid(), literal, flags)
            f.messages.append(msg)
            return self.send(f"{tag} OK [APPENDUID {f.uidvalidity} {msg.uid}] appended")
        if name == "UID" and args and self.selected is not None:
            return self.uid(tag, args[0].upper(), args[1:])
        return self.send(f"{tag} BAD not supported here")

    def uid(self, tag: str, sub: str, args: list[str]):
        srv: FakeImap = self.server
        f = self.selected
        assert f is not None
        if sub == "SEARCH":
            self.send("* SEARCH " + " ".join(str(m.uid) for m in f.messages))
            return self.send(f"{tag} OK done")
        msg = next((m for m in f.messages if args and str(m.uid) == args[0]), None)
        seq = f.messages.index(msg) + 1 if msg else 0
        if sub == "FETCH":
            if msg is not None:
                head = msg.raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
                body = msg.raw if "HEADER.FIELDS" not in args[1] else head
                gm = ""
                if srv.gmail:
                    gm = f" X-GM-THRID {msg.thread} X-GM-LABELS ({' '.join(chr(34) + x + chr(34) for x in sorted(msg.labels))})"
                meta = f"* {seq} FETCH (UID {msg.uid} FLAGS ({' '.join(sorted(msg.flags))}) RFC822.SIZE {len(msg.raw)}{gm} BODY[] {{{len(body)}}}"
                self.wfile.write(meta.encode() + b"\r\n" + body + b")\r\n")
            return self.send(f"{tag} OK done")
        if sub == "STORE":
            if msg is not None and len(args) == 3:
                values = [_unquote(v) for v in TOKEN_RE.findall(args[2].strip("()"))]
                if args[1].upper() == "+X-GM-LABELS":
                    msg.labels.update(values)
                elif args[1].upper() == "+FLAGS":
                    msg.flags.update(values)
                self.send(f"* {seq} FETCH (UID {msg.uid} FLAGS ({' '.join(sorted(msg.flags))}))")
            return self.send(f"{tag} OK done")
        return self.send(f"{tag} BAD not supported here")
