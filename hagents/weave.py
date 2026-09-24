"""Weaving the pyramid on a LoomLoop Loom.

Each agent in the org is a nanoloop in the LoomLoop sense: it sleeps until mail
lands (``Step.wait``), runs one turn in its runtime, turns its outbox into
messages, and sleeps again. The Loom supplies the clock and runs every ready
agent of a tick concurrently; the PolicyBus supplies the org chart. A run ends
when the pyramid goes quiet -- nobody has mail, nothing is in flight -- or when
the tick or turn budget is spent.

Two extra loops, both host-side and trusted:

    owner   the human's mailbox. What the apex sends up lands in
            .hagent/owner/inbox/ and is printed.
    loom    the rollup conductor. Sends ``rollup`` to the deepest level, waits
            for every ack, then moves one level up, so each manager summarises
            *fresh* reports: the pyramid as a summarisation tree.
"""

from __future__ import annotations

import time
from collections import defaultdict
from pathlib import Path
from typing import Callable, Dict, List, Optional

from loomloop import Loom, NanoLoop, Step
from loomloop.message import Message

from .mail import Audit, Envelope, PolicyBus, archive_inbox, collect_outbox, deliver_file, record_sent
from .policy import LOOM, OWNER, ensure_dirs
from .runtime import DockerRuntime, MockRuntime, runtime_for
from .tree import AnyAgent, Node, Org

DEFAULT_TURN_BUDGET = 6


def _leftovers(home: Path) -> List[Envelope]:
    """Messages delivered in an earlier run that the agent never got a turn for."""
    box = home / "mail" / "inbox"
    out = []
    if not box.is_dir() or box.is_symlink():
        return out
    for p in sorted(box.iterdir()):
        if p.is_file() and not p.is_symlink() and not p.name.startswith("."):
            text = p.read_text(errors="replace")
            hdr = dict(l.split(": ", 1) for l in text.split("\n\n", 1)[0].splitlines() if ": " in l)
            out.append(Envelope(sender=hdr.get("From", "?"), recipient=hdr.get("To", ""),
                                subject=hdr.get("Subject", ""), kind=hdr.get("Kind", "mail"),
                                body=text.split("\n\n", 1)[-1]))
    return out


class NodeLoop(NanoLoop):
    def __init__(self, org: Org, agent: AnyAgent, runtime, turn_budget: int, log: Callable[[str], None]) -> None:
        super().__init__(agent.name)
        self.org, self.agent, self.runtime = org, agent, runtime
        self.turn_budget = turn_budget
        self.turns = 0
        self.pending: List[Envelope] = []
        self._log = log

    async def setup(self, ctx) -> None:
        ensure_dirs(self.org, self.agent)
        self.pending = _leftovers(self.org.home(self.agent))
        if any(e.sender == OWNER for e in self.pending):
            ctx.loom.bus.owner_threads.add(self.name)  # still an open thread with the owner

    async def _turn(self, ctx, inbox: List[Envelope], mode: str) -> None:
        home = self.org.home(self.agent)
        self._log(f"[t{ctx.tick:>3}] {self.name:<14} turn {self.turns + 1} ({mode}, {len(inbox)} msg)")
        res = await self.runtime.run_turn(self.org, self.agent, inbox, mode)
        self.turns += 1
        if res.ok:
            archive_inbox(home)  # a failed turn keeps its mail for the next run
        outgoing = list(res.extra_outbox)
        for to, subject, body, problem in collect_outbox(home):
            if problem:
                self._log(f"         {self.name}: outbox {problem}")
            else:
                outgoing.append((to, subject, body))
        if not res.ok:
            self._log(f"         {self.name}: turn failed: {res.log}")
        hops = max((e.hops for e in inbox), default=0) + 1
        for to, subject, body in outgoing:
            env = Envelope(sender=self.name, recipient=to, subject=subject, body=body, hops=hops)
            record_sent(home, env)
            ctx.send("mail", env, to=to)

    async def step(self, ctx) -> Step:
        new = [m.payload for m in ctx.recv_all() if isinstance(m.payload, Envelope)]
        home = self.org.home(self.agent)
        for e in new:
            if e.kind != "rollup":
                deliver_file(home, e)
        envs = self.pending + new
        self.pending = []
        if not envs:
            return Step.wait()
        mail = [e for e in envs if e.kind != "rollup"]
        rollup = [e for e in envs if e.kind == "rollup"]
        if self.turns >= self.turn_budget:
            # Mail stays in the inbox folder and is picked up next run.
            self._log(f"         {self.name}: turn budget ({self.turn_budget}) spent; "
                      f"{len(mail)} message(s) wait for the next run")
            if rollup:
                ctx.send("ack", Envelope(sender=self.name, recipient=LOOM, kind="ack",
                                         subject="rollup skipped", body=""), to=LOOM)
            return Step.wait()
        if mail:
            await self._turn(ctx, mail, "mail")
        if rollup:
            await self._turn(ctx, [], "rollup")
            ctx.send("ack", Envelope(sender=self.name, recipient=LOOM, kind="ack",
                                     subject="rollup done", body=""), to=LOOM)
        return Step.wait()


class OwnerLoop(NanoLoop):
    """The human's end of the wire: collects what reaches the top."""

    def __init__(self, org: Org, log: Callable[[str], None]) -> None:
        super().__init__(OWNER)
        self.org, self._log = org, log
        self.received: List[Envelope] = []

    async def step(self, ctx) -> Step:
        box = self.org.runtime_dir / "owner" / "inbox"
        box.mkdir(parents=True, exist_ok=True)
        for m in ctx.recv_all():
            e = m.payload
            if not isinstance(e, Envelope):
                continue
            self.received.append(e)
            (box / f"{e.id}-{e.sender}.md").write_text(e.render())
            self._log(f"[t{ctx.tick:>3}] owner <- {e.sender}: {e.subject}")
        return Step.wait()


class RollupConductor(NanoLoop):
    def __init__(self, org: Org, log: Callable[[str], None]) -> None:
        super().__init__(LOOM)
        levels: Dict[int, List[str]] = defaultdict(list)
        for n in org.nodes():
            levels[n.depth].append(n.name)
        self.levels = [levels[d] for d in sorted(levels, reverse=True)]
        self.waiting: set = set()
        self._log = log

    async def step(self, ctx) -> Step:
        for m in ctx.recv_all():
            if isinstance(m.payload, Envelope) and m.payload.kind == "ack":
                self.waiting.discard(m.sender)
        if self.waiting:
            return Step.wait()
        if not self.levels:
            self._log(f"[t{ctx.tick:>3}] rollup complete")
            return Step.done()
        level = self.levels.pop(0)
        self._log(f"[t{ctx.tick:>3}] rollup level: {', '.join(level)}")
        self.waiting = set(level)
        for name in level:
            ctx.send("rollup", Envelope(sender=LOOM, recipient=name, kind="rollup",
                                        subject="rollup", body=""), to=name)
        return Step.wait()


class Weave:
    """Build and run a Loom for one org."""

    def __init__(self, org: Org, *, turn_budget: int = DEFAULT_TURN_BUDGET,
                 network: str = "bridge", log: Callable[[str], None] = print) -> None:
        self.org = org
        self.log = log
        self.audit = Audit(org)
        self.loom = Loom(logger=log)
        self.loom.bus = PolicyBus(org, self.audit, log)
        docker, mock = DockerRuntime(network), MockRuntime()
        self.loops: Dict[str, NodeLoop] = {}
        for name, agent in org.agents.items():
            loop = NodeLoop(org, agent, runtime_for(agent, docker, mock), turn_budget, log)
            self.loops[name] = loop
            self.loom.add(loop)
        self.owner = OwnerLoop(org, log)
        self.loom.add(self.owner)

    def post(self, recipient: str, subject: str, body: str, sender: str = OWNER) -> int:
        env = Envelope(sender=sender, recipient=recipient, subject=subject, body=body)
        return self.loom.bus.publish(Message(topic="mail", payload=env, sender=sender, recipient=recipient))

    def with_rollup(self) -> "Weave":
        self.loom.add(RollupConductor(self.org, self.log))
        return self

    async def run(self, max_ticks: int = 200):
        t0 = time.time()
        report = await self.loom.run(max_ticks=max_ticks)
        turns = {n: l.turns for n, l in self.loops.items() if l.turns}
        self.log(f"done in {time.time() - t0:.1f}s: {report.ticks} ticks, "
                 f"{sum(turns.values())} turns, {report.messages} delivered, "
                 f"{self.loom.bus.bounced} refused")
        return report
