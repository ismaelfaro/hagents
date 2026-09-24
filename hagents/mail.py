"""Mail between agents: envelopes, the policy-checked bus, and the files agents see.

Agents never touch each other's mailboxes. An agent writes messages into its own
``mail/outbox/``; after its turn the host reads them, stamps the true sender
(whatever the file claims), checks the org policy, and delivers into the
recipient's ``mail/inbox/``. Every decision lands in ``.hagent/mail.jsonl``, which
no agent can reach.

The host reads files that agents wrote, so it reads them carefully: no symlinks
(a planted ``outbox/x.md -> ~/.ssh/id_rsa`` would otherwise be "delivered"),
size and count caps, and folders re-checked before every write.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from loomloop.bus import MessageBus
from loomloop.message import Message

from .policy import LOOM, OWNER, can_send
from .tree import Org, OrgError

MAX_BYTES = 64 * 1024
MAX_PER_TURN = 20
MAX_HOPS = 8

KINDS = {"mail", "rollup", "ack", "bounce"}


@dataclass
class Envelope:
    sender: str
    recipient: str
    subject: str
    body: str
    kind: str = "mail"
    hops: int = 0
    id: str = field(default_factory=lambda: time.strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(3))
    ts: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S"))

    def render(self) -> str:
        return (f"From: {self.sender}\nTo: {self.recipient}\nDate: {self.ts}\n"
                f"Kind: {self.kind}\nSubject: {self.subject}\n\n{self.body.rstrip()}\n")


# --- files --------------------------------------------------------------------

def _safe_dir(d: Path) -> Path:
    if d.is_symlink():
        raise OrgError(f"{d} is a symlink; refusing to use it")
    d.mkdir(parents=True, exist_ok=True)
    return d


_HDR = re.compile(r"^([A-Za-z-]+):[ \t]*(.*)$")


def parse_message(text: str) -> Optional[tuple]:
    """``To:`` / ``Subject:`` headers, a blank line, then the body. None if no To."""
    headers, lines = {}, text.splitlines()
    i = 0
    while i < len(lines) and lines[i].strip():
        m = _HDR.match(lines[i])
        if not m:
            break
        headers[m.group(1).lower()] = m.group(2).strip()
        i += 1
    to = headers.get("to", "")
    if not to:
        return None
    body = "\n".join(lines[i:]).strip()
    return to, headers.get("subject", "(no subject)"), body


def collect_outbox(home: Path) -> List[tuple]:
    """Read and remove every message in ``home/mail/outbox``. Returns (to, subject, body, problem)."""
    box = _safe_dir(home / "mail" / "outbox")
    out = []
    for p in sorted(box.iterdir()):
        if p.name.startswith("."):
            continue
        try:
            st = os.lstat(p)
            if not os.path.isfile(p) or os.path.islink(p):
                out.append(("", "", "", f"{p.name}: not a regular file, ignored"))
            elif st.st_size > MAX_BYTES:
                out.append(("", "", "", f"{p.name}: over {MAX_BYTES} bytes, ignored"))
            elif len(out) >= MAX_PER_TURN:
                out.append(("", "", "", f"{p.name}: over {MAX_PER_TURN} messages this turn, ignored"))
            else:
                fd = os.open(p, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(fd, "r", errors="replace") as fh:
                    parsed = parse_message(fh.read())
                if parsed is None:
                    out.append(("", "", "", f"{p.name}: no 'To:' header, ignored"))
                else:
                    out.append((*parsed, ""))
        finally:
            if p.is_symlink() or p.is_file():
                p.unlink()
            elif p.is_dir():
                pass  # leave stray folders alone; they are the agent's own
    return out


def deliver_file(home: Path, env: Envelope) -> Path:
    box = _safe_dir(home / "mail" / "inbox")
    f = box / f"{env.id}-{env.sender}.md"
    tmp = box / f".{f.name}.tmp"
    tmp.write_text(env.render())
    os.replace(tmp, f)
    return f


def archive_inbox(home: Path) -> None:
    """After a turn, move what the agent was shown from inbox/ to read/."""
    box = _safe_dir(home / "mail" / "inbox")
    read = _safe_dir(home / "mail" / "read")
    for p in box.iterdir():
        if p.is_file() and not p.is_symlink() and not p.name.startswith("."):
            os.replace(p, read / p.name)


# --- audit --------------------------------------------------------------------

class Audit:
    def __init__(self, org: Org) -> None:
        self.path = org.runtime_dir / "mail.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, env: Envelope, decision: str, reason: str) -> None:
        row = {**asdict(env), "decision": decision, "reason": reason}
        with self.path.open("a") as fh:
            fh.write(json.dumps(row) + "\n")


# --- the bus ------------------------------------------------------------------

class PolicyBus(MessageBus):
    """LoomLoop's bus with the org chart in front of it.

    Every direct message is checked with ``policy.can_send``. A refused message
    is not dropped silently: it comes back to the sender as a ``bounce`` so the
    agent learns the route, and the refusal is audited.
    """

    def __init__(self, org: Org, audit: Audit, log: Callable[[str], None] = print) -> None:
        super().__init__()
        self.org = org
        self.audit = audit
        self._log = log
        self.bounced = 0

    def publish(self, msg: Message) -> int:
        env = msg.payload
        if not isinstance(env, Envelope) or msg.recipient is None:
            return super().publish(msg)
        # The bus, not the envelope, knows who sent it.
        env.sender = msg.sender
        env.recipient = msg.recipient
        if env.kind == "ack" and env.recipient == LOOM:
            # Only NodeLoop (host code) creates acks; an agent's outbox is always "mail".
            ok, reason = True, "rollup ack to the conductor"
        else:
            ok, reason = can_send(self.org, env.sender, env.recipient)
        if ok and env.hops > MAX_HOPS and env.sender != LOOM:
            ok, reason = False, f"hop limit {MAX_HOPS} reached (message loop?)"
        self.audit.record(env, "delivered" if ok else "refused", reason)
        if ok:
            return super().publish(msg)
        self.bounced += 1
        self._log(f"  refused {env.sender} -> {env.recipient}: {reason}")
        if env.kind != "bounce" and env.sender not in (LOOM, OWNER) and env.sender in self._inboxes:
            back = Envelope(sender=LOOM, recipient=env.sender, kind="bounce",
                            subject=f"Undeliverable: {env.subject}",
                            body=f"Your message to {env.recipient} was not delivered: {reason}.",
                            hops=env.hops + 1)
            super().publish(Message(topic="mail", payload=back, sender=LOOM,
                                    recipient=env.sender))
        return 0
