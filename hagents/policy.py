"""Who can see what, and who can talk to whom.

Everything here is a pure function of the tree. Two rules carry the design:

1. **Scope is your own subtree.** A unit's container gets its own work folder;
   the folders of the units below it appear inside it (read-only unless
   ``access.below = "write"``). Nothing above it or beside it is mounted at all:
   an agent cannot read what is not in its filesystem. A system agent gets its
   own memory plus the *config* of the units below its principal, never data.

2. **Messages follow the org chart.** Parent <-> child always; siblings only
   when their parent set ``mail.peers``; a unit <-> its own system agent; the
   human owner <-> the apex and the apex's system agent. Anything else bounces.

``check_mounts`` re-derives the scope and refuses a launch if any mount source
leaves it, or reaches it through a symlink. It is the last line of defence
against a bug here, or a trick played on the host filesystem by an agent.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

from .tree import AnyAgent, Node, Org, OrgError, SystemAgent

OWNER = "owner"
LOOM = "loom"

# Where things appear inside every agent container.
HOME = "/node"            # the agent's own folder: its knowledge lives here
BELOW = "/node/units"     # units below (data side)
CONTROL = "/node/org"     # config of units below (control side)


@dataclass(frozen=True)
class Mount:
    host: Path
    container: str
    mode: str  # "ro" | "rw"

    def docker_arg(self) -> str:
        return f"{self.host}:{self.container}:{self.mode}"


# --- filesystem ---------------------------------------------------------------

def mounts_for(org: Org, agent: AnyAgent) -> List[Mount]:
    if isinstance(agent, Node):
        wp = org.work_path(agent)
        out = [Mount(wp, HOME, "rw")]
        if agent.children:
            # Nested bind: the inner mount wins, so units/ can be read-only
            # inside an otherwise writable home.
            out.append(Mount(wp / "units", BELOW, "rw" if agent.below == "write" else "ro"))
        if agent.control != "none" and agent.children:
            out.append(Mount(org.control_path(agent) / "units", CONTROL,
                             "rw" if agent.control == "write" else "ro"))
        return out
    # A system agent: its own memory, plus read-write config of the units below
    # its principal. Its principal's own node.toml is NOT in scope: nobody
    # edits the config of their own level, only of the levels below.
    principal = agent.principal
    out = [Mount(org.system_home(agent), HOME, "rw")]
    out.append(Mount(org.control_path(principal) / "units", CONTROL, "rw"))
    return out


def scope_roots(org: Org, agent: AnyAgent) -> List[Path]:
    """The host folders an agent's mounts may come from. Independent of mounts_for."""
    if isinstance(agent, Node):
        roots = [org.work_path(agent)]
        if agent.control != "none":
            roots.append(org.control_path(agent) / "units")
        return roots
    return [org.system_home(agent), org.control_path(agent.principal) / "units"]


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def check_mounts(org: Org, agent: AnyAgent, mounts: List[Mount]) -> None:
    """Refuse any mount outside the agent's scope or reached through a symlink."""
    roots = [Path(os.path.abspath(r)) for r in scope_roots(org, agent)]
    for m in mounts:
        src = Path(os.path.abspath(m.host))
        # realpath == abspath means no component of the path is a symlink.
        if Path(os.path.realpath(src)) != src:
            raise OrgError(f"{agent.name}: mount source {src} goes through a symlink; refusing to launch")
        if not any(_within(src, r) for r in roots):
            raise OrgError(f"{agent.name}: mount source {src} is outside its scope; refusing to launch")
        # The org root itself and the runtime plane are never in anyone's scope.
        if _within(src, org.runtime_dir.resolve()) or src == org.root:
            raise OrgError(f"{agent.name}: mount source {src} is host-only; refusing to launch")


def ensure_dirs(org: Org, agent: AnyAgent) -> None:
    """Create the folders an agent's mounts need, refusing symlinked ones."""
    for m in mounts_for(org, agent):
        p = m.host
        if p.is_symlink():
            raise OrgError(f"{p} is a symlink; refusing to use it")
        p.mkdir(parents=True, exist_ok=True)
    home = org.home(agent)
    for sub in ("mail/inbox", "mail/outbox", "mail/read", "Memory", "Skills"):
        d = home / sub
        if d.is_symlink():
            raise OrgError(f"{d} is a symlink; refusing to use it")
        d.mkdir(parents=True, exist_ok=True)


# --- mail -----------------------------------------------------------------------

def can_send(org: Org, sender: str, recipient: str) -> Tuple[bool, str]:
    """Is ``sender`` allowed to message ``recipient``? Returns (ok, reason)."""
    if sender == LOOM:
        return True, "the loom (host) may reach anyone"
    if recipient == sender:
        return False, "you cannot message yourself"
    if sender == OWNER:
        # The human sits above the apex: they may reach anyone directly.
        if recipient in org.agents:
            return True, "the owner may reach anyone"
        return False, f"no agent named {recipient!r}"
    if sender not in org.agents:
        return False, f"unknown sender {sender!r}"
    s = org.agents[sender]

    if recipient == OWNER:
        if s is org.apex or (isinstance(s, SystemAgent) and s.principal is org.apex):
            return True, "the apex and its system agent report to the owner"
        return False, "only the apex reaches the owner; escalate through your parent"
    if recipient not in org.agents:
        return False, f"no agent named {recipient!r}"
    r = org.agents[recipient]

    if isinstance(s, SystemAgent) or isinstance(r, SystemAgent):
        if isinstance(s, SystemAgent) and s.principal is r:
            return True, "system agent -> its principal"
        if isinstance(r, SystemAgent) and r.principal is s:
            return True, "unit -> its system agent"
        if (isinstance(s, SystemAgent) and isinstance(r, SystemAgent)
                and (s.principal.parent is r.principal or r.principal.parent is s.principal)):
            return True, "system agents of adjacent levels"
        return False, "system agents only talk to their principal and to adjacent system agents"

    if r.parent is s:
        return True, "manager -> direct report"
    if s.parent is r:
        return True, "report -> manager"
    if s.parent is not None and s.parent is r.parent:
        if s.parent.peers:
            return True, f"peers under {s.parent.name}"
        return False, f"peers under {s.parent.name} talk through {s.parent.name} (mail.peers = false)"
    return False, (f"{recipient} is not your manager, report or peer; "
                   f"route it through the chain of command")


def contacts(org: Org, agent: AnyAgent) -> List[str]:
    names = [n for n in list(org.agents) + [OWNER] if n != agent.name]
    return [n for n in names if can_send(org, agent.name, n)[0]]
