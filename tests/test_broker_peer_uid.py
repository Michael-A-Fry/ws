"""C782: the lease proxy serves a connection only when its peer's uid (read from /proc/net/tcp) is the uid the lease
was given to. A wrong uid, or a peer with no row, gets 403 and a logged refusal that holds the uid and never the token."""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

import pytest
from test_broker import VALUE, Env, Stub

from ws import broker

HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"

pytestmark = pytest.mark.skipif(not os.path.exists("/proc/net/tcp"), reason="needs /proc/net/tcp (Linux)")


@pytest.fixture
def env():
    d = Path(tempfile.mkdtemp(prefix="wsp"))
    s = Stub()
    threading.Thread(target=s.serve_forever, args=(0.05,), daemon=True).start()
    e = Env(d, s)
    e.write_registry()
    e.store.write("anthropic", VALUE)
    e.start()
    yield e
    e.broker.stop()
    s.go.set()
    s.shutdown()
    s.server_close()
    shutil.rmtree(d, ignore_errors=True)


def refusals(env) -> list[dict]:
    """The refusal rows. The reply goes out before the audit line is written, so wait a moment for it."""
    deadline = time.monotonic() + 5
    while True:
        rows = [x for x in env.audit() if x["op"] == "proxy" and x["status"] == "peer_refused"]
        if rows or time.monotonic() > deadline:
            return rows
        time.sleep(0.02)


def test_right_uid_gets_through_to_the_upstream(env):
    status, _, data = env.call()
    assert status == 200 and b'"ok"' in data
    assert env.stub.seen[-1]["headers"]["x-api-key"] == VALUE


def test_a_lease_for_another_uid_is_refused_with_403_and_never_reaches_upstream(env):
    lease = env.lease()
    for g in env.broker._grants.values():
        g.uid = os.geteuid() + 1  # the run record's uid, as the grant holds it, is not this peer's
    seen = len(env.stub.seen)
    status, _, data = env.call(lease=lease)
    assert status == 403 and b"peer_refused" in data
    assert len(env.stub.seen) == seen
    assert refusals(env)[-1]["uid"] == os.geteuid()
    token = lease.base_url.rsplit("/l/", 1)[1].split("/")[0]
    assert token not in env.log.read_text()


def test_a_peer_with_no_row_is_refused(env):
    table = env.base / "tcp"
    table.write_text(HEADER, encoding="ascii")
    env.broker.proc_net_tcp = (str(table),)
    status, _, _ = env.call()
    assert status == 403
    assert refusals(env)[-1]["uid"] is None


def test_a_missing_table_on_linux_is_refused_not_served(env):
    env.broker.proc_net_tcp = (str(env.base / "no-such-table"),)
    seen = len(env.stub.seen)
    status, _, _ = env.call()
    assert status == 403 and len(env.stub.seen) == seen  # fails closed: no check, no call


def test_the_row_is_matched_by_both_ports_and_its_uid_is_read(tmp_path):
    t = tmp_path / "tcp"
    t.write_text(
        HEADER
        + "   0: 0100007F:C350 0100007F:1F90 01 00000000:00000000 00:00000000 00000000  4242        0 1 1 0 0 0\n"
        + "   1: 0100007F:C351 0100007F:1F90 01 00000000:00000000 00:00000000 00000000  7        0 2 1 0 0 0\n",
        encoding="ascii",
    )
    assert broker.loopback_peer_uid(0xC350, 0x1F90, (str(t),)) == 4242
    assert broker.loopback_peer_uid(0xC350, 0x1F91, (str(t),)) is None
    with pytest.raises(NotImplementedError):
        broker.loopback_peer_uid(1, 2, (str(tmp_path / "missing"),))


def test_ipv6_and_ipv4_mapped_loopback_rows_match_and_other_addresses_do_not(tmp_path):
    mapped = "0000000000000000FFFF00000100007F"
    v6 = "00000000000000000000000001000000"
    t = tmp_path / "tcp6"
    row = "   {n}: {a}:{lp} {b}:{rp} 01 00000000:00000000 00:00000000 00000000  {uid}        0 1 1 0 0 0\n"
    t.write_text(
        HEADER
        + row.format(n=0, a=mapped, lp="C350", b=mapped, rp="1F90", uid=11)
        + row.format(n=1, a=v6, lp="C351", b=v6, rp="1F90", uid=12)
        + row.format(n=2, a="0000000000000000FFFF00000A00000A", lp="C352", b=mapped, rp="1F90", uid=13),
        encoding="ascii",
    )
    assert broker.loopback_peer_uid(0xC350, 0x1F90, (str(t),)) == 11
    assert broker.loopback_peer_uid(0xC351, 0x1F90, (str(t),)) is None  # ::1: the proxy listens on 127.0.0.1 only
    assert broker.loopback_peer_uid(0xC352, 0x1F90, (str(t),)) is None


def test_a_decoy_row_on_another_loopback_address_is_not_the_peer(tmp_path):
    t = tmp_path / "tcp"
    row = "   {n}: {a}:C350 {b}:1F90 01 00000000:00000000 00:00000000 00000000  {uid}        0 1 1 0 0 0\n"
    t.write_text(
        HEADER
        + row.format(n=0, a="0200007F", b="0100007F", uid=66)
        + row.format(n=1, a="0100007F", b="0100007F", uid=4242),
        encoding="ascii",
    )
    assert broker.loopback_peer_uid(0xC350, 0x1F90, (str(t),)) == 4242  # 127.0.0.2 decoy ignored, no -1


def test_the_lookup_gives_up_at_its_budget_and_says_no_row(tmp_path):
    t = tmp_path / "tcp"
    t.write_text(HEADER + "x\n" * 10, encoding="ascii")
    assert broker.loopback_peer_uid(1, 2, (str(t),), budget_s=-1) is None


def test_an_unreadable_second_table_is_skipped(tmp_path):
    t = tmp_path / "tcp"
    t.write_text(HEADER, encoding="ascii")
    assert broker.loopback_peer_uid(1, 2, (str(t), str(tmp_path / "no-tcp6"))) is None
