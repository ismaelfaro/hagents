"""The pyramid's rules, checked without Docker or a model (mock runtime)."""

import asyncio
import os
import shutil
from pathlib import Path

import pytest

from hagents.cli import _apply, main
from hagents.mail import collect_outbox
from hagents.policy import can_send, check_mounts, mounts_for
from hagents.tree import OrgError, load
from hagents.weave import Weave


@pytest.fixture
def org_root(tmp_path: Path) -> Path:
    main(["init", str(tmp_path / "acme"), "--runtime", "mock"])
    return tmp_path / "acme"


def run(w, ticks=100):
    return asyncio.run(w.run(max_ticks=ticks))


def quiet(_):
    pass


# --- visibility ------------------------------------------------------------

def test_mounts_never_leave_the_subtree(org_root):
    org = load(org_root)
    for a in org.agents.values():
        mounts = mounts_for(org, a)
        check_mounts(org, a, mounts)  # raises if anything escapes
        home = org.home(a)
        for m in mounts:
            if m.container == "/node":
                assert m.host == home


def test_leaf_sees_only_itself(org_root):
    org = load(org_root)
    ms = mounts_for(org, org.get("product"))
    assert [m.container for m in ms] == ["/node"]


def test_department_reads_but_cannot_write_below(org_root):
    org = load(org_root)
    below = [m for m in mounts_for(org, org.get("engineering")) if m.container == "/node/units"]
    assert below and below[0].mode == "ro"


def test_apex_can_touch_everything_below(org_root):
    org = load(org_root)
    modes = {m.container: m.mode for m in mounts_for(org, org.apex)}
    assert modes == {"/node": "rw", "/node/units": "rw", "/node/org": "rw"}


def test_system_agent_sees_config_not_work(org_root):
    org = load(org_root)
    for m in mounts_for(org, org.get("eng-admin")):
        assert org.work_dir not in m.host.parents and m.host != org.work_dir


def test_symlinked_mount_source_is_refused(org_root, tmp_path):
    org = load(org_root)
    product = org.work_path(org.get("product"))
    shutil.rmtree(product)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, product)
    with pytest.raises(OrgError, match="symlink"):
        check_mounts(org, org.get("product"), mounts_for(org, org.get("product")))


# --- mail ------------------------------------------------------------------

@pytest.mark.parametrize("s,r,ok", [
    ("ceo", "engineering", True), ("engineering", "ceo", True),
    ("product", "engineering", True), ("product", "ceo", False),     # skip-level: no
    ("product", "platform", False),                                   # engineering.peers = false
    ("sales", "marketing", True),                                     # ceo.peers = true
    ("product", "sales", False),                                      # cousins: no
    ("sales", "owner", False), ("ceo", "owner", True), ("ops", "owner", True),
    ("ops", "ceo", True), ("ops", "sales", False),                    # system agents see config, not people
    ("eng-admin", "engineering", True), ("eng-admin", "ops", True),
    ("owner", "product", True),
])
def test_mail_follows_the_org_chart(org_root, s, r, ok):
    assert can_send(load(org_root), s, r)[0] is ok


def test_out_of_policy_mail_bounces_and_sender_is_stamped(org_root):
    org = load(org_root)
    out = org.work_path(org.get("product")) / "mail" / "outbox"
    (out / "a.md").write_text("From: ceo\nTo: sales\nSubject: psst\n\nhello")
    w = Weave(org, log=quiet)
    w.post("product", "go", "do it")
    run(w)
    rows = (org_root / ".hagent" / "mail.jsonl").read_text()
    assert '"sender": "product", "recipient": "sales"' in rows   # not the forged "ceo"
    assert '"refused"' in rows
    read = org.work_path(org.get("product")) / "mail" / "read"
    assert any("Undeliverable" in p.read_text() for p in read.iterdir())


def test_symlink_in_outbox_is_not_delivered(org_root, tmp_path):
    org = load(org_root)
    secret = tmp_path / "secret"
    secret.write_text("To: engineering\n\nTOP SECRET")
    home = org.work_path(org.get("product"))
    os.symlink(secret, home / "mail" / "outbox" / "x.md")
    got = collect_outbox(home)
    assert got[0][3] and "not a regular file" in got[0][3]
    assert not (home / "mail" / "outbox" / "x.md").exists()
    assert secret.exists()


# --- orchestration ---------------------------------------------------------

def test_task_goes_down_and_answer_comes_up(org_root):
    w = Weave(load(org_root), log=quiet)
    w.post("ceo", "Q4 plan", "Prepare the Q4 launch plan")
    run(w)
    assert len(w.owner.received) == 1
    body = w.owner.received[0].body
    for unit in ("engineering", "platform", "product", "sales", "marketing", "finance"):
        assert unit in body


def test_rollup_is_bottom_up(org_root):
    org = load(org_root)
    w = Weave(org, log=quiet).with_rollup()
    run(w)
    ceo_status = (org.work_path(org.apex) / "STATUS.md").read_text()
    eng_status = org.work_path(org.get("engineering")) / "STATUS.md"
    assert "engineering" in ceo_status
    # engineering summarised after its teams: its mtime is later than theirs
    for team in ("platform", "product"):
        assert eng_status.stat().st_mtime >= (org.work_path(org.get(team)) / "STATUS.md").stat().st_mtime


def test_turn_budget_stops_runaway_and_keeps_mail(org_root):
    w = Weave(load(org_root), turn_budget=1, log=quiet)
    w.post("ceo", "a", "task a")
    w.post("ceo", "b", "task b")
    run(w)
    assert w.loops["ceo"].turns == 1


# --- control plane ---------------------------------------------------------

def test_system_agent_proposes_and_owner_applies(org_root):
    w = Weave(load(org_root), log=quiet)
    w.post("eng-admin", "hire", "please hire data under engineering")
    run(w)
    proposal = org_root / "org" / "units" / "engineering" / "units" / "data" / "node.toml"
    assert proposal.exists()
    assert "data" not in load(org_root).agents          # not live yet
    _apply(org_root)
    org = load(org_root)
    assert org.get("data").parent.name == "engineering"
    assert can_send(org, "data", "engineering")[0]


def test_system_agent_cannot_reach_outside_its_scope(org_root):
    w = Weave(load(org_root), log=quiet)
    w.post("eng-admin", "hire", "please hire spy under sales")
    run(w)
    assert not (org_root / "org" / "units" / "sales" / "units").exists()


def test_config_cannot_smuggle_grants(org_root):
    f = org_root / "org" / "units" / "sales" / "node.toml"
    f.write_text(f.read_text() + '\nmounts = ["/Users"]\n')
    with pytest.raises(OrgError, match="unknown key"):
        load(org_root, live=True)


def test_unit_name_must_match_folder(org_root):
    f = org_root / "org" / "units" / "sales" / "node.toml"
    f.write_text(f.read_text().replace('name = "sales"', 'name = "ceo2"'))
    with pytest.raises(OrgError, match="folder name"):
        load(org_root, live=True)
