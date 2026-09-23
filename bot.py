# -*- coding: utf-8 -*-
"""
VK-бот «Склад пиломатериалов».
"""

import sqlite3
import os
from datetime import datetime
from vk_api import VkApi
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType
from vk_api.keyboard import VkKeyboard, VkKeyboardColor
from vk_api.utils import get_random_id

GROUP_TOKEN = os.getenv("GROUP_TOKEN", "ВСТАВЬТЕ_СЮДА_ТОКЕН")
GROUP_ID = int(os.getenv("GROUP_ID", 0))
ADMIN_IDS = {123456789}
DB_PATH = "/app/data/warehouse.db"

STATUS = {
    "new": "🆕 новая",
    "approved": "🔄 в работе",
    "issued": "✅ выдана",
    "rejected": "❌ отклонена",
}
PLAN_MIN, PLAN_MAX, PLAN_STEP, PLAN_BUTTONS_PER_PAGE = 1, 2000, 100, 10


def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT,
                role TEXT NOT NULL DEFAULT 'operator',
                blocked INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS materials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL, unit TEXT NOT NULL DEFAULT 'шт',
                qty REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL, material_id INTEGER NOT NULL,
                qty REAL NOT NULL, comment TEXT, plan TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'new',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS request_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id INTEGER NOT NULL, material_id INTEGER NOT NULL,
                qty REAL NOT NULL, status TEXT NOT NULL DEFAULT 'new');
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER, action TEXT NOT NULL, details TEXT,
                created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS plans (
                number INTEGER PRIMARY KEY, used_at TEXT NOT NULL);
        """)
        def cols(t):
            return [r["name"] for r in con.execute(f"PRAGMA table_info({t})").fetchall()]
        if "thickness" not in cols("materials"):
            con.execute("ALTER TABLE materials ADD COLUMN thickness TEXT DEFAULT ''")
        if "decor" not in cols("materials"):
            con.execute("ALTER TABLE materials ADD COLUMN decor TEXT DEFAULT ''")
        if "plan" not in cols("requests"):
            con.execute("ALTER TABLE requests ADD COLUMN plan TEXT DEFAULT ''")
        if "blocked" not in cols("users"):
            con.execute("ALTER TABLE users ADD COLUMN blocked INTEGER NOT NULL DEFAULT 0")
        if con.execute("SELECT COUNT(*) c FROM materials").fetchone()["c"] == 0:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            seed = [
                ("Доска 10мм Дуб", "шт", 120, "10мм", "Дуб"),
                ("Доска 10мм Орех", "шт", 80, "10мм", "Орех"),
                ("Доска 10мм Венге", "шт", 60, "10мм", "Венге"),
                ("Доска 16мм Дуб", "шт", 150, "16мм", "Дуб"),
                ("Доска 16мм Орех", "шт", 100, "16мм", "Орех"),
                ("Доска 16мм Ясень", "шт", 70, "16мм", "Ясень"),
                ("Доска 22мм Дуб", "шт", 90, "22мм", "Дуб"),
                ("Доска 22мм Венге", "шт", 50, "22мм", "Венге"),
                ("Доска 22мм Клён", "шт", 40, "22мм", "Клён"),
            ]
            con.executemany(
                "INSERT INTO materials (name, unit, qty, thickness, decor, updated_at) "
                "VALUES (?,?,?,?,?,?)", [(*s, now) for s in seed])
        con.commit()


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log_action(user_id, action, details=""):
    with db() as con:
        con.execute("INSERT INTO logs (user_id, action, details, created_at) "
                    "VALUES (?,?,?,?)", (user_id, action, details, now_str()))
        con.commit()


def ensure_user(vk, user_id):
    with db() as con:
        row = con.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        if row: return row
    try:
        info = vk.users.get(user_ids=user_id)[0]
        full_name = f"{info['first_name']} {info['last_name']}"
        username = info.get("screen_name", "")
    except Exception:
        full_name, username = str(user_id), ""
    role = "admin" if user_id in ADMIN_IDS else "operator"
    with db() as con:
        con.execute("INSERT OR IGNORE INTO users "
                    "(user_id, username, full_name, role, blocked, created_at) "
                    "VALUES (?,?,?,?,0,?)",
                    (user_id, username, full_name, role, now_str()))
        con.commit()
        return con.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()


def get_user(user_id):
    with db() as con:
        return con.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()


def get_role(user_id):
    u = get_user(user_id)
    return u["role"] if u else "operator"


def is_warehouse(user_id):
    return get_role(user_id) in ("warehouse", "admin")


def is_admin(user_id):
    return get_role(user_id) == "admin" or user_id in ADMIN_IDS


def get_material(mid):
    with db() as con:
        return con.execute("SELECT * FROM materials WHERE id=?", (mid,)).fetchone()


def all_materials():
    with db() as con:
        return con.execute("SELECT * FROM materials ORDER BY thickness, decor, name").fetchall()


def distinct_thicknesses():
    with db() as con:
        rows = con.execute("SELECT DISTINCT thickness FROM materials "
                           "WHERE COALESCE(thickness,'') != '' ORDER BY thickness").fetchall()
    return [r["thickness"] for r in rows]


def decors_by_thickness(th):
    with db() as con:
        return con.execute("SELECT * FROM materials WHERE COALESCE(thickness,'') = ? "
                           "ORDER BY decor", (th,)).fetchall()


def material_label(m):
    parts = [p for p in [m["thickness"], m["decor"]] if p]
    return " ".join(parts) if parts else m["name"]


def register_plan(n):
    with db() as con:
        con.execute("INSERT OR REPLACE INTO plans (number, used_at) VALUES (?,?)",
                    (n, now_str()))
        con.commit()


def last_plan_number():
    with db() as con:
        row = con.execute("SELECT number FROM plans ORDER BY used_at DESC LIMIT 1").fetchone()
    return row["number"] if row else None


def suggested_center():
    last = last_plan_number()
    return max(PLAN_MIN, min(PLAN_MAX, last)) if last else 1


def plan_page_items(page):
    center = suggested_center()
    base = max(PLAN_MIN, center - PLAN_STEP // 2 + (page - 1) * PLAN_STEP)
    items = [base + i for i in range(PLAN_BUTTONS_PER_PAGE)
             if PLAN_MIN <= base + i <= PLAN_MAX]
    return items, base


def render_stock():
    rows = all_materials()
    if not rows: return "📋 Склад пуст"
    out = ["📋 ОСТАТКИ ПИЛОМАТЕРИАЛОВ",
           f"обновлено: {datetime.now():%d.%m.%Y %H:%M:%S}", ""]
    groups = {}
    for r in rows:
        groups.setdefault(r["thickness"] or "—", []).append(r)
    for th in sorted(groups.keys()):
        out.append(f"▸ {th}")
        for r in groups[th]:
            name = r["decor"] or r["name"]
            out.append(f"   {name:<20} {r['qty']:>6g} {r['unit']}")
        out.append("")
    return "\n".join(out)


def main_menu(user_id):
    role = get_role(user_id)
    kb = VkKeyboard(one_time=False)
    kb.add_button("📋 Остатки на складе", color=VkKeyboardColor.PRIMARY)
    kb.add_line()
    kb.add_button("📦 Новая заявка", color=VkKeyboardColor.POSITIVE)
    kb.add_line()
    kb.add_button("📄 Мои заявки", color=VkKeyboardColor.SECONDARY)
    if role in ("warehouse", "admin"):
        kb.add_line()
        kb.add_button("📥 Заявки станочников", color=VkKeyboardColor.POSITIVE)
        kb.add_line()
        kb.add_button("➕ Приход материала", color=VkKeyboardColor.PRIMARY)
        kb.add_line()
        kb.add_button("📦 Массовый приход", color=VkKeyboardColor.PRIMARY)
        kb.add_line()
        kb.add_button("🆕 Новая номенклатура", color=VkKeyboardColor.PRIMARY)
        kb.add_line()
        kb.add_button("✏️ Редактировать", color=VkKeyboardColor.PRIMARY)
        kb.add_line()
        kb.add_button("🗑 Удалить номенклатуру", color=VkKeyboardColor.NEGATIVE)
        kb.add_line()
        kb.add_button("🧹 Очистить остатки", color=VkKeyboardColor.NEGATIVE)
        kb.add_line()
        kb.add_button("📊 Сводка", color=VkKeyboardColor.SECONDARY)
        kb.add_button("📋 Журнал", color=VkKeyboardColor.SECONDARY)
        if role == "admin":
            kb.add_line()
            kb.add_button("👥 Пользователи", color=VkKeyboardColor.SECONDARY)
    return kb.get_keyboard()


def back_kb():
    kb = VkKeyboard(one_time=False)
    kb.add_button("⬅️ В меню", color=VkKeyboardColor.SECONDARY)
    return kb.get_keyboard()


def stock_kb():
    kb = VkKeyboard(one_time=False)
    kb.add_callback_button("🔄 Обновить", color=VkKeyboardColor.PRIMARY,
                           payload={"command": "stock_refresh"})
    kb.add_line()
    kb.add_button("⬅️ В меню", color=VkKeyboardColor.SECONDARY)
    return kb.get_keyboard()


fsm_storage = {}


def set_state(user_id, state, **data):
    fsm_storage[user_id] = {"state": state, "data": data}


def get_state(user_id):
    return fsm_storage.get(user_id, {"state": None, "data": {}})


def clear_state(user_id):
    fsm_storage.pop(user_id, None)


def send(vk, user_id, text, keyboard=None):
    vk.messages.send(user_id=user_id, message=text, keyboard=keyboard,
                     random_id=get_random_id())


def notify_warehouse(vk, text):
    with db() as con:
        rows = con.execute("SELECT user_id FROM users "
                           "WHERE role IN ('warehouse','admin') AND blocked=0").fetchall()
    for r in rows:
        try: send(vk, r["user_id"], text)
        except Exception: pass


HELP_TEXT = """📖 КОМАНДЫ БОТА

Общие:
/start — меню
/help — справка
/whoami — мой ID и роль

Для всех:
📦 Новая заявка
📄 Мои заявки

Кладовщик:
📥 Заявки станочников
➕ Приход материала
📦 Массовый приход
🆕 Новая номенклатура
✏️ Редактировать
🗑 Удалить номенклатуру
🧹 Очистить остатки
📊 Сводка
📋 Журнал

Админ:
/users — пользователи
/setrole <ID> <роль> — роль
/resetstock — обнулить остатки"""


def show_thicknesses(vk, user_id):
    ths = distinct_thicknesses()
    if not ths:
        send(vk, user_id, "Склад пуст.", main_menu(user_id))
        return
    st = get_state(user_id)
    cart = list(st["data"].get("cart", []))
    plan = st["data"].get("plan", "")
    kb = VkKeyboard(one_time=False)
    for i, th in enumerate(ths):
        if i > 0: kb.add_line()
        kb.add_callback_button(th, color=VkKeyboardColor.PRIMARY,
                               payload={"command": f"cart_thick:{th}"})
    kb.add_line()
    kb.add_button("⬅️ В меню", color=VkKeyboardColor.SECONDARY)
    set_state(user_id, "cart_choose", cart=cart, plan=plan)
    send(vk, user_id, "📏 Шаг 1. Выберите толщину:", kb.get_keyboard())


def show_decors_for_cart(vk, user_id, th):
    rows = decors_by_thickness(th)
    if not rows:
        send(vk, user_id, f"Для толщины {th} нет позиций.", main_menu(user_id))
        return
    st = get_state(user_id)
    cart = list(st["data"].get("cart", []))
    plan = st["data"].get("plan", "")
    kb = VkKeyboard(one_time=False)
    for i, m in enumerate(rows):
        if i > 0: kb.add_line()
        decor = m["decor"] or m["name"]
        kb.add_callback_button(f"{decor} — {m['qty']:g} {m['unit']}",
                               color=VkKeyboardColor.PRIMARY,
                               payload={"command": f"cart_add:{m['id']}"})
    kb.add_line()
    if cart:
        kb.add_callback_button("🛒 Показать корзину", color=VkKeyboardColor.POSITIVE,
                               payload={"command": "cart_show"})
        kb.add_line()
    kb.add_button("⬅️ В меню", color=VkKeyboardColor.SECONDARY)
    set_state(user_id, "cart_choose", cart=cart, plan=plan, thickness=th)
    send(vk, user_id, f"🎨 Шаг 2. Толщина {th}. Выберите декор:", kb.get_keyboard())


def add_to_cart(vk, user_id, mid):
    m = get_material(mid)
    if not m:
        send(vk, user_id, "Материал не найден.")
        return
    st = get_state(user_id)
    cart = list(st["data"].get("cart", []))
    plan = st["data"].get("plan", "")
    cart.append({"material_id": mid, "qty": None})
    set_state(user_id, "cart_qty", cart=cart, plan=plan, editing_index=len(cart) - 1)
    send(vk, user_id,
         f"🛒 Добавлено: {m['name']}\n"
         f"Остаток: {m['qty']:g} {m['unit']}\n\n"
         f"Введите количество (число):")


def show_cart(vk, user_id):
    st = get_state(user_id)
    cart = st["data"].get("cart", [])
    plan = st["data"].get("plan", "")
    if not cart:
        send(vk, user_id, "Корзина пуста.", main_menu(user_id))
        return
    lines = ["🛒 ВАША ЗАЯВКА", ""]
    for i, it in enumerate(cart, 1):
        m = get_material(it["material_id"])
        qty = it["qty"]
        lines.append(f"{i}. {m['name'] if m else '?'} — {qty:g}" if qty else
                     f"{i}. {m['name'] if m else '?'} — ?")
    if plan:
        lines.append(""); lines.append(f"📋 План: {plan}")
    kb = VkKeyboard(one_time=False)
    kb.add_callback_button("➕ Добавить ещё", color=VkKeyboardColor.PRIMARY,
                           payload={"command": "cart_more"})
    kb.add_callback_button("🗑 Очистить", color=VkKeyboardColor.NEGATIVE,
                           payload={"command": "cart_clear"})
    kb.add_line()
    kb.add_callback_button("✅ Отправить заявку", color=VkKeyboardColor.POSITIVE,
                           payload={"command": "cart_submit"})
    kb.add_line()
    kb.add_button("⬅️ В меню", color=VkKeyboardColor.SECONDARY)
    send(vk, user_id, "\n".join(lines), kb.get_keyboard())


def show_plan_page(vk, user_id, page=1):
    items, base = plan_page_items(page)
    if not items:
        send(vk, user_id, "Нет номеров в этом диапазоне.")
        return
    last = last_plan_number()
    hint = f"Последний план: {last}" if last else "Раньше планов не было"
    st = get_state(user_id)
    cart = list(st["data"].get("cart", []))
    plan = st["data"].get("plan", "")
    kb = VkKeyboard(one_time=False)
    for i, n in enumerate(items):
        if i % 2 == 0 and i > 0: kb.add_line()
        kb.add_callback_button(str(n), color=VkKeyboardColor.PRIMARY,
                               payload={"command": f"plan_pick:{n}"})
    kb.add_line()
    if page > 1:
        kb.add_callback_button("◀️ -100", color=VkKeyboardColor.SECONDARY,
                               payload={"command": f"plan_page:{page-1}"})
    if items[-1] < PLAN_MAX:
        kb.add_callback_button("+100 ▶️", color=VkKeyboardColor.SECONDARY,
                               payload={"command": f"plan_page:{page+1}"})
    kb.add_line()
    kb.add_callback_button("🔢 Ввести вручную", color=VkKeyboardColor.PRIMARY,
                           payload={"command": "plan_manual"})
    kb.add_line()
    kb.add_button("⬅️ В меню", color=VkKeyboardColor.SECONDARY)
    set_state(user_id, "choose_plan", cart=cart, plan=plan, page=page)
    send(vk, user_id,
         f"📋 Шаг 3. Выберите план.\n{hint}\nДиапазон {items[0]}–{items[-1]} "
         f"({page}-я страница).", kb.get_keyboard())


def submit_cart(vk, user_id):
    st = get_state(user_id)
    cart = st["data"].get("cart", [])
    plan = st["data"].get("plan", "")
    if not cart:
        send(vk, user_id, "Корзина пуста.", main_menu(user_id))
        return
    for it in cart:
        if it["qty"] is None or it["qty"] <= 0:
            send(vk, user_id, "❗ У некоторых позиций не указано количество.")
            return
    with db() as con:
        cur = con.execute(
            "INSERT INTO requests (user_id, material_id, qty, plan, comment, "
            "status, created_at, updated_at) VALUES (?,?,?,?,?,'new',?,?)",
            (user_id, cart[0]["material_id"], sum(i["qty"] for i in cart),
             plan, "", now_str(), now_str()))
        rid = cur.lastrowid
        for it in cart:
            con.execute("INSERT INTO request_items (request_id, material_id, qty, status) "
                        "VALUES (?,?,?,'new')", (rid, it["material_id"], it["qty"]))
        con.commit()
    clear_state(user_id)
    log_action(user_id, f"Создана заявка №{rid}", f"{len(cart)} поз., план={plan}")
    if plan:
        try: register_plan(int(plan))
        except: pass
    lines = [f"✅ Заявка №{rid} отправлена", ""]
    for i, it in enumerate(cart, 1):
        m = get_material(it["material_id"])
        lines.append(f"{i}. {m['name']} — {it['qty']:g} {m['unit']}")
    lines.append(""); lines.append(f"📋 План: {plan or '—'}")
    send(vk, user_id, "\n".join(lines), main_menu(user_id))
    user = get_user(user_id)
    author = user["full_name"] if user else str(user_id)
    notif = [f"🔔 Новая заявка №{rid}", ""]
    for i, it in enumerate(cart, 1):
        m = get_material(it["material_id"])
        notif.append(f"{i}. {m['name']} — {it['qty']:g} {m['unit']}")
    notif += ["", f"📋 План: {plan or '—'}", f"От: {author}"]
    notify_warehouse(vk, "\n".join(notif))


def show_my_requests(vk, user_id):
    with db() as con:
        rows = con.execute(
            "SELECT r.*, m.name AS m_name, m.unit AS m_unit FROM requests r "
            "LEFT JOIN materials m ON m.id=r.material_id "
            "WHERE r.user_id=? ORDER BY r.id DESC LIMIT 15", (user_id,)).fetchall()
    if not rows:
        send(vk, user_id, "У вас нет заявок.", back_kb())
        return
    lines = ["📄 Мои заявки", ""]
    for r in rows:
        with db() as con:
            cnt = con.execute("SELECT COUNT(*) c FROM request_items WHERE request_id=?",
                              (r["id"],)).fetchone()["c"]
        tag = f" ({cnt} поз.)" if cnt else ""
        lines.append(f"№{r['id']}{tag} • {r['m_name'] or '(удалён)'} — {r['qty']:g}\n"
                     f"   План: {r['plan'] or '—'}\n"
                     f"   {STATUS.get(r['status'], r['status'])} • {r['created_at'][:16]}")
    send(vk, user_id, "\n".join(lines), back_kb())


def show_active_requests(vk, user_id):
    with db() as con:
        rows = con.execute(
            "SELECT r.*, u.full_name FROM requests r LEFT JOIN users u ON u.user_id=r.user_id "
            "WHERE r.status IN ('new','approved') ORDER BY r.id").fetchall()
    if not rows:
        send(vk, user_id, "📥 Активных заявок нет.", back_kb())
        return
    send(vk, user_id, f"📥 Активных: {len(rows)}", back_kb())
    for r in rows:
        with db() as con:
            items = con.execute(
                "SELECT ri.*, m.name AS m_name, m.unit AS m_unit FROM request_items ri "
                "LEFT JOIN materials m ON m.id=ri.material_id WHERE ri.request_id=?",
                (r["id"],)).fetchall()
        if items:
            body = "\n".join(f"   • {it['m_name'] or '?'} — {it['qty']:g} {it['m_unit'] or ''}"
                             for it in items)
        else:
            m = get_material(r["material_id"])
            body = f"   • {m['name'] if m else '?'} — {r['qty']:g}"
        text_msg = (f"Заявка №{r['id']}\n{body}\n📋 План: {r['plan'] or '—'}\n"
                    f"От: {r['full_name'] or r['user_id']}\n"
                    f"Статус: {STATUS.get(r['status'], r['status'])}")
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
        items = con.execute("SELECT * FROM request_items WHERE request_id=?",
                            (rid,)).fetchall()
        if items:
            for it in items:
                m = con.execute("SELECT * FROM materials WHERE id=?",
                                (it["material_id"],)).fetchone()
                if not m or m["qty"] < it["qty"]:
                    send(vk, user_id, f"Не хватает материала (ID {it['material_id']}).",
                         back_kb())
                    return
            summary = []
            for it in items:
                m = con.execute("SELECT * FROM materials WHERE id=?",
                                (it["material_id"],)).fetchone()
                con.execute("UPDATE materials SET qty=qty-?, updated_at=? WHERE id=?",
                            (it["qty"], now_str(), m["id"]))
                summary.append(f"{m['name']} — {it['qty']:g} {m['unit']}")
        else:
            m = con.execute("SELECT * FROM materials WHERE id=?",
                            (r["material_id"],)).fetchone()
            if not m or m["qty"] < r["qty"]:
                send(vk, user_id, "Не хватает материала.", back_kb())
                return
            con.execute("UPDATE materials SET qty=qty-?, updated_at=? WHERE id=?",
                        (r["qty"], now_str(), m["id"]))
            summary = [f"{m['name']} — {r['qty']:g} {m['unit']}"]
        con.execute("UPDATE requests SET status='issued', updated_at=? WHERE id=?",
                    (now_str(), rid))
        con.commit()
    log_action(user_id, f"Выдана заявка №{rid}", "; ".join(summary))
    send(vk, user_id, f"✅ Заявка №{rid} выдана\n" + "\n".join(summary), back_kb())
    try: send(vk, r["user_id"], f"✅ Заявка №{rid} выполнена:\n" + "\n".join(summary))
    except: pass


def reject_request(vk, user_id, rid):
    with db() as con:
        r = con.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
        if not r or r["status"] not in ("new", "approved"):
            send(vk, user_id, "Заявка уже обработана.", back_kb())
            return
        con.execute("UPDATE requests SET status='rejected', updated_at=? WHERE id=?",
                    (now_str(), rid))
        con.commit()
    log_action(user_id, f"Отклонена заявка №{rid}")
    send(vk, user_id, f"❌ Заявка №{rid} отклонена.", back_kb())
    try: send(vk, r["user_id"], f"❌ Заявка №{rid} отклонена.")
    except: pass
        

# ======================= ПРИХОД =======================
def show_inc_list(vk, user_id, mass=False):
    rows = all_materials()
    if not rows:
        send(vk, user_id, "Склад пуст.", main_menu(user_id))
        return
    cmd = "minc_mat" if mass else "inc_mat"
    header = "📦 МАССОВЫЙ ПРИХОД" if mass else "➕ ПРИХОД МАТЕРИАЛА"
    kb = VkKeyboard(one_time=False)
    for i, r in enumerate(rows):
        if i > 0:
            kb.add_line()
        kb.add_callback_button(f"{material_label(r)} ({r['qty']:g})",
                               color=VkKeyboardColor.PRIMARY,
                               payload={"command": f"{cmd}:{r['id']}"})
    kb.add_line()
    if mass:
        kb.add_callback_button("✅ Завершить", color=VkKeyboardColor.POSITIVE,
                               payload={"command": "minc_done"})
    kb.add_line()
    kb.add_button("⬅️ В меню", color=VkKeyboardColor.SECONDARY)
    send(vk, user_id, f"{header}. Выберите позицию:", kb.get_keyboard())


def show_mass_inc_cart(vk, user_id):
    st = get_state(user_id)
    cart = st["data"].get("mass_cart", [])
    if not cart:
        send(vk, user_id, "Приход пуст.", main_menu(user_id))
        return
    lines = ["📦 ПРИХОД (текущий)", ""]
    for i, it in enumerate(cart, 1):
        m = get_material(it["material_id"])
        lines.append(f"{i}. {m['name'] if m else '?'} — +{it['qty']:g}")
    kb = VkKeyboard(one_time=False)
    kb.add_callback_button("➕ Ещё позиция", color=VkKeyboardColor.PRIMARY,
                           payload={"command": "minc_more"})
    kb.add_callback_button("✅ Завершить", color=VkKeyboardColor.POSITIVE,
                           payload={"command": "minc_done"})
    kb.add_line()
    kb.add_button("⬅️ В меню", color=VkKeyboardColor.SECONDARY)
    send(vk, user_id, "\n".join(lines), kb.get_keyboard())


def finish_mass_inc(vk, user_id):
    st = get_state(user_id)
    cart = st["data"].get("mass_cart", [])
    if not cart:
        send(vk, user_id, "Приход пуст.", main_menu(user_id))
        return
    with db() as con:
        for it in cart:
            con.execute("UPDATE materials SET qty=qty+?, updated_at=? WHERE id=?",
                        (it["qty"], now_str(), it["material_id"]))
        con.commit()
    lines = ["✅ Приход оформлен", ""]
    for it in cart:
        m = get_material(it["material_id"])
        lines.append(f"{m['name']}: +{it['qty']:g} {m['unit']}")
    clear_state(user_id)
    log_action(user_id, "Массовый приход", "; ".join(
        f"{get_material(i['material_id'])['name']} +{i['qty']:g}" for i in cart))
    send(vk, user_id, "\n".join(lines), main_menu(user_id))


# ======================= РЕДАКТИРОВАНИЕ =======================
EDIT_FIELDS = {
    "name": "📝 Название",
    "thickness": "📏 Толщина",
    "decor": "🎨 Декор",
    "unit": "📐 Единица",
}


def show_edit_list(vk, user_id):
    rows = all_materials()
    if not rows:
        send(vk, user_id, "Склад пуст.", back_kb())
        return
    kb = VkKeyboard(one_time=False)
    for i, r in enumerate(rows):
        if i > 0:
            kb.add_line()
        kb.add_callback_button(f"✏️ {material_label(r)}",
                               color=VkKeyboardColor.PRIMARY,
                               payload={"command": f"edit:{r['id']}"})
    kb.add_line()
    kb.add_button("⬅️ В меню", color=VkKeyboardColor.SECONDARY)
    send(vk, user_id, "✏️ Выберите материал:", kb.get_keyboard())


def show_edit_fields(vk, user_id, mid):
    m = get_material(mid)
    if not m:
        send(vk, user_id, "Материал не найден.", back_kb())
        return
    kb = VkKeyboard(one_time=False)
    first = True
    for key, label in EDIT_FIELDS.items():
        val = m[key] if key in m.keys() else ""
        if not first:
            kb.add_line()
        first = False
        kb.add_callback_button(f"{label}: {val if val not in (None, '') else '—'}",
                               color=VkKeyboardColor.PRIMARY,
                               payload={"command": f"editf:{mid}:{key}"})
    kb.add_line()
    kb.add_button("⬅️ В меню", color=VkKeyboardColor.SECONDARY)
    send(vk, user_id, f"✏️ {m['name']}\nВыберите поле:", kb.get_keyboard())


def do_edit(vk, user_id, mid, field, value):
    m = get_material(mid)
    if not m:
        send(vk, user_id, "Материал не найден.", main_menu(user_id))
        return
    with db() as con:
        con.execute(f"UPDATE materials SET {field}=?, updated_at=? WHERE id=?",
                    (value, now_str(), mid))
        con.commit()
    clear_state(user_id)
    log_action(user_id, f"Изменён материал ID {mid}", f"{field}={value}")
    send(vk, user_id,
         f"✅ Изменено\n{m['name']}\n{EDIT_FIELDS.get(field, field)}: {value or '—'}",
         main_menu(user_id))


# ======================= УДАЛЕНИЕ =======================
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
         f"🗑 Удалить материал?\n\n📦 {m['name']}\n"
         f"Остаток: {m['qty']:g} {m['unit']}\n\n"
         f"⚠️ Связанные заявки тоже удалятся.",
         kb.get_keyboard())


def delete_material(vk, user_id, mid):
    m = get_material(mid)
    if not m:
        send(vk, user_id, "Уже удалён.", main_menu(user_id))
        return
    with db() as con:
        con.execute("DELETE FROM requests WHERE material_id=?", (mid,))
        con.execute("DELETE FROM request_items WHERE material_id=?", (mid,))
        con.execute("DELETE FROM materials WHERE id=?", (mid,))
        con.commit()
    log_action(user_id, f"Удалён материал {m['name']}")
    send(vk, user_id, f"🗑 Удалено: {m['name']}", main_menu(user_id))


# ======================= ОЧИСТКА =======================
def show_clear_stock_confirm(vk, user_id):
    kb = VkKeyboard(one_time=False)
    kb.add_callback_button("✅ Да, обнулить", color=VkKeyboardColor.NEGATIVE,
                           payload={"command": "clear_stock_ok"})
    kb.add_callback_button("❌ Отмена", color=VkKeyboardColor.SECONDARY,
                           payload={"command": "menu"})
    send(vk, user_id,
         "🧹 Обнулить остатки ВСЕХ материалов?\nПозиции и заявки останутся.",
         kb.get_keyboard())


def clear_stock(vk, user_id):
    with db() as con:
        con.execute("UPDATE materials SET qty=0, updated_at=?", (now_str(),))
        con.commit()
    log_action(user_id, "Очищены все остатки")
    send(vk, user_id, "🧹 Все остатки обнулены.", main_menu(user_id))


# ======================= СВОДКА =======================
def show_stats(vk, user_id):
    with db() as con:
        total_m = con.execute("SELECT COUNT(*) c FROM materials").fetchone()["c"]
        total_qty = con.execute("SELECT COALESCE(SUM(qty),0) s FROM materials").fetchone()["s"]
        active = con.execute("SELECT COUNT(*) c FROM requests "
                             "WHERE status IN ('new','approved')").fetchone()["c"]
        issued = con.execute("SELECT COUNT(*) c FROM requests "
                             "WHERE status='issued'").fetchone()["c"]
        rejected = con.execute("SELECT COUNT(*) c FROM requests "
                               "WHERE status='rejected'").fetchone()["c"]
        users = con.execute("SELECT role, COUNT(*) c FROM users "
                            "WHERE blocked=0 GROUP BY role").fetchall()
    lines = ["📊 СВОДКА СКЛАДА", "",
             f"📦 Материалов: {total_m}",
             f"📊 Общий остаток: {total_qty:g}", "",
             f"📥 Активных: {active}",
             f"✅ Выдано: {issued}",
             f"❌ Отклонено: {rejected}", "", "👥 Пользователи:"]
    for u in users:
        role_ru = {"operator": "станочник", "warehouse": "кладовщик",
                   "admin": "админ"}.get(u["role"], u["role"])
        lines.append(f"   • {role_ru}: {u['c']}")
    send(vk, user_id, "\n".join(lines), back_kb())


# ======================= ЖУРНАЛ =======================
def show_log(vk, user_id):
    with db() as con:
        rows = con.execute(
            "SELECT l.*, u.full_name FROM logs l "
            "LEFT JOIN users u ON u.user_id=l.user_id "
            "ORDER BY l.id DESC LIMIT 20").fetchall()
    if not rows:
        send(vk, user_id, "📋 Журнал пуст.", back_kb())
        return
    lines = ["📋 ЖУРНАЛ (последние 20)", ""]
    for r in rows:
        who = r["full_name"] or r["user_id"] or "—"
        dt = r["created_at"][5:16]
        det = f" — {r['details']}" if r["details"] else ""
        lines.append(f"{dt} | {who}: {r['action']}{det}")
    send(vk, user_id, "\n".join(lines), back_kb())


# ======================= ПОЛЬЗОВАТЕЛИ =======================
def show_users(vk, user_id):
    if not is_admin(user_id):
        send(vk, user_id, "Нет доступа.")
        return
    with db() as con:
        rows = con.execute("SELECT * FROM users ORDER BY role, full_name").fetchall()
    if not rows:
        send(vk, user_id, "Нет пользователей.", back_kb())
        return
    lines = ["👥 ПОЛЬЗОВАТЕЛИ", ""]
    for r in rows:
        role_ru = {"operator": "станочник", "warehouse": "кладовщик",
                   "admin": "админ"}.get(r["role"], r["role"])
        block = " 🚫" if r["blocked"] else ""
        lines.append(f"{r['full_name'] or r['user_id']} (id{r['user_id']})\n"
                     f"   {role_ru}{block}")
    send(vk, user_id, "\n".join(lines), back_kb())


def start_new_material(vk, user_id):
    set_state(user_id, "new_thickness")
    send(vk, user_id,
         "🆕 Шаг 1/5. Введите толщину (10мм, 16мм). Или «-», чтобы пропустить.")


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

    # --- корзина ---
    if command == "cart_start":
        clear_state(user_id)
        show_thicknesses(vk, user_id)
        return
    if command.startswith("cart_thick:"):
        th = command.split(":", 1)[1]
        st = get_state(user_id)
        cart = list(st["data"].get("cart", []))
        plan = st["data"].get("plan", "")
        set_state(user_id, "cart_choose", cart=cart, plan=plan, thickness=th)
        show_decors_for_cart(vk, user_id, th)
        return
    if command.startswith("cart_add:"):
        add_to_cart(vk, user_id, int(command.split(":", 1)[1]))
        return
    if command == "cart_show":
        show_cart(vk, user_id)
        return
    if command == "cart_more":
        st = get_state(user_id)
        cart = list(st["data"].get("cart", []))
        plan = st["data"].get("plan", "")
        set_state(user_id, "cart_choose", cart=cart, plan=plan)
        show_thicknesses(vk, user_id)
        return
    if command == "cart_clear":
        clear_state(user_id)
        send(vk, user_id, "🛒 Корзина очищена.", main_menu(user_id))
        return
    if command == "cart_submit":
        submit_cart(vk, user_id)
        return

    # --- план ---
    if command.startswith("plan_page:"):
        page = int(command.split(":", 1)[1])
        st = get_state(user_id)
        cart = list(st["data"].get("cart", []))
        plan = st["data"].get("plan", "")
        set_state(user_id, "choose_plan", cart=cart, plan=plan, page=page)
        show_plan_page(vk, user_id, page)
        return
    if command.startswith("plan_pick:"):
        n = int(command.split(":", 1)[1])
        st = get_state(user_id)
        cart = list(st["data"].get("cart", []))
        if not cart:
            send(vk, user_id, "Корзина пуста.", main_menu(user_id))
            return
        set_state(user_id, "cart", cart=cart, plan=str(n))
        try: register_plan(n)
        except: pass
        send(vk, user_id, f"✅ План: {n}")
        show_cart(vk, user_id)
        return
    if command == "plan_manual":
        st = get_state(user_id)
        cart = list(st["data"].get("cart", []))
        plan = st["data"].get("plan", "")
        set_state(user_id, "plan_manual", cart=cart, plan=plan)
        send(vk, user_id, "🔢 Введите номер плана вручную (1–2000):")
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
        show_inc_list(vk, user_id, mass=False)
        return
    if command.startswith("inc_mat:"):
        mid = int(command.split(":", 1)[1])
        m = get_material(mid)
        if not m:
            send(vk, user_id, "Материал не найден.")
            return
        set_state(user_id, "inc_qty", material_id=mid)
        send(vk, user_id,
             f"➕ {m['name']}\nТекущий остаток: {m['qty']:g} {m['unit']}\n\n"
             f"Введите количество для добавления:")
        return

    if command == "minc_start":
        set_state(user_id, "minc_choose", mass_cart=[])
        show_inc_list(vk, user_id, mass=True)
        return
    if command.startswith("minc_mat:"):
        mid = int(command.split(":", 1)[1])
        m = get_material(mid)
        if not m:
            send(vk, user_id, "Материал не найден.")
            return
        st = get_state(user_id)
        cart = st["data"].get("mass_cart", [])
        set_state(user_id, "minc_qty", mass_cart=cart, material_id=mid)
        send(vk, user_id, f"📦 {m['name']}\nВведите количество для прихода:")
        return
    if command == "minc_more":
        st = get_state(user_id)
        cart = st["data"].get("mass_cart", [])
        set_state(user_id, "minc_choose", mass_cart=cart)
        show_inc_list(vk, user_id, mass=True)
        return
    if command == "minc_done":
        finish_mass_inc(vk, user_id)
        return

    if command == "new_mat":
        start_new_material(vk, user_id)
        return

    if command == "edit_list":
        show_edit_list(vk, user_id)
        return
    if command.startswith("edit:"):
        show_edit_fields(vk, user_id, int(command.split(":", 1)[1]))
        return
    if command.startswith("editf:"):
        parts = command.split(":", 2)
        mid, field = int(parts[1]), parts[2]
        set_state(user_id, "edit_value", mid=mid, field=field)
        send(vk, user_id,
             f"✏️ Введите новое значение для «{EDIT_FIELDS.get(field, field)}»:")
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
    if text in ("/start", "Начать", "⬅️ В меню"):
        clear_state(user_id)
        role = get_role(user_id)
        role_ru = {"operator": "станочник",
                   "warehouse": "кладовщик (погрузчик)",
                   "admin": "администратор"}.get(role, role)
        send(vk, user_id,
             f"👋 Складской бот «Пиломатериалы»\n\n"
             f"Вы вошли как: {role_ru}\n\nВыберите действие:",
             main_menu(user_id))
        return

    if text == "/help":
        send(vk, user_id, HELP_TEXT, back_kb())
        return

    if text == "/whoami":
        send(vk, user_id,
             f"Ваш ID: {user_id}\nВаша роль: {get_role(user_id)}\n"
             f"В ADMIN_IDS: {'да' if user_id in ADMIN_IDS else 'нет'}",
             back_kb())
        return

    if text == "/users":
        show_users(vk, user_id)
        return

    if text.startswith("/setrole"):
        if not is_admin(user_id):
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
        log_action(user_id, f"Смена роли {uid} → {role}")
        send(vk, user_id, f"✅ Пользователь {uid} → {role}")
        return

    if text == "/resetstock":
        if not is_admin(user_id):
            send(vk, user_id, "Нет доступа.")
            return
        clear_stock(vk, user_id)
        return

    st = get_state(user_id)
    state = st["state"]
    data = st["data"]

    if state == "cart_qty":
        try:
            qty = float(text.replace(",", "."))
            if qty <= 0: raise ValueError
        except ValueError:
            send(vk, user_id, "❗ Введите положительное число.")
            return
        cart = list(data.get("cart", []))
        plan = data.get("plan", "")
        idx = data.get("editing_index", len(cart) - 1)
        if 0 <= idx < len(cart):
            cart[idx]["qty"] = qty
        set_state(user_id, "cart", cart=cart, plan=plan)
        show_plan_page(vk, user_id, 1)
        return

    if state == "plan_manual":
        try:
            n = int(text.strip())
            if n < PLAN_MIN or n > PLAN_MAX: raise ValueError
        except ValueError:
            send(vk, user_id, f"❗ Введите число от {PLAN_MIN} до {PLAN_MAX}.")
            return
        cart = data.get("cart", [])
        set_state(user_id, "cart", cart=cart, plan=str(n))
        try: register_plan(n)
        except: pass
        send(vk, user_id, f"✅ План: {n}")
        show_cart(vk, user_id)
        return

    if state == "inc_qty":
        try:
            qty = float(text.replace(",", "."))
            if qty <= 0: raise ValueError
        except ValueError:
            send(vk, user_id, "❗ Введите положительное число.")
            return
        mid = data["material_id"]
        m = get_material(mid)
        with db() as con:
            con.execute("UPDATE materials SET qty=qty+?, updated_at=? WHERE id=?",
                        (qty, now_str(), mid))
            con.commit()
        log_action(user_id, f"Приход {m['name']}", f"+{qty:g}")
        clear_state(user_id)
        send(vk, user_id,
             f"✅ Приход оформлен\n{m['name']}: +{qty:g} {m['unit']}\n"
             f"Новый остаток: {m['qty'] + qty:g} {m['unit']}",
             main_menu(user_id))
        return

    if state == "minc_qty":
        try:
            qty = float(text.replace(",", "."))
            if qty <= 0: raise ValueError
        except ValueError:
            send(vk, user_id, "❗ Введите положительное число.")
            return
        mid = data["material_id"]
        cart = data.get("mass_cart", [])
        cart.append({"material_id": mid, "qty": qty})
        set_state(user_id, "minc_choose", mass_cart=cart)
        m = get_material(mid)
        send(vk, user_id, f"✅ {m['name']}: +{qty:g}")
        show_mass_inc_cart(vk, user_id)
        return

    if state == "edit_value":
        mid, field = data["mid"], data["field"]
        value = text.strip()
        if not value:
            send(vk, user_id, "❗ Значение не может быть пустым.")
            return
        do_edit(vk, user_id, mid, field, value)
        return

    if state == "new_thickness":
        th = "" if text.strip() == "-" else text.strip()
        set_state(user_id, "new_decor", thickness=th)
        send(vk, user_id,
             "🎨 Шаг 2/5. Введите декор (Дуб, Орех, Венге). Или «-».")
        return

    if state == "new_decor":
        decor = "" if text.strip() == "-" else text.strip()
        set_state(user_id, "new_name", thickness=data["thickness"], decor=decor)
        parts = [p for p in (data["thickness"], decor) if p]
        auto = ("Доска " + " ".join(parts)).strip() if parts else ""
        if auto:
            send(vk, user_id,
                 f"📝 Шаг 3/5. Авто-имя: «{auto}»\nВведите своё или «-».")
        else:
            send(vk, user_id, "📝 Шаг 3/5. Введите название материала:")
        return

    if state == "new_name":
        raw = text.strip()
        th, decor = data["thickness"], data["decor"]
        if raw == "-":
            parts = [p for p in (th, decor) if p]
            name = ("Доска " + " ".join(parts)).strip() if parts else ""
        else:
            name = raw
        if not name:
            send(vk, user_id, "❗ Название не может быть пустым.")
            return
        set_state(user_id, "new_qty", thickness=th, decor=decor, name=name)
        send(vk, user_id, "🔢 Шаг 4/5. Введите начальный остаток:")
        return

    if state == "new_qty":
        try:
            qty = float(text.replace(",", "."))
            if qty < 0: raise ValueError
        except ValueError:
            send(vk, user_id, "❗ Введите неотрицательное число.")
            return
        set_state(user_id, "new_unit", thickness=data["thickness"],
                  decor=data["decor"], name=data["name"], qty=qty)
        send(vk, user_id,
             "📐 Шаг 5/5. Единица измерения (шт, лист, м³). Или «-» для шт.")
        return

    if state == "new_unit":
        unit = "шт" if text.strip() == "-" else text.strip()
        with db() as con:
            con.execute("INSERT INTO materials "
                        "(name, unit, qty, thickness, decor, updated_at) "
                        "VALUES (?,?,?,?,?,?)",
                        (data["name"], unit, data["qty"],
                         data["thickness"], data["decor"], now_str()))
            con.commit()
        log_action(user_id, f"Создан материал {data['name']}")
        clear_state(user_id)
        send(vk, user_id,
             f"✅ Материал добавлен\n\n📦 {data['name']}\n"
             f"Толщина: {data['thickness'] or '—'}\n"
             f"Декор: {data['decor'] or '—'}\n"
             f"Остаток: {data['qty']:g} {unit}",
             main_menu(user_id))
        return

    if text == "📋 Остатки на складе":
        send(vk, user_id, render_stock(), stock_kb())
        return
    if text == "📦 Новая заявка":
        clear_state(user_id)
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
        show_inc_list(vk, user_id, mass=False)
        return
    if text == "📦 Массовый приход":
        if not is_warehouse(user_id):
            send(vk, user_id, "Нет доступа.")
            return
        set_state(user_id, "minc_choose", mass_cart=[])
        show_inc_list(vk, user_id, mass=True)
        return
    if text == "🆕 Новая номенклатура":
        if not is_warehouse(user_id):
            send(vk, user_id, "Нет доступа.")
            return
        start_new_material(vk, user_id)
        return
    if text == "✏️ Редактировать":
        if not is_warehouse(user_id):
            send(vk, user_id, "Нет доступа.")
            return
        show_edit_list(vk, user_id)
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
    if text == "📊 Сводка":
        if not is_warehouse(user_id):
            send(vk, user_id, "Нет доступа.")
            return
        show_stats(vk, user_id)
        return
    if text == "📋 Журнал":
        if not is_warehouse(user_id):
            send(vk, user_id, "Нет доступа.")
            return
        show_log(vk, user_id)
        return
    if text == "👥 Пользователи":
        if not is_admin(user_id):
            send(vk, user_id, "Нет доступа.")
            return
        show_users(vk, user_id)
        return

    send(vk, user_id, "Не понимаю команду. Воспользуйтесь кнопками меню.",
         main_menu(user_id))


# ======================= ЗАПУСК =======================
def main():
    init_db()
    vk_session = VkApi(token=GROUP_TOKEN)
    vk = vk_session.get_api()
    longpoll = VkBotLongPoll(vk_session, GROUP_ID)
    print("Бот запущен. Ожидание сообщений...")

    for event in longpoll.listen():
        if event.type == VkBotEventType.MESSAGE_EVENT:
            user_id = event.obj.user_id
            payload = event.obj.payload or {}
            command = payload.get("command")
            try:
                vk.messages.sendMessageEventAnswer(
                    event_id=event.obj.event_id,
                    user_id=event.obj.user_id,
                    peer_id=event.obj.peer_id)
            except Exception:
                pass
            ensure_user(vk, user_id)
            try:
                handle_callback(vk, user_id, command)
            except Exception as e:
                print(f"[ERROR callback] user={user_id} cmd={command}: {e}")
            continue

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
                send(vk, user_id, "⚠️ Произошла ошибка. Попробуйте /start.",
                     main_menu(user_id))
            except Exception:
                pass


if __name__ == "__main__":
    main()
