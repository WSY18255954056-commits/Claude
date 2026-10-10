"""
实验室仪器管理系统 (Lab Equipment Management System)

功能:
    - 仪器管理: 仪器信息的增删改查, 状态 (可用/维护中/停用), 维护记录
    - 用户管理: 使用人信息 (姓名/课题组/联系方式/角色)
    - 预约管理: 预约时间段 + 使用人 + 用途, 自动检测时间冲突
    - 使用登记: 上机签到 / 下机签退 记录实际使用时间, 支持补录
    - 日程视图: 按天查看所有仪器的预约时间轴
    - 统计报表: 按仪器 / 按使用人统计预约时长、实际使用时长、爽约次数
    - 数据导出: 使用记录导出 CSV

运行:
    pip install -r requirements.txt
    python app.py            # 访问 http://127.0.0.1:5000
"""

import csv
import io
import os
import sqlite3
from datetime import datetime, timedelta

from flask import (Flask, Response, abort, flash, g, redirect,
                   render_template, request, url_for)

TIME_FMT = "%Y-%m-%dT%H:%M"

EQUIPMENT_STATUSES = ["可用", "维护中", "停用"]
USER_ROLES = ["学生", "教师", "工程师", "管理员", "校外人员"]

# 预约状态
BOOKED = "已预约"
IN_USE = "使用中"
DONE = "已完成"
CANCELLED = "已取消"
NO_SHOW = "爽约"
RESERVATION_STATUSES = [BOOKED, IN_USE, DONE, CANCELLED, NO_SHOW]
ACTIVE_STATUSES = (BOOKED, IN_USE)  # 占用时间段的状态

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    department  TEXT DEFAULT '',
    phone       TEXT DEFAULT '',
    email       TEXT DEFAULT '',
    role        TEXT DEFAULT '学生',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS equipment (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    model        TEXT DEFAULT '',
    serial_no    TEXT DEFAULT '',
    location     TEXT DEFAULT '',
    manager      TEXT DEFAULT '',
    status       TEXT NOT NULL DEFAULT '可用',
    max_hours    REAL DEFAULT 0,          -- 单次最长预约时长(小时), 0 表示不限
    description  TEXT DEFAULT '',
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reservations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    equipment_id  INTEGER NOT NULL REFERENCES equipment(id) ON DELETE CASCADE,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    start_time    TEXT NOT NULL,          -- 预约开始
    end_time      TEXT NOT NULL,          -- 预约结束
    purpose       TEXT DEFAULT '',
    status        TEXT NOT NULL DEFAULT '已预约',
    actual_start  TEXT,                   -- 实际开始使用
    actual_end    TEXT,                   -- 实际结束使用
    usage_note    TEXT DEFAULT '',        -- 使用情况 / 异常说明
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS maintenance (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    equipment_id  INTEGER NOT NULL REFERENCES equipment(id) ON DELETE CASCADE,
    date          TEXT NOT NULL,
    content       TEXT NOT NULL,
    operator      TEXT DEFAULT '',
    cost          REAL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_res_equipment ON reservations(equipment_id, start_time);
CREATE INDEX IF NOT EXISTS idx_res_user ON reservations(user_id);
"""


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------

def now_str():
    return datetime.now().strftime(TIME_FMT)


def parse_time(value):
    """解析 datetime-local 字符串, 失败返回 None。"""
    if not value:
        return None
    try:
        return datetime.strptime(value.strip()[:16], TIME_FMT)
    except ValueError:
        return None


def hours_between(start, end):
    s, e = parse_time(start), parse_time(end)
    if not s or not e or e <= s:
        return 0.0
    return (e - s).total_seconds() / 3600


def fmt_hours(h):
    return f"{h:.1f}"


def fmt_time(value):
    return value.replace("T", " ") if value else "—"


def find_conflicts(db, equipment_id, start, end, exclude_id=None):
    """查找同一仪器在 [start, end) 内与之重叠的有效预约。"""
    sql = (
        "SELECT r.*, u.name AS user_name FROM reservations r "
        "JOIN users u ON u.id = r.user_id "
        "WHERE r.equipment_id = ? AND r.status IN (?, ?) "
        "AND r.start_time < ? AND r.end_time > ?"
    )
    args = [equipment_id, *ACTIVE_STATUSES, end, start]
    if exclude_id is not None:
        sql += " AND r.id != ?"
        args.append(exclude_id)
    return db.execute(sql, args).fetchall()


def validate_reservation(db, form, exclude_id=None):
    """校验预约表单, 返回 (errors, cleaned_data)。"""
    errors = []
    equipment_id = form.get("equipment_id", type=int)
    user_id = form.get("user_id", type=int)
    start = parse_time(form.get("start_time"))
    end = parse_time(form.get("end_time"))

    equipment = db.execute("SELECT * FROM equipment WHERE id = ?",
                           (equipment_id,)).fetchone() if equipment_id else None
    user = db.execute("SELECT * FROM users WHERE id = ?",
                      (user_id,)).fetchone() if user_id else None

    if not equipment:
        errors.append("请选择仪器")
    elif equipment["status"] != "可用":
        errors.append(f"仪器「{equipment['name']}」当前状态为「{equipment['status']}」, 不可预约")
    if not user:
        errors.append("请选择使用人")
    if not start or not end:
        errors.append("请填写正确的预约开始和结束时间")
    elif end <= start:
        errors.append("结束时间必须晚于开始时间")
    else:
        if exclude_id is None and end < datetime.now():
            errors.append("不能预约已经过去的时间段")
        if equipment and equipment["max_hours"]:
            dur = (end - start).total_seconds() / 3600
            if dur > equipment["max_hours"]:
                errors.append(f"该仪器单次最长可预约 {equipment['max_hours']:g} 小时, "
                              f"当前为 {dur:.1f} 小时")

    data = {
        "equipment_id": equipment_id,
        "user_id": user_id,
        "start_time": start.strftime(TIME_FMT) if start else "",
        "end_time": end.strftime(TIME_FMT) if end else "",
        "purpose": form.get("purpose", "").strip(),
    }

    if not errors:
        conflicts = find_conflicts(db, equipment_id, data["start_time"],
                                   data["end_time"], exclude_id)
        for c in conflicts:
            errors.append(f"时间冲突: {c['user_name']} 已预约 "
                          f"{fmt_time(c['start_time'])} ~ {fmt_time(c['end_time'])}")
    return errors, data


# --------------------------------------------------------------------------
# 应用工厂
# --------------------------------------------------------------------------

def create_app(config=None):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", "lab-equipment-dev-key"),
        DATABASE=os.environ.get(
            "LAB_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "lab.db")),
    )
    if config:
        app.config.update(config)

    app.jinja_env.filters["t"] = fmt_time
    app.jinja_env.filters["h"] = fmt_hours
    app.jinja_env.globals.update(hours_between=hours_between, now_str=now_str)

    def get_db():
        if "db" not in g:
            g.db = sqlite3.connect(app.config["DATABASE"])
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys = ON")
        return g.db

    @app.teardown_appcontext
    def close_db(_exc):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    with app.app_context():
        get_db().executescript(SCHEMA)

    app.get_db = get_db

    # ------------------------------------------------------------------ 首页
    @app.route("/")
    def index():
        db = get_db()
        now = now_str()
        today = now[:10]
        counts = {
            "equipment": db.execute("SELECT COUNT(*) FROM equipment").fetchone()[0],
            "available": db.execute("SELECT COUNT(*) FROM equipment WHERE status='可用'").fetchone()[0],
            "users": db.execute("SELECT COUNT(*) FROM users").fetchone()[0],
            "today": db.execute(
                "SELECT COUNT(*) FROM reservations WHERE substr(start_time,1,10)=? "
                "AND status != ?", (today, CANCELLED)).fetchone()[0],
        }
        base = ("SELECT r.*, e.name AS equipment_name, e.location, u.name AS user_name "
                "FROM reservations r JOIN equipment e ON e.id=r.equipment_id "
                "JOIN users u ON u.id=r.user_id ")
        in_use = db.execute(base + "WHERE r.status=? ORDER BY r.actual_start", (IN_USE,)).fetchall()
        upcoming = db.execute(
            base + "WHERE r.status=? AND r.end_time>=? ORDER BY r.start_time LIMIT 10",
            (BOOKED, now)).fetchall()
        overdue = db.execute(
            base + "WHERE r.status=? AND r.end_time<? ORDER BY r.start_time",
            (BOOKED, now)).fetchall()
        return render_template("index.html", counts=counts, in_use=in_use,
                               upcoming=upcoming, overdue=overdue, now=now)

    # ------------------------------------------------------------------ 仪器
    @app.route("/equipment")
    def equipment_list():
        db = get_db()
        q = request.args.get("q", "").strip()
        status = request.args.get("status", "")
        sql = ("SELECT e.*, "
               "(SELECT COUNT(*) FROM reservations r WHERE r.equipment_id=e.id "
               " AND r.status=?) AS in_use "
               "FROM equipment e WHERE 1=1")
        args = [IN_USE]
        if q:
            sql += " AND (e.name LIKE ? OR e.model LIKE ? OR e.location LIKE ? OR e.serial_no LIKE ?)"
            args += [f"%{q}%"] * 4
        if status:
            sql += " AND e.status=?"
            args.append(status)
        rows = db.execute(sql + " ORDER BY e.id", args).fetchall()
        return render_template("equipment_list.html", rows=rows, q=q, status=status,
                               statuses=EQUIPMENT_STATUSES)

    def _equipment_form_data():
        f = request.form
        try:
            max_hours = float(f.get("max_hours") or 0)
        except ValueError:
            max_hours = 0
        return {
            "name": f.get("name", "").strip(),
            "model": f.get("model", "").strip(),
            "serial_no": f.get("serial_no", "").strip(),
            "location": f.get("location", "").strip(),
            "manager": f.get("manager", "").strip(),
            "status": f.get("status") if f.get("status") in EQUIPMENT_STATUSES else "可用",
            "max_hours": max(max_hours, 0),
            "description": f.get("description", "").strip(),
        }

    @app.route("/equipment/new", methods=["GET", "POST"])
    def equipment_new():
        if request.method == "POST":
            data = _equipment_form_data()
            if not data["name"]:
                flash("仪器名称不能为空", "danger")
                return render_template("equipment_form.html", item=data,
                                       statuses=EQUIPMENT_STATUSES)
            db = get_db()
            cur = db.execute(
                "INSERT INTO equipment (name, model, serial_no, location, manager, status, "
                "max_hours, description, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (*data.values(), now_str()))
            db.commit()
            flash(f"已添加仪器「{data['name']}」", "success")
            return redirect(url_for("equipment_detail", eid=cur.lastrowid))
        return render_template("equipment_form.html", item={}, statuses=EQUIPMENT_STATUSES)

    def _get_equipment(eid):
        row = get_db().execute("SELECT * FROM equipment WHERE id=?", (eid,)).fetchone()
        if row is None:
            abort(404)
        return row

    @app.route("/equipment/<int:eid>")
    def equipment_detail(eid):
        db = get_db()
        item = _get_equipment(eid)
        reservations = db.execute(
            "SELECT r.*, u.name AS user_name FROM reservations r "
            "JOIN users u ON u.id=r.user_id WHERE r.equipment_id=? "
            "ORDER BY r.start_time DESC LIMIT 50", (eid,)).fetchall()
        maint = db.execute("SELECT * FROM maintenance WHERE equipment_id=? ORDER BY date DESC",
                           (eid,)).fetchall()
        done = [r for r in reservations if r["status"] == DONE]
        summary = {
            "count": len(done),
            "hours": sum(hours_between(r["actual_start"], r["actual_end"]) for r in done),
        }
        return render_template("equipment_detail.html", item=item, reservations=reservations,
                               maint=maint, summary=summary, today=now_str()[:10])

    @app.route("/equipment/<int:eid>/edit", methods=["GET", "POST"])
    def equipment_edit(eid):
        item = _get_equipment(eid)
        if request.method == "POST":
            data = _equipment_form_data()
            if not data["name"]:
                flash("仪器名称不能为空", "danger")
                return render_template("equipment_form.html", item={**data, "id": eid},
                                       statuses=EQUIPMENT_STATUSES)
            db = get_db()
            db.execute(
                "UPDATE equipment SET name=?, model=?, serial_no=?, location=?, manager=?, "
                "status=?, max_hours=?, description=? WHERE id=?", (*data.values(), eid))
            db.commit()
            flash("仪器信息已更新", "success")
            return redirect(url_for("equipment_detail", eid=eid))
        return render_template("equipment_form.html", item=item, statuses=EQUIPMENT_STATUSES)

    @app.route("/equipment/<int:eid>/delete", methods=["POST"])
    def equipment_delete(eid):
        item = _get_equipment(eid)
        db = get_db()
        db.execute("DELETE FROM equipment WHERE id=?", (eid,))
        db.commit()
        flash(f"已删除仪器「{item['name']}」及其相关记录", "warning")
        return redirect(url_for("equipment_list"))

    @app.route("/equipment/<int:eid>/maintenance", methods=["POST"])
    def maintenance_add(eid):
        _get_equipment(eid)
        content = request.form.get("content", "").strip()
        if not content:
            flash("维护内容不能为空", "danger")
        else:
            try:
                cost = float(request.form.get("cost") or 0)
            except ValueError:
                cost = 0
            db = get_db()
            db.execute("INSERT INTO maintenance (equipment_id, date, content, operator, cost) "
                       "VALUES (?,?,?,?,?)",
                       (eid, request.form.get("date") or now_str()[:10], content,
                        request.form.get("operator", "").strip(), cost))
            db.commit()
            flash("维护记录已添加", "success")
        return redirect(url_for("equipment_detail", eid=eid))

    @app.route("/maintenance/<int:mid>/delete", methods=["POST"])
    def maintenance_delete(mid):
        db = get_db()
        row = db.execute("SELECT * FROM maintenance WHERE id=?", (mid,)).fetchone()
        if row is None:
            abort(404)
        db.execute("DELETE FROM maintenance WHERE id=?", (mid,))
        db.commit()
        flash("维护记录已删除", "warning")
        return redirect(url_for("equipment_detail", eid=row["equipment_id"]))

    # ------------------------------------------------------------------ 用户
    @app.route("/users", methods=["GET", "POST"])
    def users():
        db = get_db()
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            if not name:
                flash("姓名不能为空", "danger")
            else:
                role = request.form.get("role")
                db.execute("INSERT INTO users (name, department, phone, email, role, created_at) "
                           "VALUES (?,?,?,?,?,?)",
                           (name, request.form.get("department", "").strip(),
                            request.form.get("phone", "").strip(),
                            request.form.get("email", "").strip(),
                            role if role in USER_ROLES else USER_ROLES[0], now_str()))
                db.commit()
                flash(f"已添加使用人「{name}」", "success")
            return redirect(url_for("users"))
        rows = db.execute(
            "SELECT u.*, "
            "(SELECT COUNT(*) FROM reservations r WHERE r.user_id=u.id AND r.status!=?) AS res_count "
            "FROM users u ORDER BY u.id", (CANCELLED,)).fetchall()
        return render_template("users.html", rows=rows, roles=USER_ROLES)

    @app.route("/users/<int:uid>", methods=["GET", "POST"])
    def user_detail(uid):
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if user is None:
            abort(404)
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            if not name:
                flash("姓名不能为空", "danger")
            else:
                role = request.form.get("role")
                db.execute("UPDATE users SET name=?, department=?, phone=?, email=?, role=? "
                           "WHERE id=?",
                           (name, request.form.get("department", "").strip(),
                            request.form.get("phone", "").strip(),
                            request.form.get("email", "").strip(),
                            role if role in USER_ROLES else user["role"], uid))
                db.commit()
                flash("使用人信息已更新", "success")
            return redirect(url_for("user_detail", uid=uid))
        records = db.execute(
            "SELECT r.*, e.name AS equipment_name FROM reservations r "
            "JOIN equipment e ON e.id=r.equipment_id WHERE r.user_id=? "
            "ORDER BY r.start_time DESC", (uid,)).fetchall()
        return render_template("user_detail.html", user=user, records=records, roles=USER_ROLES)

    @app.route("/users/<int:uid>/delete", methods=["POST"])
    def user_delete(uid):
        db = get_db()
        db.execute("DELETE FROM users WHERE id=?", (uid,))
        db.commit()
        flash("使用人已删除", "warning")
        return redirect(url_for("users"))

    # ------------------------------------------------------------------ 预约
    def _reservation_query(where="", args=()):
        return get_db().execute(
            "SELECT r.*, e.name AS equipment_name, e.location, u.name AS user_name, "
            "u.department FROM reservations r "
            "JOIN equipment e ON e.id=r.equipment_id JOIN users u ON u.id=r.user_id "
            + where, args).fetchall()

    def _get_reservation(rid):
        rows = _reservation_query("WHERE r.id=?", (rid,))
        if not rows:
            abort(404)
        return rows[0]

    @app.route("/reservations")
    def reservations():
        f = request.args
        where, args = ["1=1"], []
        if f.get("equipment_id", type=int):
            where.append("r.equipment_id=?")
            args.append(f.get("equipment_id", type=int))
        if f.get("user_id", type=int):
            where.append("r.user_id=?")
            args.append(f.get("user_id", type=int))
        if f.get("status") in RESERVATION_STATUSES:
            where.append("r.status=?")
            args.append(f["status"])
        if f.get("date_from"):
            where.append("substr(r.start_time,1,10)>=?")
            args.append(f["date_from"])
        if f.get("date_to"):
            where.append("substr(r.start_time,1,10)<=?")
            args.append(f["date_to"])
        rows = _reservation_query("WHERE " + " AND ".join(where) +
                                  " ORDER BY r.start_time DESC", args)
        if f.get("export") == "csv":
            return _export_csv(rows)
        db = get_db()
        return render_template(
            "reservations.html", rows=rows, f=f, statuses=RESERVATION_STATUSES,
            equipment=db.execute("SELECT id, name FROM equipment ORDER BY name").fetchall(),
            users=db.execute("SELECT id, name FROM users ORDER BY name").fetchall(),
            now=now_str())

    def _export_csv(rows):
        buf = io.StringIO()
        buf.write("﻿")  # BOM, 方便 Excel 正确识别中文
        w = csv.writer(buf)
        w.writerow(["编号", "仪器", "位置", "使用人", "课题组", "预约开始", "预约结束",
                    "预约时长(h)", "实际开始", "实际结束", "实际时长(h)", "状态", "用途", "使用说明"])
        for r in rows:
            w.writerow([r["id"], r["equipment_name"], r["location"], r["user_name"],
                        r["department"], fmt_time(r["start_time"]), fmt_time(r["end_time"]),
                        fmt_hours(hours_between(r["start_time"], r["end_time"])),
                        fmt_time(r["actual_start"]), fmt_time(r["actual_end"]),
                        fmt_hours(hours_between(r["actual_start"], r["actual_end"])),
                        r["status"], r["purpose"], r["usage_note"]])
        name = f"usage_records_{datetime.now():%Y%m%d_%H%M}.csv"
        return Response(buf.getvalue(), mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename={name}"})

    def _form_context():
        db = get_db()
        return {
            "equipment": db.execute(
                "SELECT * FROM equipment WHERE status='可用' ORDER BY name").fetchall(),
            "users": db.execute("SELECT * FROM users ORDER BY name").fetchall(),
        }

    @app.route("/reservations/new", methods=["GET", "POST"])
    def reservation_new():
        db = get_db()
        if request.method == "POST":
            errors, data = validate_reservation(db, request.form)
            if errors:
                for e in errors:
                    flash(e, "danger")
                return render_template("reservation_form.html", r=data, **_form_context())
            cur = db.execute(
                "INSERT INTO reservations (equipment_id, user_id, start_time, end_time, purpose, "
                "status, created_at) VALUES (?,?,?,?,?,?,?)",
                (data["equipment_id"], data["user_id"], data["start_time"], data["end_time"],
                 data["purpose"], BOOKED, now_str()))
            db.commit()
            flash("预约成功", "success")
            return redirect(url_for("reservation_detail", rid=cur.lastrowid))
        # 默认: 下一个整点开始 (或日程页点击的时间), 时长 1 小时
        start = parse_time(request.args.get("start")) or (
            datetime.now() + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        r = {
            "equipment_id": request.args.get("equipment_id", type=int),
            "user_id": request.args.get("user_id", type=int),
            "start_time": start.strftime(TIME_FMT),
            "end_time": (start + timedelta(hours=1)).strftime(TIME_FMT),
        }
        return render_template("reservation_form.html", r=r, **_form_context())

    @app.route("/reservations/<int:rid>")
    def reservation_detail(rid):
        return render_template("reservation_detail.html", r=_get_reservation(rid), now=now_str())

    @app.route("/reservations/<int:rid>/edit", methods=["GET", "POST"])
    def reservation_edit(rid):
        db = get_db()
        r = _get_reservation(rid)
        if r["status"] != BOOKED:
            flash(f"状态为「{r['status']}」的预约不能修改", "warning")
            return redirect(url_for("reservation_detail", rid=rid))
        if request.method == "POST":
            errors, data = validate_reservation(db, request.form, exclude_id=rid)
            if errors:
                for e in errors:
                    flash(e, "danger")
                return render_template("reservation_form.html", r={**data, "id": rid},
                                       **_form_context())
            db.execute("UPDATE reservations SET equipment_id=?, user_id=?, start_time=?, "
                       "end_time=?, purpose=? WHERE id=?",
                       (data["equipment_id"], data["user_id"], data["start_time"],
                        data["end_time"], data["purpose"], rid))
            db.commit()
            flash("预约已修改", "success")
            return redirect(url_for("reservation_detail", rid=rid))
        return render_template("reservation_form.html", r=r, **_form_context())

    def _set_status(rid, allowed_from, **fields):
        r = _get_reservation(rid)
        if r["status"] not in allowed_from:
            flash(f"当前状态「{r['status']}」不允许此操作", "warning")
            return None
        db = get_db()
        cols = ", ".join(f"{k}=?" for k in fields)
        db.execute(f"UPDATE reservations SET {cols} WHERE id=?", (*fields.values(), rid))
        db.commit()
        return r

    @app.route("/reservations/<int:rid>/checkin", methods=["POST"])
    def reservation_checkin(rid):
        r = _get_reservation(rid)
        busy = get_db().execute(
            "SELECT COUNT(*) FROM reservations WHERE equipment_id=? AND status=? AND id!=?",
            (r["equipment_id"], IN_USE, rid)).fetchone()[0]
        if busy:
            flash("该仪器正在被他人使用, 请等待其签退后再上机", "danger")
        elif _set_status(rid, (BOOKED,), status=IN_USE, actual_start=now_str()):
            flash(f"{r['user_name']} 已上机, 开始计时", "success")
        return redirect(request.referrer or url_for("reservation_detail", rid=rid))

    @app.route("/reservations/<int:rid>/checkout", methods=["POST"])
    def reservation_checkout(rid):
        r = _get_reservation(rid)
        end = now_str()
        if r["actual_start"] and end <= r["actual_start"]:
            # 同一分钟内签到又签退, 至少记 1 分钟
            end = (parse_time(r["actual_start"]) + timedelta(minutes=1)).strftime(TIME_FMT)
        note = request.form.get("usage_note", "").strip()
        if _set_status(rid, (IN_USE,), status=DONE, actual_end=end,
                       usage_note=note or r["usage_note"]):
            flash(f"已下机, 本次实际使用 "
                  f"{fmt_hours(hours_between(r['actual_start'], end))} 小时", "success")
        return redirect(request.referrer or url_for("reservation_detail", rid=rid))

    @app.route("/reservations/<int:rid>/cancel", methods=["POST"])
    def reservation_cancel(rid):
        if _set_status(rid, (BOOKED,), status=CANCELLED):
            flash("预约已取消", "warning")
        return redirect(request.referrer or url_for("reservation_detail", rid=rid))

    @app.route("/reservations/<int:rid>/noshow", methods=["POST"])
    def reservation_noshow(rid):
        if _set_status(rid, (BOOKED,), status=NO_SHOW):
            flash("已标记为爽约", "warning")
        return redirect(request.referrer or url_for("reservation_detail", rid=rid))

    @app.route("/reservations/<int:rid>/record", methods=["POST"])
    def reservation_record(rid):
        """手动登记 / 补录实际使用时间。"""
        r = _get_reservation(rid)
        if r["status"] == CANCELLED:
            flash("已取消的预约不能登记使用时间", "warning")
            return redirect(url_for("reservation_detail", rid=rid))
        start = parse_time(request.form.get("actual_start"))
        end = parse_time(request.form.get("actual_end"))
        if not start or not end:
            flash("请填写完整的实际开始和结束时间", "danger")
        elif end <= start:
            flash("实际结束时间必须晚于实际开始时间", "danger")
        elif end > datetime.now() + timedelta(minutes=1):
            flash("实际结束时间不能晚于当前时间", "danger")
        else:
            db = get_db()
            db.execute("UPDATE reservations SET actual_start=?, actual_end=?, status=?, "
                       "usage_note=? WHERE id=?",
                       (start.strftime(TIME_FMT), end.strftime(TIME_FMT), DONE,
                        request.form.get("usage_note", "").strip(), rid))
            db.commit()
            flash("实际使用时间已登记", "success")
        return redirect(url_for("reservation_detail", rid=rid))

    @app.route("/reservations/<int:rid>/delete", methods=["POST"])
    def reservation_delete(rid):
        _get_reservation(rid)
        db = get_db()
        db.execute("DELETE FROM reservations WHERE id=?", (rid,))
        db.commit()
        flash("记录已删除", "warning")
        return redirect(url_for("reservations"))

    # ------------------------------------------------------------------ 日程
    @app.route("/schedule")
    def schedule():
        db = get_db()
        day = request.args.get("date") or now_str()[:10]
        try:
            day_dt = datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            day_dt = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        day = day_dt.strftime("%Y-%m-%d")
        day_start, day_end = day + "T00:00", (day_dt + timedelta(days=1)).strftime(TIME_FMT)

        equipment = db.execute("SELECT * FROM equipment WHERE status!='停用' "
                               "ORDER BY name").fetchall()
        rows = _reservation_query(
            "WHERE r.status!=? AND r.start_time<? AND r.end_time>? ORDER BY r.start_time",
            (CANCELLED, day_end, day_start))

        def pct(t):
            t = max(min(t, day_end), day_start)
            minutes = (parse_time(t) - day_dt).total_seconds() / 60
            return round(minutes / (24 * 60) * 100, 3)

        lanes = {e["id"]: [] for e in equipment}
        for r in rows:
            if r["equipment_id"] in lanes:
                left = pct(r["start_time"])
                lanes[r["equipment_id"]].append({
                    "r": r, "left": left, "width": max(pct(r["end_time"]) - left, 0.5)})
        return render_template(
            "schedule.html", day=day, equipment=equipment, lanes=lanes,
            prev_day=(day_dt - timedelta(days=1)).strftime("%Y-%m-%d"),
            next_day=(day_dt + timedelta(days=1)).strftime("%Y-%m-%d"),
            now_pct=pct(now_str()) if now_str()[:10] == day else None)

    # ------------------------------------------------------------------ 统计
    @app.route("/stats")
    def stats():
        db = get_db()
        today = datetime.now()
        date_from = request.args.get("date_from") or (today - timedelta(days=30)).strftime("%Y-%m-%d")
        date_to = request.args.get("date_to") or today.strftime("%Y-%m-%d")
        rows = _reservation_query(
            "WHERE substr(r.start_time,1,10) BETWEEN ? AND ? AND r.status!=?",
            (date_from, date_to, CANCELLED))

        def bucket():
            return {"count": 0, "reserved": 0.0, "actual": 0.0, "done": 0,
                    "no_show": 0, "overtime": 0}

        by_eq, by_user = {}, {}
        for r in rows:
            for key, table, label in ((r["equipment_id"], by_eq, r["equipment_name"]),
                                      (r["user_id"], by_user, r["user_name"])):
                b = table.setdefault(key, {**bucket(), "name": label, "id": key})
                b["count"] += 1
                b["reserved"] += hours_between(r["start_time"], r["end_time"])
                if r["status"] == DONE:
                    b["done"] += 1
                    b["actual"] += hours_between(r["actual_start"], r["actual_end"])
                    if r["actual_end"] and r["actual_end"] > r["end_time"]:
                        b["overtime"] += 1
                elif r["status"] == NO_SHOW:
                    b["no_show"] += 1

        try:
            span_days = (datetime.strptime(date_to, "%Y-%m-%d") -
                         datetime.strptime(date_from, "%Y-%m-%d")).days + 1
        except ValueError:
            span_days = 0
        for b in by_eq.values():
            # 利用率 = 实际使用时长 / 统计区间总时长 (按每天 8 小时工作时间计)
            b["utilization"] = b["actual"] / (span_days * 8) * 100 if span_days > 0 else 0

        totals = {k: sum(b[k] for b in by_eq.values())
                  for k in ("count", "reserved", "actual", "done", "no_show", "overtime")}
        return render_template(
            "stats.html", date_from=date_from, date_to=date_to,
            by_eq=sorted(by_eq.values(), key=lambda b: -b["actual"]),
            by_user=sorted(by_user.values(), key=lambda b: -b["actual"]),
            totals=totals)

    @app.errorhandler(404)
    def not_found(_e):
        return render_template("404.html"), 404

    return app


# --------------------------------------------------------------------------
# 示例数据
# --------------------------------------------------------------------------

def seed_demo_data(app):
    """填充演示数据 (仅当数据库为空时)。"""
    with app.app_context():
        db = app.get_db()
        if db.execute("SELECT COUNT(*) FROM equipment").fetchone()[0]:
            return
        now = datetime.now()
        ts = now_str()
        equipment = [
            ("高效液相色谱仪", "Agilent 1260", "HPLC-001", "A楼 301", "王老师", "可用", 4, "配 DAD 检测器"),
            ("PCR 仪", "Bio-Rad T100", "PCR-002", "A楼 305", "李老师", "可用", 6, ""),
            ("激光共聚焦显微镜", "Zeiss LSM 900", "CLSM-003", "B楼 102", "张工", "可用", 3, "需培训后使用"),
            ("超速离心机", "Beckman Optima XPN", "UC-004", "A楼 310", "王老师", "维护中", 0, "转子年检中"),
            ("流式细胞仪", "BD FACSCanto II", "FCM-005", "B楼 106", "张工", "可用", 2, ""),
        ]
        for e in equipment:
            db.execute("INSERT INTO equipment (name, model, serial_no, location, manager, status, "
                       "max_hours, description, created_at) VALUES (?,?,?,?,?,?,?,?,?)", (*e, ts))
        users = [
            ("张三", "分子生物学课题组", "13800000001", "zhangsan@example.com", "学生"),
            ("李四", "细胞生物学课题组", "13800000002", "lisi@example.com", "学生"),
            ("王五", "分析化学课题组", "13800000003", "wangwu@example.com", "教师"),
            ("赵六", "分子生物学课题组", "13800000004", "zhaoliu@example.com", "工程师"),
        ]
        for u in users:
            db.execute("INSERT INTO users (name, department, phone, email, role, created_at) "
                       "VALUES (?,?,?,?,?,?)", (*u, ts))

        def t(days, hour, minute=0):
            d = (now + timedelta(days=days)).replace(hour=hour, minute=minute, second=0,
                                                    microsecond=0)
            return d.strftime(TIME_FMT)

        # (仪器, 用户, 预约开始, 预约结束, 用途, 状态, 实际开始, 实际结束, 备注)
        records = [
            (1, 3, t(-3, 9), t(-3, 12), "样品纯度检测", DONE, t(-3, 9, 10), t(-3, 11, 40), "运行正常"),
            (2, 1, t(-2, 14), t(-2, 17), "基因扩增", DONE, t(-2, 14), t(-2, 17, 20), "超时 20 分钟"),
            (3, 2, t(-2, 9), t(-2, 11), "细胞成像", NO_SHOW, None, None, ""),
            (5, 2, t(-1, 10), t(-1, 12), "细胞周期分析", DONE, t(-1, 10, 5), t(-1, 11, 30), ""),
            (1, 4, t(-1, 13), t(-1, 16), "方法开发", DONE, t(-1, 13), t(-1, 15, 45), "更换色谱柱"),
            (2, 4, t(1, 9), t(1, 12), "qPCR 预实验", BOOKED, None, None, ""),
            (3, 1, t(1, 14), t(1, 16), "荧光染色观察", BOOKED, None, None, ""),
            (1, 3, t(2, 9), t(2, 12), "样品检测", BOOKED, None, None, ""),
        ]
        for rec in records:
            db.execute("INSERT INTO reservations (equipment_id, user_id, start_time, end_time, "
                       "purpose, status, actual_start, actual_end, usage_note, created_at) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?)", (*rec, ts))
        db.execute("INSERT INTO maintenance (equipment_id, date, content, operator, cost) "
                   "VALUES (4, ?, '转子年度安全检测', '厂家工程师', 1500)", (ts[:10],))
        db.commit()


if __name__ == "__main__":
    application = create_app()
    if os.environ.get("LAB_NO_DEMO") != "1":
        seed_demo_data(application)
    application.run(host=os.environ.get("HOST", "127.0.0.1"),
                    port=int(os.environ.get("PORT", 5000)),
                    debug=os.environ.get("FLASK_DEBUG") == "1")
