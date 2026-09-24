"""hagent -- run a pyramid of agents, one per folder.

    hagent init <dir> [--template small-company] [--runtime mock|nanoloop]
    hagent tree                      the org, with access and system agents
    hagent plan [agent]              mounts and mail routes per agent (dry run)
    hagent verify                    prove the isolation with real containers
    hagent diff                      config changes proposed since last apply
    hagent apply                     validate org/ and make it live
    hagent ask [agent] <message> [-s subject]
                                     owner -> agent, then run until quiet
                                     (agent defaults to the unit whose folder you are in)
    hagent run                       process mail left from earlier runs
    hagent rollup                    refresh STATUS.md bottom-up, level by level
    hagent inbox                     what reached the owner
    hagent log [-n N]                the mail audit trail

Run it anywhere inside an org (it walks up to find org/node.toml), or pass --org DIR.
"""

from __future__ import annotations

import argparse
import asyncio
import filecmp
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .policy import OWNER, check_mounts, contacts, ensure_dirs, mounts_for
from .tree import Node, Org, OrgError, SystemAgent, find_root, load

TEMPLATES = Path(__file__).parent / "templates"


def _root(args) -> Path:
    if args.org:
        return Path(args.org).resolve()
    r = find_root(Path.cwd())
    if r is None:
        raise OrgError("not inside an org (no org/node.toml above here); pass --org DIR or run: hagent init DIR")
    return r


def _copy_config(src: Path, dst: Path) -> None:
    """Copy the control plane without system agents' memory (.system) or symlinks."""
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, symlinks=True,
                    ignore=lambda d, names: [n for n in names if n.startswith(".")])
    for p in dst.rglob("*"):
        if p.is_symlink():
            raise OrgError(f"{p}: symlinks are not allowed in the org tree")


# --- commands -------------------------------------------------------------------

def cmd_init(args) -> None:
    dst = Path(args.dir).resolve()
    tpl = TEMPLATES / args.template
    if not tpl.is_dir():
        raise OrgError(f"no template {args.template!r} (have: {', '.join(p.name for p in TEMPLATES.iterdir())})")
    if (dst / "org").exists():
        raise OrgError(f"{dst}/org already exists")
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copytree(tpl / "org", dst / "org")
    if args.runtime:
        for f in (dst / "org").rglob("node.toml"):
            f.write_text(f.read_text().replace('runtime = "nanoloop"', f'runtime = "{args.runtime}"'))
    _apply(dst)
    print(f"org created at {dst}")
    print(f"  control plane: {dst}/org   (edit, then: hagent diff / hagent apply)")
    print(f"  data plane:    {dst}/work  (one folder per unit: its knowledge)")
    print(f"next: cd {dst} && hagent tree")


def _apply(root: Path) -> Org:
    live = load(root, live=True)  # validates everything first
    _copy_config(root / "org", root / ".hagent" / "applied")
    org = load(root)
    for a in org.agents.values():
        ensure_dirs(org, a)
        check_mounts(org, a, mounts_for(org, a))
    gi = root / ".gitignore"
    if not gi.exists():
        gi.write_text(".hagent/\n")
    return org


def cmd_apply(args) -> None:
    root = _root(args)
    org = _apply(root)
    print(f"applied: {len(org.agents)} agents live ({', '.join(org.agents)})")


def cmd_diff(args) -> None:
    root = _root(args)
    applied = root / ".hagent" / "applied"
    load(root, live=True)  # show validation errors first
    r = subprocess.run(["diff", "-ruN", "-x", ".*", str(applied), str(root / "org")],
                       capture_output=True, text=True)
    print(r.stdout.replace(str(applied), "applied").replace(str(root / "org"), "org") or "(no pending changes)")


def _access(a) -> str:
    if isinstance(a, SystemAgent):
        return "config of units below " + a.principal.name
    s = f"below:{a.below}" if a.children else "leaf"
    if a.control != "none" and a.children:
        s += f" control:{a.control}"
    if a.peers and a.children:
        s += " peers"
    return s


def cmd_tree(args) -> None:
    org = load(_root(args))

    def show(n: Node, prefix: str, last: bool) -> None:
        conn = "" if n.parent is None else ("└─ " if last else "├─ ")
        print(f"{prefix}{conn}{n.name:<12} {n.role:<28} [{n.runtime}] {_access(n)}")
        kid_prefix = prefix + ("" if n.parent is None else ("   " if last else "│  "))
        if n.system:
            print(f"{kid_prefix}{'│  ' if n.children else '   '}⚙ {n.system.name} -- {n.system.role} [{n.system.runtime}]")
        for i, c in enumerate(n.children):
            show(c, kid_prefix, i == len(n.children) - 1)

    print("owner (human)")
    show(org.apex, "", True)


def cmd_plan(args) -> None:
    org = load(_root(args))
    names = [args.agent] if args.agent else list(org.agents)
    for name in names:
        a = org.get(name)
        kind = "system agent" if isinstance(a, SystemAgent) else "unit"
        print(f"{a.name} ({kind}, {a.runtime})")
        from .runtime import DockerRuntime
        for m in mounts_for(org, a, DockerRuntime.base_for(a)):
            print(f"  {m.mode}  {m.container:<12} <- {m.host.relative_to(org.root)}")
        print(f"  mail  {', '.join(contacts(org, a))}")
        print()


PROBE = r'''
t() { if sh -c "$2" >/dev/null 2>&1; then r=yes; else r=no; fi; printf '%s=%s\n' "$1" "$r"; }
t write_own   'echo x > /node/.probe && rm /node/.probe'
t read_below  'ls /node/units/*/ >/dev/null'
t write_below 'for d in /node/units/*/; do echo x > "$d.probe" && rm "$d.probe" || exit 1; done'
t read_ctrl   'ls /node/org >/dev/null && [ -n "$(ls /node/org)" ]'
t write_ctrl  'echo x > /node/org/.probe && rm /node/org/.probe'
'''


def cmd_verify(args) -> None:
    """Start a throwaway container per agent with its real mounts and try things."""
    org = load(_root(args))
    image = args.image
    if subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode != 0:
        raise OrgError(f"probe image {image!r} not present locally; pass --image <local image with sh>")
    print(f"{'agent':<12} {'own':<5} {'read↓':<6} {'write↓':<7} {'ctrl↓r':<7} {'ctrl↓w':<7} expected")
    bad = 0
    for a in org.agents.values():
        ensure_dirs(org, a)
        mounts = mounts_for(org, a)
        check_mounts(org, a, mounts)
        argv = ["docker", "run", "--rm", "--network", "none", "--entrypoint", "sh"]
        for m in mounts:
            argv += ["-v", m.docker_arg()]
        r = subprocess.run(argv + [image, "-c", PROBE], capture_output=True, text=True, timeout=120)
        got = dict(l.split("=", 1) for l in r.stdout.split() if "=" in l)
        if isinstance(a, Node):
            exp = {"write_own": "yes",
                   "read_below": "yes" if a.children else "no",
                   "write_below": "yes" if a.children and a.below == "write" else "no",
                   "read_ctrl": "yes" if a.children and a.control != "none" else "no",
                   "write_ctrl": "yes" if a.children and a.control == "write" else "no"}
        else:
            exp = {"write_own": "yes", "read_below": "no", "write_below": "no",
                   "read_ctrl": "yes", "write_ctrl": "yes"}
        ok = all(got.get(k) == v for k, v in exp.items())
        bad += not ok
        cols = [got.get(k, "?") for k in ("write_own", "read_below", "write_below", "read_ctrl", "write_ctrl")]
        print(f"{a.name:<12} {cols[0]:<5} {cols[1]:<6} {cols[2]:<7} {cols[3]:<7} {cols[4]:<7} "
              f"{'ok' if ok else 'MISMATCH ' + json.dumps(exp)}")
    print("isolation verified" if not bad else f"{bad} agent(s) do not match the policy")
    if bad:
        sys.exit(1)


def _weave(args, org: Org):
    from .weave import Weave
    return Weave(org, turn_budget=args.budget, network=args.network)


def cmd_ask(args) -> None:
    root = _root(args)
    org = load(root)
    words = list(args.words)
    if words and words[0] in org.agents and len(words) > 1:
        target = words.pop(0)
    else:
        here = org.node_at(Path.cwd())
        target = here.name if here else org.apex.name
    msg = " ".join(words).strip()
    if not msg:
        raise OrgError("empty message")
    w = _weave(args, org)
    w.post(target, args.subject or msg.splitlines()[0][:70], msg)
    print(f"owner -> {target}: {msg[:70]}")
    asyncio.run(w.run(max_ticks=args.ticks))


def cmd_run(args) -> None:
    org = load(_root(args))
    asyncio.run(_weave(args, org).run(max_ticks=args.ticks))


def cmd_rollup(args) -> None:
    org = load(_root(args))
    asyncio.run(_weave(args, org).with_rollup().run(max_ticks=args.ticks))
    print((org.work_path(org.apex) / "STATUS.md").read_text()
          if (org.work_path(org.apex) / "STATUS.md").exists() else "")


def cmd_inbox(args) -> None:
    box = _root(args) / ".hagent" / "owner" / "inbox"
    files = sorted(box.glob("*.md")) if box.is_dir() else []
    if not files:
        print("(nothing for the owner)")
    for f in files:
        print(f"=== {f.name}\n{f.read_text()}")


def cmd_log(args) -> None:
    f = _root(args) / ".hagent" / "mail.jsonl"
    rows = f.read_text().splitlines()[-args.n:] if f.exists() else []
    for line in rows:
        r = json.loads(line)
        mark = "  " if r["decision"] == "delivered" else "✗ "
        print(f"{mark}{r['ts']} {r['sender']:>12} -> {r['recipient']:<12} [{r['kind']}] "
              f"{r['subject'][:50]}" + ("" if mark == "  " else f"  ({r['reason']})"))


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="hagent", description="Hierarchical agents: one pyramid, one folder per agent.")
    p.add_argument("--org", help="org root (default: search upward from the current folder)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init"); s.add_argument("dir")
    s.add_argument("--template", default="small-company")
    s.add_argument("--runtime", choices=["mock", "nanoloop"], help="override every agent's runtime")
    s.set_defaults(fn=cmd_init)
    for name, fn in [("tree", cmd_tree), ("apply", cmd_apply), ("diff", cmd_diff), ("inbox", cmd_inbox)]:
        sub.add_parser(name).set_defaults(fn=fn)
    s = sub.add_parser("plan"); s.add_argument("agent", nargs="?"); s.set_defaults(fn=cmd_plan)
    s = sub.add_parser("verify")
    s.add_argument("--image", default=os.environ.get("HAGENT_PROBE_IMAGE", "hermes-web:local"))
    s.set_defaults(fn=cmd_verify)
    s = sub.add_parser("log"); s.add_argument("-n", type=int, default=40); s.set_defaults(fn=cmd_log)
    for name, fn in [("ask", cmd_ask), ("run", cmd_run), ("rollup", cmd_rollup)]:
        s = sub.add_parser(name)
        if name == "ask":
            s.add_argument("words", nargs="+")
            s.add_argument("-s", "--subject")
        s.add_argument("--budget", type=int, default=6, help="max turns per agent this run")
        s.add_argument("--ticks", type=int, default=200)
        s.add_argument("--network", default=os.environ.get("HAGENT_NETWORK", "bridge"))
        s.set_defaults(fn=fn)

    args = p.parse_args(argv)
    # Progress lines should appear as turns happen, also when piped to a file.
    sys.stdout.reconfigure(line_buffering=True)
    try:
        args.fn(args)
    except OrgError as e:
        print(f"hagent: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
