"""存量画像清理脚本:该清的清掉,不该动的一个都不能少。

读取侧与写入侧的归属过滤分别兜住了"念出去"和"再写进来",但**已经躺在磁盘上的
数据它们清不掉**。全量审计 `app/sessions/memory/*.json`(用与运行时同源的判据):
**62 份画像里 49 份受影响,越权事实 1 条、越权摘要 56 条。**

这个脚本删的是不可逆的数据,而误删买家自己的记忆没有任何补救办法——所以本文件里
"不该删的没被删"那几条断言和"该删的删掉了"同等重要。
"""

import json

import pytest

from app.scripts.clean_leaked_memory import clean_file, main, scan_file


LEAK_FACT = "订单ORD-20240115-001（物流单号SF1234567890）已发货，当前正在派送中"
LEAK_SUMMARY = "客服确认订单 ORD-20240115-001 已发货"


@pytest.fixture()
def db(monkeypatch, tmp_path_factory):
    from app.config import settings as st
    monkeypatch.setattr(st.settings, "auth_enabled", True)
    import app.db as db_mod
    from app.db import Database

    # 不能用 ":memory:":`Database` 每次 connect()/close() 都开新连接,
    # 内存库每次都是空的,建表和查询根本不在同一个库上。
    d = Database(db_path=str(tmp_path_factory.mktemp("db") / "ecom.db"))
    d.init_schema()
    conn = d.connect()
    conn.execute("INSERT INTO orders (order_id,user,status,total,tracking_number) "
                 "VALUES (?,?,?,?,?)",
                 ("ORD-20240115-001", "小明", "shipped", 899.0, "SF1234567890"))
    conn.execute("INSERT INTO orders (order_id,user,status,total) VALUES (?,?,?,?)",
                 ("ORD-MINE-1", "1", "pending", 99.0))
    conn.commit()
    conn.close()
    monkeypatch.setattr(db_mod, "_DB", d)
    return d


def _profile(tmp_path, name="1.json", user_id="1", facts=(), summaries=()):
    p = tmp_path / name
    p.write_text(json.dumps({
        "user_id": user_id,
        "facts": [{"content": c, "category": "behavior", "created_at": "2026-08-13"}
                  for c in facts],
        "interaction_summaries": [{"summary": s, "timestamp": "2026-08-13"}
                                  for s in summaries],
    }, ensure_ascii=False), encoding="utf-8")
    return p


# --------------------------------------------------------------------------
# 审计
# --------------------------------------------------------------------------

def test_scan_finds_other_users_entries(db, tmp_path):
    p = _profile(tmp_path, facts=[LEAK_FACT, "订单ORD-MINE-1待发货", "偏好红褐色系"],
                 summaries=[LEAK_SUMMARY, "用户咨询了退货运费规则"])
    r = scan_file(p)
    assert len(r["bad_facts"]) == 1
    assert len(r["bad_summaries"]) == 1
    assert r["bad_facts"][0]["content"] == LEAK_FACT


def test_scan_does_not_modify_the_file(db, tmp_path):
    """审计必须是只读的——干跑模式的全部意义就在这里。"""
    p = _profile(tmp_path, facts=[LEAK_FACT])
    before = p.read_text(encoding="utf-8")
    scan_file(p)
    assert p.read_text(encoding="utf-8") == before


def test_owner_profile_is_clean(db, tmp_path):
    """同一条内容,在**订单属主**的画像里不算越权。"""
    p = _profile(tmp_path, name="xm.json", user_id="小明", facts=[LEAK_FACT])
    r = scan_file(p)
    assert r["bad_facts"] == []


def test_unknown_order_ids_are_not_touched(db, tmp_path):
    """库里查不到的单号不算泄漏——查不到就没有谁的隐私可泄,删掉只是白丢记忆。"""
    p = _profile(tmp_path, facts=["订单ORD-19990101-999 已取消"])
    assert scan_file(p)["bad_facts"] == []


# --------------------------------------------------------------------------
# 清理
# --------------------------------------------------------------------------

def test_clean_removes_only_the_bad_entries(db, tmp_path):
    """**核心断言的两半**:该删的删了,不该删的一条不少。"""
    p = _profile(tmp_path, facts=[LEAK_FACT, "订单ORD-MINE-1待发货", "偏好红褐色系"],
                 summaries=[LEAK_SUMMARY, "用户咨询了退货运费规则"])
    clean_file(scan_file(p))

    data = json.loads(p.read_text(encoding="utf-8"))
    contents = [f["content"] for f in data["facts"]]
    summaries = [s["summary"] for s in data["interaction_summaries"]]

    assert LEAK_FACT not in contents
    assert contents == ["订单ORD-MINE-1待发货", "偏好红褐色系"], "误删了买家自己的记忆"
    assert summaries == ["用户咨询了退货运费规则"]


def test_clean_preserves_other_top_level_keys(db, tmp_path):
    """画像里还有别的字段(user_id 等),清理不能把它们抹掉。"""
    p = _profile(tmp_path, facts=[LEAK_FACT])
    clean_file(scan_file(p))
    assert json.loads(p.read_text(encoding="utf-8"))["user_id"] == "1"


def test_clean_leaves_no_temp_file(db, tmp_path):
    """原子写:清理被打断不能留下半个画像顶替掉好文件。"""
    p = _profile(tmp_path, facts=[LEAK_FACT])
    clean_file(scan_file(p))
    assert not (tmp_path / "1.json.tmp").exists()
    json.loads(p.read_text(encoding="utf-8"))          # 能解析 = 完整写入


def test_clean_is_idempotent(db, tmp_path):
    p = _profile(tmp_path, facts=[LEAK_FACT, "偏好红褐色系"])
    clean_file(scan_file(p))
    once = p.read_text(encoding="utf-8")
    clean_file(scan_file(p))
    assert p.read_text(encoding="utf-8") == once


# --------------------------------------------------------------------------
# CLI:默认不改文件;门控与运行时一致
# --------------------------------------------------------------------------

def test_dry_run_changes_nothing(db, tmp_path, capsys):
    """**默认干跑**。删记忆不可逆,默认行为必须是只看不动。"""
    p = _profile(tmp_path, facts=[LEAK_FACT])
    before = p.read_text(encoding="utf-8")

    assert main(["--memory-dir", str(tmp_path)]) == 0
    assert p.read_text(encoding="utf-8") == before
    assert "没有改动任何文件" in capsys.readouterr().out


def test_apply_backs_up_before_writing(db, tmp_path, capsys):
    """写之前必须先整目录备份——误删买家记忆没有别的补救办法。"""
    p = _profile(tmp_path, facts=[LEAK_FACT, "偏好红褐色系"])
    assert main(["--memory-dir", str(tmp_path), "--apply"]) == 0

    backups = list(tmp_path.parent.glob("memory_backup_*"))
    assert backups, "没有备份就动了文件"
    restored = json.loads((backups[0] / "1.json").read_text(encoding="utf-8"))
    assert any(f["content"] == LEAK_FACT for f in restored["facts"]), "备份里没有原始内容"
    assert LEAK_FACT not in [f["content"] for f in
                             json.loads(p.read_text(encoding="utf-8"))["facts"]]


def test_auth_disabled_cleans_nothing(db, tmp_path, monkeypatch, capsys):
    """`auth_enabled=False` 时没有"归属"这回事,一个字都不该动。

    门控与 `owned_order` / `filter_owned` 严格一致——清理脚本和运行时判据分叉,
    会清出"运行时还拦着、脚本以为干净"这种最糟的状态。
    """
    from app.config import settings as st
    monkeypatch.setattr(st.settings, "auth_enabled", False)

    p = _profile(tmp_path, facts=[LEAK_FACT])
    before = p.read_text(encoding="utf-8")
    assert main(["--memory-dir", str(tmp_path), "--apply"]) == 0
    assert p.read_text(encoding="utf-8") == before
    assert "不做清理" in capsys.readouterr().out


def test_unreadable_file_is_reported_not_skipped_silently(db, tmp_path, capsys):
    """读不出的文件要报出来。静默跳过会让"这份没清"变成没人知道的事。"""
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    _profile(tmp_path, facts=[LEAK_FACT])
    main(["--memory-dir", str(tmp_path)])
    assert "读不出的文件" in capsys.readouterr().out


def test_missing_dir_returns_nonzero(db, tmp_path):
    assert main(["--memory-dir", str(tmp_path / "nope")]) == 1
