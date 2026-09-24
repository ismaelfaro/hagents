"""The org tree: who exists, where they sit, and what their config says.

An org lives in one directory with three planes:

    <org>/org/     CONTROL plane. One node.toml per unit; units nest under
                   units/<name>/. System agents keep their own memory in
                   <unit>/.system/. This is what system agents edit.
    <org>/work/    DATA plane. Mirrors org/: every unit has a folder here that
                   holds its knowledge (Memory/, Skills/, STATUS.md, mail/...).
    <org>/.hagent/ RUNTIME. Host-only: the applied config snapshot, the mail
                   audit log, the owner's inbox. Never mounted into an agent.

The loader is strict on purpose. Access is derived from a node's *position* in
the tree, never from what its config says, so the config schema has no field
that names a path, a mount or another agent's scope. Unknown keys are errors,
symlinks are errors: a system agent that edits node.toml cannot smuggle in a
grant the schema does not have.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Dict, Iterator, List, Optional, Union

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
RESERVED = {"owner", "all", "loom", "units", "system"}

RUNTIMES = {"nanoloop", "hermes", "mock", "command"}
BELOW = {"read", "write"}
CONTROL = {"none", "read", "write"}

_NODE_KEYS = {"name", "role", "charter", "runtime", "model", "command", "image",
              "access", "mail", "system"}
_ACCESS_KEYS = {"below", "control"}
_MAIL_KEYS = {"peers"}
_SYSTEM_KEYS = {"name", "role", "charter", "runtime", "model", "command", "image"}


class OrgError(ValueError):
    """The org tree or a node.toml breaks a rule."""


@dataclass(eq=False)
class Agent:
    """Anything that runs a turn: a unit (data side) or a system agent (control side)."""

    name: str
    role: str
    charter: str
    runtime: str = "nanoloop"
    model: str = ""
    command: List[str] = field(default_factory=list)
    image: str = ""

    @property
    def kind(self) -> str:
        raise NotImplementedError


@dataclass(eq=False)
class Node(Agent):
    """A unit of the organisation: the apex (CEO), a department, a team..."""

    rel: PurePosixPath = PurePosixPath(".")
    below: str = "read"
    control: str = "none"
    peers: bool = False
    parent: Optional["Node"] = None
    children: List["Node"] = field(default_factory=list)
    system: Optional["SystemAgent"] = None

    @property
    def kind(self) -> str:
        return "unit"

    @property
    def depth(self) -> int:
        return 0 if self.parent is None else self.parent.depth + 1

    def ancestors(self) -> List["Node"]:
        out, n = [], self.parent
        while n is not None:
            out.append(n)
            n = n.parent
        return out

    def descendants(self) -> List["Node"]:
        out: List[Node] = []
        for c in self.children:
            out.append(c)
            out.extend(c.descendants())
        return out

    def is_ancestor_of(self, other: "Node") -> bool:
        return self in other.ancestors()

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<Node {self.name} @{self.rel}>"


@dataclass(eq=False)
class SystemAgent(Agent):
    """The control-side twin of a unit: manages the config of everything below it."""

    principal: Optional[Node] = None

    @property
    def kind(self) -> str:
        return "system"

    @property
    def depth(self) -> int:
        return self.principal.depth if self.principal else 0

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<SystemAgent {self.name} for {self.principal.name if self.principal else '?'}>"


AnyAgent = Union[Node, SystemAgent]


class Org:
    """A loaded org tree plus the host paths of its three planes."""

    def __init__(self, root: Path, apex: Node, config_root: Path) -> None:
        self.root = root
        self.apex = apex
        # Where the tree was loaded from: .hagent/applied/ normally, org/ for `diff`.
        self.config_root = config_root
        self.agents: Dict[str, AnyAgent] = {}
        for n in self.nodes():
            self.agents[n.name] = n
            if n.system:
                self.agents[n.system.name] = n.system

    # -- planes ------------------------------------------------------------
    @property
    def org_dir(self) -> Path:
        return self.root / "org"

    @property
    def work_dir(self) -> Path:
        return self.root / "work"

    @property
    def runtime_dir(self) -> Path:
        return self.root / ".hagent"

    def work_path(self, node: Node) -> Path:
        return self.work_dir / node.rel

    def control_path(self, node: Node) -> Path:
        return self.org_dir / node.rel

    def system_home(self, sys_agent: SystemAgent) -> Path:
        return self.control_path(sys_agent.principal) / ".system"

    def home(self, agent: AnyAgent) -> Path:
        return self.work_path(agent) if isinstance(agent, Node) else self.system_home(agent)

    # -- walking -----------------------------------------------------------
    def nodes(self) -> Iterator[Node]:
        yield self.apex
        yield from self.apex.descendants()

    def get(self, name: str) -> AnyAgent:
        try:
            return self.agents[name]
        except KeyError:
            raise OrgError(f"no agent named {name!r} (known: {', '.join(sorted(self.agents))})") from None

    def node_at(self, path: Path) -> Optional[Node]:
        """The deepest unit whose work folder contains ``path`` (for `cd`-based targeting)."""
        path = path.resolve()
        best = None
        for n in self.nodes():
            wp = self.work_path(n).resolve()
            if path == wp or wp in path.parents:
                if best is None or n.depth > best.depth:
                    best = n
        return best


# --- loading ------------------------------------------------------------------

def _str(d: dict, key: str, where: Path, default: str = "") -> str:
    v = d.get(key, default)
    if not isinstance(v, str):
        raise OrgError(f"{where}: {key!r} must be a string")
    return v


def _cmd(d: dict, where: Path) -> List[str]:
    v = d.get("command", [])
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise OrgError(f"{where}: 'command' must be a list of strings")
    return v


def _check_keys(d: dict, allowed: set, where: str) -> None:
    extra = set(d) - allowed
    if extra:
        raise OrgError(f"{where}: unknown key(s) {sorted(extra)} (allowed: {sorted(allowed)})")


def _check_name(name: str, where: Path) -> None:
    if not NAME_RE.match(name) or name in RESERVED:
        raise OrgError(f"{where}: bad name {name!r}: lowercase letters, digits, dashes; "
                       f"max 32; not one of {sorted(RESERVED)}")


def _check_runtime(rt: str, where: Path) -> None:
    if rt not in RUNTIMES:
        raise OrgError(f"{where}: runtime must be one of {sorted(RUNTIMES)}, not {rt!r}")


def _no_symlinks(path: Path, stop: Path) -> None:
    """Refuse a path if it, or any parent up to ``stop``, is a symlink."""
    p = path
    while True:
        if p.is_symlink():
            raise OrgError(f"{p}: symlinks are not allowed in the org tree")
        if p == stop or p == p.parent:
            return
        p = p.parent


def _load_node(dir_: Path, top: Path, rel: PurePosixPath, parent: Optional[Node]) -> Node:
    f = dir_ / "node.toml"
    _no_symlinks(f, top)
    if not f.is_file():
        raise OrgError(f"{dir_}: missing node.toml")
    try:
        data = tomllib.loads(f.read_text())
    except tomllib.TOMLDecodeError as e:
        raise OrgError(f"{f}: {e}") from None
    _check_keys(data, _NODE_KEYS, str(f))
    access = data.get("access", {})
    mail = data.get("mail", {})
    if not isinstance(access, dict) or not isinstance(mail, dict):
        raise OrgError(f"{f}: [access] and [mail] must be tables")
    _check_keys(access, _ACCESS_KEYS, f"{f} [access]")
    _check_keys(mail, _MAIL_KEYS, f"{f} [mail]")

    name = _str(data, "name", f)
    _check_name(name, f)
    # The folder name is the unit's address in the tree; keep them in lockstep so
    # nobody can rename a unit into someone else's identity by editing a string.
    if parent is not None and name != dir_.name:
        raise OrgError(f"{f}: name {name!r} must match its folder name {dir_.name!r}")
    runtime = _str(data, "runtime", f, "nanoloop")
    _check_runtime(runtime, f)
    below = _str(access, "below", f, "read")
    control = _str(access, "control", f, "none")
    if below not in BELOW:
        raise OrgError(f"{f}: access.below must be one of {sorted(BELOW)}")
    if control not in CONTROL:
        raise OrgError(f"{f}: access.control must be one of {sorted(CONTROL)}")
    peers = mail.get("peers", False)
    if not isinstance(peers, bool):
        raise OrgError(f"{f}: mail.peers must be true or false")

    node = Node(
        name=name, role=_str(data, "role", f), charter=_str(data, "charter", f),
        runtime=runtime, model=_str(data, "model", f), command=_cmd(data, f),
        image=_str(data, "image", f), rel=rel, below=below, control=control,
        peers=peers, parent=parent,
    )

    sysd = data.get("system")
    if sysd is not None:
        if not isinstance(sysd, dict):
            raise OrgError(f"{f}: [system] must be a table")
        _check_keys(sysd, _SYSTEM_KEYS, f"{f} [system]")
        sname = _str(sysd, "name", f)
        _check_name(sname, f)
        srt = _str(sysd, "runtime", f, "nanoloop")
        _check_runtime(srt, f)
        node.system = SystemAgent(
            name=sname, role=_str(sysd, "role", f, "System agent"),
            charter=_str(sysd, "charter", f), runtime=srt, model=_str(sysd, "model", f),
            command=_cmd(sysd, f), image=_str(sysd, "image", f), principal=node,
        )

    units = dir_ / "units"
    if units.exists():
        _no_symlinks(units, top)
        for child_dir in sorted(p for p in units.iterdir() if not p.name.startswith(".")):
            if child_dir.is_symlink():
                raise OrgError(f"{child_dir}: symlinks are not allowed in the org tree")
            if child_dir.is_dir():
                node.children.append(
                    _load_node(child_dir, top, rel / "units" / child_dir.name, node))
    return node


def load(root: Path, *, live: bool = False) -> Org:
    """Load the org at ``root``.

    By default the *applied* snapshot (.hagent/applied/) is loaded: edits a
    system agent makes to org/ are proposals until a human runs `hagent apply`.
    ``live=True`` reads org/ directly (for `diff`, `apply` and `init`).
    """
    root = root.resolve()
    config_root = root / "org" if live else root / ".hagent" / "applied"
    if not (config_root / "node.toml").is_file():
        if not live and (root / "org" / "node.toml").is_file():
            raise OrgError(f"{root}: config not applied yet. Review it, then run: hagent apply")
        raise OrgError(f"{root}: no org here (expected {config_root}/node.toml)")
    apex = _load_node(config_root, config_root, PurePosixPath("."), None)
    org = Org(root, apex, config_root)
    seen: Dict[str, str] = {}
    for n in org.nodes():
        for a in [n] + ([n.system] if n.system else []):
            if a.name in seen:
                raise OrgError(f"name {a.name!r} is used twice ({seen[a.name]} and {n.rel})")
            seen[a.name] = str(n.rel)
    return org


def find_root(start: Path) -> Optional[Path]:
    """Walk up from ``start`` to the org root (the folder holding org/node.toml)."""
    p = start.resolve()
    for cand in [p, *p.parents]:
        if (cand / "org" / "node.toml").is_file():
            return cand
    return None
