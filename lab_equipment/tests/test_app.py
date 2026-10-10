import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import BOOKED, CANCELLED, DONE, IN_USE, NO_SHOW, TIME_FMT, create_app, seed_demo_data  # noqa: E402


def ts(hours):
    d = (datetime.now() + timedelta(hours=hours)).replace(second=0, microsecond=0)
    return d.strftime(TIME_FMT)


@pytest.fixture
def app(tmp_path):
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "test.db")})
    with app.app_context():
        db = app.get_db()
        db.execute("INSERT INTO equipment (name, status, max_hours, created_at) "
                   "VALUES ('HPLC', '可用', 4, '2026-01-01T00:00')")
        db.execute("INSERT INTO equipment (name, status, created_at) "
                   "VALUES ('离心机', '维护中', '2026-01-01T00:00')")
        db.execute("INSERT INTO users (name, created_at) VALUES ('张三', '2026-01-01T00:00')")
        db.execute("INSERT INTO users (name, created_at) VALUES ('李四', '2026-01-01T00:00')")
        db.commit()
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def reservation(app, rid):
    with app.app_context():
        return dict(app.get_db().execute("SELECT * FROM reservations WHERE id=?", (rid,)).fetchone())


def book(client, eq=1, user=1, start=1, end=2):
    return client.post("/reservations/new", data={
        "equipment_id": eq, "user_id": user, "start_time": ts(start), "end_time": ts(end),
        "purpose": "测试"}, follow_redirects=True)


def test_all_pages_render(client):
    book(client)
    for url in ["/", "/equipment", "/equipment/1", "/equipment/new", "/equipment/1/edit",
                "/reservations", "/reservations/new", "/reservations/1",
                "/reservations/1/edit", "/schedule", "/users", "/users/1", "/stats"]:
        assert client.get(url).status_code == 200, url
    assert client.get("/equipment/999").status_code == 404


def test_create_reservation(client, app):
    resp = book(client)
    assert "预约成功" in resp.get_data(as_text=True)
    r = reservation(app, 1)
    assert r["status"] == BOOKED and r["user_id"] == 1 and r["start_time"] == ts(1)


def test_conflict_detection(client, app):
    book(client, start=1, end=3)
    resp = book(client, user=2, start=2, end=4)
    assert "时间冲突" in resp.get_data(as_text=True)
    # 首尾相接不算冲突
    resp = book(client, user=2, start=3, end=4)
    assert "预约成功" in resp.get_data(as_text=True)
    # 取消后时间段释放
    client.post("/reservations/1/cancel")
    assert reservation(app, 1)["status"] == CANCELLED
    assert "预约成功" in book(client, user=2, start=1, end=2).get_data(as_text=True)


def test_validation_rules(client):
    assert "结束时间必须晚于开始时间" in book(client, start=2, end=1).get_data(as_text=True)
    assert "不可预约" in book(client, eq=2).get_data(as_text=True)
    assert "单次最长" in book(client, start=1, end=6).get_data(as_text=True)
    assert "已经过去" in book(client, start=-3, end=-2).get_data(as_text=True)


def test_checkin_checkout_records_actual_time(client, app):
    book(client)
    client.post("/reservations/1/checkin")
    r = reservation(app, 1)
    assert r["status"] == IN_USE and r["actual_start"]
    client.post("/reservations/1/checkout", data={"usage_note": "正常"})
    r = reservation(app, 1)
    assert r["status"] == DONE and r["actual_end"] > r["actual_start"]
    assert r["usage_note"] == "正常"


def test_checkin_blocked_while_in_use(client, app):
    book(client, start=1, end=2)
    book(client, user=2, start=2, end=3)
    client.post("/reservations/1/checkin")
    resp = client.post("/reservations/2/checkin", follow_redirects=True)
    assert "正在被他人使用" in resp.get_data(as_text=True)
    assert reservation(app, 2)["status"] == BOOKED


def test_manual_record_and_noshow(client, app):
    book(client)
    resp = client.post("/reservations/1/record", data={
        "actual_start": ts(-2), "actual_end": ts(-1), "usage_note": "补录"}, follow_redirects=True)
    assert "已登记" in resp.get_data(as_text=True)
    r = reservation(app, 1)
    assert r["status"] == DONE and r["actual_start"] == ts(-2)
    # 结束早于开始被拒绝
    resp = client.post("/reservations/1/record", data={
        "actual_start": ts(-1), "actual_end": ts(-2)}, follow_redirects=True)
    assert "必须晚于" in resp.get_data(as_text=True)

    book(client, start=3, end=4)
    client.post("/reservations/2/noshow")
    assert reservation(app, 2)["status"] == NO_SHOW


def test_stats_and_csv(client, app):
    book(client)
    client.post("/reservations/1/record", data={"actual_start": ts(-3), "actual_end": ts(-1)})
    with app.app_context():  # 让预约落在统计区间内
        app.get_db().execute("UPDATE reservations SET start_time=?, end_time=?", (ts(-3), ts(-2)))
        app.get_db().commit()
    html = client.get("/stats").get_data(as_text=True)
    assert "HPLC" in html and "2.0 h" in html
    resp = client.get("/reservations?export=csv")
    assert resp.mimetype == "text/csv"
    body = resp.get_data(as_text=True)
    assert "实际时长(h)" in body and "张三" in body


def test_equipment_and_user_crud(client, app):
    client.post("/equipment/new", data={"name": "PCR 仪", "status": "可用", "max_hours": "2"})
    client.post("/equipment/3/edit", data={"name": "PCR 仪 2", "status": "停用"})
    client.post("/equipment/3/maintenance", data={"content": "校准", "cost": "100"})
    client.post("/users", data={"name": "王五", "role": "教师"})
    with app.app_context():
        db = app.get_db()
        assert db.execute("SELECT name, status FROM equipment WHERE id=3").fetchone()[:] == ("PCR 仪 2", "停用")
        assert db.execute("SELECT COUNT(*) FROM maintenance").fetchone()[0] == 1
        assert db.execute("SELECT role FROM users WHERE name='王五'").fetchone()[0] == "教师"
    client.post("/equipment/3/delete")
    with app.app_context():
        assert app.get_db().execute("SELECT COUNT(*) FROM maintenance").fetchone()[0] == 0


def test_seed_demo_data(tmp_path):
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "demo.db")})
    seed_demo_data(app)
    client = app.test_client()
    for url in ["/", "/schedule", "/stats", "/reservations", "/equipment/1"]:
        assert client.get(url).status_code == 200
