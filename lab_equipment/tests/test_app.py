import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from werkzeug.security import generate_password_hash  # noqa: E402

from app import (BOOKED, CANCELLED, DONE, IN_USE, NO_SHOW, ROLE_ADMIN, ROLE_OPERATOR,  # noqa: E402
                 TIME_FMT, create_app, seed_demo_data)

ADMIN_PASSWORD = "admin-pass-123"
OPERATOR_PASSWORD = "operator-pass-123"


def ts(hours):
    d = (datetime.now() + timedelta(hours=hours)).replace(second=0, microsecond=0)
    return d.strftime(TIME_FMT)


class AuthClient:
    """测试用包装: 自动附带 CSRF 令牌, 与浏览器表单行为一致。"""

    def __init__(self, client):
        self.c = client

    def token(self):
        with self.c.session_transaction() as s:
            return s.get("csrf_token", "")

    def login(self, username, password):
        self.c.get("/login")
        return self.c.post("/login", data={"username": username, "password": password,
                                           "csrf_token": self.token()})

    def get(self, url, **kw):
        return self.c.get(url, **kw)

    def post(self, url, data=None, **kw):
        data = dict(data or {})
        data.setdefault("csrf_token", self.token())
        return self.c.post(url, data=data, **kw)


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("LAB_ADMIN_PASSWORD", ADMIN_PASSWORD)  # 首次启动创建的管理员
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "test.db")})
    with app.app_context():
        db = app.get_db()
        db.execute("INSERT INTO equipment (name, status, max_hours, created_at) "
                   "VALUES ('HPLC', '可用', 4, '2026-01-01T00:00')")
        db.execute("INSERT INTO equipment (name, status, created_at) "
                   "VALUES ('离心机', '维护中', '2026-01-01T00:00')")
        db.execute("INSERT INTO users (name, created_at) VALUES ('张三', '2026-01-01T00:00')")
        db.execute("INSERT INTO users (name, created_at) VALUES ('李四', '2026-01-01T00:00')")
        db.execute("INSERT INTO accounts (username, password_hash, role, created_at) VALUES (?,?,?,?)",
                   ("op", generate_password_hash(OPERATOR_PASSWORD), ROLE_OPERATOR,
                    "2026-01-01T00:00"))
        db.commit()
    return app


@pytest.fixture
def client(app):
    c = AuthClient(app.test_client())
    assert c.login("admin", ADMIN_PASSWORD).status_code == 302
    return c


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
                "/reservations/1/edit", "/schedule", "/users", "/users/1", "/stats", "/accounts"]:
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


def test_seed_demo_data(tmp_path, monkeypatch):
    monkeypatch.setenv("LAB_ADMIN_PASSWORD", ADMIN_PASSWORD)
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "demo.db")})
    seed_demo_data(app)
    client = AuthClient(app.test_client())
    assert client.login("admin", ADMIN_PASSWORD).status_code == 302
    for url in ["/", "/schedule", "/stats", "/reservations", "/equipment/1"]:
        assert client.get(url).status_code == 200


# ---------------------------------------------------------------- 登录与权限

def test_anonymous_is_redirected_to_login(app):
    c = app.test_client()
    resp = c.get("/reservations")
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]
    assert c.get("/login").status_code == 200


def test_login_rejects_wrong_password_and_keeps_anonymous(app):
    c = AuthClient(app.test_client())
    resp = c.login("admin", "wrong-password")
    assert resp.status_code == 200 and "用户名或密码错误" in resp.get_data(as_text=True)
    assert c.get("/reservations").status_code == 302


def test_login_redirects_to_safe_next_only(app):
    c = AuthClient(app.test_client())
    c.get("/login")
    resp = c.post("/login?next=//evil.example.com", data={
        "username": "admin", "password": ADMIN_PASSWORD, "csrf_token": c.token()})
    assert resp.headers["Location"].endswith("/")
    assert "evil" not in resp.headers["Location"]


def test_logout_ends_session(client):
    client.post("/logout")
    assert client.get("/reservations").status_code == 302


def test_operator_cannot_do_admin_actions(app):
    op = AuthClient(app.test_client())
    assert op.login("op", OPERATOR_PASSWORD).status_code == 302
    assert op.get("/equipment/new").status_code == 403
    assert op.post("/equipment/new", data={"name": "X"}).status_code == 403
    assert op.post("/equipment/1/delete").status_code == 403
    assert op.post("/users/1/delete").status_code == 403
    assert op.post("/reservations/1/delete").status_code == 403
    assert op.post("/equipment/1/maintenance", data={"content": "x"}).status_code == 403
    assert op.get("/accounts").status_code == 403
    with app.app_context():  # 数据未被改动
        assert app.get_db().execute("SELECT COUNT(*) FROM equipment").fetchone()[0] == 2


def test_operator_can_run_bookings(app):
    op = AuthClient(app.test_client())
    op.login("op", OPERATOR_PASSWORD)
    resp = op.post("/reservations/new", data={
        "equipment_id": 1, "user_id": 1, "start_time": ts(1), "end_time": ts(2)}, follow_redirects=True)
    assert "预约成功" in resp.get_data(as_text=True)
    assert "上机签到" in op.get("/").get_data(as_text=True)
    assert "删除" not in op.get("/reservations/1").get_data(as_text=True)


def test_csrf_token_required_on_post(app, client):
    # 已登录但未携带令牌 -> 400, 数据不变
    raw = app.test_client()
    raw.get("/login")
    with raw.session_transaction() as s:
        s["account_id"] = 1
        s["csrf_token"] = "real-token"
    assert raw.post("/reservations/new", data={"equipment_id": 1}).status_code == 400
    assert raw.post("/reservations/new", data={"equipment_id": 1,
                                               "csrf_token": "forged"}).status_code == 400
    with app.app_context():
        assert app.get_db().execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 0


def test_admin_creates_and_deletes_accounts(client, app):
    client.post("/accounts", data={"username": "newbie", "password": "longenough",
                                   "role": ROLE_OPERATOR})
    with app.app_context():
        row = app.get_db().execute("SELECT * FROM accounts WHERE username='newbie'").fetchone()
        assert row["role"] == ROLE_OPERATOR and row["password_hash"] != "longenough"
    new = AuthClient(app.test_client())
    assert new.login("newbie", "longenough").status_code == 302

    client.post("/accounts", data={"username": "newbie", "password": "longenough", "role": ROLE_ADMIN})
    client.post("/accounts", data={"username": "short", "password": "1", "role": ROLE_OPERATOR})
    with app.app_context():
        assert app.get_db().execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 3

    aid = 3  # newbie
    client.post(f"/accounts/{aid}/delete")
    with app.app_context():
        assert app.get_db().execute("SELECT COUNT(*) FROM accounts WHERE username='newbie'").fetchone()[0] == 0


def test_cannot_delete_self_or_last_admin(client, app):
    client.post("/accounts/1/delete")  # 当前登录账号
    client.post("/accounts/1/update", data={"role": ROLE_OPERATOR})  # 自己降级
    with app.app_context():
        row = app.get_db().execute("SELECT role FROM accounts WHERE id=1").fetchone()
        assert row["role"] == ROLE_ADMIN
        assert app.get_db().execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 2


def test_admin_resets_password_and_changes_role(client, app):
    client.post("/accounts/2/update", data={"role": ROLE_ADMIN, "password": "brand-new-pw"})
    with app.app_context():
        assert app.get_db().execute("SELECT role FROM accounts WHERE id=2").fetchone()[0] == ROLE_ADMIN
    op = AuthClient(app.test_client())
    assert op.login("op", "brand-new-pw").status_code == 302
    assert op.get("/accounts").status_code == 200  # 已升为管理员


def test_initial_admin_generated_password_is_printed(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("LAB_ADMIN_PASSWORD", raising=False)
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "gen.db")})
    out = capsys.readouterr().out
    assert "初始密码:" in out
    password = out.split("初始密码:")[1].split("\n")[0].strip()
    c = AuthClient(app.test_client())
    assert c.login("admin", password).status_code == 302
    # 二次启动不会再创建或打印
    create_app({"TESTING": True, "DATABASE": str(tmp_path / "gen.db")})
    assert "初始密码" not in capsys.readouterr().out
