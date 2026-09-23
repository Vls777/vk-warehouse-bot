# -*- coding: utf-8 -*-
"""
VK-бот «Склад пиломатериалов».
Роли:
  operator  — станочник (смотрит остатки, создаёт заявки)
  warehouse — водитель погрузчика / кладовщик (приход, выдача, корректировка)
  admin     — всё + управление ролями

Установка:  pip install vk_api
Запуск:     python bot.py
"""

import sqlite3
import os
from datetime import datetime
from vk_api import VkApi
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType
from vk_api.keyboard import VkKeyboard, VkKeyboardColor
from vk_api.utils import get_random_id

# ======================= НАСТРОЙКИ =======================
# Токен и ID группы берутся из переменных окружения (на BotHost).
# Если переменных нет — подставьте значения вручную (только для локального теста!).
GROUP_TOKEN = os.getenv("GROUP_TOKEN", "ВСТАВЬТЕ_СЮДА_ТОКЕН")
GROUP_ID = int(os.getenv("GROUP_ID", 0))  # ID вашей группы

ADMIN_IDS = {123456789}  # ваш ID ВКонтакте (замените на реальный)
DB_PATH = "/app/data/warehouse.db"  # путь для BotHost
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
        con.executescript(
            """
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
            """
        )
        # Стартовый справочник пиломатериалов
        if con.execute("SELECT COUNT(*) c FROM materials").fetchone()["c"] == 0:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            seed = [
                ("Доска обрезная 25x100x6000", "шт", 120, 50, "Стеллаж A1"),
                ("Доска обрезная 40x150x6000", "шт", 80, 30, "Стеллаж A2"),
                ("Брус 100x100x6000", "шт", 45, 20, "Стеллаж B1"),
                ("Брус 50x50x3000", "шт", 200, 60, "Стеллаж B2"),
                ("Вагонка 20x100x3000", "шт", 15, 25, "Стеллаж C1"),
                ("Фанера 10 мм 1520x1520", "лист", 60, 20, "Стеллаж C2"),
                ("Рейка 20x40x3000", "шт", 300, 100, "Пол D1"),
            ]
            con.executemany(
                "INSERT INTO materials (name, unit, qty, min_qty, location, updated_at) "
                "VALUES (?,?,?,?,?,?)",
                [(*s, now) for s in seed],
            )
        con.commit()


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_user(tg_user):
    """Создаёт запись пользователя, если её нет."""
    with db() as con:
        row = con.execute("SELECT 1 FROM users WHERE user_id=?", (tg_user.id,)).fetchone()
        if row is None:
            role = "admin" if tg_user.id in ADMIN_IDS else "operator"
            con.execute(
                "INSERT INTO users (user_id, username, full_name, role, created_at) VALUES (?,?,?,?,?)",
                (tg_user.id, tg_user.username, tg_user.full_name, role, now_str()),
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
        return con.execute("SELECT * FROM materials ORDER BY name").fetchall()


# ======================= ОТРИСОВКА «ТАБЛИЦЫ» =======================
def render_stock() -> str:
    rows = all_materials()
    if not rows:
        return "📋 Склад пуст"

    out = [
        "📋 ОСТАТКИ ПИЛОМАТЕРИАЛОВ",
        f"обновлено: {datetime.now():%d.%m.%Y %H:%M:%S}",
        "",
        f"{'№':<3}{'Наименование':<26}{'Ост.':>7}{'Мин.':>7}",
        "─" * 43,
    ]
    for i, r in enumerate(rows, 1):
        name = r["name"]
        if len(name) > 25:
            name = name[:24] + "…"
        flag = "!" if r["qty"] <= r["min_qty"] else " "
        out.append(f"{i:<3}{name:<26}{r['qty']:>7g}{r['min_qty']:>7g}{flag}")
    out.append("")

    low = [r["name"] for r in rows if r["qty"] <= r["min_qty"]]
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
        kb.add_button("➕ Приход материала", color=VkKeyboardColor.SECONDARY)
        kb.add_line()
        kb.add_button("✏️ Корректировка остатков", color=VkKeyboardColor.SECONDARY)
    else:
        kb.add_line()
        kb.add_button("📦 Запросить материал", color=VkKeyboardColor.POSITIVE)
        kb.add_line()
        kb.add_button("📄 Мои заявки", color=VkKeyboardColor.SECONDARY)

    if role == "admin":
        kb.add_line()
        kb.add_button("📊 Сводка", color=VkKeyboardColor.PRIMARY)

    return kb.get_keyboard()


def back_kb() -> str:
    kb = VkKeyboard(one_time=False)
    kb.add_button("⬅️ В меню", color=VkKeyboardColor.SECONDARY)
    return kb.get_keyboard()


def stock_kb() -> str:
    kb = VkKeyboard(one_time=False)
    kb.add_button("🔄 Обновить", color=VkKeyboardColor.PRIMARY)
    kb.add_line()
    kb.add_button("⬅️ В меню", color=VkKeyboardColor.SECONDARY)
    return kb.get_keyboard()


# ======================= FSM (машина состояний) =======================
# Простая реализация через словарь: {user_id: {"state": ..., "data": {...}}}
fsm_storage = {}


def set_state(user_id, state, **data):
    fsm_storage[user_id] = {"state": state, "data": data}


def get_state(user_id):
    return fsm_storage.get(user_id, {"state": None, "data": {}})


def clear_state(user_id):
    fsm_storage.pop(user_id, None)


# ======================= ОТПРАВКА СООБЩЕНИЙ =======================
def send(vk, user_id, text, keyboard=None):
    vk.messages.send(
        user_id=user_id,
        message=text,
        keyboard=keyboard,
        random_id=get_random_id(),
    )


# ======================= ОСНОВНАЯ ЛОГИКА =======================
def main():
    init_db()

    vk_session = VkApi(token=GROUP_TOKEN)
    vk = vk_session.get_api()
    longpoll = VkBotLongPoll(vk_session, GROUP_ID)

    print("Бот запущен. Ожидание сообщений...")

    for event in longpoll.listen():
        if event.type != VkBotEventType.MESSAGE_NEW:
            continue

        user_id = event.obj.message["from_id"]
        text = event.obj.message["text"].strip()

        # Получаем информацию о пользователе
        try:
            user_info = vk.users.get(user_ids=user_id)[0]
            full_name = f"{user_info['first_name']} {user_info['last_name']}"
            username = user_info.get("screen_name", "")
        except Exception:
            full_name = str(user_id)
            username = ""

        # Создаём пользователя в БД, если его нет
        ensure_user(type("User", (), {"id": user_id, "username": username, "full_name": full_name}))

        # ---------- Обработка callback-кнопок (message_event) ----------
        if event.type == VkBotEventType.MESSAGE_EVENT:
            payload = event.obj.payload
            command = payload.get("command")

            # Отвечаем на callback, чтобы у пользователя не крутился индикатор
            vk.messages.sendMessageEventAnswer(
                event_id=event.obj.event_id,
                user_id=user_id,
                peer_id=event.obj.peer_id,
            )

            if command == "stock_refresh":
                send(vk, user_id, render_stock(), stock_kb())
            elif command == "menu":
                clear_state(user_id)
                send(vk, user_id, "Главное меню:", main_menu(user_id))
            elif command == "req_new":
                rows = all_materials()
                kb = VkKeyboard(one_time=False)
                for i, r in enumerate(rows):
                    if i > 0:
                        kb.add_line()
                    kb.add_button(f"{r['name']} — {r['qty']:g} {r['unit']}",
                                  color=VkKeyboardColor.PRIMARY,
                                  payload={"command": f"req_mat:{r['id']}"})
                kb.add_line()
                kb.add_button("⬅️ В меню", color=VkKeyboardColor.SECONDARY,
                              payload={"command": "menu"})
                send(vk, user_id, "📦 Выберите материал для заявки:", kb.get_keyboard())
            elif command.startswith("req_mat:"):
                mid = int(command.split(":")[1])
                m = get_material(mid)
                if not m:
                    send(vk, user_id, "Материал не найден.")
                    continue
                set_state(user_id, "req_qty", material_id=mid)
                send(vk, user_id,
                     f"📦 {m['name']}\nОстаток: {m['qty']:g} {m['unit']}\n\n"
                     f"Введите нужное количество (число):")
            elif command == "req_my":
                with db() as con:
                    rows = con.execute(
                        """SELECT r.*, m.name, m.unit FROM requests r
                           JOIN materials m ON m.id = r.material_id
                           WHERE r.user_id = ? ORDER BY r.id DESC LIMIT 15""",
                        (user_id,),
                    ).fetchall()
                if not rows:
                    send(vk, user_id, "У вас пока нет заявок.", back_kb())
                else:
                    lines = ["📄 Мои заявки", ""]
                    for r in rows:
                        lines.append(
                            f"№{r['id']} • {r['name']} — {r['qty']:g} {r['unit']}\n"
                            f"   {STATUS.get(r['status'], r['status'])} • {r['created_at'][:16]}"
                        )
                    send(vk, user_id, "\n".join(lines), back_kb())
            elif command == "wh_requests":
                if not is_warehouse(user_id):
                    send(vk, user_id, "Нет доступа.")
                    continue
                with db() as con:
                    rows = con.execute(
                        """SELECT r.*, m.name, m.unit, u.full_name FROM requests r
                           JOIN materials m ON m.id = r.material_id
                           LEFT JOIN users u ON u.user_id = r.user_id
                           WHERE r.status IN ('new','approved')
                           ORDER BY r.id"""
                    ).fetchall()
                if not rows:
                    send(vk, user_id, "📥 Активных заявок нет.", back_kb())
                else:
                    send(vk, user_id, f"📥 Активных заявок: {len(rows)}", back_kb())
                    for r in rows:
                        text_msg = (
                            f"Заявка №{r['id']}\n"
                            f"Материал: {r['name']}\n"
                            f"Кол-во: {r['qty']:g} {r['unit']}\n"
                            f"От: {r['full_name'] or r['user_id']}\n"
                            f"Комментарий: {r['comment'] or '—'}\n"
                            f"Статус: {STATUS.get(r['status'], r['status'])}"
                        )
                        kb = VkKeyboard(one_time=False)
                        kb.add_button("✅ Выдать", color=VkKeyboardColor.POSITIVE,
                                      payload={"command": f"wh_issue:{r['id']}"})
                        kb.add_button("❌ Отклонить", color=VkKeyboardColor.NEGATIVE,
                                      payload={"command": f"wh_reject:{r['id']}"})
                        send(vk, user_id, text_msg, kb.get_keyboard())
            elif command.startswith("wh_issue:"):
                if not is_warehouse(user_id):
                    send(vk, user_id, "Нет доступа.")
                    continue
                rid = int(command.split(":")[1])
                with db() as con:
                    r = con.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
                    if not r or r["status"] not in ("new", "approved"):
                        send(vk, user_id, "Заявка уже обработана.")
                        continue
                    m = con.execute("SELECT * FROM materials WHERE id=?", (r["material_id"],)).fetchone()
                    if m["qty"] < r["qty"]:
                        send(vk, user_id, f"На складе только {m['qty']:g} {m['unit']}.")
                        continue
                    con.execute(
                        "UPDATE materials SET qty = qty - ?, updated_at = ? WHERE id = ?",
                        (r["qty"], now_str(), m["id"]),
                    )
                    con.execute(
                        "UPDATE requests SET status='issued', updated_at=? WHERE id=?",
                        (now_str(), rid),
                    )
                    con.commit()
                send(vk, user_id,
                     f"✅ Заявка №{rid} выдана\n{m['name']} — {r['qty']:g} {m['unit']}\n"
                     f"Новый остаток: {m['qty'] - r['qty']:g} {m['unit']}",
                     back_kb())
                try:
                    send(vk, r["user_id"],
                         f"✅ Заявка №{rid} выполнена\n"
                         f"{m['name']} — {r['qty']:g} {m['unit']} можно забирать.")
                except Exception:
                    pass
            elif command.startswith("wh_reject:"):
                if not is_warehouse(user_id):
                    send(vk, user_id, "Нет доступа.")
                    continue
                rid = int(command.split(":")[1])
                with db() as con:
                    r = con.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
                    if not r or r["status"] not in ("new", "approved"):
                        send(vk, user_id, "Заявка уже обработана.")
                        continue
                    con.execute(
                        "UPDATE requests SET status='rejected', updated_at=? WHERE id=?",
                        (now_str(), rid),
                    )
                    con.commit()
                send(vk, user_id, f"❌ Заявка №{rid} отклонена.", back_kb())
                try:
                    send(vk, r["user_id"], f"❌ Заявка №{rid} отклонена кладовщиком.")
                except Exception:
                    pass
            continue

        # ---------- Обработка текстовых сообщений ----------
        # Проверяем, есть ли активное состояние FSM
        state_info = get_state(user_id)
        current_state = state_info["state"]

        if current_state == "req_qty":
            try:
                qty = float(text.replace(",", "."))
                if qty <= 0:
                    raise ValueError
            except ValueError:
                send(vk, user_id, "❗ Введите положительное число, например: 25")
                continue
            set_state(user_id, "req_comment", material_id=state_info["data"]["material_id"], qty=qty)
            send(vk, user_id, "💬 Комментарий к заявке (или «-», чтобы пропустить):")
            continue

        if current_state == "req_comment":
            comment = "" if text == "-" else text
            mid = state_info["data"]["material_id"]
            qty = state_info["data"]["qty"]
            m = get_material(mid)
            with db() as con:
                cur = con.execute(
                    "INSERT INTO requests (user_id, material_id, qty, comment, status, created_at, updated_at) "
                    "VALUES (?,?,?,?, 'new', ?, ?)",
                    (user_id, mid, qty, comment, now_str(), now_str()),
                )
                con.commit()
                rid = cur.lastrowid
            clear_state(user_id)
            send(vk, user_id,
                 f"✅ Заявка №{rid} отправлена\n{m['name']} — {qty:g} {m['unit']}\n"
                 f"Кладовщик получил уведомление.", main_menu(user_id))
            # Уведомление кладовщикам
            with db() as con:
                rows = con.execute(
                    "SELECT user_id FROM users WHERE role IN ('warehouse','admin')"
                ).fetchall()
            for r in rows:
                try:
                    send(vk, r["user_id"],
                         f"🔔 Новая заявка №{rid}\n"
                         f"Материал: {m['name']}\n"
                         f"Кол-во: {qty:g} {m['unit']}\n"
                         f"От: {full_name}\n"
                         f"Комментарий: {comment or '—'}")
                except Exception:
                    pass
            continue

        # ---------- Обработка команд главного меню ----------
        if text == "/start" or text == "Начать":
            clear_state(user_id)
            role = get_role(user_id)
            role_ru = {"operator": "станочник", "warehouse": "кладовщик (погрузчик)", "admin": "администратор"}[role]
            send(vk, user_id,
                 f"👋 Складской бот «Пиломатериалы»\n\n"
                 f"Вы вошли как: {role_ru}\n\n"
                 f"Выберите действие:", main_menu(user_id))
        elif text == "📋 Остатки на складе":
            send(vk, user_id, render_stock(), stock_kb())
        elif text == "📦 Запросить материал":
            rows = all_materials()
            kb = VkKeyboard(one_time=False)
            for i, r in enumerate(rows):
                if i > 0:
                    kb.add_line()
                kb.add_button(f"{r['name']} — {r['qty']:g} {r['unit']}",
                              color=VkKeyboardColor.PRIMARY,
                              payload={"command": f"req_mat:{r['id']}"})
            kb.add_line()
            kb.add_button("⬅️ В меню", color=VkKeyboardColor.SECONDARY,
                          payload={"command": "menu"})
            send(vk, user_id, "📦 Выберите материал для заявки:", kb.get_keyboard())
        elif text == "📄 Мои заявки":
            with db() as con:
                rows = con.execute(
                    """SELECT r.*, m.name, m.unit FROM requests r
                       JOIN materials m ON m.id = r.material_id
                       WHERE r.user_id = ? ORDER BY r.id DESC LIMIT 15""",
                    (user_id,),
                ).fetchall()
            if not rows:
                send(vk, user_id, "У вас пока нет заявок.", back_kb())
            else:
                lines = ["📄 Мои заявки", ""]
                for r in rows:
                    lines.append(
                        f"№{r['id']} • {r['name']} — {r['qty']:g} {r['unit']}\n"
                        f"   {STATUS.get(r['status'], r['status'])} • {r['created_at'][:16]}"
                    )
                send(vk, user_id, "\n".join(lines), back_kb())
        elif text == "📥 Заявки станочников":
            if not is_warehouse(user_id):
                send(vk, user_id, "Нет доступа.")
                continue
            with db() as con:
                rows = con.execute(
                    """SELECT r.*, m.name, m.unit, u.full_name FROM requests r
                       JOIN materials m ON m.id = r.material_id
                       LEFT JOIN users u ON u.user_id = r.user_id
                       WHERE r.status IN ('new','approved')
                       ORDER BY r.id"""
                ).fetchall()
            if not rows:
                send(vk, user_id, "📥 Активных заявок нет.", back_kb())
            else:
                send(vk, user_id, f"📥 Активных заявок: {len(rows)}", back_kb())
                for r in rows:
                    text_msg = (
                        f"Заявка №{r['id']}\n"
                        f"Материал: {r['name']}\n"
                        f"Кол-во: {r['qty']:g} {r['unit']}\n"
                        f"От: {r['full_name'] or r['user_id']}\n"
                        f"Комментарий: {r['comment'] or '—'}\n"
                        f"Статус: {STATUS.get(r['status'], r['status'])}"
                    )
                    kb = VkKeyboard(one_time=False)
                    kb.add_button("✅ Выдать", color=VkKeyboardColor.POSITIVE,
                                  payload={"command": f"wh_issue:{r['id']}"})
                    kb.add_button("❌ Отклонить", color=VkKeyboardColor.NEGATIVE,
                                  payload={"command": f"wh_reject:{r['id']}"})
                    send(vk, user_id, text_msg, kb.get_keyboard())
        elif text == "➕ Приход материала":
            if not is_warehouse(user_id):
                send(vk, user_id, "Нет доступа.")
                continue
            rows = all_materials()
            kb = VkKeyboard(one_time=False)
            for i, r in enumerate(rows):
                if i > 0:
                    kb.add_line()
                kb.add_button(f"➕ {r['name']}", color=VkKeyboardColor.POSITIVE,
                              payload={"command": f"inc_mat:{r['id']}"})
            kb.add_line()
            kb.add_button("🆕 Новый материал", color=VkKeyboardColor.PRIMARY,
                          payload={"command": "inc_new"})
            kb.add_line()
            kb.add_button("⬅️ В меню", color=VkKeyboardColor.SECONDARY,
                          payload={"command": "menu"})
            send(vk, user_id, "➕ Приход материала\nВыберите позицию:", kb.get_keyboard())
        elif text == "✏️ Корректировка остатков":
            send(vk, user_id, "Функция в разработке.", back_kb())
        elif text == "📊 Сводка":
            if get_role(user_id) != "admin":
                send(vk, user_id, "Нет доступа.")
                continue
            send(vk, user_id, "Сводка в разработке.", back_kb())
        else:
            send(vk, user_id, "Не понимаю команду. Воспользуйтесь кнопками меню.", main_menu(user_id))


if __name__ == "__main__":
    main()
