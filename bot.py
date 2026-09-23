# -*- coding: utf-8 -*-
"""
VK-бот «Склад пиломатериалов».
Роли: operator / warehouse / admin
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
ADMIN_IDS = {123456789}
DB_PATH = "/app/data/warehouse.db"
# =========================================================

STATUS = {
    "new": "🆕 новая",
    "approved": "🔄 в работе",
    "issued": "✅ выдана",
    "rejected": "❌ отклонена",
}

PLAN_MIN = 1
PLAN_MAX = 2000
PLAN_STEP = 100
PLAN_BUTTONS_PER_PAGE = 10


# ======================= БАЗА =======================
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
                blocked    INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS materials (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                unit       TEXT NOT NULL DEFAULT 'шт',
                qty        REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS requests (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                material_id INTEGER NOT NULL,
                qty         REAL NOT NULL,
                comment     TEXT,
                plan        TEXT DEFAULT '',
                status      TEXT NOT NULL DEFAULT 'new',
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS request_items (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id  INTEGER NOT NULL,
                material_id INTEGER NOT NULL,
                qty         REAL NOT NULL,
                status      TEXT NOT NULL DEFAULT 'new'
            );

            CREATE TABLE IF NOT EXISTS logs (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER,
                action     TEXT NOT NULL,
                details    TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS plans (
                number     INTEGER PRIMARY KEY,
                used_at    TEXT NOT NULL
            );
        """)

        def cols(table):
            return [r["name"] for r in con.execute(f"PRAGMA table_info({table})").fetchall()]

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
                ("Доска 10мм Дуб",   "шт", 120, "10мм", "Дуб"),
                ("Доска 10мм Орех",  "шт",  80, "10мм", "Орех"),
                ("Доска 10мм Венге", "шт",  60, "10мм", "Венге"),
                ("Доска 16мм Дуб",   "шт", 150, "16мм", "Дуб"),
                ("Доска 16мм Орех",  "шт", 100, "16мм", "Орех"),
                ("Доска 16мм Ясень", "шт",  70, "16мм", "Ясень"),
                ("Доска 22мм Дуб",   "шт",  90, "22мм", "Дуб"),
                ("Доска 22мм Венге", "шт",  50, "22мм", "Венге"),
                ("Доска 22мм Клён",  "шт",  40, "22мм", "Клён"),
            ]
            con.executemany(
                "INSERT INTO materials (name, unit, qty, thickness, decor, updated_at) "
                "VALUES (?,?,?,?,?,?)",
                [(*s, now) for s in seed],
            )
        con.commit()


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log_action(user_id, action, details=""):
    with db() as con:
        con.execute(
            "INSERT INTO logs (user_id, action, details, created_at) VALUES (?,?,?,?)",
            (user_id, action, details, now_str()),
        )
        con.commit()


def ensure_user(vk, user_id):
    with db() as con:
        row = con.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        if row is not None:
            return row
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
            "INSERT OR IGNORE INTO users (user_id, username, full_name, role, blocked, created_at) "
            "VALUES (?,?,?,?,0,?)",
            (user_id, username, full_name, role, now_str()),
        )
        con.commit()
        return con.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()


def get_user(user_id):
    with db() as con:
        return con.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()


def get_role(user_id) -> str:
    u = get_user(user_id)
    return u["role"] if u else "operator"


def is_blocked(user_id) -> bool:
    u = get_user(user_id)
    return bool(u and u["blocked"])


def is_warehouse(user_id) -> bool:
    return get_role(user_id) in ("warehouse", "admin")


def is_admin(user_id) -> bool:
    return get_role(user_id) == "admin" or user_id in ADMIN_IDS


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
            "SELECT DISTINCT thickness FROM materials WHERE COALESCE(thickness,'') != '' "
            "ORDER BY thickness"
        ).fetchall()
    return [r["thickness"] for r in rows]


def decors_by_thickness(th):
    with db() as con:
        return con.execute(
            "SELECT * FROM materials WHERE COALESCE(thickness,'') = ? ORDER BY decor",
            (th,),
        ).fetchall()


def material_label(m):
    parts = [p for p in [m["thickness"], m["decor"]] if p]
    return " ".join(parts) if parts else m["name"]


# ======================= ПЛАНЫ =======================
def register_plan(number: int):
    with db() as con:
        con.execute(
            "INSERT OR REPLACE INTO plans (number, used_at) VALUES (?, ?)",
            (number, now_str()),
        )
        con.commit()


def last_plan_number():
    with db() as con:
        row = con.execute(
            "SELECT number FROM plans ORDER BY used_at DESC LIMIT 1"
        ).fetchone()
    return row["number"] if row else None


def suggested_center():
    last = last_plan_number()
    if last is None:
        return 1
    return max(PLAN_MIN, min(PLAN_MAX, last))


def plan_page_items(page: int):
    """Страница: PLAN_BUTTONS_PER_PAGE номеров вокруг последнего плана."""
    center = suggested_center()
    # Первая страница — диапазон [center-50, center+49], каждая следующая — сдвиг на 100
    half = PLAN_STEP // 2
    base = center - half + (page - 1) * PLAN_STEP
    base = max(PLAN_MIN, base)
    items = []
    for i in range(PLAN_BUTTONS_PER_PAGE):
        n = base + i
        if PLAN_MIN <= n <= PLAN_MAX:
            items.append(n)
    return items, base


def show_plan_page(vk, user_id, page: int = 1):
    items, base = plan_page_items(page)
    if not items:
        send(vk, user_id, "Нет доступных номеров в этом диапазоне.")
        return

    last = last_plan_number()
    hint = f"Последний использованный план: {last}" if last else "Раньше планов не было"

    kb = VkKeyboard(one_time=False)
    for i, n in enumerate(items):
        if i % 2 == 0 and i > 0:
            kb.add_line()
        kb.add_callback_button(str(n), color=VkKeyboardColor.PRIMARY,
                               payload={"command": f"plan_pick:{n}"})
    kb.add_line()
    if page > 1:
        kb.add_callback_button("◀️ -100", color=VkKeyboardColor.SECONDARY,
                               payload={"command": f"plan_page:{page - 1}"})
    if items[-1] < PLAN_MAX:
        kb.add_callback_button("+100 ▶️", color=VkKeyboardColor.SECONDARY,
                               payload={"command": f"plan_page:{page + 1}"})
    kb.add_line()
    kb.add_callback_button("🔢 Ввести вручную", color=VkKeyboardColor.PRIMARY,
                           payload={"command": "plan_manual"})
    kb.add_line()
    kb.add_button("⬅️ В меню", color=VkKeyboardColor.SECONDARY)

    set_state(user_id, "choose_plan", page=page)
    send(vk, user_id,
         f"📋 Шаг 3. Выберите номер плана производства.\n\n"
         f"{hint}\n"
         f"Показан диапазон {items[0]}–{items[-1]} ({page}-я страница).\n"
         f"Если нужного номера нет — жмите ◀️ / ▶️ или «🔢 Ввести вручную».",
         kb.get_keyboard())


# ======================= ТАБЛИЦА =======================
def render_stock() -> str:
    rows = all_materials()
    if not rows:
        return "📋 Склад пуст"
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


# ======================= МЕНЮ =======================
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
    else:
        kb.add_line()
        kb.add_button("📦 Новая заявка", color=VkKeyboardColor.POSITIVE)
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
        user_id=user_id, message=text, keyboard=keyboard,
        random_id=get_random_id(),
    )


def notify_warehouse(vk, text):
    with db() as con:
        rows = con.execute(
            "SELECT user_id FROM users WHERE role IN ('warehouse','admin') AND blocked=0"
        ).fetchall()
    for r in rows:
        try:
            send(vk, r["user_id"], text)
        except Exception:
            pass


HELP_TEXT = """📖 КОМАНДЫ БОТА

Для всех:
/start — главное меню
/help — эта справка
/whoami — мой ID и роль

Для станочника:
📦 Новая заявка — заявка на несколько позиций
📄 Мои заявки — история заявок

Для кладовщика:
📥 Заявки станочников — выдача/отклонение
➕ Приход материала — +количество к позиции
📦 Массовый приход — приход нескольких позиций
🆕 Новая номенклатура — создать материал
✏️ Редактировать — изменить поля
🗑 Удалить номенклатуру — удалить позицию
🧹 Очистить остатки — обнулить всё
📊 Сводка — состояние склада
📋 Журнал — последние действия

Только админ:
/users — список пользователей
/setrole <ID> <роль> — назначить роль
/resetstock — обнулить остатки
/resetdb — полный сброс базы"""


# ======================= КОРЗИНА =======================
def show_thicknesses(vk, user_id):
    ths = distinct_thicknesses()
    if not ths:
        send(vk, user_id, "Склад пуст.", main_menu(user_id))
        return
    kb = VkKeyboard(one_time=False)
    for i, th in enumerate(ths):
        if i > 0:
            kb.add_line()
        kb.add_callback_button(th, color=VkKeyboardColor.PRIMARY,
                               payload={"command": f"cart_thick:{th}"})
    kb.add_line()
    kb.add_button("⬅️ В меню", color=VkKeyboardColor.SECONDARY)
    send(vk, user_id, "📏 Шаг 1. Выберите толщину:", kb.get_keyboard())


def show_decors_for_cart(vk, user_id, th):
    rows = decors_by_thickness(th)
    if not rows:
        send(vk, user_id, f"Для толщины {th} нет позиций.", main_menu(user_id))
        return
    st = get_state(user_id)
    cart = st["data"].get("cart", []) if st["state"] in ("cart", "cart_qty", "choose_plan", "plan_manual") else []
    kb = VkKeyboard(one_time=False)
    for i, m in enumerate(rows):
        if i > 0:
            kb.add_line()
        decor = m["decor"] or m["name"]
        kb.add_callback_button(
            f"{decor} — {m['qty']:g} {m['unit']}",
            color=VkKeyboardColor.PRIMARY,
            payload={"command": f"cart_add:{m['id']}"},
        )
    kb.add_line()
    if cart:
        kb.add_callback_button("🛒 Показать корзину", color=VkKeyboardColor.POSITIVE,
                               payload={"command": "cart_show"})
        kb.add_line()
    kb.add_button("⬅️ В меню", color=VkKeyboardColor.SECONDARY)
    send(vk, user_id,
         f"🎨 Шаг 2. Толщина {th}. Выберите декор:",
         kb.get_keyboard())


def add_to_cart(vk, user_id, mid):
    m = get_material(mid)
    if not m:
        send(vk, user_id, "Материал не найден.")
        return
    st = get_state(user_id)
    cart = st["data"].get("cart", []) if st["state"] in ("cart", "cart_qty", "choose_plan", "plan_manual") else []
    cart.append({"material_id": mid, "qty": None})
    set_state(user_id, "cart_qty", cart=cart, editing_index=len(cart) - 1)
    send(vk, user_id,
         f"🛒 Добавлено: {m['name']}\n"
         f"Остаток: {m['qty']:g} {m['unit']}\n\n"
         f"Введите количество (число):")


def show_cart(vk, user_id):
    st = get_state(user_id)
    cart = st["data"].get("cart", [])
    if not cart:
        send(vk, user_id, "Корзина пуста.", main_menu(user_id))
        return
    lines = ["🛒 ВАША ЗАЯВКА", ""]
    for i, it in enumerate(cart, 1):
        m = get_material(it["material_id"])
        qty = it["qty"]
        qty_str = f"{qty:g}" if qty is not None else "?"
        lines.append(f"{i}. {m['name'] if m else '(удалён)'} — {qty_str}")
    if st["data"].get("plan"):
        lines.append("")
        lines.append(f"📋 План производства: {st['data']['plan']}")
    kb = VkKeyboard(one_time=False)
    kb.add_callback_button("➕ Добавить ещё позицию", color=VkKeyboardColor.PRIMARY,
                           payload={"command": "cart_more"})
    kb.add_callback_button("🗑 Очистить корзину", color=VkKeyboardColor.NEGATIVE,
                           payload={"command": "cart_clear"})
    kb.add_line()
    kb.add_callback_button("✅ Отправить заявку", color=VkKeyboardColor.POSITIVE,
                           payload={"command": "cart_submit"})
    kb.add_line()
    kb.add_button("⬅️ В меню", color=VkKeyboardColor.SECONDARY)
    send(vk, user_id, "\n".join(lines), kb.get_keyboard())


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
            "status, created_at, updated_at) VALUES (?,?,?,?,?, 'new', ?, ?)",
            (user_id, cart[0]["material_id"], sum(i["qty"] for i in cart),
             plan, "", now_str(), now_str()),
        )
        rid = cur.lastrowid
        for it in cart:
            con.execute(
                "INSERT INTO request_items (request_id, material_id, qty, status) "
                "VALUES (?,?,?,'new')",
                (rid, it["material_id"], it["qty"]),
            )
        con.commit()

    clear_state(user_id)
    log_action(user_id, f"Создана заявка №{rid}",
               f"{len(cart)} позиций, план={plan}")
    try:
        if plan:
            register_plan(int(plan))
    except (ValueError, TypeError):
        pass

    lines = [f"✅ Заявка №{rid} отправлена", ""]
    for i, it in enumerate(cart, 1):
        m = get_material(it["material_id"])
        lines.append(f"{i}. {m['name']} — {it['qty']:g} {m['unit']}")
    lines.append("")
    lines.append(f"📋 План: {plan or '—'}")
    send(vk, user_id, "\n".join(lines), main_menu(user_id))

    user = get_user(user_id)
    author = user["full_name"] if user else str(user_id)
    notif = [f"🔔 Новая заявка №{rid} (мульти-позиции)", ""]
    for i, it in enumerate(cart, 1):
        m = get_material(it["material_id"])
        notif.append(f"{i}. {m['name']} — {it['qty']:g} {m['unit']}")
    notif.append("")
    notif.append(f"📋 План: {plan or '—'}")
    notif.append(f"От: {author}")
    notify_warehouse(vk, "\n".join(notif))


# ======================= ЗАЯВКИ =======================
def show_my_requests(vk, user_id):
    with db() as con:
        rows = con.execute(
            """SELECT r.*, m.name AS m_name, m.unit AS m_unit
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
        with db() as con:
            items = con.execute(
                "SELECT COUNT(*) c FROM request_items WHERE request_id=?",
                (r["id"],),
            ).fetchone()["c"]
        tag = f" ({items} поз.)" if items else ""
        lines.append(
            f"№{r['id']}{tag} • {r['m_name'] or '(удалён)'} — {r['qty']:g}\n"
            f"   План: {r['plan'] or '—'}\n"
            f"   {STATUS.get(r['status'], r['status'])} • {r['created_at'][:16]}"
        )
    send(vk, user_id, "\n".join(lines), back_kb())


def show_active_requests(vk, user_id):
    with db() as con:
        rows = con.execute(
            """SELECT r.*, u.full_name FROM requests r
               LEFT JOIN users u ON u.user_id = r.user_id
               WHERE r.status IN ('new','approved')
               ORDER BY r.id"""
        ).fetchall()
    if not rows:
        send(vk, user_id, "📥 Активных заявок нет.", back_kb())
        return
    send(vk, user_id, f"📥 Активных заявок: {len(rows)}", back_kb())
    for r in rows:
        with db() as con:
            items = con.execute(
                """SELECT ri.*, m.name AS m_name, m.unit AS m_unit
                   FROM request_items ri
                   LEFT JOIN materials m ON m.id = ri.material_id
                   WHERE ri.request_id=?""",
                (r["id"],),
            ).fetchall()
        if items:
            body = "\n".join(
                f"   • {it['m_name'] or '(удалён)'} — {it['qty']:g} {it['m_unit'] or ''}"
                for it in items
            )
        else:
            m = get_material(r["material_id"])
            body = f"   • {m['name'] if m else '(удалён)'} — {r['qty']:g}"
        text_msg = (
            f"Заявка №{r['id']}\n"
            f"{body}\n"
            f"📋 План: {r['plan'] or '—'}\n"
            f"От: {r['full_name'] or r['user_id']}\n"
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
        items = con.execute(
            "SELECT * FROM request_items WHERE request_id=?", (rid,)
        ).fetchall()
        if items:
            for it in items:
                m = con.execute("SELECT * FROM materials WHERE id=?",
                                (it["material_id"],)).fetchone()
                if not m:
                    send(vk, user_id, f"Материал ID {it['material_id']} удалён.", back_kb())
                    return
                if m["qty"] < it["qty"]:
                    send(vk, user_id,
                         f"Не хватает: {m['name']} (есть {m['qty']:g}, нужно {it['qty']:g}).",
                         back_kb())
                    return
            summary = []
            for it in items:
                m = con.execute("SELECT * FROM materials WHERE id=?",
                                (it["material_id"],)).fetchone()
                con.execute(
                    "UPDATE materials SET qty = qty - ?, updated_at = ? WHERE id = ?",
                    (it["qty"], now_str(), m["id"]),
                )
                summary.append(f"{m['name']} — {it['qty']:g} {m['unit']}")
        else:
            m = con.execute("SELECT * FROM materials WHERE id=?",
                            (r["material_id"],)).fetchone()
            if not m:
                send(vk, user_id, "Материал удалён.", back_kb())
                return
            if m["qty"] < r["qty"]:
                send(vk, user_id, f"На складе только {m['qty']:g} {m['unit']}.", back_kb())
                return
            con.execute("UPDATE materials SET qty = qty - ?, updated_at = ? WHERE id = ?",
                        (r["qty"], now_str(), m["id"]))
            summary = [f"{m['name']} — {r['qty']:g} {m['unit']}"]

        con.execute("UPDATE requests SET status='issued', updated_at=? WHERE id=?",
                    (now_str(), rid))
        con.commit()

    log_action(user_id, f"Выдана заявка №{rid}", "; ".join(summary))
    send(vk, user_id,
         f"✅ Заявка №{rid} выдана\n" + "\n".join(summary),
         back_kb())
    try:
        send(vk, r["user_id"],
             f"✅ Заявка №{rid} выполнена:\n" + "\n".join(summary))
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
    log_action(user_id, f"Отклонена заявка №{rid}")
    send(vk, user_id, f"❌ Заявка №{rid} отклонена.", back_kb())
    try:
        send(vk, r["user_id"], f"❌ Заявка №{rid} отклонена.")
    except Exception:
        pass


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
        kb.add_callback_button(
            f"{material_label(r)} ({r['qty']:g})",
            color=VkKeyboardColor.PRIMARY,
            payload={"command": f"{cmd}:{r['id']}"},
        )
    kb.add_line()
    if mass:
        kb.add_callback_button("✅ Завершить приход", color=VkKeyboardColor.POSITIVE,
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
    lines = ["📦 ПРИХОД (текущий список)", ""]
    for i, it in enumerate(cart, 1):
        m = get_material(it["material_id"])
        lines.append(f"{i}. {m['name'] if m else '?'} — +{it['qty']:g}")
    kb = VkKeyboard(one_time=False)
    kb.add_callback_button("➕ Ещё позиция", color=VkKeyboardColor.PRIMARY,
                           payload={"command": "minc_more"})
    kb.add_callback_button("✅ Завершить приход", color=VkKeyboardColor.POSITIVE,
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
            con.execute(
                "UPDATE materials SET qty = qty + ?, updated_at = ? WHERE id = ?",
                (it["qty"], now_str(), it["material_id"]),
            )
        con.commit()
    lines = ["✅ Приход оформлен", ""]
    for it in cart:
        m = get_material(it["material_id"])
        lines.append(f"{m['name']}: +{it['qty']:g} {m['unit']}")
    clear_state(user_id)
    log_action(user_id, "Массовый приход", "; ".join(
        f"{get_material(i['material_id'])['name']} +{i['qty']:g}" for i in cart
    ))
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
    kb.add_button
