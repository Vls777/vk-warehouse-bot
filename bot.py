# -*- coding: utf-8 -*-
"""VK-бот «Склад»."""

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

STATUS = {"new": "🆕 новая", "approved": "🔄 в работе",
          "issued": "✅ выдана", "rejected": "❌ отклонена"}
PLAN_MIN, PLAN_MAX = 1, 2000
PLAN_BUTTONS_PER_PAGE = 10

CATEGORIES = {
    "board": {"label": "Пиломатериалы", "emoji": "🪵", "has_thickness": True},
    "edge":  {"label": "Кромка",        "emoji": "📏", "has_thickness": False},
    "film":  {"label": "Плёнка ПВХ",    "emoji": "🎞", "has_thickness": False},
}
CATEGORY_ORDER = ["board", "edge", "film"]
CAT_WORD = {"board": "Пил", "edge": "Кром", "film": "Плён"}


def cat_label(code):
    c = CATEGORIES.get(code)
    return f"{c['emoji']} {c['label']}" if c else code


def cat_emoji(code):
    c = CATEGORIES.get(code)
    return c["emoji"] if c else "📦"


def cat_word(code):
    return CAT_WORD.get(code or "board", "?")


def full_label(m):
    parts = [p for p in [m["thickness"], m["decor"]] if p]
    tail = " ".join(parts) if parts else m["name"]
    return f"{cat_emoji(m['category'])} {tail}"


def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT,
                full_name TEXT, role TEXT NOT NULL DEFAULT 'operator',
                blocked INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS materials (id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL, unit TEXT NOT NULL DEFAULT 'шт',
                qty REAL NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS requests (id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL, material_id INTEGER NOT NULL, qty REAL NOT NULL,
                comment TEXT, plan TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'new',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS request_items (id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id INTEGER NOT NULL, material_id INTEGER NOT NULL,
                qty REAL NOT NULL, plan TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'new');
            CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER, action TEXT NOT NULL, details TEXT, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS plans (number INTEGER PRIMARY KEY, used_at TEXT NOT NULL);
        """)
        def cols(t):
            return [r["name"] for r in con.execute(f"PRAGMA table_info({t})").fetchall()]
        if "thickness" not in cols("materials"):
            con.execute("ALTER TABLE materials ADD COLUMN thickness TEXT DEFAULT ''")
        if "decor" not in cols("materials"):
            con.execute("ALTER TABLE materials ADD COLUMN decor TEXT DEFAULT ''")
        if "category" not in cols("materials"):
            con.execute("ALTER TABLE materials ADD COLUMN category TEXT DEFAULT 'board'")
        if "plan" not in cols("requests"):
            con.execute("ALTER TABLE requests ADD COLUMN plan TEXT DEFAULT ''")
        if "plan" not in cols("request_items"):
            con.execute("ALTER TABLE request_items ADD COLUMN plan TEXT DEFAULT ''")
        if "blocked" not in cols("users"):
            con.execute("ALTER TABLE users ADD COLUMN blocked INTEGER NOT NULL DEFAULT 0")
        if "hidden" not in cols("materials"):
            con.execute("ALTER TABLE materials ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")

        # Служебные позиции для заявок на кромку
        now2 = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        r1 = con.execute("SELECT 1 FROM materials WHERE category='edge' AND hidden=1 "
                         "AND name='Кромка'").fetchone()
        if not r1:
            con.execute("INSERT INTO materials (category,name,unit,qty,thickness,decor,"
                        "hidden,updated_at) VALUES ('edge','Кромка','м',0,'','',1,?)",
                        (now2,))
        r2 = con.execute("SELECT 1 FROM materials WHERE category='edge' AND hidden=1 "
                         "AND name='Клей кромочный'").fetchone()
        if not r2:
            con.execute("INSERT INTO materials (category,name,unit,qty,thickness,decor,"
                        "hidden,updated_at) VALUES ('edge','Клей кромочный','канистра',0,'','',1,?)",
                        (now2,))

        if con.execute("SELECT COUNT(*) c FROM materials WHERE hidden=0").fetchone()["c"] == 0:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            seed = [
                ("board", "Доска 10мм Дуб",   "шт", 120, "10мм", "Дуб"),
                ("board", "Доска 10мм Орех",  "шт",  80, "10мм", "Орех"),
                ("board", "Доска 10мм Венге", "шт",  60, "10мм", "Венге"),
                ("board", "Доска 16мм Дуб",   "шт", 150, "16мм", "Дуб"),
                ("board", "Доска 16мм Орех",  "шт", 100, "16мм", "Орех"),
                ("board", "Доска 16мм Ясень", "шт",  70, "16мм", "Ясень"),
                ("board", "Доска 22мм Дуб",   "шт",  90, "22мм", "Дуб"),
                ("board", "Доска 22мм Венге", "шт",  50, "22мм", "Венге"),
                ("board", "Доска 22мм Клён",  "шт",  40, "22мм", "Клён"),
                ("board", "ЛДСП 22мм Тефия",  "лист", 30, "22мм", "Тефия"),
                ("film",  "Плёнка Красный",   "м",  300, "",     "Красный"),
                ("film",  "Плёнка Белый",     "м",  250, "",     "Белый"),
                ("film",  "Плёнка Венге",     "м",  180, "",     "Венге"),
                ("film",  "Плёнка Серый",     "м",  150, "",     "Серый"),
                ("film",  "Клей для МДФ Kleiberit 431", "кг", 15, "", "Kleiberit 431"),
            ]
            con.executemany(
                "INSERT INTO materials (category,name,unit,qty,thickness,decor,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                [(c, n, u, q, th, d, now) for c, n, u, q, th, d in seed])
        con.commit()


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log_action(user_id, action, details=""):
    with db() as con:
        con.execute("INSERT INTO logs (user_id,action,details,created_at) VALUES (?,?,?,?)",
                    (user_id, action, details, now_str()))
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
        con.execute("INSERT OR IGNORE INTO users (user_id,username,full_name,role,blocked,created_at) "
                    "VALUES (?,?,?,?,0,?)", (user_id, username, full_name, role, now_str()))
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


def is_warehouse_strict(user_id):
    return get_role(user_id) == "warehouse"


def is_driver(user_id):
    return get_role(user_id) in ("driver", "warehouse", "admin")


def is_admin(user_id):
    return get_role(user_id) == "admin" or user_id in ADMIN_IDS


def get_material(mid):
    with db() as con:
        return con.execute("SELECT * FROM materials WHERE id=?", (mid,)).fetchone()


def all_materials():
    with db() as con:
        return con.execute("SELECT * FROM materials WHERE COALESCE(hidden,0)=0 "
                           "ORDER BY category,thickness,decor,name").fetchall()


def distinct_categories():
    with db() as con:
        rows = con.execute("SELECT DISTINCT category FROM materials "
                           "WHERE COALESCE(hidden,0)=0").fetchall()
    seen = []
    for r in rows:
        c = r["category"] or "board"
        if c not in seen:
            seen.append(c)
    return sorted(seen, key=lambda c: CATEGORY_ORDER.index(c) if c in CATEGORY_ORDER else 99)


def thicknesses_by_category(cat):
    with db() as con:
        rows = con.execute("SELECT DISTINCT thickness FROM materials "
                           "WHERE category=? AND COALESCE(hidden,0)=0 "
                           "AND COALESCE(thickness,'')!='' "
                           "ORDER BY thickness", (cat,)).fetchall()
    return [r["thickness"] for r in rows]


def materials_by_category(cat, th=None):
    with db() as con:
        if th is None:
            return con.execute("SELECT * FROM materials WHERE category=? "
                               "AND COALESCE(hidden,0)=0 "
                               "ORDER BY decor, name", (cat,)).fetchall()
        if th == "__none__":
            return con.execute("SELECT * FROM materials WHERE category=? "
                               "AND COALESCE(hidden,0)=0 "
                               "AND COALESCE(thickness,'')='' "
                               "ORDER BY decor, name", (cat,)).fetchall()
        return con.execute("SELECT * FROM materials WHERE category=? "
                           "AND COALESCE(hidden,0)=0 "
                           "AND COALESCE(thickness,'')=? ORDER BY decor, name",
                           (cat, th)).fetchall()


def material_label(m):
    parts = [p for p in [m["thickness"], m["decor"]] if p]
    return " ".join(parts) if parts else m["name"]


def _find_edge_main():
    with db() as con:
        rows = con.execute("SELECT * FROM materials WHERE category='edge' "
                           "AND hidden=1").fetchall()
    for r in rows:
        if (r["name"] or "").strip() == "Кромка":
            return r
    with db() as con:
        con.execute("INSERT INTO materials (category,name,unit,qty,thickness,decor,"
                    "hidden,updated_at) VALUES ('edge','Кромка','м',0,'','',1,?)",
                    (now_str(),))
        con.commit()
        return con.execute("SELECT * FROM materials WHERE category='edge' AND hidden=1 "
                           "AND name='Кромка' LIMIT 1").fetchone()


def _find_edge_glue():
    with db() as con:
        rows = con.execute("SELECT * FROM materials WHERE category='edge' "
                           "AND hidden=1").fetchall()
    for r in rows:
        nm = (r["name"] or "")
        if "клей" in nm.lower():
            return r
    with db() as con:
        con.execute("INSERT INTO materials (category,name,unit,qty,thickness,decor,"
                    "hidden,updated_at) VALUES ('edge','Клей кромочный','канистра',0,'','',1,?)",
                    (now_str(),))
        con.commit()
        return con.execute("SELECT * FROM materials WHERE category='edge' AND hidden=1 "
                           "AND name='Клей кромочный' LIMIT 1").fetchone()


def _add_edge_to_cart(cart, plan, glue_qty=None):
    edge = _find_edge_main()
    glue = _find_edge_glue()
    if edge:
        cart.append({"material_id": edge["id"], "qty": 0, "plan": plan})
    if glue_qty and glue:
        cart.append({"material_id": glue["id"], "qty": glue_qty, "plan": plan})


def _cats_words_for_items(items):
    seen = []
    for it in items:
        c = it["m_cat"] if "m_cat" in it.keys() else None
        s = cat_word(c)
        if s not in seen:
            seen.append(s)
    return "+".join(seen) if seen else "?"


def _request_cats_words(rid):
    with db() as con:
        rows = con.execute(
            """SELECT m.category AS m_cat FROM request_items ri
               LEFT JOIN materials m ON m.id=ri.material_id
               WHERE ri.request_id=?""", (rid,)).fetchall()
    seen = []
    for r in rows:
        s = cat_word(r["m_cat"])
        if s not in seen:
            seen.append(s)
    return "+".join(seen) if seen else "?"


def _all_board(cart):
    if not cart: return False
    for it in cart:
        m = get_material(it["material_id"])
        if not m: return False
        if (m["category"] or "board") != "board" or m["hidden"]:
            return False
    return True


def _is_edge_only_request(items):
    if not items: return False
    for it in items:
        if not it["m_cat"] or it["m_cat"] != "edge":
            return False
    return True


def _edge_request_data(items):
    plan = ""
    glue_needed = False
    glue_qty = 0.0
    for it in items:
        name = (it["m_name"] or "").lower()
        if "клей" in name:
            glue_needed = True
            glue_qty = float(it["qty"] or 0)
        else:
            if it["plan"]:
                plan = it["plan"]
    return plan, glue_needed, glue_qty


def register_plan(n):
    with db() as con:
        con.execute("INSERT OR REPLACE INTO plans (number,used_at) VALUES (?,?)", (n, now_str()))
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
    half = PLAN_BUTTONS_PER_PAGE // 2
    base = center - half + (page - 1) * PLAN_BUTTONS_PER_PAGE
    if base < PLAN_MIN: base = PLAN_MIN
    if base + PLAN_BUTTONS_PER_PAGE - 1 > PLAN_MAX:
        base = PLAN_MAX - PLAN_BUTTONS_PER_PAGE + 1
    if base < PLAN_MIN: base = PLAN_MIN
    items = [base + i for i in range(PLAN_BUTTONS_PER_PAGE)
             if PLAN_MIN <= base + i <= PLAN_MAX]
    return items, base


def render_stock():
    rows = all_materials()
    if not rows: return "📋 Склад пуст"
    out = ["📋 ОСТАТКИ СКЛАДА",
           f"обновлено: {datetime.now():%d.%m.%Y %H:%M:%S}", ""]
    cats = {}
    for r in rows:
        cats.setdefault(r["category"] or "board", []).append(r)
    ordered = sorted(cats.keys(),
                     key=lambda c: CATEGORY_ORDER.index(c) if c in CATEGORY_ORDER else 99)
    for cat_code in ordered:
        out.append(f"═══ {cat_label(cat_code)} ═══")
        groups = {}
        for r in cats[cat_code]:
            key = r["thickness"] or "—"
            groups.setdefault(key, []).append(r)
        for th in sorted(groups.keys()):
            if th != "—":
                out.append(f"▸ {th}")
            for r in groups[th]:
                name = r["decor"] or r["name"]
                out.append(f"   {name:<24} {r['qty']:>6g} {r['unit']}")
        out.append("")
    return "\n".join(out)


def main_menu(user_id):
    role = get_role(user_id)
    kb = VkKeyboard(one_time=False)
    kb.add_button("📋 Остатки на складе", color=VkKeyboardColor.PRIMARY)
    kb.add_line()
    kb.add_button("📦 Новая заявка", color=VkKeyboardColor.POSITIVE)
    kb.add_button("📄 Мои заявки", color=VkKeyboardColor.SECONDARY)
    if role in ("driver", "warehouse", "admin"):
        kb.add_line()
        kb.add_button("📥 Заявки", color=VkKeyboardColor.POSITIVE)
        kb.add_line()
        kb.add_button("➕ Приход", color=VkKeyboardColor.PRIMARY)
        kb.add_button("📦 Массовый приход", color=VkKeyboardColor.PRIMARY)
    if role in ("warehouse", "admin"):
        kb.add_line()
        kb.add_button("🆕 Номенклатура", color=VkKeyboardColor.PRIMARY)
        kb.add_button("✏️ Редактировать", color=VkKeyboardColor.PRIMARY)
        kb.add_line()
        kb.add_button("🗑 Удалить", color=VkKeyboardColor.NEGATIVE)
        kb.add_button("🧹 Очистить", color=VkKeyboardColor.NEGATIVE)
        kb.add_line()
        kb.add_button("📋 Инвентаризация", color=VkKeyboardColor.PRIMARY)
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


def notify_board_and_warehouse(vk, text):
    with db() as con:
        rows = con.execute("SELECT user_id FROM users "
                           "WHERE role IN ('driver','warehouse','admin') AND blocked=0").fetchall()
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

Водитель погрузчика:
📥 Заявки
➕ Приход
📦 Массовый приход

Кладовщик:
🆕 Номенклатура
✏️ Редактировать
🗑 Удалить
🧹 Очистить
📋 Инвентаризация
📊 Сводка
📋 Журнал

Админ:
/users — пользователи
/setrole <ID> <роль>
   роли: operator / driver / warehouse / admin
/resetstock"""


# ======================= КОРЗИНА =======================
def show_categories_for_cart(vk, user_id):
    cats = distinct_categories()
    if not cats:
        send(vk, user_id, "Склад пуст.", main_menu(user_id)); return
    st = get_state(user_id)
    cart = list(st["data"].get("cart", []))
    default_plan = st["data"].get("default_plan", "")
    kb = VkKeyboard(one_time=False)
    for i, c in enumerate(cats):
        if i > 0: kb.add_line()
        kb.add_callback_button(cat_label(c), color=VkKeyboardColor.PRIMARY,
                               payload={"command": f"cart_cat:{c}"})
    kb.add_line()
    if cart:
        kb.add_callback_button("🛒 Корзина", color=VkKeyboardColor.POSITIVE,
                               payload={"command": "cart_show"})
        kb.add_line()
    kb.add_button("⬅️ В меню", color=VkKeyboardColor.SECONDARY)
    set_state(user_id, "cart_choose", cart=cart, default_plan=default_plan)
    send(vk, user_id, "📂 Шаг 1. Выберите категорию:", kb.get_keyboard())


def show_thicknesses_for_cart(vk, user_id, cat):
    ths = thicknesses_by_category(cat)
    with db() as con:
        no_th = con.execute(
            "SELECT COUNT(*) c FROM materials WHERE category=? "
            "AND COALESCE(hidden,0)=0 AND COALESCE(thickness,'')=''",
            (cat,)).fetchone()["c"]
    if not ths and no_th == 0:
        send(vk, user_id, "Нет материалов.", main_menu(user_id)); return
    st = get_state(user_id)
    cart = list(st["data"].get("cart", []))
    default_plan = st["data"].get("default_plan", "")
    kb = VkKeyboard(one_time=False)
    for i, th in enumerate(ths):
        if i > 0: kb.add_line()
        kb.add_callback_button(th, color=VkKeyboardColor.PRIMARY,
                               payload={"command": f"cart_thick:{cat}:{th}"})
    if no_th > 0:
        if ths: kb.add_line()
        kb.add_callback_button("📦 Без толщины", color=VkKeyboardColor.PRIMARY,
                               payload={"command": f"cart_thick:{cat}:__none__"})
    kb.add_line()
    if cart:
        kb.add_callback_button("🛒 Корзина", color=VkKeyboardColor.POSITIVE,
                               payload={"command": "cart_show"})
        kb.add_line()
    kb.add_callback_button("⬅️ К категориям", color=VkKeyboardColor.SECONDARY,
                           payload={"command": "cart_back_cat"})
    kb.add_line()
    kb.add_button("⬅️ В меню", color=VkKeyboardColor.SECONDARY)
    set_state(user_id, "cart_choose", cart=cart, default_plan=default_plan, category=cat)
    send(vk, user_id, f"{cat_label(cat)}. Шаг 2. Выберите толщину:", kb.get_keyboard())


def show_materials_for_cart(vk, user_id, cat, th=None, page=1):
    per_page = 6
    with db() as con:
        if th is None:
            total = con.execute("SELECT COUNT(*) c FROM materials WHERE category=? "
                                "AND COALESCE(hidden,0)=0", (cat,)).fetchone()["c"]
        elif th == "__none__":
            total = con.execute("SELECT COUNT(*) c FROM materials WHERE category=? "
                                "AND COALESCE(hidden,0)=0 "
                                "AND COALESCE(thickness,'')=''", (cat,)).fetchone()["c"]
        else:
            total = con.execute("SELECT COUNT(*) c FROM materials WHERE category=? "
                                "AND COALESCE(hidden,0)=0 "
                                "AND COALESCE(thickness,'')=?", (cat, th)).fetchone()["c"]
    if total == 0:
        send(vk, user_id, "Нет материалов.", main_menu(user_id)); return
    pages = (total + per_page - 1) // per_page
    if page < 1: page = 1
    if page > pages: page = pages
    offset = (page - 1) * per_page
    with db() as con:
        if th is None:
            rows = con.execute("SELECT * FROM materials WHERE category=? "
                               "AND COALESCE(hidden,0)=0 "
                               "ORDER BY decor, name LIMIT ? OFFSET ?",
                               (cat, per_page, offset)).fetchall()
        elif th == "__none__":
            rows = con.execute("SELECT * FROM materials WHERE category=? "
                               "AND COALESCE(hidden,0)=0 "
                               "AND COALESCE(thickness,'')='' "
                               "ORDER BY decor, name LIMIT ? OFFSET ?",
                               (cat, per_page, offset)).fetchall()
        else:
            rows = con.execute("SELECT * FROM materials WHERE category=? "
                               "AND COALESCE(hidden,0)=0 "
                               "AND COALESCE(thickness,'')=? "
                               "ORDER BY decor, name LIMIT ? OFFSET ?",
                               (cat, th, per_page, offset)).fetchall()
    st = get_state(user_id)
    cart = list(st["data"].get("cart", []))
    default_plan = st["data"].get("default_plan", "")
    kb = VkKeyboard(one_time=False)
    for i, m in enumerate(rows):
        if i > 0: kb.add_line()
        label = m["decor"] or m["name"]
        kb.add_callback_button(f"{label} — {m['qty']:g} {m['unit']}",
                               color=VkKeyboardColor.PRIMARY,
                               payload={"command": f"cart_add:{m['id']}"})
    kb.add_line()
    if page > 1:
        kb.add_callback_button("◀️", color=VkKeyboardColor.SECONDARY,
                               payload={"command": f"cart_mat_page:{cat}:{th}:{page-1}"})
    kb.add_callback_button(f"{page}/{pages}", color=VkKeyboardColor.SECONDARY,
                           payload={"command": "noop"})
    if page < pages:
        kb.add_callback_button("▶️", color=VkKeyboardColor.SECONDARY,
                               payload={"command": f"cart_mat_page:{cat}:{th}:{page+1}"})
    kb.add_line()
    if cart:
        kb.add_callback_button("🛒 Корзина", color=VkKeyboardColor.POSITIVE,
                               payload={"command": "cart_show"})
        kb.add_line()
    if th is not None:
        kb.add_callback_button("⬅️ К толщинам", color=VkKeyboardColor.SECONDARY,
                               payload={"command": f"cart_back_thick:{cat}"})
    else:
        kb.add_callback_button("⬅️ К категориям", color=VkKeyboardColor.SECONDARY,
                               payload={"command": "cart_back_cat"})
    kb.add_line()
    kb.add_button("⬅️ В меню", color=VkKeyboardColor.SECONDARY)
    set_state(user_id, "cart_choose", cart=cart, default_plan=default_plan,
              category=cat, thickness=th)
    if th == "__none__":
        title = f"{cat_label(cat)} · без толщины"
    else:
        title = f"{cat_label(cat)}" + (f" {th}" if th else "")
    send(vk, user_id, f"{title}. Выберите:", kb.get_keyboard())


def add_to_cart(vk, user_id, mid):
    m = get_material(mid)
    if not m:
        send(vk, user_id, "Материал не найден."); return
    st = get_state(user_id)
    cart = list(st["data"].get("cart", []))
    default_plan = st["data"].get("default_plan", "")
    cart.append({"material_id": mid, "qty": None, "plan": None})
    idx = len(cart) - 1
    set_state(user_id, "cart_qty", cart=cart, editing_index=idx,
              default_plan=default_plan)
    kb = VkKeyboard(one_time=False)
    kb.add_callback_button("⬅️ Назад", color=VkKeyboardColor.SECONDARY,
                           payload={"command": "cart_back_add"})
    kb.add_line()
    kb.add_button("⬅️ В меню", color=VkKeyboardColor.SECONDARY)
    send(vk, user_id,
         f"🛒 Добавлено: {full_label(m)}\nОстаток: {m['qty']:g} {m['unit']}\n\n"
         f"Введите количество:", kb.get_keyboard())


def show_cart(vk, user_id):
    st = get_state(user_id)
    cart = st["data"].get("cart", [])
    if not cart:
        send(vk, user_id, "Корзина пуста.", main_menu(user_id)); return
    lines = ["🛒 ВАША ЗАЯВКА", ""]
    for i, it in enumerate(cart, 1):
        m = get_material(it["material_id"])
        qty = it["qty"]
        plan = it.get("plan") or "—"
        label = full_label(m) if m else "?"
        is_hidden = m and m["hidden"] and (m["category"] or "") == "edge"
        is_edge_main = is_hidden and "клей" not in (m["name"] or "").lower()
        if is_edge_main:
            lines.append(f"{i}. {label} (план {plan})")
        else:
            qty_str = f"{qty:g}" if qty is not None else "?"
            unit = (m["unit"] if m else "") or ""
            lines.append(f"{i}. {label} — {qty_str} {unit} (план {plan})")
    kb = VkKeyboard(one_time=False)
    kb.add_callback_button("➕ Добавить", color=VkKeyboardColor.PRIMARY,
                           payload={"command": "cart_more"})
    kb.add_callback_button("🗑 Очистить", color=VkKeyboardColor.NEGATIVE,
                           payload={"command": "cart_clear"})
    kb.add_line()
    kb.add_callback_button("✅ Отправить заявку", color=VkKeyboardColor.POSITIVE,
                           payload={"command": "cart_submit"})
    kb.add_line()
    kb.add_button("⬅️ В меню", color=VkKeyboardColor.SECONDARY)
    send(vk, user_id, "\n".join(lines), kb.get_keyboard())


def show_edge_plan_page(vk, user_id, page=1):
    items, base = plan_page_items(page)
    if not items:
        send(vk, user_id, "Нет номеров."); return
    last = last_plan_number()
    st = get_state(user_id)
    default_plan = st["data"].get("default_plan", "")
    hint = f"Последний план: {last}" if last else "Раньше планов не было"
    kb = VkKeyboard(one_time=False)
    for i, n in enumerate(items):
        if i % 2 == 0 and i > 0: kb.add_line()
        kb.add_callback_button(str(n), color=VkKeyboardColor.PRIMARY,
                               payload={"command": f"edge_plan:{n}"})
    kb.add_line()
    if page > 1:
        kb.add_callback_button("◀️ -10", color=VkKeyboardColor.SECONDARY,
                               payload={"command": f"edge_plan_page:{page-1}"})
    if items[-1] < PLAN_MAX:
        kb.add_callback_button("+10 ▶️", color=VkKeyboardColor.SECONDARY,
                               payload={"command": f"edge_plan_page:{page+1}"})
    kb.add_line()
    kb.add_callback_button("🔢 Вручную", color=VkKeyboardColor.PRIMARY,
                           payload={"command": "edge_plan_manual"})
    kb.add_line()
    kb.add_callback_button("⬅️ К категориям", color=VkKeyboardColor.SECONDARY,
                           payload={"command": "cart_back_cat"})
    kb.add_line()
    kb.add_button("⬅️ В меню", color=VkKeyboardColor.SECONDARY)
    set_state(user_id, "edge_plan", cart=list(st["data"].get("cart", [])),
              default_plan=default_plan, page=page)
    send(vk, user_id, f"📏 Кромка. Шаг 1. Выберите план.\n{hint}\n"
                      f"Диапазон {items[0]}–{items[-1]}.", kb.get_keyboard())


def show_edge_glue_question(vk, user_id, plan):
    kb = VkKeyboard(one_time=False)
    kb.add_callback_button("✅ Да, с клеем", color=VkKeyboardColor.POSITIVE,
                           payload={"command": "edge_glue:yes"})
    kb.add_callback_button("❌ Нет, без клея", color=VkKeyboardColor.SECONDARY,
                           payload={"command": "edge_glue:no"})
    kb.add_line()
    kb.add_callback_button("⬅️ К выбору плана", color=VkKeyboardColor.SECONDARY,
                           payload={"command": "edge_plan_again"})
    kb.add_line()
    kb.add_button("⬅️ В меню", color=VkKeyboardColor.SECONDARY)
    send(vk, user_id,
         f"📏 Кромка, план {plan}.\n\n"
         f"🧴 Кромочный клей нужен?",
         kb.get_keyboard())


def show_plan_page(vk, user_id, page=1):
    items, base = plan_page_items(page)
    if not items:
        send(vk, user_id, "Нет номеров."); return
    last = last_plan_number()
    st = get_state(user_id)
    default_plan = st["data"].get("default_plan", "")
    hint = f"Последний план: {last}" if last else "Раньше планов не было"
    kb = VkKeyboard(one_time=False)
    for i, n in enumerate(items):
        if i % 2 == 0 and i > 0: kb.add_line()
        kb.add_callback_button(str(n), color=VkKeyboardColor.PRIMARY,
                               payload={"command": f"plan_pick:{n}"})
    kb.add_line()
    if page > 1:
        kb.add_callback_button("◀️ -10", color=VkKeyboardColor.SECONDARY,
                               payload={"command": f"plan_page:{page-1}"})
    if items[-1] < PLAN_MAX:
        kb.add_callback_button("+10 ▶️", color=VkKeyboardColor.SECONDARY,
                               payload={"command": f"plan_page:{page+1}"})
    kb.add_line()
    kb.add_callback_button("🔢 Вручную", color=VkKeyboardColor.PRIMARY,
                           payload={"command": "plan_manual"})
    if default_plan:
        kb.add_callback_button(f"⏭ План {default_plan}",
                               color=VkKeyboardColor.POSITIVE,
                               payload={"command": "plan_skip"})
    kb.add_line()
    kb.add_callback_button("⬅️ В корзину", color=VkKeyboardColor.SECONDARY,
                           payload={"command": "cart_show"})
    kb.add_line()
    kb.add_button("⬅️ В меню", color=VkKeyboardColor.SECONDARY)
    set_state(user_id, "cart_plan", cart=list(st["data"].get("cart", [])),
              default_plan=default_plan, page=page,
              editing_index=st["data"].get("editing_index", -1))
    send(vk, user_id, f"📋 Шаг 3. Выберите план для позиции.\n{hint}\n"
                      f"Диапазон {items[0]}–{items[-1]}.", kb.get_keyboard())


def submit_cart(vk, user_id):
    st = get_state(user_id)
    cart = st["data"].get("cart", [])
    if not cart:
        send(vk, user_id, "Корзина пуста.", main_menu(user_id)); return
    default_plan = st["data"].get("default_plan", "")
    for it in cart:
        m = get_material(it["material_id"])
        is_hidden_edge = m and m["hidden"] and (m["category"] or "") == "edge"
        if not is_hidden_edge:
            if it["qty"] is None or it["qty"] <= 0:
                send(vk, user_id, "❗ У некоторых позиций не указано количество."); return
        if not it.get("plan") and default_plan and not is_hidden_edge:
            it["plan"] = default_plan

    auto_issue = _all_board(cart)
    init_status = "issued" if auto_issue else "new"
    plans = sorted({str(it.get("plan") or "") for it in cart if it.get("plan")})
    plan_str = ", ".join(plans) if plans else ""

    with db() as con:
        cur = con.execute(
            "INSERT INTO requests (user_id,material_id,qty,plan,comment,"
            "status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (user_id, cart[0]["material_id"],
             sum(i["qty"] or 0 for i in cart), plan_str, "", init_status,
             now_str(), now_str()))
        rid = cur.lastrowid
        for it in cart:
            con.execute("INSERT INTO request_items (request_id,material_id,qty,plan,status) "
                        "VALUES (?,?,?,?,?)",
                        (rid, it["material_id"], it["qty"] or 0, it.get("plan") or "",
                         init_status))
        minus_lines = []
        if auto_issue:
            for it in cart:
                m = con.execute("SELECT * FROM materials WHERE id=?",
                                (it["material_id"],)).fetchone()
                if not m: continue
                new_qty = m["qty"] - it["qty"]
                con.execute("UPDATE materials SET qty=?, updated_at=? WHERE id=?",
                            (new_qty, now_str(), m["id"]))
                if new_qty < 0:
                    minus_lines.append(f"{m['name']}: {new_qty:g} {m['unit']}")
        con.commit()

    clear_state(user_id)
    log_action(user_id, f"Создана заявка №{rid}",
               f"{len(cart)} поз., планы: {plan_str}, "
               f"{'автосписание' if auto_issue else 'ждёт выдачи'}")

    for it in cart:
        if it.get("plan"):
            try: register_plan(int(it["plan"]))
            except: pass

    user = get_user(user_id)
    author = user["full_name"] if user else str(user_id)

    if auto_issue:
        lines = [f"✅ Заявка №{rid} — списано автоматически", ""]
        for i, it in enumerate(cart, 1):
            m = get_material(it["material_id"])
            plan = it.get("plan") or "—"
            lines.append(f"{i}. {full_label(m)} — {it['qty']:g} {m['unit']} (план {plan})")
        if minus_lines:
            lines.append("")
            lines.append("⚠️ Ушло в минус:")
            lines += [f"  {x}" for x in minus_lines]
        send(vk, user_id, "\n".join(lines), main_menu(user_id))
        notif = [f"📦 Заявка №{rid} (пиломатериалы) — списано автоматически", "",
                 f"От: {author}"]
        for i, it in enumerate(cart, 1):
            m = get_material(it["material_id"])
            notif.append(f"{i}. {full_label(m)} — {it['qty']:g} {m['unit']}")
        notify_board_and_warehouse(vk, "\n".join(notif))
    else:
        edge_only = True
        plan_card = ""
        glue_needed_card = False
        glue_qty_card = 0.0
        for it in cart:
            m = get_material(it["material_id"])
            if not m or not m["hidden"] or (m["category"] or "") != "edge":
                edge_only = False
                break
            name = (m["name"] or "").lower()
            if "клей" in name:
                glue_needed_card = True
                glue_qty_card = float(it["qty"] or 0)
            else:
                plan_card = it.get("plan") or ""

        if edge_only:
            lines_n = [
                f"📏 НОВАЯ ЗАЯВКА НА КРОМКУ №{rid}",
                "",
                f"👤 От: {author}",
                f"📋 План: {plan_card or '—'}",
                "",
            ]
            if glue_needed_card and glue_qty_card > 0:
                lines_n.append(f"🧴 Клей кромочный: НУЖЕН, {glue_qty_card:g} канистр")
            else:
                lines_n.append("🧴 Клей кромочный: НЕ НУЖЕН")
            lines_n += ["", "Откройте 📥 Заявки для подтверждения."]
            notify_warehouse(vk, "\n".join(lines_n))

            lines = [f"✅ Заявка на кромку №{rid} отправлена", ""]
            lines.append(f"📋 План: {plan_card or '—'}")
            if glue_needed_card and glue_qty_card > 0:
                lines.append(f"🧴 Клей кромочный: {glue_qty_card:g} канистр")
            else:
                lines.append("🧴 Без клея")
            lines += ["", "Кладовщик подтвердит выдачу."]
            send(vk, user_id, "\n".join(lines), main_menu(user_id))
        else:
            lines = [f"✅ Заявка №{rid} отправлена на выдачу", ""]
            for i, it in enumerate(cart, 1):
                m = get_material(it["material_id"])
                plan = it.get("plan") or "—"
                if m and m["hidden"] and (m["category"] or "") == "edge":
                    if "клей" in (m["name"] or "").lower():
                        lines.append(f"{i}. {full_label(m)} — {it['qty']:g} {m['unit']} (план {plan})")
                    else:
                        lines.append(f"{i}. {full_label(m)} (план {plan})")
                else:
                    lines.append(f"{i}. {full_label(m)} — {it['qty']:g} {m['unit']} (план {plan})")
            lines += ["", "Кладовщик подтвердит выдачу."]
            send(vk, user_id, "\n".join(lines), main_menu(user_id))
            notif = [f"🔔 Новая заявка №{rid} (кромка/плёнка)", ""]
            for i, it in enumerate(cart, 1):
                m = get_material(it["material_id"])
                plan = it.get("plan") or "—"
                if m and m["hidden"] and (m["category"] or "") == "edge":
                    if "клей" in (m["name"] or "").lower():
                        notif.append(f"{i}. {full_label(m)} — {it['qty']:g} {m['unit']} (план {plan})")
                    else:
                        notif.append(f"{i}. {full_label(m)} (план {plan})")
                else:
                    notif.append(f"{i}. {full_label(m)} — {it['qty']:g} {m['unit']} (план {plan})")
            notif += ["", f"От: {author}"]
            notify_warehouse(vk, "\n".join(notif))


def show_my_requests(vk, user_id):
    with db() as con:
        rows = con.execute(
            """SELECT r.*, m.name AS m_name, m.unit AS m_unit,
                      m.category AS m_cat,
                      (SELECT COUNT(*) FROM request_items WHERE request_id=r.id) AS cnt
               FROM requests r
               LEFT JOIN materials m ON m.id=r.material_id
               WHERE r.user_id=? ORDER BY r.id DESC LIMIT 15""",
            (user_id,)).fetchall()
    if not rows:
        send(vk, user_id, "У вас нет заявок.", back_kb()); return
    lines = ["📄 Мои заявки", ""]
    for r in rows:
        cnt = r["cnt"] or 0
        tag = f" ({cnt} поз.)" if cnt else ""
        cats = _request_cats_words(r["id"])
        name = r["m_name"] or "(удалён)"
        lines.append(f"№{r['id']}{tag} • [{cats}] {name} — {r['qty']:g}\n"
                     f"   План: {r['plan'] or '—'}\n"
                     f"   {STATUS.get(r['status'], r['status'])} • {r['created_at'][:16]}")
    send(vk, user_id, "\n".join(lines), back_kb())


def show_active_requests(vk, user_id, page=1):
    per_page = 7
    with db() as con:
        total = con.execute("SELECT COUNT(*) c FROM requests "
                            "WHERE status IN ('new','approved')").fetchone()["c"]
        if total == 0:
            send(vk, user_id, "📥 Активных заявок нет.", back_kb()); return
        pages = (total + per_page - 1) // per_page
        if page < 1: page = 1
        if page > pages: page = pages
        offset = (page - 1) * per_page
        rows = con.execute(
            """SELECT r.*, u.full_name,
                      (SELECT COUNT(*) FROM request_items WHERE request_id=r.id) AS cnt
               FROM requests r
               LEFT JOIN users u ON u.user_id=r.user_id
               WHERE r.status IN ('new','approved')
               ORDER BY r.id DESC LIMIT ? OFFSET ?""",
            (per_page, offset)).fetchall()
    kb = VkKeyboard(one_time=False)
    for i, r in enumerate(rows):
        if i > 0: kb.add_line()
        cnt = r["cnt"] or 0
        tag = f"{cnt} поз." if cnt else f"{r['qty']:g} шт"
        cats = _request_cats_words(r["id"])
        author = (r["full_name"] or str(r["user_id"])).split()[0]
        kb.add_callback_button(
            f"№{r['id']} [{cats}] {author} • {tag}",
            color=VkKeyboardColor.PRIMARY,
            payload={"command": f"req_view:{r['id']}"})
    kb.add_line()
    if page > 1:
        kb.add_callback_button("◀️", color=VkKeyboardColor.SECONDARY,
                               payload={"command": f"req_page:{page-1}"})
    kb.add_callback_button(f"{page}/{pages}", color=VkKeyboardColor.SECONDARY,
                           payload={"command": "noop"})
    if page < pages:
        kb.add_callback_button("▶️", color=VkKeyboardColor.SECONDARY,
                               payload={"command": f"req_page:{page+1}"})
    kb.add_line()
    kb.add_button("⬅️ В меню", color=VkKeyboardColor.SECONDARY)
    send(vk, user_id,
         f"📥 Активных: {total} (стр. {page}/{pages})\n\nВыберите заявку:",
         kb.get_keyboard())


def _render_edge_card(vk, user_id, r, items):
    plan, glue_needed, glue_qty = _edge_request_data(items)
    author = r["author_name"] or r["user_id"]
    lines = [
        "📏 ЗАЯВКА НА КРОМКУ",
        f"№{r['id']}",
        "",
        f"👤 От: {author}",
        f"🕒 {r['created_at'][:16]}",
        f"📋 План: {plan or '—'}",
        "",
        "Требуется выдать кромку.",
        "",
    ]
    if glue_needed and glue_qty > 0:
        lines.append(f"🧴 Клей кромочный: НУЖЕН, {glue_qty:g} канистр")
    else:
        lines.append("🧴 Клей кромочный: НЕ НУЖЕН")
    lines += ["", f"Статус: {STATUS.get(r['status'], r['status'])}"]

    kb = VkKeyboard(one_time=False)
    if is_warehouse_strict(user_id) and r["status"] in ("new", "approved"):
        kb.add_callback_button("✅ Выдал", color=VkKeyboardColor.POSITIVE,
                               payload={"command": f"wh_issue:{r['id']}"})
        kb.add_callback_button("❌ Не выдал", color=VkKeyboardColor.NEGATIVE,
                               payload={"command": f"wh_reject:{r['id']}"})
        kb.add_line()
    kb.add_callback_button("⬅️ К списку", color=VkKeyboardColor.SECONDARY,
                           payload={"command": "wh_requests"})
    send(vk, user_id, "\n".join(lines), kb.get_keyboard())


def show_request_details(vk, user_id, rid):
    with db() as con:
        r = con.execute(
            """SELECT r.*, u.full_name AS author_name
               FROM requests r LEFT JOIN users u ON u.user_id=r.user_id
               WHERE r.id=?""", (rid,)).fetchone()
        if not r:
            send(vk, user_id, "Заявка не найдена.", back_kb()); return
        items = con.execute(
            """SELECT ri.*, m.name AS m_name, m.unit AS m_unit,
                      m.qty AS stock, m.category AS m_cat,
                      m.thickness AS m_th, m.decor AS m_decor
               FROM request_items ri
               LEFT JOIN materials m ON m.id=ri.material_id
               WHERE ri.request_id=?""", (rid,)).fetchall()

    if _is_edge_only_request(items):
        _render_edge_card(vk, user_id, r, items)
        return

    cats_line = _cats_words_for_items(items)
    lines = [f"📋 Заявка №{r['id']} · {cats_line}", ""]
    if items:
        for i, it in enumerate(items, 1):
            stock = it["stock"] if it["stock"] is not None else 0
            plan = it["plan"] if it["plan"] else "—"
            name = it["m_name"] or "?"
            th = it["m_th"] or ""
            dec = it["m_decor"] or ""
            label = " ".join(p for p in [th, dec] if p) or name
            cat = it["m_cat"] or ""
            is_hidden = (cat == "edge" and not th and not dec
                         and (name or "").strip() in ("Кромка", "Клей кромочный"))
            is_edge_main = is_hidden and "клей" not in (name or "").lower()
            if is_edge_main:
                lines.append(f"{i}. [{cat_word(cat)}] {label} (план {plan})")
            else:
                warn = ""
                if cat and cat != "edge" and stock < it["qty"]:
                    warn = " ⚠️"
                lines.append(f"{i}. [{cat_word(cat)}] {label} — "
                             f"{it['qty']:g} {it['m_unit'] or ''} (план {plan}){warn}")
    else:
        m = get_material(r["material_id"])
        lines.append(f"• {full_label(m) if m else '?'} — {r['qty']:g}")

    lines += ["",
              f"📋 План(ы): {r['plan'] or '—'}",
              f"👤 От: {r['author_name'] or r['user_id']}",
              f"🕒 {r['created_at'][:16]}",
              f"Статус: {STATUS.get(r['status'], r['status'])}"]

    kb = VkKeyboard(one_time=False)
    if is_warehouse_strict(user_id) and r["status"] in ("new", "approved"):
        kb.add_callback_button("✅ Выдал", color=VkKeyboardColor.POSITIVE,
                               payload={"command": f"wh_issue:{rid}"})
        kb.add_callback_button("❌ Не выдал", color=VkKeyboardColor.NEGATIVE,
                               payload={"command": f"wh_reject:{rid}"})
        kb.add_line()
    kb.add_callback_button("⬅️ К списку", color=VkKeyboardColor.SECONDARY,
                           payload={"command": "wh_requests"})
    send(vk, user_id, "\n".join(lines), kb.get_keyboard())
          

# ======================= ВЫДАЧА / ОТКЛОНЕНИЕ =======================
def issue_request(vk, user_id, rid):
    with db() as con:
        r = con.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
        if not r or r["status"] not in ("new", "approved"):
            send(vk, user_id, "Заявка уже обработана.", back_kb()); return
        items = con.execute("SELECT * FROM request_items WHERE request_id=?",
                            (rid,)).fetchall()
        summary, minus_lines = [], []
        if items:
            for it in items:
                m = con.execute("SELECT * FROM materials WHERE id=?",
                                (it["material_id"],)).fetchone()
                if not m:
                    summary.append(f"ID {it['material_id']} — удалён"); continue
                if m["hidden"]:
                    nm = (m["name"] or "").lower()
                    if "клей" in nm:
                        summary.append(f"{full_label(m)} — {it['qty']:g} {m['unit']}")
                    else:
                        summary.append(f"{full_label(m)}")
                    continue
                new_qty = m["qty"] - it["qty"]
                con.execute("UPDATE materials SET qty=?, updated_at=? WHERE id=?",
                            (new_qty, now_str(), m["id"]))
                summary.append(f"{full_label(m)} — {it['qty']:g} {m['unit']}")
                if new_qty < 0:
                    minus_lines.append(f"{m['name']}: {new_qty:g} {m['unit']}")
        else:
            m = con.execute("SELECT * FROM materials WHERE id=?",
                            (r["material_id"],)).fetchone()
            if m and not m["hidden"]:
                new_qty = m["qty"] - r["qty"]
                con.execute("UPDATE materials SET qty=?, updated_at=? WHERE id=?",
                            (new_qty, now_str(), m["id"]))
                summary.append(f"{full_label(m)} — {r['qty']:g} {m['unit']}")
                if new_qty < 0:
                    minus_lines.append(f"{m['name']}: {new_qty:g} {m['unit']}")
        con.execute("UPDATE requests SET status='issued', updated_at=? WHERE id=?",
                    (now_str(), rid))
        con.commit()
    log_action(user_id, f"Выдана заявка №{rid}", "; ".join(summary))
    txt = f"✅ Заявка №{rid} — «Выдал»\n" + "\n".join(summary)
    if minus_lines:
        txt += "\n\n⚠️ Ушло в минус:\n" + "\n".join(minus_lines)
    send(vk, user_id, txt)
    try:
        send(vk, r["user_id"], f"✅ Заявка №{rid} выполнена:\n" + "\n".join(summary))
    except Exception: pass


def reject_request(vk, user_id, rid):
    with db() as con:
        r = con.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
        if not r or r["status"] not in ("new", "approved"):
            send(vk, user_id, "Заявка уже обработана.", back_kb()); return
        con.execute("UPDATE requests SET status='rejected', updated_at=? WHERE id=?",
                    (now_str(), rid))
        con.commit()
    log_action(user_id, f"Отклонена заявка №{rid}")
    send(vk, user_id, f"❌ Заявка №{rid} — «Не выдал».")
    try:
        send(vk, r["user_id"], f"❌ Заявка №{rid} — не выдана.")
    except Exception: pass


# ======================= ПРИХОД =======================
def show_inc_list(vk, user_id, mass=False):
    cats = distinct_categories()
    if not cats:
        send(vk, user_id, "Склад пуст.", main_menu(user_id)); return
    header = "📦 МАССОВЫЙ ПРИХОД" if mass else "➕ ПРИХОД"
    prefix = "minc" if mass else "inc"
    kb = VkKeyboard(one_time=False)
    for i, c in enumerate(cats):
        if i > 0: kb.add_line()
        kb.add_callback_button(cat_label(c), color=VkKeyboardColor.PRIMARY,
                               payload={"command": f"{prefix}_cat:{c}"})
    kb.add_line()
    if mass:
        kb.add_callback_button("✅ Завершить", color=VkKeyboardColor.POSITIVE,
                               payload={"command": "minc_done"})
        kb.add_line()
    kb.add_button("⬅️ В меню", color=VkKeyboardColor.SECONDARY)
    send(vk, user_id, f"{header}. Выберите категорию:", kb.get_keyboard())


def show_inc_list_in_cat(vk, user_id, cat, mass=False, page=1):
    per_page = 6
    with db() as con:
        total = con.execute("SELECT COUNT(*) c FROM materials WHERE category=? "
                            "AND COALESCE(hidden,0)=0", (cat,)).fetchone()["c"]
    if total == 0:
        send(vk, user_id, "В этой категории нет материалов.", back_kb()); return
    pages = (total + per_page - 1) // per_page
    if page < 1: page = 1
    if page > pages: page = pages
    offset = (page - 1) * per_page
    with db() as con:
        rows = con.execute("SELECT * FROM materials WHERE category=? "
                           "AND COALESCE(hidden,0)=0 "
                           "ORDER BY thickness, decor, name LIMIT ? OFFSET ?",
                           (cat, per_page, offset)).fetchall()
    prefix = "minc" if mass else "inc"
    header = "📦 МАССОВЫЙ ПРИХОД" if mass else "➕ ПРИХОД"
    kb = VkKeyboard(one_time=False)
    for i, r in enumerate(rows):
        if i > 0: kb.add_line()
        lbl = r["decor"] or r["name"]
        if r["thickness"]: lbl = f"{r['thickness']} {lbl}"
        kb.add_callback_button(f"{lbl} ({r['qty']:g})",
                               color=VkKeyboardColor.PRIMARY,
                               payload={"command": f"{prefix}_mat:{r['id']}"})
    kb.add_line()
    if page > 1:
        kb.add_callback_button("◀️", color=VkKeyboardColor.SECONDARY,
                               payload={"command": f"{prefix}_page:{cat}:{page-1}"})
    kb.add_callback_button(f"{page}/{pages}", color=VkKeyboardColor.SECONDARY,
                           payload={"command": "noop"})
    if page < pages:
        kb.add_callback_button("▶️", color=VkKeyboardColor.SECONDARY,
                               payload={"command": f"{prefix}_page:{cat}:{page+1}"})
    kb.add_line()
    if mass:
        kb.add_callback_button("✅ Завершить", color=VkKeyboardColor.POSITIVE,
                               payload={"command": "minc_done"})
        kb.add_line()
    back_cmd = "minc_start" if mass else "inc_list"
    kb.add_callback_button("⬅️ К категориям", color=VkKeyboardColor.SECONDARY,
                           payload={"command": back_cmd})
    kb.add_line()
    kb.add_button("⬅️ В меню", color=VkKeyboardColor.SECONDARY)
    send(vk, user_id, f"{header} · {cat_label(cat)}:", kb.get_keyboard())


def show_mass_inc_cart(vk, user_id):
    st = get_state(user_id)
    cart = st["data"].get("mass_cart", [])
    if not cart:
        send(vk, user_id, "Приход пуст.", main_menu(user_id)); return
    lines = ["📦 ПРИХОД (текущий)", ""]
    for i, it in enumerate(cart, 1):
        m = get_material(it["material_id"])
        lines.append(f"{i}. {full_label(m) if m else '?'} — +{it['qty']:g}")
    kb = VkKeyboard(one_time=False)
    kb.add_callback_button("➕ Ещё", color=VkKeyboardColor.PRIMARY,
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
        send(vk, user_id, "Приход пуст.", main_menu(user_id)); return
    with db() as con:
        for it in cart:
            con.execute("UPDATE materials SET qty=qty+?, updated_at=? WHERE id=?",
                        (it["qty"], now_str(), it["material_id"]))
        con.commit()
    lines = ["✅ Приход оформлен", ""]
    for it in cart:
        m = get_material(it["material_id"])
        lines.append(f"{full_label(m)}: +{it['qty']:g} {m['unit']}")
    clear_state(user_id)
    log_action(user_id, "Массовый приход", "; ".join(
        f"{get_material(i['material_id'])['name']} +{i['qty']:g}" for i in cart))
    send(vk, user_id, "\n".join(lines), main_menu(user_id))


# ======================= РЕДАКТИРОВАНИЕ =======================
EDIT_FIELDS = {"name": "📝 Название", "category": "📂 Категория",
               "thickness": "📏 Толщина", "decor": "🎨 Декор/Цвет",
               "unit": "📐 Единица"}


def show_edit_list(vk, user_id):
    cats = distinct_categories()
    if not cats:
        send(vk, user_id, "Склад пуст.", back_kb()); return
    kb = VkKeyboard(one_time=False)
    for i, c in enumerate(cats):
        if i > 0: kb.add_line()
        kb.add_callback_button(cat_label(c), color=VkKeyboardColor.PRIMARY,
                               payload={"command": f"edit_cat:{c}"})
    kb.add_line()
    kb.add_button("⬅️ В меню", color=VkKeyboardColor.SECONDARY)
    send(vk, user_id, "✏️ Редактировать. Выберите категорию:", kb.get_keyboard())


def show_edit_list_in_cat(vk, user_id, cat, page=1):
    per_page = 6
    with db() as con:
        total = con.execute("SELECT COUNT(*) c FROM materials WHERE category=? "
                            "AND COALESCE(hidden,0)=0", (cat,)).fetchone()["c"]
    if total == 0:
        send(vk, user_id, "В этой категории нет материалов.", back_kb()); return
    pages = (total + per_page - 1) // per_page
    if page < 1: page = 1
    if page > pages: page = pages
    offset = (page - 1) * per_page
    with db() as con:
        rows = con.execute("SELECT * FROM materials WHERE category=? "
                           "AND COALESCE(hidden,0)=0 "
                           "ORDER BY thickness, decor, name LIMIT ? OFFSET ?",
                           (cat, per_page, offset)).fetchall()
    kb = VkKeyboard(one_time=False)
    for i, r in enumerate(rows):
        if i > 0: kb.add_line()
        kb.add_callback_button(f"✏️ {material_label(r)}",
                               color=VkKeyboardColor.PRIMARY,
                               payload={"command": f"edit:{r['id']}"})
    kb.add_line()
    if page > 1:
        kb.add_callback_button("◀️", color=VkKeyboardColor.SECONDARY,
                               payload={"command": f"edit_page:{cat}:{page-1}"})
    kb.add_callback_button(f"{page}/{pages}", color=VkKeyboardColor.SECONDARY,
                           payload={"command": "noop"})
    if page < pages:
        kb.add_callback_button("▶️", color=VkKeyboardColor.SECONDARY,
                               payload={"command": f"edit_page:{cat}:{page+1}"})
    kb.add_line()
    kb.add_callback_button("⬅️ К категориям", color=VkKeyboardColor.SECONDARY,
                           payload={"command": "edit_list"})
    kb.add_line()
    kb.add_button("⬅️ В меню", color=VkKeyboardColor.SECONDARY)
    send(vk, user_id, f"✏️ {cat_label(cat)} — выберите материал:", kb.get_keyboard())


def show_edit_fields(vk, user_id, mid):
    m = get_material(mid)
    if not m:
        send(vk, user_id, "Материал не найден.", back_kb()); return
    kb = VkKeyboard(one_time=False)
    first = True
    for key, label in EDIT_FIELDS.items():
        val = m[key] if key in m.keys() else ""
        if key == "category":
            val = cat_label(val) if val in CATEGORIES else val
        if not first: kb.add_line()
        first = False
        kb.add_callback_button(f"{label}: {val if val not in (None, '') else '—'}",
                               color=VkKeyboardColor.PRIMARY,
                               payload={"command": f"editf:{mid}:{key}"})
    kb.add_line()
    cat = m["category"] or "board"
    kb.add_callback_button("⬅️ К списку", color=VkKeyboardColor.SECONDARY,
                           payload={"command": f"edit_cat:{cat}"})
    kb.add_line()
    kb.add_button("⬅️ В меню", color=VkKeyboardColor.SECONDARY)
    send(vk, user_id, f"✏️ {full_label(m)}\nВыберите поле:", kb.get_keyboard())


def do_edit(vk, user_id, mid, field, value):
    m = get_material(mid)
    if not m:
        send(vk, user_id, "Материал не найден.", main_menu(user_id)); return
    if field == "category" and value not in CATEGORIES:
        send(vk, user_id, "❗ Неизвестная категория."); return
    with db() as con:
        con.execute(f"UPDATE materials SET {field}=?, updated_at=? WHERE id=?",
                    (value, now_str(), mid))
        con.commit()
    clear_state(user_id)
    log_action(user_id, f"Изменён материал ID {mid}", f"{field}={value}")
    show_val = cat_label(value) if field == "category" else (value or "—")
    send(vk, user_id, f"✅ Изменено\n{m['name']}\n{EDIT_FIELDS.get(field, field)}: "
                      f"{show_val}", main_menu(user_id))


# ======================= УДАЛЕНИЕ =======================
def show_delete_list(vk, user_id):
    cats = distinct_categories()
    if not cats:
        send(vk, user_id, "Склад пуст.", back_kb()); return
    kb = VkKeyboard(one_time=False)
    for i, c in enumerate(cats):
        if i > 0: kb.add_line()
        kb.add_callback_button(cat_label(c), color=VkKeyboardColor.NEGATIVE,
                               payload={"command": f"del_cat:{c}"})
    kb.add_line()
    kb.add_button("⬅️ В меню", color=VkKeyboardColor.SECONDARY)
    send(vk, user_id, "🗑 Удалить. Выберите категорию:", kb.get_keyboard())


def show_delete_list_in_cat(vk, user_id, cat, page=1):
    per_page = 6
    with db() as con:
        total = con.execute("SELECT COUNT(*) c FROM materials WHERE category=? "
                            "AND COALESCE(hidden,0)=0", (cat,)).fetchone()["c"]
    if total == 0:
        send(vk, user_id, "В этой категории нет материалов.", back_kb()); return
    pages = (total + per_page - 1) // per_page
    if page < 1: page = 1
    if page > pages: page = pages
    offset = (page - 1) * per_page
    with db() as con:
        rows = con.execute("SELECT * FROM materials WHERE category=? "
                           "AND COALESCE(hidden,0)=0 "
                           "ORDER BY thickness, decor, name LIMIT ? OFFSET ?",
                           (cat, per_page, offset)).fetchall()
    kb = VkKeyboard(one_time=False)
    for i, r in enumerate(rows):
        if i > 0: kb.add_line()
        kb.add_callback_button(f"🗑 {material_label(r)}",
                               color=VkKeyboardColor.NEGATIVE,
                               payload={"command": f"delmat:{r['id']}"})
    kb.add_line()
    if page > 1:
        kb.add_callback_button("◀️", color=VkKeyboardColor.SECONDARY,
                               payload={"command": f"del_page:{cat}:{page-1}"})
    kb.add_callback_button(f"{page}/{pages}", color=VkKeyboardColor.SECONDARY,
                           payload={"command": "noop"})
    if page < pages:
        kb.add_callback_button("▶️", color=VkKeyboardColor.SECONDARY,
                               payload={"command": f"del_page:{cat}:{page+1}"})
    kb.add_line()
    kb.add_callback_button("⬅️ К категориям", color=VkKeyboardColor.SECONDARY,
                           payload={"command": "del_list"})
    kb.add_line()
    kb.add_button("⬅️ В меню", color=VkKeyboardColor.SECONDARY)
    send(vk, user_id, f"🗑 {cat_label(cat)} — выберите материал:", kb.get_keyboard())


def show_delete_confirm(vk, user_id, mid):
    m = get_material(mid)
    if not m:
        send(vk, user_id, "Материал не найден.", back_kb()); return
    cat = m["category"] or "board"
    kb = VkKeyboard(one_time=False)
    kb.add_callback_button("✅ Да, удалить", color=VkKeyboardColor.NEGATIVE,
                           payload={"command": f"delmat_ok:{mid}"})
    kb.add_callback_button("❌ Отмена", color=VkKeyboardColor.SECONDARY,
                           payload={"command": f"del_cat:{cat}"})
    send(vk, user_id, f"🗑 Удалить материал?\n\n{full_label(m)}\n"
                      f"Остаток: {m['qty']:g} {m['unit']}", kb.get_keyboard())


def delete_material(vk, user_id, mid):
    m = get_material(mid)
    if not m:
        send(vk, user_id, "Уже удалён.", main_menu(user_id)); return
    with db() as con:
        con.execute("DELETE FROM requests WHERE material_id=?", (mid,))
        con.execute("DELETE FROM request_items WHERE material_id=?", (mid,))
        con.execute("DELETE FROM materials WHERE id=?", (mid,))
        con.commit()
    log_action(user_id, f"Удалён материал {m['name']}")
    send(vk, user_id, f"🗑 Удалено: {full_label(m)}", main_menu(user_id))


# ======================= ОЧИСТКА / СВОДКА / ЖУРНАЛ / ЮЗЕРЫ =======================
def show_clear_stock_confirm(vk, user_id):
    kb = VkKeyboard(one_time=False)
    kb.add_callback_button("✅ Да", color=VkKeyboardColor.NEGATIVE,
                           payload={"command": "clear_stock_ok"})
    kb.add_callback_button("❌ Отмена", color=VkKeyboardColor.SECONDARY,
                           payload={"command": "menu"})
    send(vk, user_id, "🧹 Обнулить остатки ВСЕХ материалов?", kb.get_keyboard())


def clear_stock(vk, user_id):
    with db() as con:
        con.execute("UPDATE materials SET qty=0, updated_at=? WHERE COALESCE(hidden,0)=0",
                    (now_str(),))
        con.commit()
    log_action(user_id, "Очищены все остатки")
    send(vk, user_id, "🧹 Все остатки обнулены.", main_menu(user_id))


def show_stats(vk, user_id):
    with db() as con:
        tm = con.execute("SELECT COUNT(*) c FROM materials WHERE COALESCE(hidden,0)=0").fetchone()["c"]
        tq = con.execute("SELECT COALESCE(SUM(qty),0) s FROM materials "
                         "WHERE COALESCE(hidden,0)=0").fetchone()["s"]
        ac = con.execute("SELECT COUNT(*) c FROM requests WHERE status IN ('new','approved')").fetchone()["c"]
        isd = con.execute("SELECT COUNT(*) c FROM requests WHERE status='issued'").fetchone()["c"]
        rj = con.execute("SELECT COUNT(*) c FROM requests WHERE status='rejected'").fetchone()["c"]
        us = con.execute("SELECT role, COUNT(*) c FROM users WHERE blocked=0 GROUP BY role").fetchall()
        by_cat = con.execute("SELECT category, COUNT(*) c, COALESCE(SUM(qty),0) s "
                             "FROM materials WHERE COALESCE(hidden,0)=0 "
                             "GROUP BY category").fetchall()
    lines = ["📊 СВОДКА СКЛАДА", "", f"📦 Позиций: {tm}",
             f"📊 Общий остаток: {tq:g}", "", "По категориям:"]
    for r in by_cat:
        lines.append(f"   {cat_label(r['category'])}: {r['c']} поз., {r['s']:g}")
    lines += ["", f"📥 Активных: {ac}",
              f"✅ Выдано: {isd}", f"❌ Отклонено: {rj}", "", "👥 Пользователи:"]
    for u in us:
        rr = {"operator": "станочник", "driver": "водитель погрузчика",
              "warehouse": "кладовщик", "admin": "админ"}.get(u["role"], u["role"])
        lines.append(f"   • {rr}: {u['c']}")
    send(vk, user_id, "\n".join(lines), back_kb())


def show_log(vk, user_id):
    with db() as con:
        rows = con.execute("SELECT l.*, u.full_name FROM logs l "
                           "LEFT JOIN users u ON u.user_id=l.user_id "
                           "ORDER BY l.id DESC LIMIT 20").fetchall()
    if not rows:
        send(vk, user_id, "📋 Журнал пуст.", back_kb()); return
    lines = ["📋 ЖУРНАЛ (последние 20)", ""]
    for r in rows:
        who = r["full_name"] or r["user_id"] or "—"
        det = f" — {r['details']}" if r["details"] else ""
        lines.append(f"{r['created_at'][5:16]} | {who}: {r['action']}{det}")
    send(vk, user_id, "\n".join(lines), back_kb())


def show_users(vk, user_id):
    if not is_admin(user_id):
        send(vk, user_id, "Нет доступа."); return
    with db() as con:
        rows = con.execute("SELECT * FROM users ORDER BY role, full_name").fetchall()
    if not rows:
        send(vk, user_id, "Нет пользователей.", back_kb()); return
    lines = ["👥 ПОЛЬЗОВАТЕЛИ", ""]
    for r in rows:
        rr = {"operator": "станочник", "driver": "водитель погрузчика",
              "warehouse": "кладовщик", "admin": "админ"}.get(r["role"], r["role"])
        block = " 🚫" if r["blocked"] else ""
        lines.append(f"{r['full_name'] or r['user_id']} (id{r['user_id']})\n   {rr}{block}")
    send(vk, user_id, "\n".join(lines), back_kb())


# ======================= НАВИГАЦИЯ В НОМЕНКЛАТУРЕ =======================
def _new_step_send(vk, uid, step):
    st = get_state(uid)
    data = dict(st["data"])
    cat = data.get("category", "board")
    kb = VkKeyboard(one_time=False)
    if step > 2:
        kb.add_callback_button("⬅️ Назад", color=VkKeyboardColor.SECONDARY,
                               payload={"command": "newnav:back"})
        kb.add_callback_button("❌ Отменить", color=VkKeyboardColor.NEGATIVE,
                               payload={"command": "newnav:cancel"})
    else:
        kb.add_callback_button("⬅️ К категориям", color=VkKeyboardColor.SECONDARY,
                               payload={"command": "newnav:cat"})
        kb.add_callback_button("❌ Отменить", color=VkKeyboardColor.NEGATIVE,
                               payload={"command": "newnav:cancel"})
    kb.add_line()
    kb.add_button("⬅️ В меню", color=VkKeyboardColor.SECONDARY)

    if step == 2:
        if CATEGORIES[cat]["has_thickness"]:
            send(vk, uid, "🆕 Шаг 2. Введите толщину (10мм, 16мм). Или «-»:",
                 kb.get_keyboard())
        else:
            send(vk, uid, "🆕 Шаг 2. Введите декор/цвет. Или «-»:",
                 kb.get_keyboard())
    elif step == 3:
        p = [x for x in (data.get("thickness"), data.get("decor")) if x]
        auto = ("Материал " + " ".join(p)).strip() if p else ""
        if auto:
            send(vk, uid, f"📝 Шаг 3. Авто-имя: «{auto}»\nВведите своё или «-»:",
                 kb.get_keyboard())
        else:
            send(vk, uid, "📝 Шаг 3. Название материала:", kb.get_keyboard())
    elif step == 4:
        send(vk, uid, "🔢 Шаг 4. Начальный остаток:", kb.get_keyboard())
    elif step == 5:
        send(vk, uid, "📐 Шаг 5. Единица (шт, м, лист, кг, канистра). Или «-» для шт:",
             kb.get_keyboard())


def _newnav_back(vk, uid):
    st = get_state(uid)
    state = st["state"]
    data = dict(st["data"])
    cat = data.get("category", "board")
    if state == "new_decor":
        set_state(uid, "new_thickness", category=cat)
        _new_step_send(vk, uid, 2)
    elif state == "new_name":
        if CATEGORIES[cat]["has_thickness"]:
            set_state(uid, "new_decor", category=cat,
                      thickness=data.get("thickness", ""))
            _new_step_send(vk, uid, 3)
        else:
            set_state(uid, "new_thickness", category=cat)
            _new_step_send(vk, uid, 2)
    elif state == "new_qty":
        set_state(uid, "new_name", category=cat,
                  thickness=data.get("thickness", ""),
                  decor=data.get("decor", ""))
        _new_step_send(vk, uid, 4)
    elif state == "new_unit":
        set_state(uid, "new_qty", category=cat,
                  thickness=data.get("thickness", ""),
                  decor=data.get("decor", ""),
                  name=data.get("name", ""),
                  qty=data.get("qty", 0))
        _new_step_send(vk, uid, 5)
    else:
        clear_state(uid)
        start_new_material(vk, uid)


# ======================= НОВАЯ НОМЕНКЛАТУРА =======================
def start_new_material(vk, user_id):
    kb = VkKeyboard(one_time=False)
    for i, c in enumerate(CATEGORY_ORDER):
        if i > 0: kb.add_line()
        kb.add_callback_button(cat_label(c), color=VkKeyboardColor.PRIMARY,
                               payload={"command": f"newcat:{c}"})
    kb.add_line()
    kb.add_button("⬅️ В меню", color=VkKeyboardColor.SECONDARY)
    send(vk, user_id, "🆕 Шаг 1/5. Выберите категорию:", kb.get_keyboard())


# ======================= ИНВЕНТАРИЗАЦИЯ =======================
def start_inventory(vk, user_id):
    cats = distinct_categories()
    if not cats:
        send(vk, user_id, "Склад пуст.", main_menu(user_id)); return
    kb = VkKeyboard(one_time=False)
    kb.add_callback_button("📦 Все категории", color=VkKeyboardColor.PRIMARY,
                           payload={"command": "inv_cat:all"})
    for c in cats:
        kb.add_line()
        kb.add_callback_button(cat_label(c), color=VkKeyboardColor.PRIMARY,
                               payload={"command": f"inv_cat:{c}"})
    kb.add_line()
    kb.add_button("⬅️ В меню", color=VkKeyboardColor.SECONDARY)
    send(vk, user_id, "📋 Инвентаризация. Что пересчитываем?", kb.get_keyboard())


def inventory_begin(vk, user_id, cat):
    with db() as con:
        if cat == "all":
            rows = con.execute("SELECT id FROM materials "
                               "WHERE COALESCE(hidden,0)=0 "
                               "ORDER BY category, thickness, decor, name").fetchall()
        else:
            rows = con.execute("SELECT id FROM materials WHERE category=? "
                               "AND COALESCE(hidden,0)=0 "
                               "ORDER BY thickness, decor, name", (cat,)).fetchall()
    if not rows:
        send(vk, user_id, "Нет материалов.", main_menu(user_id)); return
    queue = [r["id"] for r in rows]
    set_state(user_id, "inv_qty", queue=queue, idx=0, changes={})
    show_inventory_item(vk, user_id)


def show_inventory_item(vk, user_id):
    st = get_state(user_id)
    data = st["data"]
    queue = data.get("queue", [])
    idx = data.get("idx", 0)
    if idx >= len(queue):
        finish_inventory(vk, user_id); return
    mid = queue[idx]
    m = get_material(mid)
    if not m:
        data["idx"] = idx + 1
        set_state(user_id, "inv_qty", **data)
        show_inventory_item(vk, user_id); return
    kb = VkKeyboard(one_time=False)
    if idx > 0:
        kb.add_callback_button("⬅️ К предыдущей", color=VkKeyboardColor.SECONDARY,
                               payload={"command": "inv_prev"})
        kb.add_line()
    kb.add_callback_button("⏭ Пропустить", color=VkKeyboardColor.SECONDARY,
                           payload={"command": "inv_skip"})
    kb.add_line()
    kb.add_callback_button("⏹ Завершить и сохранить",
                           color=VkKeyboardColor.POSITIVE,
                           payload={"command": "inv_finish"})
    kb.add_line()
    kb.add_button("⬅️ В меню", color=VkKeyboardColor.SECONDARY)
    send(vk, user_id,
         f"📋 Инвентаризация ({idx+1}/{len(queue)})\n\n"
         f"{full_label(m)}\n"
         f"Текущий остаток: {m['qty']:g} {m['unit']}\n\n"
         f"Введите фактическое количество (число):",
         kb.get_keyboard())


def finish_inventory(vk, user_id):
    st = get_state(user_id)
    changes = st["data"].get("changes", {})
    if not changes:
        clear_state(user_id)
        send(vk, user_id, "Изменений нет.", main_menu(user_id)); return
    lines = ["📋 ИНВЕНТАРИЗАЦИЯ — итог", ""]
    with db() as con:
        for mid, new_qty in changes.items():
            m = con.execute("SELECT * FROM materials WHERE id=?", (mid,)).fetchone()
            if not m: continue
            old = m["qty"]
            diff = new_qty - old
            sign = "+" if diff > 0 else ""
            lines.append(f"{full_label(m)}\n   {old:g} → {new_qty:g} ({sign}{diff:g})")
            con.execute("UPDATE materials SET qty=?, updated_at=? WHERE id=?",
                        (new_qty, now_str(), mid))
        con.commit()
    clear_state(user_id)
    log_action(user_id, f"Инвентаризация: {len(changes)} позиций")
    lines += ["", "✅ Остатки обновлены."]
    send(vk, user_id, "\n".join(lines), main_menu(user_id))


# ======================= CALLBACK =======================
def safe_int(s):
    try: return int(s)
    except (ValueError, TypeError): return None


def handle_callback(vk, user_id, command):
    print(f"[CALLBACK] user={user_id} cmd={command!r}")
    if not command: return
    if command == "menu":
        clear_state(user_id); send(vk, user_id, "Главное меню:", main_menu(user_id)); return
    if command == "stock_refresh":
        send(vk, user_id, render_stock(), stock_kb()); return
    if command == "noop":
        return

    if command.startswith("inv_cat:"):
        if not is_warehouse(user_id):
            send(vk, user_id, "Нет доступа."); return
        cat = command.split(":", 1)[1]
        inventory_begin(vk, user_id, cat); return
    if command == "inv_prev":
        if not is_warehouse(user_id):
            send(vk, user_id, "Нет доступа."); return
        st = get_state(user_id)
        data = dict(st["data"])
        idx = data.get("idx", 0)
        if idx > 0:
            data["idx"] = idx - 1
            set_state(user_id, "inv_qty", **data)
            show_inventory_item(vk, user_id)
        return
    if command == "inv_skip":
        if not is_warehouse(user_id):
            send(vk, user_id, "Нет доступа."); return
        st = get_state(user_id)
        data = dict(st["data"])
        data["idx"] = data.get("idx", 0) + 1
        set_state(user_id, "inv_qty", **data)
        show_inventory_item(vk, user_id); return
    if command == "inv_finish":
        if not is_warehouse(user_id):
            send(vk, user_id, "Нет доступа."); return
        finish_inventory(vk, user_id); return

    if command == "newnav:back":
        if not is_warehouse(user_id):
            send(vk, user_id, "Нет доступа."); return
        _newnav_back(vk, user_id); return
    if command == "newnav:cancel":
        clear_state(user_id)
        send(vk, user_id, "Отменено.", main_menu(user_id)); return
    if command == "newnav:cat":
        clear_state(user_id)
        start_new_material(vk, user_id); return
    if command.startswith("newcat:"):
        cat = command.split(":", 1)[1]
        if cat not in CATEGORIES:
            send(vk, user_id, "Неизвестная категория."); return
        set_state(user_id, "new_thickness", category=cat)
        _new_step_send(vk, user_id, 2)
        return

    if command.startswith("cart_cat:"):
        cat = command.split(":", 1)[1]
        st = get_state(user_id)
        cart = list(st["data"].get("cart", []))
        default_plan = st["data"].get("default_plan", "")
        if cat == "edge":
            set_state(user_id, "edge_plan", cart=cart, default_plan=default_plan)
            show_edge_plan_page(vk, user_id, 1); return
        set_state(user_id, "cart_choose", cart=cart, default_plan=default_plan)
        show_thicknesses_for_cart(vk, user_id, cat); return
    if command.startswith("cart_thick:"):
        rest = command.split(":", 2)[1:]
        if len(rest) >= 2:
            cat, th = rest[0], rest[1]
            st = get_state(user_id)
            set_state(user_id, "cart_choose", cart=list(st["data"].get("cart", [])),
                      default_plan=st["data"].get("default_plan", ""),
                      category=cat, thickness=th)
            show_materials_for_cart(vk, user_id, cat, th, 1); return
    if command.startswith("cart_mat_page:"):
        rest = command.split(":", 3)[1:]
        if len(rest) >= 3:
            cat, th_s, page_s = rest[0], rest[1], rest[2]
            page = safe_int(page_s) or 1
            th_val = None if th_s == "None" else th_s
            show_materials_for_cart(vk, user_id, cat, th_val, page); return
    if command.startswith("cart_add:"):
        mid = safe_int(command.split(":", 1)[1])
        if mid is not None: add_to_cart(vk, user_id, mid)
        return
    if command == "cart_show":
        show_cart(vk, user_id); return
    if command == "cart_more":
        st = get_state(user_id)
        set_state(user_id, "cart_choose", cart=list(st["data"].get("cart", [])),
                  default_plan=st["data"].get("default_plan", ""))
        show_categories_for_cart(vk, user_id); return
    if command == "cart_clear":
        clear_state(user_id); send(vk, user_id, "🛒 Корзина очищена.", main_menu(user_id)); return
    if command == "cart_submit":
        submit_cart(vk, user_id); return
    if command == "cart_back_cat":
        st = get_state(user_id)
        set_state(user_id, "cart_choose", cart=list(st["data"].get("cart", [])),
                  default_plan=st["data"].get("default_plan", ""))
        show_categories_for_cart(vk, user_id); return
    if command.startswith("cart_back_thick:"):
        cat = command.split(":", 1)[1]
        st = get_state(user_id)
        set_state(user_id, "cart_choose", cart=list(st["data"].get("cart", [])),
                  default_plan=st["data"].get("default_plan", ""), category=cat)
        show_thicknesses_for_cart(vk, user_id, cat); return
    if command == "cart_back_add":
        st = get_state(user_id)
        cart = list(st["data"].get("cart", []))
        idx = st["data"].get("editing_index", len(cart) - 1)
        if 0 <= idx < len(cart) and cart[idx].get("qty") is None:
            cart.pop(idx)
        set_state(user_id, "cart_choose", cart=cart,
                  default_plan=st["data"].get("default_plan", ""))
        show_categories_for_cart(vk, user_id); return

    # --- Кромка ---
    if command.startswith("edge_plan_page:"):
        page = safe_int(command.split(":", 1)[1]) or 1
        st = get_state(user_id)
        set_state(user_id, "edge_plan", cart=list(st["data"].get("cart", [])),
                  default_plan=st["data"].get("default_plan", ""), page=page)
        show_edge_plan_page(vk, user_id, page); return
    if command.startswith("edge_plan:"):
        n = safe_int(command.split(":", 1)[1])
        if n is None: return
        st = get_state(user_id)
        cart = list(st["data"].get("cart", []))
        try: register_plan(n)
        except: pass
        set_state(user_id, "edge_glue", cart=cart, plan=str(n), default_plan=str(n))
        show_edge_glue_question(vk, user_id, n); return
    if command == "edge_plan_manual":
        st = get_state(user_id)
        set_state(user_id, "edge_plan_manual",
                  cart=list(st["data"].get("cart", [])),
                  default_plan=st["data"].get("default_plan", ""))
        send(vk, user_id, "🔢 Введите номер плана (1–2000):"); return
    if command == "edge_plan_again":
        st = get_state(user_id)
        set_state(user_id, "edge_plan", cart=list(st["data"].get("cart", [])),
                  default_plan=st["data"].get("default_plan", ""))
        show_edge_plan_page(vk, user_id, 1); return
    if command == "edge_glue:no":
        st = get_state(user_id)
        cart = list(st["data"].get("cart", []))
        plan = st["data"].get("plan", "")
        _add_edge_to_cart(cart, plan, glue_qty=None)
        set_state(user_id, "cart", cart=cart, default_plan=plan)
        send(vk, user_id, "✅ Кромка добавлена в корзину")
        show_cart(vk, user_id); return
    if command == "edge_glue:yes":
        st = get_state(user_id)
        cart = list(st["data"].get("cart", []))
        plan = st["data"].get("plan", "")
        set_state(user_id, "edge_glue_qty", cart=cart, plan=plan)
        send(vk, user_id, "🔢 Сколько КАНИСТР клея нужно? Введите число:"); return

    # --- План для пиломатериалов ---
    if command.startswith("plan_page:"):
        page = safe_int(command.split(":", 1)[1]) or 1
        st = get_state(user_id)
        set_state(user_id, "cart_plan", cart=list(st["data"].get("cart", [])),
                  default_plan=st["data"].get("default_plan", ""),
                  page=page, editing_index=st["data"].get("editing_index", -1))
        show_plan_page(vk, user_id, page); return
    if command.startswith("plan_pick:"):
        n = safe_int(command.split(":", 1)[1])
        if n is None: return
        st = get_state(user_id)
        cart = list(st["data"].get("cart", []))
        if not cart:
            send(vk, user_id, "Корзина пуста.", main_menu(user_id)); return
        idx = st["data"].get("editing_index", len(cart) - 1)
        if 0 <= idx < len(cart):
            cart[idx]["plan"] = str(n)
        try: register_plan(n)
        except: pass
        set_state(user_id, "cart", cart=cart, default_plan=str(n))
        send(vk, user_id, f"✅ План {n}")
        show_cart(vk, user_id); return
    if command == "plan_skip":
        st = get_state(user_id)
        cart = list(st["data"].get("cart", []))
        default_plan = st["data"].get("default_plan", "")
        idx = st["data"].get("editing_index", len(cart) - 1)
        if default_plan and 0 <= idx < len(cart):
            cart[idx]["plan"] = default_plan
        set_state(user_id, "cart", cart=cart, default_plan=default_plan)
        show_cart(vk, user_id); return
    if command == "plan_manual":
        st = get_state(user_id)
        set_state(user_id, "plan_manual", cart=list(st["data"].get("cart", [])),
                  default_plan=st["data"].get("default_plan", ""),
                  editing_index=st["data"].get("editing_index", -1))
        send(vk, user_id, "🔢 Введите номер плана (1–2000):"); return

    if command == "wh_requests":
        if not is_driver(user_id):
            send(vk, user_id, "Нет доступа."); return
        show_active_requests(vk, user_id, 1); return
    if command.startswith("req_page:"):
        if not is_driver(user_id):
            send(vk, user_id, "Нет доступа."); return
        page = safe_int(command.split(":", 1)[1]) or 1
        show_active_requests(vk, user_id, page); return
    if command.startswith("req_view:"):
        if not is_driver(user_id):
            send(vk, user_id, "Нет доступа."); return
        rid = safe_int(command.split(":", 1)[1])
        if rid is not None: show_request_details(vk, user_id, rid)
        return
    if command.startswith("wh_issue:"):
        if not is_warehouse_strict(user_id):
            send(vk, user_id, "Нет доступа."); return
        rid = safe_int(command.split(":", 1)[1])
        if rid is None: return
        issue_request(vk, user_id, rid)
        show_request_details(vk, user_id, rid); return
    if command.startswith("wh_reject:"):
        if not is_warehouse_strict(user_id):
            send(vk, user_id, "Нет доступа."); return
        rid = safe_int(command.split(":", 1)[1])
        if rid is None: return
        reject_request(vk, user_id, rid)
        show_request_details(vk, user_id, rid); return

    if not is_driver(user_id):
        send(vk, user_id, "Нет доступа."); return
    if command == "inc_list":
        show_inc_list(vk, user_id, mass=False); return
    if command == "minc_start":
        set_state(user_id, "minc_choose", mass_cart=[])
        show_inc_list(vk, user_id, mass=True); return
    if command.startswith("inc_cat:"):
        cat = command.split(":", 1)[1]
        show_inc_list_in_cat(vk, user_id, cat, mass=False, page=1); return
    if command.startswith("inc_page:"):
        parts = command.split(":", 2)
        if len(parts) == 3:
            cat = parts[1]; page = safe_int(parts[2]) or 1
            show_inc_list_in_cat(vk, user_id, cat, mass=False, page=page)
        return
    if command.startswith("minc_cat:"):
        cat = command.split(":", 1)[1]
        show_inc_list_in_cat(vk, user_id, cat, mass=True, page=1); return
    if command.startswith("minc_page:"):
        parts = command.split(":", 2)
        if len(parts) == 3:
            cat = parts[1]; page = safe_int(parts[2]) or 1
            show_inc_list_in_cat(vk, user_id, cat, mass=True, page=page)
        return
    if command.startswith("inc_mat:"):
        mid = safe_int(command.split(":", 1)[1]); m = get_material(mid) if mid else None
        if not m: send(vk, user_id, "Материал не найден."); return
        set_state(user_id, "inc_qty", material_id=mid)
        cat = m["category"] or "board"
        kb = VkKeyboard(one_time=False)
        kb.add_callback_button("⬅️ К списку", color=VkKeyboardColor.SECONDARY,
                               payload={"command": f"inc_cat:{cat}"})
        kb.add_line()
        kb.add_button("⬅️ В меню", color=VkKeyboardColor.SECONDARY)
        send(vk, user_id, f"➕ {full_label(m)}\nОстаток: {m['qty']:g} {m['unit']}\n\n"
                          f"Введите количество:", kb.get_keyboard()); return
    if command.startswith("minc_mat:"):
        mid = safe_int(command.split(":", 1)[1]); m = get_material(mid) if mid else None
        if not m: send(vk, user_id, "Материал не найден."); return
        st = get_state(user_id)
        set_state(user_id, "minc_qty", mass_cart=st["data"].get("mass_cart", []), material_id=mid)
        cat = m["category"] or "board"
        kb = VkKeyboard(one_time=False)
        kb.add_callback_button("⬅️ К списку", color=VkKeyboardColor.SECONDARY,
                               payload={"command": f"minc_cat:{cat}"})
        kb.add_line()
        kb.add_button("⬅️ В меню", color=VkKeyboardColor.SECONDARY)
        send(vk, user_id, f"📦 {full_label(m)}\nВведите количество:",
             kb.get_keyboard()); return
    if command == "minc_more":
        st = get_state(user_id)
        set_state(user_id, "minc_choose", mass_cart=st["data"].get("mass_cart", []))
        show_inc_list(vk, user_id, mass=True); return
    if command == "minc_done":
        finish_mass_inc(vk, user_id); return

    if not is_warehouse(user_id):
        send(vk, user_id, "Нет доступа."); return
    if command == "edit_list":
        show_edit_list(vk, user_id); return
    if command.startswith("edit_cat:"):
        cat = command.split(":", 1)[1]
        show_edit_list_in_cat(vk, user_id, cat, 1); return
    if command.startswith("edit_page:"):
        parts = command.split(":", 2)
        if len(parts) == 3:
            cat = parts[1]; page = safe_int(parts[2]) or 1
            show_edit_list_in_cat(vk, user_id, cat, page)
        return
    if command.startswith("edit:"):
        mid = safe_int(command.split(":", 1)[1])
        if mid is not None: show_edit_fields(vk, user_id, mid)
        return
    if command.startswith("editf:"):
        parts = command.split(":", 2)
        if len(parts) == 3:
            mid = safe_int(parts[1]); field = parts[2]
            if mid is not None and field in EDIT_FIELDS:
                set_state(user_id, "edit_value", mid=mid, field=field)
                kb = VkKeyboard(one_time=False)
                kb.add_callback_button("⬅️ К полям", color=VkKeyboardColor.SECONDARY,
                                       payload={"command": f"edit:{mid}"})
                kb.add_callback_button("⬅️ К списку", color=VkKeyboardColor.SECONDARY,
                                       payload={"command": "edit_list"})
                kb.add_line()
                kb.add_button("⬅️ В меню", color=VkKeyboardColor.SECONDARY)
                send(vk, user_id,
                     f"✏️ Новое значение для «{EDIT_FIELDS.get(field, field)}»:",
                     kb.get_keyboard())
        return
    if command == "del_list":
        show_delete_list(vk, user_id); return
    if command.startswith("del_cat:"):
        cat = command.split(":", 1)[1]
        show_delete_list_in_cat(vk, user_id, cat, 1); return
    if command.startswith("del_page:"):
        parts = command.split(":", 2)
        if len(parts) == 3:
            cat = parts[1]; page = safe_int(parts[2]) or 1
            show_delete_list_in_cat(vk, user_id, cat, page)
        return
    if command.startswith("delmat:"):
        mid = safe_int(command.split(":", 1)[1])
        if mid is not None: show_delete_confirm(vk, user_id, mid)
        return
    if command.startswith("delmat_ok:"):
        mid = safe_int(command.split(":", 1)[1])
        if mid is not None: delete_material(vk, user_id, mid)
        return
    if command == "clear_stock_ask":
        show_clear_stock_confirm(vk, user_id); return
    if command == "clear_stock_ok":
        clear_stock(vk, user_id); return


# ======================= ТЕКСТ =======================
def handle_message(vk, user_id, text):
    if text == "/start" or text == "Начать" or "В меню" in text:
        clear_state(user_id)
        r = get_role(user_id)
        rr = {"operator": "станочник", "driver": "водитель погрузчика",
              "warehouse": "кладовщик", "admin": "администратор"}.get(r, r)
        send(vk, user_id, f"👋 Складской бот\n\n"
                          f"Вы вошли как: {rr}\n\nВыберите действие:", main_menu(user_id))
        return
    if text == "/help":
        send(vk, user_id, HELP_TEXT, back_kb()); return
    if text == "/whoami":
        send(vk, user_id, f"Ваш ID: {user_id}\nРоль: {get_role(user_id)}\n"
                          f"В ADMIN_IDS: {'да' if user_id in ADMIN_IDS else 'нет'}",
             back_kb()); return
    if text == "/users":
        show_users(vk, user_id); return
    if text.startswith("/setrole"):
        if not is_admin(user_id): send(vk, user_id, "Нет доступа."); return
        p = text.split()
        if len(p) != 3 or p[2] not in ("operator", "driver", "warehouse", "admin"):
            send(vk, user_id,
                 "Использование: /setrole 123456789 driver\n"
                 "Роли: operator, driver, warehouse, admin"); return
        with db() as con:
            con.execute("UPDATE users SET role=? WHERE user_id=?", (p[2], int(p[1])))
            con.commit()
        send(vk, user_id, f"✅ {p[1]} → {p[2]}"); return
    if text == "/resetstock":
        if not is_admin(user_id): send(vk, user_id, "Нет доступа."); return
        clear_stock(vk, user_id); return

    st = get_state(user_id); state = st["state"]; data = st["data"]

    if state == "inv_qty":
        data2 = dict(data)
        queue = data2.get("queue", [])
        idx = data2.get("idx", 0)
        if idx >= len(queue):
            finish_inventory(vk, user_id); return
        try:
            qty = float(text.replace(",", "."))
            if qty < 0: raise ValueError
        except ValueError:
            send(vk, user_id, "❗ Введите неотрицательное число."); return
        mid = queue[idx]
        changes = dict(data2.get("changes", {}))
        changes[mid] = qty
        data2["idx"] = idx + 1
        data2["changes"] = changes
        set_state(user_id, "inv_qty", **data2)
        show_inventory_item(vk, user_id); return

    if state == "edge_glue_qty":
        try:
            qty = float(text.replace(",", "."))
            if qty <= 0: raise ValueError
        except ValueError:
            send(vk, user_id, "❗ Введите положительное число."); return
        cart = list(data.get("cart", []))
        plan = data.get("plan", "")
        _add_edge_to_cart(cart, plan, glue_qty=qty)
        set_state(user_id, "cart", cart=cart, default_plan=plan)
        send(vk, user_id,
             f"✅ Кромка добавлена в корзину, клей — {qty:g} канистр")
        show_cart(vk, user_id); return

    if state == "edge_plan_manual":
        try:
            n = int(text.strip())
            if n < PLAN_MIN or n > PLAN_MAX: raise ValueError
        except ValueError:
            send(vk, user_id, f"❗ Число от {PLAN_MIN} до {PLAN_MAX}."); return
        cart = list(data.get("cart", []))
        try: register_plan(n)
        except: pass
        set_state(user_id, "edge_glue", cart=cart, plan=str(n), default_plan=str(n))
        show_edge_glue_question(vk, user_id, n); return

    if state == "cart_qty":
        try:
            qty = float(text.replace(",", "."))
            if qty <= 0: raise ValueError
        except ValueError:
            send(vk, user_id, "❗ Введите положительное число."); return
        cart = list(data.get("cart", []))
        idx = data.get("editing_index", len(cart) - 1)
        if 0 <= idx < len(cart): cart[idx]["qty"] = qty
        m = get_material(cart[idx]["material_id"]) if 0 <= idx < len(cart) else None
        if m and (m["category"] or "board") == "film":
            cart[idx]["plan"] = ""
            set_state(user_id, "cart", cart=cart,
                      default_plan=data.get("default_plan", ""))
            show_cart(vk, user_id); return
        set_state(user_id, "cart_plan", cart=cart, editing_index=idx,
                  default_plan=data.get("default_plan", ""))
        show_plan_page(vk, user_id, 1); return

    if state == "plan_manual":
        try:
            n = int(text.strip())
            if n < PLAN_MIN or n > PLAN_MAX: raise ValueError
        except ValueError:
            send(vk, user_id, f"❗ Число от {PLAN_MIN} до {PLAN_MAX}."); return
        cart = list(data.get("cart", []))
        idx = data.get("editing_index", len(cart) - 1)
        if 0 <= idx < len(cart): cart[idx]["plan"] = str(n)
        try: register_plan(n)
        except: pass
        set_state(user_id, "cart", cart=cart, default_plan=str(n))
        send(vk, user_id, f"✅ План: {n}")
        show_cart(vk, user_id); return

    if state == "inc_qty":
        try:
            qty = float(text.replace(",", "."))
            if qty <= 0: raise ValueError
        except ValueError:
            send(vk, user_id, "❗ Введите положительное число."); return
        mid = data["material_id"]; m = get_material(mid)
        with db() as con:
            con.execute("UPDATE materials SET qty=qty+?, updated_at=? WHERE id=?",
                        (qty, now_str(), mid)); con.commit()
        clear_state(user_id)
        send(vk, user_id, f"✅ Приход\n{full_label(m)}: +{qty:g} {m['unit']}\n"
                          f"Новый остаток: {m['qty'] + qty:g} {m['unit']}", main_menu(user_id))
        return

    if state == "minc_qty":
        try:
            qty = float(text.replace(",", "."))
            if qty <= 0: raise ValueError
        except ValueError:
            send(vk, user_id, "❗ Введите положительное число."); return
        mid = data["material_id"]
        cart = data.get("mass_cart", [])
        cart.append({"material_id": mid, "qty": qty})
        set_state(user_id, "minc_choose", mass_cart=cart)
        m = get_material(mid)
        send(vk, user_id, f"✅ {full_label(m)}: +{qty:g}")
        show_mass_inc_cart(vk, user_id); return

    if state == "edit_value":
        do_edit(vk, user_id, data["mid"], data["field"], text.strip()); return

    if state == "new_thickness":
        cat = data["category"]
        val = "" if text.strip() == "-" else text.strip()
        if CATEGORIES[cat]["has_thickness"]:
            set_state(user_id, "new_decor", category=cat, thickness=val)
        else:
            set_state(user_id, "new_name", category=cat, thickness="", decor=val)
        _new_step_send(vk, user_id, 3); return
    if state == "new_decor":
        decor = "" if text.strip() == "-" else text.strip()
        set_state(user_id, "new_name", category=data["category"],
                  thickness=data["thickness"], decor=decor)
        _new_step_send(vk, user_id, 3); return
    if state == "new_name":
        raw = text.strip()
        if raw == "-":
            p = [x for x in (data.get("thickness"), data.get("decor")) if x]
            name = ("Материал " + " ".join(p)).strip() if p else ""
        else: name = raw
        if not name:
            send(vk, user_id, "❗ Название не пустое."); return
        set_state(user_id, "new_qty", category=data["category"],
                  thickness=data.get("thickness", ""),
                  decor=data.get("decor", ""), name=name)
        _new_step_send(vk, user_id, 4); return
    if state == "new_qty":
        try: qty = float(text.replace(",", "."))
        except ValueError:
            send(vk, user_id, "❗ Число."); return
        set_state(user_id, "new_unit", category=data["category"],
                  thickness=data.get("thickness", ""),
                  decor=data.get("decor", ""), name=data["name"], qty=qty)
        _new_step_send(vk, user_id, 5); return
    if state == "new_unit":
        unit = "шт" if text.strip() == "-" else text.strip()
        with db() as con:
            con.execute("INSERT INTO materials (category,name,unit,qty,thickness,decor,"
                        "hidden,updated_at) VALUES (?,?,?,?,?,?,0,?)",
                        (data["category"], data["name"], unit, data["qty"],
                         data.get("thickness", ""), data.get("decor", ""), now_str()))
            con.commit()
        log_action(user_id, f"Создан материал {data['name']}")
        clear_state(user_id)
        send(vk, user_id,
             f"✅ Материал добавлен\n\n{cat_label(data['category'])}\n"
             f"📦 {data['name']}\n"
             f"Толщина: {data.get('thickness') or '—'}\n"
             f"Декор/цвет: {data.get('decor') or '—'}\n"
             f"Остаток: {data['qty']:g} {unit}",
             main_menu(user_id)); return

    if text == "📋 Остатки на складе":
        send(vk, user_id, render_stock(), stock_kb()); return
    if text == "📦 Новая заявка":
        clear_state(user_id); show_categories_for_cart(vk, user_id); return
    if text == "📄 Мои заявки":
        show_my_requests(vk, user_id); return
    if text == "📥 Заявки":
        if not is_driver(user_id): send(vk, user_id, "Нет доступа."); return
        show_active_requests(vk, user_id, 1); return
    if text == "➕ Приход":
        if not is_driver(user_id): send(vk, user_id, "Нет доступа."); return
        show_inc_list(vk, user_id, mass=False); return
    if text == "📦 Массовый приход":
        if not is_driver(user_id): send(vk, user_id, "Нет доступа."); return
        set_state(user_id, "minc_choose", mass_cart=[])
        show_inc_list(vk, user_id, mass=True); return
    if text == "🆕 Номенклатура":
        if not is_warehouse(user_id): send(vk, user_id, "Нет доступа."); return
        start_new_material(vk, user_id); return
    if text == "✏️ Редактировать":
        if not is_warehouse(user_id): send(vk, user_id, "Нет доступа."); return
        show_edit_list(vk, user_id); return
    if text == "🗑 Удалить":
        if not is_warehouse(user_id): send(vk, user_id, "Нет доступа."); return
        show_delete_list(vk, user_id); return
    if text == "🧹 Очистить":
        if not is_warehouse(user_id): send(vk, user_id, "Нет доступа."); return
        show_clear_stock_confirm(vk, user_id); return
    if text == "📋 Инвентаризация":
        if not is_warehouse(user_id): send(vk, user_id, "Нет доступа."); return
        start_inventory(vk, user_id); return
    if text == "📊 Сводка":
        if not is_warehouse(user_id): send(vk, user_id, "Нет доступа."); return
        show_stats(vk, user_id); return
    if text == "📋 Журнал":
        if not is_warehouse(user_id): send(vk, user_id, "Нет доступа."); return
        show_log(vk, user_id); return
    if text == "👥 Пользователи":
        if not is_admin(user_id): send(vk, user_id, "Нет доступа."); return
        show_users(vk, user_id); return

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
            if isinstance(payload, str):
                try:
                    import json as _json
                    payload = _json.loads(payload)
                except Exception: payload = {}
            command = payload.get("command") if isinstance(payload, dict) else None
            try:
                vk.messages.sendMessageEventAnswer(
                    event_id=event.obj.event_id,
                    user_id=event.obj.user_id,
                    peer_id=event.obj.peer_id)
            except Exception as e:
                print(f"[ANSWER ERROR] {e}")
            try: ensure_user(vk, user_id)
            except Exception as e: print(f"[ENSURE ERROR] {e}")
            try:
                handle_callback(vk, user_id, command)
            except Exception as e:
                import traceback
                print(f"[ERROR callback] {e}")
                traceback.print_exc()
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
                send(vk, user_id, "⚠️ Ошибка. Попробуйте /start.", main_menu(user_id))
            except Exception: pass


if __name__ == "__main__":
    main()
