# -*- coding: utf-8 -*-
"""
VK-бот «Склад пиломатериалов».
Роли:
  operator  — станочник (создаёт заявки)
  warehouse — кладовщик / водитель погрузчика
  admin     — администратор
"""

import sqlite3
import os
from datetime import datetime
from vk_api import VkApi
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType
from vk_api.keyboard import VkKeyboard, VkKeyboardColor
from vk_api.utils import get_random_id

# ======================= НАСТРОЙКИ =======================
GROUP_TOKEN = os.getenv("GROUP_TOKEN", "ВСТАВЬТЕ_СЮДА_ТОКЕН")
GROUP_ID = int(os.getenv("GROUP_ID", 0))
ADMIN_IDS = {358344629}  # замените на свой ID ВКонтакте
DB_PATH = "/app/data/warehouse.db"
# =========================================================

STATUS = {
    "new": "🆕 новая",
    "approved": "🔄 в работе",
    "issued": "✅ выдана",
    "rejected": "❌ отклонена",
}


# ======================= БАЗА ДАННЫХ =======================
def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id    INTEGER PRIMARY KEY,
                username   TEXT,
                full_name  TEXT,
                role       TEXT NOT NULL DEFAULT 'operator',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS materials (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                unit       TEXT NOT NULL DEFAULT 'шт',
                qty        REAL NOT NULL DEFAULT 0,
                min_qty    REAL NOT NULL DEFAULT 0,
                location   TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS requests (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                material_id INTEGER NOT NULL,
                qty         REAL NOT NULL,
                comment     TEXT,
                status      TEXT NOT NULL DEFAULT 'new',
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );
        """)

        # --- миграции (безопасно для существующей базы) ---
        cols = [r["name"] for r in con.execute("PRAGMA table_info(materials)").fetchall()]
        if "thickness" not in cols:
            con.execute("ALTER TABLE materials ADD COLUMN thickness TEXT DEFAULT ''")
        if "decor" not in cols:
            con.execute("ALTER TABLE materials ADD COLUMN decor TEXT DEFAULT ''")

        cols = [r["name"] for r in con.execute("PRAGMA table_info(requests)").fetchall()]
        if "plan" not in cols:
            con.execute("ALTER TABLE requests ADD COLUMN plan TEXT DEFAULT ''")

        # --- стартовые данные ---
        if con.execute("SELECT COUNT(*) c FROM materials").fetchone()["c"] == 0:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            seed = [
                ("Доска 10мм Дуб",   "шт", 120, 50, "Стеллаж A1", "10мм", "Дуб"),
                ("Доска 10мм Орех",  "шт",  80, 30, "Стеллаж A2", "10мм", "Орех"),
                ("Доска 10мм Венге", "шт",  60, 20, "Стеллаж A3", "10мм", "Венге"),
                ("Доска 16мм Дуб",   "шт", 150, 60, "Стеллаж B1", "16мм", "Дуб"),
                ("Доска 16мм Орех",  "шт", 100, 40, "Стеллаж B2", "16мм", "Орех"),
                ("Доска 16мм Ясень", "шт",  70, 30, "Стеллаж B3", "16мм", "Ясень"),
                ("Доска 22мм Дуб",   "шт",  90, 40, "Стеллаж C1", "22мм", "Дуб"),
                ("Доска 22мм Венге", "шт",  50, 20, "Стеллаж C2", "22мм", "Венге"),
                ("Доска 22мм Клён",  "шт",  40, 15, "Стеллаж C3", "22мм", "Клён"),
            ]
            con.executemany(
                "INSERT INTO materials (name, unit, qty, min_qty, location, "
                "thickness, decor, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                [(*s, now) for s in seed],
            )
        con.commit()


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_user(vk, user_id):
    with db() as con:
        row = con.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone()
        if row is not None:
            return
    try:
        info = vk.users.get(user_ids=user_id)[0]
        full_name = f"{info['first_name']} {info['last_name']}"
        username = info.get("screen_name", "")
    except Exception:
        full_name = str(user_id)
        username = ""
    role = "admin" if user_id in ADMIN_IDS else "operator"
    with db() as con:
        con.execute(
            "INSERT OR IGNORE INTO users (user_id, username, full_name, role, created_at) "
            "VALUES (?,?,?,?,?)",
            (user_id, username, full_name, role, now_str()),
        )
        con.commit()


def get_role(user_id) -> str:
    with db() as con:
        row = con.execute("SELECT role FROM users WHERE user_id=?", (user_id,)).fetchone()
    return row["role"] if row else "operator"


def is_warehouse(user_id) -> bool:
    return get_role(user_id) in ("warehouse", "admin")


def get_material(mid):
    with db() as con:
        return con.execute("SELECT * FROM materials WHERE id=?", (mid,)).fetchone()


def all_materials():
    with db() as con:
        return con.execute(
            "SELECT * FROM materials ORDER BY thickness, decor, name"
        ).fetchall()


def distinct_thicknesses():
    with db() as con:
        rows = con.execute(
            "SELECT DISTINCT thickness FROM materials ORDER BY thickness"
        ).fetchall()
    return [r["thickness"] or "—" for r in rows]


def decors_by_thickness(th):
    th_val = "" if th == "—" else th
    with db() as con:
        return con.execute(
            "SELECT * FROM materials WHERE COALESCE(thickness,'') = ? ORDER BY decor",
            (th_val,),
        ).fetchall()


def material_label(m):
    parts = [p for p in [m["thickness"], m["decor"]] if p]
    return " ".join(parts) if parts else m["name"]


# ======================= ТАБЛИЦА ОСТАТКОВ =======================
def render_stock() -> str:
    rows = all_materials()
    if not rows:
        return "📋 Склад пуст"

    out = [
        "📋 ОСТАТКИ ПИЛОМАТЕРИАЛОВ",
        f"обновлено: {datetime.now():%d.%m.%Y %H:%M:%S}",
        "",
    ]
    groups = {}
    for r in rows:
        th = r["thickness"] or "—"
        groups.setdefault(th, []).append(r)

    for th in sorted(groups.keys()):
        out.append(f"▸ {th}")
        for r in groups[th]:
            name = r["decor"] or r["name"]
            flag = "!" if r["qty"] <= r["min_qty"] else ""
            out.append(f"   {name:<20} {r['qty']:>6g} {r['unit']:<4}{flag}")
        out.append("")

    low = [material_label(r) for r in rows if r["qty"] <= r["min_qty"]]
    if low:
        out.append("⚠️ Ниже минимума: " + ", ".join(low))
    return "\n".join(out)


# ======================= КЛАВИАТУРЫ =======================
def main_menu(user_id) -> str:
    role = get_role(user_id)
    kb = VkKeyboard(one_time=False)
    kb.add_button("📋 Остатки на складе", color=VkKeyboardColor.PRIMARY)

    if role in ("warehouse", "admin"):
        kb.add_line()
        kb.add_button("📥 Заявки станочников", color=VkKeyboardColor.POSITIVE)
        kb.add_line()
        kb.add_button("➕ Приход материала", color=VkKeyboardColor.PRIMARY)
        kb.add_line()
        kb.add_button("🆕 Новая номенклатура", color=VkKeyboardColor.PRIMARY)
        kb.add_line()
        kb.add_button("🗑 Удалить номенклатуру", color=VkKeyboardColor.NEGATIVE)
        kb.add_line()
        kb.add_button("🧹 Очистить остатки", color=VkKeyboardColor.NEGATIVE)
    else:
        kb.add_line()
        kb.add_button("📦 Запросить материал", color=VkKeyboardColor.POSITIVE)
        kb.add_line()
        kb.add_button("📄 Мои заявки", color=VkKeyboardColor.SECONDARY)

    return kb.get_keyboard()


def back_kb() -> str:
    kb = VkKeyboard(one_time=False)
    kb.add_button("⬅️ В меню", color=VkKeyboardColor.SECONDARY)
    return kb.get_keyboard()


def stock_kb() -> str:
    kb = VkKeyboard(one_time=False)
    kb.add_callback_button("🔄 Обновить", color=VkKeyboardColor.PRIMARY,
                           payload={"command": "stock_refresh"})
    kb.add_line()
    kb.add_button("⬅️ В меню", color=VkKeyboardColor.SECONDARY)
    return kb.get_keyboard()


# ======================= FSM =======================
fsm_storage = {}


def set_state(user_id, state, **data):
    fsm_storage[user_id] = {"state": state, "data": data}


def get_state(user_id):
    return fsm_storage.get(user_id, {"state": None, "data": {}})


def clear_state(user_id):
    fsm_storage.pop(user_id, None)


# ======================= ОТПРАВКА =======================
def send(vk, user_id, text, keyboard=None):
    vk.messages.send(
        user_id=user_id,
        message=text,
        keyboard=keyboard,
        random_id=get_random_id(),
    )


# ======================= ДЕЙСТВИЯ =======================
def show_thicknesses(vk, user_id):
    ths = distinct_thicknesses()
    if not ths:
        send(vk, user_id, "Склад пуст — нет ни одной позиции.", main_menu(user_id))
        return
    kb = VkKeyboard(one_time=False)
    for i, th in enumerate(ths):
        if i > 0:
            kb.add_line()
        kb.add_callback_button(th, color=VkKeyboardColor.PRIMARY,
                               payload={"command": f"req_thick:{th}"})
    kb.add_line()
    kb.add_button("⬅️ В меню", color=VkKeyboardColor.SECONDARY)
    send(vk, user_id, "📏 Шаг 1/4. Выберите толщину:", kb.get_keyboard())


def show_decors(vk, user_id, th):
    rows = decors_by_thickness(th)
    if not rows:
        send(vk, user_id, f"Для толщины {th} нет позиций.", main_menu(user_id))
        return
    kb = VkKeyboard(one_time=False)
    for i, m in enumerate(rows):
        if i > 0:
            kb.add_line()
        decor = m["decor"] or m["name"]
        kb.add_callback_button(
            f"{decor} — {m['qty']:g} {m['unit']}",
            color=VkKeyboardColor.PRIMARY,
            payload={"command": f"req_decor:{m['id']}"},
        )
    kb.add_line()
    kb.add_button("⬅️ В меню", color=VkKeyboardColor.SECONDARY)
    send(vk, user_id,
         f"🎨 Шаг 2/4. Толщина {th}. Выберите декор:",
         kb.get_keyboard())


def create_request(vk, user_id, mid, plan, qty, comment):
    m = get_material(mid)
    if not m:
        clear_state(user_id)
        send(vk, user_id, "Материал не найден.", main_menu(user_id))
        return
    with db() as con:
        cur = con.execute(
            "INSERT INTO requests (user_id, material_id, qty, plan, comment, "
            "status, created_at, updated_at) VALUES (?,?,?,?,?, 'new', ?, ?)",
            (user_id, mid, qty, plan, comment, now_str(), now_str()),
        )
        con.commit()
        rid = cur.lastrowid
        author = con.execute(
            "SELECT full_name FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        wh = con.execute(
            "SELECT user_id FROM users WHERE role IN ('warehouse','admin')"
        ).fetchall()

    clear_state(user_id)
    send(vk, user_id,
         f"✅ Заявка №{rid} отправлена\n\n"
         f"📦 {m['name']}\n"
         f"Толщина: {m['thickness'] or '—'}\n"
         f"Декор: {m['decor'] or '—'}\n"
         f"Количество: {qty:g} {m['unit']}\n"
         f"План: {plan}",
         main_menu(user_id))

    author_name = author["full_name"] if author else str(user_id)
    for r in wh:
        try:
            send(vk, r["user_id"],
                 f"🔔 Новая заявка №{rid}\n"
                 f"Материал: {m['name']}\n"
                 f"Толщина: {m['thickness'] or '—'}\n"
                 f"Декор: {m['decor'] or '—'}\n"
                 f"Кол-во: {qty:g} {m['unit']}\n"
                 f"План: {plan}\n"
                 f"От: {author_name}\n"
                 f"Комментарий: {comment or '—'}")
        except Exception:
            pass


def show_my_requests(vk, user_id):
    with db() as con:
        rows = con.execute(
            """SELECT r.*, m.name AS m_name, m.unit AS m_unit,
                      m.thickness, m.decor
               FROM requests r
               LEFT JOIN materials m ON m.id = r.material_id
               WHERE r.user_id = ? ORDER BY r.id DESC LIMIT 15""",
            (user_id,),
        ).fetchall()
    if not rows:
        send(vk, user_id, "У вас пока нет заявок.", back_kb())
        return
    lines = ["📄 Мои заявки", ""]
    for r in rows:
        lines.append(
            f"№{r['id']} • {r['m_name'] or '(удалён)'} — {r['qty']:g} {r['m_unit'] or ''}\n"
            f"   План: {r['plan'] or '—'}\n"
            f"   {STATUS.get(r['status'], r['status'])} • {r['created_at'][:16]}"
        )
    send(vk, user_id, "\n".join(lines), back_kb())


def show_active_requests(vk, user_id):
    with db() as con:
        rows = con.execute(
            """SELECT r.*, m.name AS m_name, m.unit AS m_unit,
                      m.thickness, m.decor, u.full_name
               FROM requests r
               LEFT JOIN materials m ON m.id = r.material_id
               LEFT JOIN users u ON u.user_id = r.user_id
               WHERE r.status IN ('new','approved')
               ORDER BY r.id"""
        ).fetchall()
    if not rows:
        send(vk, user_id, "📥 Активных заявок нет.", back_kb())
        return
    send(vk, user_id, f"📥 Активных заявок: {len(rows)}", back_kb())
    for r in rows:
        text_msg = (
            f"Заявка №{r['id']}\n"
            f"Материал: {r['m_name'] or '(удалён)'}\n"
            f"Толщина: {r['thickness'] or '—'}\n"
            f"Декор: {r['decor'] or '—'}\n"
            f"Кол-во: {r['qty']:g} {r['m_unit'] or ''}\n"
            f"План: {r['plan'] or '—'}\n"
            f"От: {r['full_name'] or r['user_id']}\n"
            f"Комментарий: {r['comment'] or '—'}\n"
            f"Статус: {STATUS.get(r['status'], r['status'])}"
        )
        kb = VkKeyboard(one_time=False)
        kb.add_callback_button("✅ Выдать", color=VkKeyboardColor.POSITIVE,
                               payload={"command": f"wh_issue:{r['id']}"})
        kb.add_callback_button("❌ Отклонить", color=VkKeyboardColor.NEGATIVE,
                               payload={"command": f"wh_reject:{r['id']}"})
        send(vk, user_id, text_msg, kb.get_keyboard())


def issue_request(vk, user_id, rid):
    with db() as con:
        r = con.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
        if not r or r["status"] not in ("new", "approved"):
            send(vk, user_id, "Заявка уже обработана.", back_kb())
            return
        m = con.execute("SELECT * FROM materials WHERE id=?",
                        (r["material_id"],)).fetchone()
        if not m:
            send(vk, user_id, "Материал удалён со склада.", back_kb())
            return
        if m["qty"] < r["qty"]:
            send(vk, user_id,
                 f"На складе только {m['qty']:g} {m['unit']}.", back_kb())
            return
        con.execute("UPDATE materials SET qty = qty - ?, updated_at = ? WHERE id = ?",
                    (r["qty"], now_str(), m["id"]))
        con.execute("UPDATE requests SET status='issued', updated_at=? WHERE id=?",
                    (now_str(), rid))
        con.commit()

    send(vk, user_id,
         f"✅ Заявка №{rid} выдана\n"
         f"{m['name']} — {r['qty']:g} {m['unit']}\n"
         f"Новый остаток: {m['qty'] - r['qty']:g} {m['unit']}",
         back_kb())
    try:
        send(vk, r["user_id"],
             f"✅ Заявка №{rid} выполнена\n"
             f"{m['name']} — {r['qty']:g} {m['unit']} можно забирать.\n"
             f"План: {r['plan'] or '—'}")
    except Exception:
        pass


def reject_request(vk, user_id, rid):
    with db() as con:
        r = con.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
        if not r or r["status"] not in ("new", "approved"):
            send(vk, user_id, "Заявка уже обработана.", back_kb())
            return
        con.execute("UPDATE requests SET status='rejected', updated_at=? WHERE id=?",
                    (now_str(), rid))
        con.commit()
    send(vk, user_id, f"❌ Заявка №{rid} отклонена.", back_kb())
    try:
        send(vk, r["user_id"], f"❌ Заявка №{rid} отклонена кладовщиком.")
    except Exception:
        pass


def show_inc_list(vk, user_id):
    rows = all_materials()
    if not rows:
        send(vk, user_id, "Склад пуст. Добавьте номенклатуру.", main_menu(user_id))
        return
    kb = VkKeyboard(one_time=False)
    for i, r in enumerate(rows):
        if i > 0:
            kb.add_line()
        kb.add_callback_button(
            f"➕ {material_label(r)} ({r['qty']:g} {r['unit']})",
            color=VkKeyboardColor.PRIMARY,
            payload={"command": f"inc_mat:{r['id']}"},
        )
    kb.add_line()
    kb.add_button("⬅️ В меню", color=VkKeyboardColor.SECONDARY)
    send(vk, user_id, "➕ Приход материала. Выберите позицию:", kb.get_keyboard())


def show_delete_list(vk, user_id):
    rows = all_materials()
    if not rows:
        send(vk, user_id, "Склад пуст.", back_kb())
        return
    kb = VkKeyboard(one_time=False)
    for i, r in enumerate(rows):
        if i > 0:
            kb.add_line()
        kb.add_callback_button(f"🗑 {material_label(r)}",
                               color=VkKeyboardColor.NEGATIVE,
                               payload={"command": f"delmat:{r['id']}"})
    kb.add_line()
    kb.add_button("⬅️ В меню", color=VkKeyboardColor.SECONDARY)
    send(vk, user_id, "🗑 Выберите материал для удаления:", kb.get_keyboard())


def show_delete_confirm(vk, user_id, mid):
    m = get_material(mid)
    if not m:
        send(vk, user_id, "Материал не найден.", back_kb())
        return
    kb = VkKeyboard(one_time=False)
    kb.add_callback_button("✅ Да, удалить", color=VkKeyboardColor.NEGATIVE,
                           payload={"command": f"delmat_ok:{mid}"})
    kb.add_callback_button("❌ Отмена", color=VkKeyboardColor.SECONDARY,
                           payload={"command": "del_list"})
    send(vk, user_id,
         f"🗑 Удалить материал?\n\n"
         f"📦 {m['name']}\n"
         f"Толщина: {m['thickness'] or '—'}\n"
         f"Декор: {m['decor'] or '—'}\n"
         f"Остаток: {m['qty']:g} {m['unit']}\n"
         f"Место: {m['location'] or '—'}\n\n"
         f"⚠️ Вместе с материалом удалятся все связанные заявки.",
         kb.get_keyboard())


def delete_material(vk, user_id, mid):
    m = get_material(mid)
    if not m:
        send(vk, user_id, "Уже удалён.", main_menu(user_id))
        return
    with db() as con:
        con.execute("DELETE FROM requests WHERE material_id = ?", (mid,))
        con.execute("DELETE FROM materials WHERE id = ?", (mid,))
        con.commit()
    send(vk, user_id, f"🗑 Удалено: {m['name']}", main_menu(user_id))


def show_clear_stock_confirm(vk, user_id):
    kb = VkKeyboard(one_time=False)
    kb.add_callback_button("✅ Да, обнулить", color=VkKeyboardColor.NEGATIVE,
                           payload={"command": "clear_stock_ok"})
    kb.add_callback_button("❌ Отмена", color=VkKeyboardColor.SECONDARY,
                           payload={"command": "menu"})
    send(vk, user_id,
         "🧹 Обнулить остатки ВСЕХ материалов?\n"
         "Сами позиции и заявки останутся.",
         kb.get_keyboard())


def clear_stock(vk, user_id):
    with db() as con:
        con.execute("UPDATE materials SET qty = 0, updated_at = ?", (now_str(),))
        con.commit()
    send(vk, user_id, "🧹 Все остатки обнулены.", main_menu(user_id))


# ======================= CALLBACK =======================
def handle_callback(vk, user_id, command):
    if not command:
        return

    if command == "menu":
        clear_state(user_id)
        send(vk, user_id, "Главное меню:", main_menu(user_id))
        return
    if command == "stock_refresh":
        send(vk, user_id, render_stock(), stock_kb())
        return

    # --- заявка (станочник) ---
    if command == "req_new":
        show_thicknesses(vk, user_id)
        return
    if command.startswith("req_thick:"):
        th = command.split(":", 1)[1]
        show_decors(vk, user_id, th)
        return
    if command.startswith("req_decor:"):
        mid = int(command.split(":", 1)[1])
        m = get_material(mid)
        if not m:
            send(vk, user_id, "Материал не найден.")
            return
        set_state(user_id, "req_plan", material_id=mid)
        send(vk, user_id,
             f"📦 {m['name']}\n"
             f"Остаток: {m['qty']:g} {m['unit']}\n\n"
             f"📋 Шаг 3/4. Введите номер плана производства:")
        return

    # --- кладовщик ---
    if not is_warehouse(user_id):
        send(vk, user_id, "Нет доступа.")
        return

    if command == "wh_requests":
        show_active_requests(vk, user_id)
        return
    if command.startswith("wh_issue:"):
        issue_request(vk, user_id, int(command.split(":", 1)[1]))
        return
    if command.startswith("wh_reject:"):
        reject_request(vk, user_id, int(command.split(":", 1)[1]))
        return

    if command == "inc_list":
        show_inc_list(vk, user_id)
        return
    if command.startswith("inc_mat:"):
        mid = int(command.split(":", 1)[1])
        m = get_material(mid)
        if not m:
            send(vk, user_id, "Материал не найден.")
            return
        set_state(user_id, "inc_qty", material_id=mid)
        send(vk, user_id,
             f"➕ {m['name']}\n"
             f"Текущий остаток: {m['qty']:g} {m['unit']}\n\n"
             f"Введите количество для ДОБАВЛЕНИЯ (число):")
        return

    if command == "inc_new":
        set_state(user_id, "new_thickness")
        send(vk, user_id,
             "🆕 Шаг 1/6. Введите толщину.\n\n"
             "Примеры: 10мм, 16мм, 22мм.\n"
             "Или «-», чтобы оставить пустым.")
        return

    if command == "del_list":
        show_delete_list(vk, user_id)
        return
    if command.startswith("delmat:"):
        show_delete_confirm(vk, user_id, int(command.split(":", 1)[1]))
        return
    if command.startswith("delmat_ok:"):
        delete_material(vk, user_id, int(command.split(":", 1)[1]))
        return

    if command == "clear_stock_ask":
        show_clear_stock_confirm(vk, user_id)
        return
    if command == "clear_stock_ok":
        clear_stock(vk, user_id)
        return


# ======================= ТЕКСТ =======================
def handle_message(vk, user_id, text):
    # --- выходы ---
    if text in ("/start", "Начать", "⬅️ В меню"):
        clear_state(user_id)
        role = get_role(user_id)
        role_ru = {"operator": "станочник",
                   "warehouse": "кладовщик (погрузчик)",
                   "admin": "администратор"}.get(role, role)
        send(vk, user_id,
             f"👋 Складской бот «Пиломатериалы»\n\n"
             f"Вы вошли как: {role_ru}\n\n"
             f"Выберите действие:",
             main_menu(user_id))
        return

    # --- админ-команды ---
    if text.startswith("/setrole"):
        if user_id not in ADMIN_IDS:
            send(vk, user_id, "Нет доступа.")
            return
        parts = text.split()
        if len(parts) != 3 or parts[2] not in ("operator", "warehouse", "admin"):
            send(vk, user_id, "Использование: /setrole 123456789 warehouse")
            return
        uid, role = int(parts[1]), parts[2]
        with db() as con:
            con.execute("UPDATE users SET role=? WHERE user_id=?", (role, uid))
            con.commit()
        send(vk, user_id, f"✅ Пользователь {uid} → {role}")
        return

    if text == "/resetstock":
        if user_id not in ADMIN_IDS:
            send(vk, user_id, "Нет доступа.")
            return
        clear_stock(vk, user_id)
        return

    # --- FSM ---
    st = get_state(user_id)
    state = st["state"]
    data = st["data"]

    # --- сценарий заявки станочника ---
    if state == "req_plan":
        plan = text.strip()
        if not plan:
            send(vk, user_id, "❗ Введите номер плана производства.")
            return
        set_state(user_id, "req_qty",
                  material_id=data["material_id"], plan=plan)
        send(vk, user_id, "🔢 Шаг 4/4. Введите количество (число):")
        return

    if state == "req_qty":
        try:
            qty = float(text.replace(",", "."))
            if qty <= 0:
                raise ValueError
        except ValueError:
            send(vk, user_id, "❗ Введите положительное число, например: 25")
            return
        set_state(user_id, "req_comment",
                  material_id=data["material_id"], plan=data["plan"], qty=qty)
        send(vk, user_id, "💬 Комментарий (или «-», чтобы пропустить):")
        return

    if state == "req_comment":
        comment = "" if text == "-" else text
        create_request(vk, user_id, data["material_id"],
                       data["plan"], data["qty"], comment)
        return

    # --- приход на существующий ---
    if state == "inc_qty":
        try:
            qty = float(text.replace(",", "."))
            if qty <= 0:
                raise ValueError
        except ValueError:
            send(vk, user_id, "❗ Введите положительное число, например: 50")
            return
        mid = data["material_id"]
        m = get_material(mid)
        with db() as con:
            con.execute(
                "UPDATE materials SET qty = qty + ?, updated_at = ? WHERE id = ?",
                (qty, now_str(), mid),
            )
            con.commit()
        clear_state(user_id)
        send(vk, user_id,
             f"✅ Приход оформлен\n{m['name']}: +{qty:g} {m['unit']}\n"
             f"Новый остаток: {m['qty'] + qty:g} {m['unit']}",
             main_menu(user_id))
        return

    # --- создание номенклатуры ---
    if state == "new_thickness":
        th = "" if text.strip() == "-" else text.strip()
        set_state(user_id, "new_decor", thickness=th)
        send(vk, user_id,
             "🎨 Шаг 2/6. Введите название декора.\n"
             "Примеры: Дуб, Орех, Венге.\n"
             "Или «-», чтобы оставить пустым.")
        return

    if state == "new_decor":
        decor = "" if text.strip() == "-" else text.strip()
        th = data["thickness"]
        parts = [p for p in (th, decor) if p]
        auto_name = ("Доска " + " ".join(parts)).strip() if parts else ""
        set_state(user_id, "new_name",
                  thickness=th, decor=decor, auto_name=auto_name)
        if auto_name:
            send(vk, user_id,
                 f"📝 Шаг 3/6. Автоматическое имя: «{auto_name}»\n\n"
                 f"Введите своё название или «-», чтобы использовать авто-имя.")
        else:
            send(vk, user_id, "📝 Шаг 3/6. Введите название материала:")
        return

    if state == "new_name":
        raw = text.strip()
        name = data.get("auto_name") if raw == "-" else raw
        if not name:
            send(vk, user_id, "❗ Название не может быть пустым. Введите ещё раз:")
            return
        set_state(user_id, "new_qty",
                  thickness=data["thickness"], decor=data["decor"], name=name)
        send(vk, user_id, "🔢 Шаг 4/6. Введите начальный остаток (число):")
        return

    if state == "new_qty":
        try:
            qty = float(text.replace(",", "."))
            if qty < 0:
                raise ValueError
        except ValueError:
            send(vk, user_id, "❗ Введите неотрицательное число, например: 100")
            return
        set_state(user_id, "new_unit",
                  thickness=data["thickness"], decor=data["decor"],
                  name=data["name"], qty=qty)
        send(vk, user_id,
             "📏 Шаг 5/6. Единица измерения (шт, лист, м³, кг).\n"
             "Или «-» для «шт».")
        return

    if state == "new_unit":
        unit = "шт" if text.strip() == "-" else text.strip()
        set_state(user_id, "new_min",
                  thickness=data["thickness"], decor=data["decor"],
                  name=data["name"], qty=data["qty"], unit=unit)
        send(vk, user_id,
             "⚠️ Шаг 6/6. Минимальный запас (число).\n"
             "Или «-» для 0.")
        return

    if state == "new_min":
        try:
            min_qty = 0.0 if text.strip() == "-" else float(text.replace(",", "."))
            if min_qty < 0:
                raise ValueError
        except ValueError:
            send(vk, user_id, "❗ Введите неотрицательное число или «-».")
            return
        set_state(user_id, "new_location",
                  thickness=data["thickness"], decor=data["decor"],
                  name=data["name"], qty=data["qty"], unit=data["unit"],
                  min_qty=min_qty)
        send(vk, user_id,
             "📍 Дополнительно: место хранения (например, Стеллаж A3).\n"
             "Или «-», чтобы пропустить.")
        return

    if state == "new_location":
        location = "" if text.strip() == "-" else text.strip()
        with db() as con:
            con.execute(
                "INSERT INTO materials (name, unit, qty, min_qty, location, "
                "thickness, decor, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (data["name"], data["unit"], data["qty"], data["min_qty"],
                 location, data["thickness"], data["decor"], now_str()),
            )
            con.commit()
        clear_state(user_id)
        send(vk, user_id,
             f"✅ Номенклатура добавлена\n\n"
             f"📦 {data['name']}\n"
             f"Толщина: {data['thickness'] or '—'}\n"
             f"Декор: {data['decor'] or '—'}\n"
             f"Остаток: {data['qty']:g} {data['unit']}\n"
             f"Минимум: {data['min_qty']:g} {data['unit']}\n"
             f"Место: {location or '—'}",
             main_menu(user_id))
        return

    # --- пункты главного меню ---
    if text == "📋 Остатки на складе":
        send(vk, user_id, render_stock(), stock_kb())
        return
    if text == "📦 Запросить материал":
        show_thicknesses(vk, user_id)
        return
    if text == "📄 Мои заявки":
        show_my_requests(vk, user_id)
        return

    if text == "📥 Заявки станочников":
        if not is_warehouse(user_id):
            send(vk, user_id, "Нет доступа.")
            return
        show_active_requests(vk, user_id)
        return
    if text == "➕ Приход материала":
        if not is_warehouse(user_id):
            send(vk, user_id, "Нет доступа.")
            return
        show_inc_list(vk, user_id)
        return
    if text == "🆕 Новая номенклатура":
        if not is_warehouse(user_id):
            send(vk, user_id, "Нет доступа.")
            return
        set_state(user_id, "new_thickness")
        send(vk, user_id,
             "🆕 Шаг 1/6. Введите толщину.\n\n"
             "Примеры: 10мм, 16мм, 22мм.\n"
             "Или «-», чтобы оставить пустым.")
        return
    if text == "🗑 Удалить номенклатуру":
        if not is_warehouse(user_id):
            send(vk, user_id, "Нет доступа.")
            return
        show_delete_list(vk, user_id)
        return
    if text == "🧹 Очистить остатки":
        if not is_warehouse(user_id):
            send(vk, user_id, "Нет доступа.")
            return
        show_clear_stock_confirm(vk, user_id)
        return

    # --- если ничего не подошло ---
    send(vk, user_id, "Не понимаю команду. Воспользуйтесь кнопками меню.",
         main_menu(user_id))


# ======================= ОСНОВНОЙ ЦИКЛ =======================
def main():
    init_db()

    vk_session = VkApi(token=GROUP_TOKEN)
    vk = vk_session.get_api()
    longpoll = VkBotLongPoll(vk_session, GROUP_ID)

    print("Бот запущен. Ожидание сообщений...")

    for event in longpoll.listen():
        # Обработка нажатий на callback-кнопки
        if event.type == VkBotEventType.MESSAGE_EVENT:
            user_id = event.obj.user_id
            payload = event.obj.payload or {}
            command = payload.get("command")

            # Обязательный ответ, чтобы у пользователя не крутился индикатор
            try:
                vk.messages.sendMessageEventAnswer(
                    event_id=event.obj.event_id,
                    user_id=event.obj.user_id,
                    peer_id=event.obj.peer_id,
                )
            except Exception:
                pass

            ensure_user(vk, user_id)
            try:
                handle_callback(vk, user_id, command)
            except Exception as e:
                print(f"[ERROR callback] user={user_id} cmd={command}: {e}")
            continue

        # Обработка обычных сообщений
        if event.type != VkBotEventType.MESSAGE_NEW:
            continue

        user_id = event.obj.message["from_id"]
        text = (event.obj.message.get("text") or "").strip()

        ensure_user(vk, user_id)

        try:
            handle_message(vk, user_id, text)
        except Exception as e:
            print(f"[ERROR message] user={user_id} text={text!r}: {e}")
            try:
                send(vk, user_id,
                     "⚠️ Произошла ошибка. Попробуйте /start.",
                     main_menu(user_id))
            except Exception:
                pass


if __name__ == "__main__":
    main()
