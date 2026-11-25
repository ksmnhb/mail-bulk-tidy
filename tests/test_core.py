import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import core  # noqa: E402
from core import MailCleaner, ToolError  # noqa: E402
from tests.fake_imap import FakeIMAP  # noqa: E402


def make_messages():
    msgs = {}
    for i in range(1, 2501):
        if i % 10 == 0:
            msgs[i] = b"From: evil <news@example.com.evil.jp>"          # 部分一致だけ
        elif i % 25 == 1:
            msgs[i] = b"From: a <news@example.com>, b <x@y.jp>"         # 複数アドレス
        else:
            msgs[i] = b"From: =?UTF-8?B?44OL44Ol44O844K5?= <News@Example.com>"
    msgs[5000] = b"From: other <someone@else.jp>"
    msgs[5001] = b"From: other <someone@else.jp>"
    msgs[5002] = b"From: other <someone@else.jp>"
    msgs[5003] = b"From: other <someone@else.jp>"
    msgs[5004] = b"From: other <someone@else.jp>"
    return msgs


def cleaner(tmp_path, **kw):
    fake = FakeIMAP(make_messages(), **kw)
    c = MailCleaner(imap_factory=lambda host: fake, log_file=tmp_path / "log.csv")
    return c, fake


def test_helpers():
    assert core.compress_uids([b"1", b"2", b"3", b"7", b"9", b"10"]) == "1:3,7,9:10"
    sets = core.split_uid_sets(range(1, 3000, 2), max_len=200)
    assert all(len(s) <= 200 for s in sets)
    assert sum(len(core.split_uid_sets([int(x) for x in s.split(",")], 10**9)[0].split(",")) for s in sets) == 1500
    assert core.decode_folder_name("&MLQw33ux-") == "ゴミ箱"
    assert core._csv_safe("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert core._csv_safe("news@example.com") == "news@example.com"


def test_classify_from():
    t = "news@example.com"
    assert core.classify_from("ニュース <News@Example.com>", t) == (True, "")
    assert core.classify_from("x <news@example.com.evil.jp>", t)[0] is False
    ok, reason = core.classify_from("a <news@example.com>, b <x@y.jp>", t)
    assert not ok and reason.startswith("差出人が複数")
    assert core.classify_from("", t) == (False, "差出人を読み取れない")


def test_login_failure_message(tmp_path):
    c, _ = cleaner(tmp_path)
    with pytest.raises(ToolError, match="ログインできません"):
        c.connect("h", "u", "wrong")
    assert c.imap is None


def test_refuses_server_without_safe_move(tmp_path):
    c, _ = cleaner(tmp_path, caps="IMAP4rev1")
    with pytest.raises(ToolError, match="MOVE / UIDPLUS"):
        c.connect("h", "u", "pw")


def test_trash_guess(tmp_path):
    c, _ = cleaner(tmp_path)
    c.connect("h", "u", "pw")
    folders = c.list_folders()
    assert [f.display for f in folders] == ["INBOX", "Trash", "ゴミ箱"]
    assert MailCleaner.guess_trash(folders).raw == "Trash"


def test_search_exact_only(tmp_path):
    c, _ = cleaner(tmp_path)
    c.connect("h", "u", "pw")
    res = c.search(" News@Example.com ")
    assert res.candidates == 2500
    multi = sum(1 for i in range(1, 2501) if i % 25 == 1 and i % 10 != 0)
    partial = 250
    assert len(res.uids) == 2500 - multi - partial
    assert res.excluded["news@example.com.evil.jp"] == partial
    assert res.preview and "news@example.com" in res.preview[0][1].lower()
    with pytest.raises(ToolError):
        c.search("news")


@pytest.mark.parametrize("caps", ["IMAP4rev1 MOVE", "IMAP4rev1 UIDPLUS"])
def test_move_and_verify(tmp_path, caps):
    c, fake = cleaner(tmp_path, caps=caps)
    c.connect("h", "u", "pw")
    res = c.search("news@example.com")
    r1 = c.move_batch(res.address, res.uids[:200], "Trash")
    r2 = c.move_batch(res.address, res.uids[200:1200], "Trash")
    assert r1.ok and r2.ok and fake.trash == 1200
    # 他のメールは巻き込まない
    assert all(u in fake.inbox for u in range(5000, 5005))
    # 再検索すると残りだけが出る（中断しても続きから）
    assert len(c.search("news@example.com").uids) == len(res.uids) - 1200
    log = (tmp_path / "log.csv").read_text(encoding="utf-8-sig").splitlines()
    assert len(log) == 3 and log[1].endswith(",OK")
    if "MOVE" not in caps:
        assert any(cmd == "EXPUNGE" for cmd, _ in fake.commands)


def test_mismatch_is_reported(tmp_path):
    c, _ = cleaner(tmp_path, trash_drift=-1)
    c.connect("h", "u", "pw")
    res = c.search("news@example.com")
    r = c.move_batch(res.address, res.uids[:200], "Trash")
    assert not r.ok
    assert (tmp_path / "log.csv").read_text(encoding="utf-8-sig").splitlines()[-1].endswith("要確認")


def test_tls_context_verifies():
    import ssl
    ctx = core.make_ssl_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
