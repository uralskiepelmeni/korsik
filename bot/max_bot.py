# -*- coding: utf-8 -*-
"""MAX-бот «Корсик»: вопросы по документам, опросы, HR-панель в чате, роли, эскалация.

Запуск:  python -m bot.max_bot
"""
import base64
import json
import os
import sys
import threading
import time

import requests
try:
    from bot import scheduler
except ImportError:          # запуск как python bot/max_bot.py
    import scheduler

MAX_BASE = "https://platform-api2.max.ru"
TOKEN = os.environ.get("MAX_TOKEN", "").strip()
API = os.environ.get("KORSIK_API", "http://127.0.0.1:8000").rstrip("/")
CA = os.environ.get("MAX_CA") or True   # True — системные сертификаты; путь — если нужен свой набор
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MARKER_FILE = os.path.join(BASE, "data", ".max_marker")
LOG = os.path.join(BASE, "data", "bot.log")

HELLO = (
    "Здравствуйте! Я Корсик — помощник по адаптации в «Группе ПОЛИПЛАСТИК».\n\n"
    "Спросите про правила распорядка, отпуск, испытательный срок, больничный, командировки, "
    "дисциплину — найду пункт в документах и покажу дословно, со ссылкой на документ и страницу.\n\n"
    "Если ответа в документах нет — передам вопрос кадровой службе."
)
OFFTOPIC = "С этим не помогу: я работаю только по документам и рабочим вопросам компании."

PENDING = {}      # user_id -> (kind, data)  — ожидание текста
MULTI = {}        # мультивыбор в опросах
BUILDER = {}      # user_id -> {"title":..., "questions": [...]}
QMSG = {}         # user_id -> mid сообщения с текущим вопросом
OPTIONS = {}      # user_id -> {"type","text","opts":[...]} — набор вариантов по одному сообщению
SEL = {}          # user_id -> {"sid","qid","value","label","opts"} — выбранный, но не подтверждённый ответ
SEL_Q = {}        # user_id -> последний показанный вопрос
SEL_QIDX = {}     # user_id -> (qid, total)
SENT_MIDS = set()  # mid всех наших сообщений — чтобы не реагировать на себя


RIGHTS_TEXT = (
    "Права ролей\n\n"
    "👤 Сотрудник\n"
    "• задаёт вопросы по документам\n"
    "• проходит опросы, видит свой прогресс\n"
    "• видит документы уровня «для всех»\n"
    "• видит свои обращения и ответы по ним\n\n"
    "🧑‍💼 Руководитель\n"
    "• всё, что сотрудник\n"
    "• видит своих новичков: этап, площадку, дату выхода\n"
    "• получает «3 вещи на сегодня» в ключевые дни\n"
    "• НЕ видит опросы и переписку новичка\n\n"
    "🧑‍💻 Кадровая служба (HR)\n"
    "• всё, что сотрудник\n"
    "• обращения: очередь, ответ сотруднику, закрытие\n"
    "• документы: загрузка и удаление, уровень «для всех» и «кадровая служба»\n"
    "• сотрудники: заведение, пауза рассылок, привязка руководителя\n"
    "• конструктор опросов, сводка за неделю\n"
    "• не может менять роли и создавать документы уровня «администратор»\n\n"
    "⚙️ Администратор\n"
    "• всё, что HR\n"
    "• назначает роли (employee / manager / hr / admin)\n"
    "• документы любого уровня, включая «администратор»"
)

TITLES = {"day3": "3-й рабочий день", "week2": "Через 2 недели",
          "month": "Через месяц", "final": "Конец испытательного срока"}


def log(m):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {m}\n")
    except Exception:
        pass
    print(m, flush=True)


def api(method, path, **kw):
    r = requests.request(method, MAX_BASE + path, headers={"Authorization": TOKEN},
                         verify=CA, timeout=kw.pop("timeout", 60), **kw)
    if r.status_code >= 400:
        log(f"! {method} {path} -> {r.status_code} {r.text[:200]}")
    return r


def send(user_id=None, chat_id=None, text="", buttons=None):
    params = {"chat_id": chat_id} if chat_id else ({"user_id": user_id} if user_id else {})
    body = {"text": (text or "")[:3900], "notify": True}
    if buttons:
        body["attachments"] = [{"type": "inline_keyboard", "payload": {"buttons": buttons}}]
    r = api("POST", "/messages", params=params, json=body)
    if r.status_code >= 400 and buttons:
        r = api("POST", "/messages", params=params, json={"text": (text or "")[:3900], "notify": True})
    log(f"send {params} -> {r.status_code}")
    try:
        mid = ((r.json().get("message") or {}).get("body") or {}).get("mid")
    except Exception:
        mid = None
    if mid:
        SENT_MIDS.add(str(mid))
        if len(SENT_MIDS) > 3000:
            SENT_MIDS.clear()
    return mid


def svc(path, payload=None, method="POST"):
    url = f"{API}{path}"
    r = requests.get(url, params=payload or {}, timeout=200) if method == "GET" else requests.post(url, json=payload, timeout=200)
    r.raise_for_status()
    return r.json()


BOT_ID = {"v": None}


def bot_id():
    if BOT_ID["v"] is None:
        try:
            BOT_ID["v"] = int(api("GET", "/me").json().get("user_id") or 0)
        except Exception:
            BOT_ID["v"] = 0
    return BOT_ID["v"]


def edit(mid, text, buttons=None):
    if not mid:
        return
    body = {"text": (text or "")[:3900]}
    if buttons:
        body["attachments"] = [{"type": "inline_keyboard", "payload": {"buttons": buttons}}]
    r = api("PUT", "/messages", params={"message_id": mid}, json=body)
    log(f"edit {mid} -> {r.status_code}")


def delete(mid):
    if not mid:
        return
    r = api("DELETE", "/messages", params={"message_id": mid})
    log(f"delete {mid} -> {r.status_code}")


def role_of(user_id):
    if str(user_id) == str(bot_id()):
        return "employee"
    for attempt in (1, 2):
        try:
            r = svc("/whoami", {"user_id": str(user_id)}, method="GET")
            log(f"whoami {user_id} -> {r.get('role')} ({API})")
            return r.get("role", "employee")
        except Exception as e:
            log(f"! whoami {user_id} попытка {attempt}: {e} ({API})")
    return "employee"


def btn(text, payload):
    return {"type": "callback", "text": text, "payload": payload}


BACK = [btn("← Назад", "nav:main")]


def main_menu(role):
    role = role if role in ("employee", "manager", "hr", "admin") else "employee"
    rows = [[btn("Опросы", "nav:surveys"), btn("Документы", "nav:docs")]]
    if role in ("hr", "admin"):
        rows.append([btn("Обращения", "nav:tickets"), btn("Сотрудники", "nav:team")])
        rows.append([btn("Опросы: конструктор", "nav:builder"), btn("Сводка", "nav:digest")])
        rows.append([btn("Права ролей", "nav:rights"), btn("Роли", "nav:roles")])
    elif role == "manager":
        rows.append([btn("Мои новички", "nav:mgr")])
    return rows


def fmt(a):
    q = " ".join((a.get("quote") or "").split())
    src = f'Источник: {a.get("doc")}, п. {a.get("clause")}'
    if a.get("page"):
        src += f", стр. {a['page']}"
    return f"«{q}»\n\n{src}"


def docs_view(user_id, role="employee"):
    d = svc("/documents", {"user_id": str(user_id)}, method="GET")
    lines = ["Доступно для вас:"]
    for x in d.get("docs", []):
        lvl = {"public": "для всех", "hr": "кадровая служба", "admin": "администратор"}.get(x["level"], x["level"])
        lines.append(f"• {x['title']} — {x['clauses']} фрагм. ({lvl})")
    rows = [BACK]
    if role in ("hr", "admin"):
        rows = [[btn("Добавить документ", "nav:adddoc")], BACK]
    return "\n".join(lines), rows


def surveys_view(user_id):
    d = svc("/surveys", {"user_id": str(user_id)}, method="GET")
    rows, lines = [], ["Опросы об адаптации:"]
    for s in d.get("surveys", []):
        st = {"not_started": "не начат", "active": "в процессе", "done": "пройден"}.get(s["status"], s["status"])
        title = s.get("title") or TITLES.get(s["survey_id"], s["survey_id"])
        lines.append(f"• {title} — {st}")
        mark = "▶ " if s["status"] != "done" else ""
        rows.append([btn(f"{mark}{title}", f"s:{s['survey_id']}")])
    rows.append(BACK)
    return "\n".join(lines), rows


def tickets_view():
    d = svc("/tickets", {"status": "open"}, method="GET")
    ts = d.get("tickets", [])
    if not ts:
        return "Открытых обращений нет.", [BACK]
    lines = [f"Обращения без ответа: {len(ts)}"]
    rows = []
    for t in ts:
        lines.append(f"#{t['id']} · {t['user_id']}: {(t['text'] or '')[:70]}")
        rows.append([btn(f"#{t['id']} — {(t['text'] or '')[:40]}", f"t:{t['id']}")])
    rows.append(BACK)
    return "\n".join(lines), rows


def ticket_card(tid):
    d = svc(f"/tickets/{int(tid)}", method="GET").get("ticket") or {}
    if not d:
        return "Обращение не найдено.", [BACK]
    text = (f"Обращение #{d['id']}\nСотрудник: {d['user_id']}\nСтатус: {d['status']}\n\n"
            f"Вопрос: {d.get('text') or ''}")
    if d.get("answer"):
        text += f"\n\nОтвет: {d['answer']}"
    rows = [[btn("Ответить сотруднику", f"ta:{d['id']}")],
            [btn("Закрыть обращение", f"tc:{d['id']}")],
            [btn("← К обращениям", "nav:tickets"), btn("В меню", "nav:main")]]
    return text, rows


def team_view():
    data = scheduler.load()
    if not data:
        return "Сотрудников нет.\nHR может завести первого: команда /hire или кнопка ниже.", \
               [[btn("Как завести сотрудника", "nav:team_help")], BACK]
    lines = ["Сотрудники в адаптации:"]
    rows = []
    for uid, e in data.items():
        day = scheduler.stage_day(e)
        lines.append(f"• {e.get('name')} · {e.get('platform')} · день {day if day is not None else '—'}"
                     f"{' · пауза' if e.get('paused') else ''}")
        rows.append([btn(f"{e.get('name')}", f"e:{uid}")])
    rows.append([btn("Как завести сотрудника", "nav:team_help")])
    rows.append(BACK)
    return "\n".join(lines), rows


def employee_card(uid):
    data = scheduler.load()
    e = data.get(str(uid))
    if not e:
        return "Сотрудник не найден.", [BACK]
    day = scheduler.stage_day(e)
    text = (f"{e.get('name')}\nПлощадка: {e.get('platform')}\nДата выхода: {e.get('start') or '—'}\n"
            f"День адаптации: {day if day is not None else '—'}\n"
            f"Руководитель: {e.get('manager') or 'не привязан'}\n"
            f"Рассылки: {'на паузе' if e.get('paused') else 'идут'}")
    rows = [[btn("Снять с паузы" if e.get("paused") else "Поставить на паузу", f"tp:{uid}")]]
    rows.append([btn("← К сотрудникам", "nav:team"), btn("В меню", "nav:main")])
    return text, rows


def mgr_view(user_id):
    data = scheduler.load()
    mine = [(u, e) for u, e in data.items() if str(e.get("manager")) == str(user_id)]
    if not mine:
        return ("За вами пока никто не закреплён. Кадровая служба привяжет новичка — "
                "тогда здесь появятся его этап и ваши действия."), [BACK]
    lines = ["Ваши новички:"]
    rows = []
    for u, e in mine:
        day = scheduler.stage_day(e)
        lines.append(f"• {e.get('name')} · день {day if day is not None else '—'}")
        rows.append([btn(f"{e.get('name')}", f"e:{u}")])
    rows.append(BACK)
    return "\n".join(lines), rows


def _add_question(user_id, qtype, text, options):
    BUILDER.setdefault(str(user_id), {"title": "Опрос от кадровой службы", "questions": []})
    qs = BUILDER[str(user_id)]["questions"]
    q = {"id": f"q{len(qs) + 1}", "type": qtype, "text": text}
    if options:
        q["options"] = options
    qs.append(q)
    return q


def roles_menu():
    try:
        users = svc("/users", method="GET").get("users", [])
    except Exception:
        users = []
    rows = []
    for u in users[:20]:
        uid = u.get("user_id")
        if str(uid).isdigit() and len(str(uid)) > 6:
            rows.append([btn(f"{uid} · {u.get('role')}", f"r:{uid}")])
    rows.append([btn("Моя роль", f"r:{0}")])
    rows.append(BACK)
    return rows


def role_card(uid, me):
    target = me if str(uid) == "0" else str(uid)
    cur = svc("/whoami", {"user_id": target}, method="GET")
    text = f"Пользователь {target}\nТекущая роль: {cur.get('role')}"
    rows = []
    for r in ("employee", "manager", "hr", "admin"):
        mark = "✓ " if cur.get("role") == r else ""
        rows.append([btn(f"{mark}{r}", f"rs:{target}:{r}")])
    rows.append([btn("← К списку", "nav:roles"), BACK])
    return text, rows


def builder_view(user_id):
    b = BUILDER.get(str(user_id)) or {"title": "Опрос от кадровой службы", "questions": []}
    BUILDER[str(user_id)] = b
    lines = [f"Конструктор опросов\nВопросов: {len(b['questions'])}"]
    for i, q in enumerate(b["questions"], 1):
        lines.append(f"{i}. {q['text']} ({q['type']})")
    rows = [[btn("Добавить вопрос", "b:type")]]
    if b["questions"]:
        rows.append([btn("Сохранить опрос", "b:save"), btn("Сбросить", "b:reset")])
    rows.append(BACK)
    return "\n".join(lines), rows


def builder_types():
    return ("Какой тип вопроса?", [
        [btn("Да / Нет", "b:t:yesno"), btn("Шкала 1–10", "b:t:scale")],
        [btn("Один вариант из списка", "b:t:single"), btn("Несколько вариантов", "b:t:multi")],
        [btn("Свободный текст", "b:t:text")],
        [btn("← Назад", "nav:builder")],
    ])


def register_commands():
    cmds = {"commands": [
        {"name": "start", "description": "Главное меню"},
        {"name": "survey", "description": "Опросы"},
        {"name": "docs", "description": "Документы"},
        {"name": "whoami", "description": "Моя роль"},
    ]}
    r = api("PATCH", "/me/commands", json=cmds)
    log(f"commands -> {r.status_code}")


def question_view(q, sid, sel=None):
    head = q["text"]
    t = q["type"]
    mark = lambda label, value: ("✓ " if sel and str(sel.get("value")) == str(value) else "") + label
    if t == "scale":
        nums = [str(i) for i in range(1, 11)]
        rows = [[btn(mark(n, n), f"a:{sid}:{q['qid']}:{n}") for n in nums[0:5]],
                [btn(mark(n, n), f"a:{sid}:{q['qid']}:{n}") for n in nums[5:10]]]
        return head + "\n\nОцените от 1 до 10:", rows
    if t == "yesno":
        return head, [[btn(mark("Да", "да"), f"a:{sid}:{q['qid']}:да"),
                       btn(mark("Нет", "нет"), f"a:{sid}:{q['qid']}:нет")]]
    if t == "single":
        return head, [[btn(mark(o[:60], o), f"a:{sid}:{q['qid']}:{i}")]
                      for i, o in enumerate(q.get("options") or [])]
    if t == "multi":
        chosen = set((sel or {}).get("opts") or [])
        rows = [[btn(("✓ " if str(i) in chosen else "") + o[:60], f"m:{sid}:{q['qid']}:{i}")]
                for i, o in enumerate(q.get("options") or [])]
        rows.append([btn("Продолжить" if sel else "Готово", f"md:{sid}:{q['qid']}")])
        return head + "\n\nМожно выбрать несколько. Отметьте и нажмите «Готово».", rows
    return head + "\n\nНапишите ответ сообщением.", None


def render_question(user_id, chat_id, sid, q, sel=None, confirm=False):
    """Один и тот же пузырь: либо вопрос с кнопками, либо подтверждение выбора."""
    head = f"{q['idx'] + 1}/{q['total']} · {q['text']}"
    if confirm and sel:
        label = sel.get("label") or sel.get("value")
        base_text, base_buttons = question_view(q, sid, sel)
        mark_line = f"\n\nВыбрано: ✓ {label}" if label else ""
        if q.get("type") == "text":
            text = f"{head}\n\nВаш ответ: {label}\n\nНажмите «Продолжить», если всё верно."
            buttons = [[btn("Продолжить", f"c:{sid}:{q['qid']}")]]
        else:
            text = f"{base_text}{mark_line}\n\nМожно выбрать другой вариант или нажать «Продолжить»."
            buttons = list(base_buttons or []) + [[btn("Продолжить", f"c:{sid}:{q['qid']}")]]
    else:
        text, buttons = question_view(q, sid, sel)
        text = f"{q['idx'] + 1}/{q['total']} · " + text.split("] ", 1)[-1] if text.startswith("[") else text
    prev = QMSG.get(str(user_id))
    if prev:
        edit(prev["mid"], text, buttons=buttons)
        QMSG[str(user_id)] = {"mid": prev["mid"], "text": text}
        return prev["mid"]
    mid = send(chat_id=chat_id, user_id=user_id, text=text, buttons=buttons)
    if mid:
        QMSG[str(user_id)] = {"mid": mid, "text": text}
    return mid


def show_question(user_id, chat_id, sid, q):
    if q.get("done"):
        prev = QMSG.pop(str(user_id), None)
        if prev:
            delete(prev["mid"])          # убираем последний вопрос из чата
        send(chat_id=chat_id, user_id=user_id, text=q.get("text") or "Спасибо!", buttons=[BACK])
        return
    text, buttons = question_view(q, sid)
    if q["type"] == "text":
        PENDING[str(user_id)] = ("answer_text", {"sid": sid, "qid": q["qid"]})
    SEL_Q[str(user_id)] = q
    SEL_QIDX[str(user_id)] = (q["qid"], q["total"])
    prev = QMSG.get(str(user_id))
    head = f"{q['idx'] + 1}/{q['total']} · "
    if prev:
        edit(prev["mid"], head + text, buttons=buttons)      # правим то же сообщение
        QMSG[str(user_id)] = {"mid": prev["mid"], "text": head + text}
    else:
        mid = send(chat_id=chat_id, user_id=user_id, text=head + text, buttons=buttons)
        if mid:
            QMSG[str(user_id)] = {"mid": mid, "text": head + text}


# ─────────────────────── обработка текста ───────────────────────
def handle_pending(text, chat_id, user_id, role):
    p = PENDING.get(str(user_id))
    if not p:
        return False
    kind, data = p
    PENDING.pop(str(user_id), None)

    if kind == "answer_text":
        SEL[str(user_id)] = {"sid": data["sid"], "qid": data["qid"], "value": text[:300], "label": text[:120]}
        q = SEL_Q.get(str(user_id)) or {"idx": 0, "total": 1, "qid": data["qid"], "text": "Вопрос", "type": "text"}
        render_question(user_id, chat_id, data["sid"], q, sel=SEL[str(user_id)], confirm=True)
        return True

    if kind == "ticket_answer":
        res = svc("/tickets/answer", {"id": data["id"], "answer": text, "hr_id": str(user_id)})
        if res.get("ok"):
            send(user_id=res["user_id"],
                 text=f"Ответ кадровой службы по вашему вопросу:\n\n{text}")
            send(chat_id=chat_id, user_id=user_id, text=f"Ответ отправлен сотруднику (обращение #{data['id']}).",
                 buttons=[BACK])
        else:
            send(chat_id=chat_id, user_id=user_id, text=f"Не получилось: {res.get('error')}", buttons=[BACK])
        return True

    if kind == "b_q_text":
        qtype = data["type"]
        if qtype in ("single", "multi"):
            OPTIONS[str(user_id)] = {"type": qtype, "text": text[:300], "opts": []}
            PENDING[str(user_id)] = ("b_opt", {})
            send(chat_id=chat_id, user_id=user_id,
                 text=("Присылайте варианты ответа по одному сообщению. Когда закончите — "
                       "нажмите «Подтвердить»."),
                 buttons=[[btn("Подтвердить варианты", "b_ok")], [btn("← Отмена", "nav:builder")]])
            return True
        _add_question(user_id, qtype, text[:300], [])
        body, rows = builder_view(user_id)
        send(chat_id=chat_id, user_id=user_id, text="Вопрос добавлен.\n\n" + body, buttons=rows)
        return True

    if kind == "b_opt":
        cur = OPTIONS.get(str(user_id))
        if not cur:
            send(chat_id=chat_id, user_id=user_id, text="Начните заново: «Добавить вопрос».",
                 buttons=[btn("← В конструктор", "nav:builder")])
            return True
        opt = text.strip()[:80]
        if opt and opt not in cur["opts"]:
            cur["opts"].append(opt)
        PENDING[str(user_id)] = ("b_opt", {})
        lines = [f"Вопрос: {cur['text']}", "", f"Варианты ({len(cur['opts'])}):"]
        lines += [f"{i}. {o}" for i, o in enumerate(cur["opts"], 1)]
        lines.append("")
        lines.append("Пришлите следующий вариант или нажмите «Подтвердить».")
        send(chat_id=chat_id, user_id=user_id, text="\n".join(lines),
             buttons=[[btn("Подтвердить варианты", "b_ok")], [btn("← Отмена", "nav:builder")]])
        return True

    if kind == "b_q_opts":
        opts = [o.strip() for o in text.replace(";", ",").split(",") if o.strip()]
        if len(opts) < 2:
            send(chat_id=chat_id, user_id=user_id,
                 text="Нужно минимум два варианта через запятую.", buttons=[btn("← Назад", "nav:builder")])
            return True
        _add_question(user_id, data["type"], data["text"], opts)
        body, rows = builder_view(user_id)
        send(chat_id=chat_id, user_id=user_id,
             text=f"Вопрос добавлен с {len(opts)} вариантами.\n\n" + body, buttons=rows)
        return True

    if kind == "b_title":
        BUILDER.setdefault(str(user_id), {"title": "Опрос", "questions": []})
        BUILDER[str(user_id)]["title"] = text[:120]
        body, rows = builder_view(user_id)
        send(chat_id=chat_id, user_id=user_id, text="Название сохранено.\n\n" + body, buttons=rows)
        return True

    if kind == "hire":
        parts = text.replace(",", " ").split()
        if len(parts) < 2:
            send(chat_id=chat_id, user_id=user_id,
                 text="Нужно: <user_id> <платформа> [ГГГГ-ММ-ДД] [Имя]. Платформы: GrPP, Luzhniki, GTP.",
                 buttons=[BACK])
            return True
        emp = scheduler.add_employee(parts[0], parts[1], parts[2] if len(parts) > 2 else "",
                                     " ".join(parts[3:]) if len(parts) > 3 else "")
        send(chat_id=chat_id, user_id=user_id,
             text=f"Сотрудник заведён: {emp['name']} ({emp['platform']}).", buttons=[BACK])
        try:
            send(user_id=parts[0], text=HELLO, buttons=main_menu(role_of(parts[0])))
        except Exception as e:
            log(f"! приветствие: {e}")
        return True

    if kind == "manager_link":
        parts = text.split()
        if len(parts) < 2:
            send(chat_id=chat_id, user_id=user_id, text="Нужно два user_id: сотрудник и руководитель.",
                 buttons=[BACK])
            return True
        ok = scheduler.set_manager(parts[0], parts[1])
        send(chat_id=chat_id, user_id=user_id,
             text="Руководитель привязан." if ok else "Сотрудник не найден.", buttons=[BACK])
        return True

    return False


def handle_text(text, chat_id, user_id, role):
    low = text.strip().lower()
    if handle_pending(text, chat_id, user_id, role):
        return

    if low.startswith("/start") or low in ("привет", "здравствуйте", "начать", "/help", "помощь"):
        send(chat_id=chat_id, user_id=user_id, text=HELLO, buttons=main_menu(role))
        return
    if low.startswith("/survey"):
        body, rows = surveys_view(user_id)
        send(chat_id=chat_id, user_id=user_id, text=body, buttons=rows)
        return
    if low.startswith("/docs"):
        body, rows = docs_view(user_id, role)
        send(chat_id=chat_id, user_id=user_id, text=body, buttons=rows)
        return
    if low.startswith("/rights") or low.startswith("/roles_help"):
        send(chat_id=chat_id, user_id=user_id, text=RIGHTS_TEXT, buttons=[btn("← В меню", "nav:main")])
        return
    if low.startswith("/whoami"):
        send(chat_id=chat_id, user_id=user_id, text=f"user_id: {user_id}\nроль: {role}")
        return
    if low.startswith("/bootstrap"):
        parts = text.split()
        token = os.environ.get("KORSIK_PANEL_TOKEN", "change-me")
        if token == "change-me":
            send(chat_id=chat_id, user_id=user_id,
                 text="Сначала задайте свой код в .env: KORSIK_PANEL_TOKEN=<придумайте код> и перезапустите бота.")
            return
        if len(parts) > 1 and parts[1] == token:
            svc("/roles/set", {"user_id": str(user_id), "role": "admin"})
            send(chat_id=chat_id, user_id=user_id, text="Вы администратор.", buttons=main_menu("admin"))
        else:
            send(chat_id=chat_id, user_id=user_id, text="Неверный код.")
        return
    if low.startswith("/role") or low.startswith("/roles"):
        if role != "admin":
            return                      # не админ — молчим
        parts = text.split()
        if len(parts) >= 3:             # совместимость: /role <id> <роль>
            r = svc("/roles/set", {"user_id": parts[1], "role": parts[2], "admin_id": str(user_id)})
            send(chat_id=chat_id, user_id=user_id,
                 text=f"Роль {parts[1]} → {r.get('role') or r.get('error')}", buttons=[BACK])
            return
        send(chat_id=chat_id, user_id=user_id, text="Кому меняем роль?",
             buttons=roles_menu())
        return

    if text.startswith("/"):
        send(chat_id=chat_id, user_id=user_id,
             text="Не знаю такой команды. Вот меню:", buttons=main_menu(role))
        return

    try:
        data = svc("/ask", {"user_id": str(user_id), "text": text})
    except Exception as e:
        log(f"! ask {e}")
        send(chat_id=chat_id, user_id=user_id, text="Не смог получить ответ, попробуйте ещё раз.")
        return

    log(f"ask {user_id} role={role} work={data.get('work', {}).get('verdict')} ticket={data.get('ticket_id')}")
    if data.get("work_related") is False:
        send(chat_id=chat_id, user_id=user_id, text=OFFTOPIC, buttons=main_menu(role))
        return

    answers = [r for r in (data.get("replies") or []) if r.get("status") == "answer"]
    if not answers:
        tid = data.get("ticket_id")
        if tid:
            send(chat_id=chat_id, user_id=user_id,
                 text=(f"В документах ответа нет — передал вопрос кадровой службе, обращение #{tid}. "
                       f"Ответ придёт в этот чат."), buttons=[BACK])
            for hr_id in data.get("hr_ids") or []:
                if str(hr_id) == str(user_id):
                    continue
                send(user_id=hr_id,
                     text=f"Новое обращение #{tid} от {user_id}:\n\n{text[:600]}",
                     buttons=[[btn("Ответить", f"ta:{tid}"), btn("Открыть", f"t:{tid}")],
                              [btn("Все обращения", "nav:tickets")]])
        else:
            send(chat_id=chat_id, user_id=user_id,
                 text="В документах, доступных вашей роли, ответа нет.", buttons=[BACK])
        return
    send(chat_id=chat_id, user_id=user_id, text="\n\n— — —\n\n".join(fmt(a) for a in answers))


# ─────────────────────── кнопки ───────────────────────
def handle_callback(upd):
    cb = upd.get("callback") or {}
    cid = cb.get("callback_id")
    if cid:
        try:
            if not svc("/seen", {"callback_id": cid}).get("new", True):
                log(f"skip дубль колбэка {cid}")
                api("POST", "/answers", params={"callback_id": cid}, json={"notification": " "})
                return
        except Exception as e:
            log(f"! seen {e}")
    payload = (cb.get("payload") or "").strip()
    user_id = (cb.get("user") or {}).get("user_id")
    chat_id = (upd.get("message") or {}).get("recipient", {}).get("chat_id")
    role = role_of(user_id)
    hr_ok = role in ("hr", "admin")
    HR_ONLY = ("nav:tickets", "nav:team", "nav:team_help", "nav:team_hire", "nav:manager_link",
               "nav:builder", "nav:digest", "b:type", "b:save", "b:reset")
    if (payload in HR_ONLY or payload.startswith(("t:", "ta:", "tc:", "e:", "tp:", "b:t:"))) and not hr_ok:
        send(chat_id=chat_id, user_id=user_id, text="Этот раздел доступен только кадровой службе.",
             buttons=main_menu(role))
        if cb.get("callback_id"):
            api("POST", "/answers", params={"callback_id": cb["callback_id"]}, json={"notification": " "})
        return

    try:
        if payload in ("nav:main", "help", ""):
            send(chat_id=chat_id, user_id=user_id, text=HELLO, buttons=main_menu(role))
        elif payload == "nav:docs":
            body, rows = docs_view(user_id, role)
            send(chat_id=chat_id, user_id=user_id, text=body, buttons=rows)
        elif payload == "nav:surveys":
            body, rows = surveys_view(user_id)
            send(chat_id=chat_id, user_id=user_id, text=body, buttons=rows)
        elif payload == "nav:tickets" and hr_ok:
            body, rows = tickets_view()
            send(chat_id=chat_id, user_id=user_id, text=body, buttons=rows)
        elif payload == "nav:team" and hr_ok:
            body, rows = team_view()
            send(chat_id=chat_id, user_id=user_id, text=body, buttons=rows)
        elif payload == "nav:team_help" and hr_ok:
            send(chat_id=chat_id, user_id=user_id,
                 text=("Завести сотрудника: пришлите одной строкой\n"
                       "<user_id> <платформа> <ГГГГ-ММ-ДД> <Имя>\n"
                       "например: 123456789 GrPP 2026-11-10 Иван Петров\n"
                       "Платформа: GrPP, Luzhniki или GTP."),
                 buttons=[[btn("Ввести сейчас", "nav:team_hire")], BACK])
        elif payload == "nav:team_hire" and hr_ok:
            PENDING[str(user_id)] = ("hire", {})
            send(chat_id=chat_id, user_id=user_id,
                 text="Отправьте сообщением: user_id, платформа, дата выхода, имя.", buttons=[BACK])
        elif payload == "nav:manager_link" and hr_ok:
            PENDING[str(user_id)] = ("manager_link", {})
            send(chat_id=chat_id, user_id=user_id,
                 text="Отправьте два user_id: сначала сотрудник, потом руководитель.", buttons=[BACK])
        elif payload == "nav:mgr":
            body, rows = mgr_view(user_id)
            send(chat_id=chat_id, user_id=user_id, text=body, buttons=rows)
        elif payload == "nav:builder" and hr_ok:
            body, rows = builder_view(user_id)
            send(chat_id=chat_id, user_id=user_id, text=body, buttons=rows)
        elif payload == "nav:roles":
            if role != "admin":
                send(chat_id=chat_id, user_id=user_id, text="Недоступно.", buttons=main_menu(role))
            else:
                send(chat_id=chat_id, user_id=user_id, text="Выберите пользователя:", buttons=roles_menu())
        elif payload.startswith("r:") and role == "admin":
            body, rows = role_card(payload.split(":", 1)[1], user_id)
            send(chat_id=chat_id, user_id=user_id, text=body, buttons=rows)
        elif payload.startswith("rs:") and role == "admin":
            _, target, newrole = payload.split(":", 2)
            r = svc("/roles/set", {"user_id": target, "role": newrole, "admin_id": str(user_id)})
            body, rows = role_card(target, user_id)
            send(chat_id=chat_id, user_id=user_id,
                 text=f"Готово: {target} → {r.get('role') or r.get('error')}\n\n" + body, buttons=rows)
        elif payload == "nav:adddoc" and hr_ok:
            send(chat_id=chat_id, user_id=user_id,
                 text=("Пришлите документ файлом (PDF, DOCX, PPTX, TXT) — я разберу его и добавлю в базу.\n\n"
                       "Уровень доступа задаётся подписью к файлу:\n"
                       "• без подписи — доступно всем\n"
                       "• «hr» в подписи — только кадровой службе\n"
                       "• «admin» — только администратору"),
                 buttons=[btn("← В меню", "nav:main")])
        elif payload == "nav:rights":
            send(chat_id=chat_id, user_id=user_id, text=RIGHTS_TEXT,
                 buttons=[btn("← В меню", "nav:main")])
        elif payload == "nav:digest" and hr_ok:
            d = svc("/digest", {"days": 7}, method="GET")
            send(chat_id=chat_id, user_id=user_id, text=d.get("summary") or "Пусто.", buttons=[BACK])
        elif payload == "b_ok" and hr_ok:
            cur = OPTIONS.get(str(user_id)) or {}
            opts = cur.get("opts") or []
            if len(opts) < 2:
                send(chat_id=chat_id, user_id=user_id,
                     text=f"Пока {len(opts)} вариант(а). Нужно минимум два — пришлите ещё сообщением.",
                     buttons=[[btn("← Отмена", "nav:builder")]])
            else:
                _add_question(user_id, cur.get("type", "single"), cur.get("text", "Вопрос"), opts)
                OPTIONS.pop(str(user_id), None)
                PENDING.pop(str(user_id), None)
                body, rows = builder_view(user_id)
                send(chat_id=chat_id, user_id=user_id,
                     text=f"Вопрос добавлен с {len(opts)} вариантами.\n\n" + body, buttons=rows)
        elif payload == "b:type":
            text, rows = builder_types()
            send(chat_id=chat_id, user_id=user_id, text=text, buttons=rows)
        elif payload.startswith("b:t:"):
            qtype = payload.split(":")[2]
            BUILDER.setdefault(str(user_id), {"title": "Опрос от кадровой службы", "questions": []})
            PENDING[str(user_id)] = ("b_q_text", {"type": qtype})
            hint = {"yesno": "Текст вопроса (ответ Да/Нет).",
                    "scale": "Текст вопроса (ответ числом от 1 до 10).",
                    "text": "Текст вопроса (свободный ответ).",
                    "single": "Текст вопроса. Затем отдельным сообщением пришлёте варианты.",
                    "multi": "Текст вопроса. Затем отдельным сообщением пришлёте варианты."}.get(qtype, "Текст вопроса.")
            send(chat_id=chat_id, user_id=user_id, text=hint, buttons=[btn("← Назад", "nav:builder")])
        elif payload == "b:save" and hr_ok:
            b = BUILDER.get(str(user_id)) or {}
            if not b.get("questions"):
                send(chat_id=chat_id, user_id=user_id, text="Добавьте хотя бы один вопрос.",
                     buttons=[btn("← Назад", "nav:builder")])
            else:
                r = svc("/surveys/create", {"user_id": str(user_id), "title": b["title"],
                                            "questions": b["questions"]})
                BUILDER.pop(str(user_id), None)
                send(chat_id=chat_id, user_id=user_id,
                     text=(f"Опрос создан: «{r.get('title')}», вопросов {r.get('questions')}. "
                           f"Сотрудники увидят его в разделе «Опросы»."),
                     buttons=[btn("К опросам", "nav:surveys"), BACK])
        elif payload == "b:reset" and hr_ok:
            OPTIONS.pop(str(user_id), None)
            BUILDER.pop(str(user_id), None)
            body, rows = builder_view(user_id)
            send(chat_id=chat_id, user_id=user_id, text=body, buttons=rows)
        elif payload.startswith("t:"):
            body, rows = ticket_card(payload.split(":", 1)[1])
            send(chat_id=chat_id, user_id=user_id, text=body, buttons=rows)
        elif payload.startswith("ta:") and hr_ok:
            tid = payload.split(":", 1)[1]
            PENDING[str(user_id)] = ("ticket_answer", {"id": int(tid)})
            send(chat_id=chat_id, user_id=user_id,
                 text=f"Напишите ответ по обращению #{tid} одним сообщением — я отправлю его сотруднику.",
                 buttons=[btn("← Назад", f"t:{tid}")])
        elif payload.startswith("tc:") and hr_ok:
            tid = payload.split(":", 1)[1]
            svc("/tickets/close", {"id": int(tid), "hr_id": str(user_id)})
            body, rows = tickets_view()
            send(chat_id=chat_id, user_id=user_id, text=f"Обращение #{tid} закрыто.\n\n" + body, buttons=rows)
        elif payload.startswith("e:"):
            body, rows = employee_card(payload.split(":", 1)[1])
            send(chat_id=chat_id, user_id=user_id, text=body, buttons=rows)
        elif payload.startswith("tp:"):
            uid = payload.split(":", 1)[1]
            data = scheduler.load()
            cur = bool((data.get(uid) or {}).get("paused"))
            scheduler.set_paused(uid, not cur)
            body, rows = employee_card(uid)
            send(chat_id=chat_id, user_id=user_id, text=body, buttons=rows)
        elif payload.startswith("s:"):
            sid = payload.split(":", 1)[1]
            r = svc("/surveys/start", {"user_id": str(user_id), "survey_id": sid})
            show_question(user_id, chat_id, sid, r)
        elif payload.startswith("m:"):
            _, sid, qid, i = payload.split(":", 3)
            cur = SEL.setdefault(str(user_id), {"sid": sid, "qid": qid, "opts": [], "value": "", "label": ""})
            if cur.get("qid") != qid:
                cur = SEL[str(user_id)] = {"sid": sid, "qid": qid, "opts": [], "value": "", "label": ""}
            cur.setdefault("opts", [])
            if i in cur["opts"]:
                cur["opts"].remove(i)
            else:
                cur["opts"].append(i)
            q = SEL_Q.get(str(user_id)) or {"idx": 0, "total": 1, "qid": qid, "text": "Вопрос", "type": "multi",
                                            "options": []}
            render_question(user_id, chat_id, sid, q, sel=cur, confirm=False)
        elif payload.startswith("md:"):
            _, sid, qid = payload.split(":", 2)
            cur = SEL.setdefault(str(user_id), {"sid": sid, "qid": qid, "opts": [], "value": "", "label": ""})
            q = SEL_Q.get(str(user_id)) or {"idx": 0, "total": 1, "qid": qid, "text": "Вопрос", "type": "multi",
                                            "options": []}
            labels = [q.get("options")[int(i)] for i in sorted(cur.get("opts") or []) if i.isdigit()
                      and int(i) < len(q.get("options") or [])]
            value = ",".join(sorted(cur.get("opts") or []))
            cur.update({"value": value, "label": ", ".join(labels) or "ничего не выбрано"})
            render_question(user_id, chat_id, sid, q, sel=cur, confirm=True)
        elif payload.startswith("a:"):
            _, sid, qid, val = payload.split(":", 3)
            q = SEL_Q.get(str(user_id)) or {"idx": 0, "total": 1, "qid": qid,
                                            "text": "Вопрос", "type": "single", "options": []}
            label = val
            if q.get("type") == "single" and val.isdigit() and int(val) < len(q.get("options") or []):
                label = q["options"][int(val)]
            SEL[str(user_id)] = {"sid": sid, "qid": qid, "value": val, "label": label}
            render_question(user_id, chat_id, sid, q, sel=SEL[str(user_id)], confirm=True)
        elif payload.startswith("b:"):
            _, sid, qid = payload.split(":", 2)
            q = SEL_Q.get(str(user_id)) or {"idx": 0, "total": 1, "qid": qid,
                                            "text": "Вопрос", "type": "single", "options": []}
            render_question(user_id, chat_id, sid, q, sel=SEL.get(str(user_id)), confirm=True)
        elif payload.startswith("c:"):
            _, sid, qid = payload.split(":", 2)
            sel = SEL.pop(str(user_id), None)
            if not sel:
                log("повторное «продолжить» — игнор")
                if cb.get("callback_id"):
                    api("POST", "/answers", params={"callback_id": cb["callback_id"]}, json={"notification": " "})
                return
            val = sel.get("value") or ""
            r = svc("/surveys/answer", {"user_id": str(user_id), "survey_id": sid, "qid": qid, "value": val})
            if r.get("need_reason"):
                PENDING[str(user_id)] = ("answer_text", {"sid": sid, "qid": qid, "reason": True})
                prompt = r.get("prompt") or "Что именно мешает? Напишите коротко."
                prev = QMSG.get(str(user_id))
                txt = f"{prompt}\n\n(ваш ответ: {sel.get('label') or val})"
                if prev:
                    edit(prev["mid"], txt, buttons=None)
                    QMSG[str(user_id)] = {"mid": prev["mid"], "text": txt}
                else:
                    send(chat_id=chat_id, user_id=user_id, text=txt)
            else:
                show_question(user_id, chat_id, sid, r)
        else:
            send(chat_id=chat_id, user_id=user_id, text=HELLO, buttons=main_menu(role))
    except Exception as e:
        log(f"! callback {payload}: {e}")
        send(chat_id=chat_id, user_id=user_id, text="Что-то сломалось, попробуйте ещё раз.",
             buttons=[BACK])

    if cb.get("callback_id"):
        api("POST", "/answers", params={"callback_id": cb["callback_id"]}, json={"notification": " "})


# ─────────────────────── файлы ───────────────────────
def download_file(att):
    payload = att.get("payload") or {}
    url = payload.get("url") or (f"{MAX_BASE}/files/{payload.get('token')}" if payload.get("token") else None)
    if not url:
        return None, None
    r = requests.get(url, headers={"Authorization": TOKEN}, verify=CA, timeout=120)
    if r.status_code >= 400:
        return None, None
    return r.content, payload.get("filename") or "document"


def handle_file(msg, chat_id, user_id, role):
    all_atts = (msg.get("body") or {}).get("attachments") or []
    files = [a for a in all_atts if a.get("type") in ("file", "document")]
    if not files:
        if all_atts:
            send(chat_id=chat_id, user_id=user_id,
                 text="Вижу вложение, но принять могу только документ (PDF, DOCX, PPTX, TXT).")
            return True
        return False
    if role not in ("hr", "admin"):
        send(chat_id=chat_id, user_id=user_id, text="Добавлять документы может только кадровая служба.")
        return True
    caption = ((msg.get("body") or {}).get("text") or "").lower()
    level = "admin" if (role == "admin" and "admin" in caption) else ("hr" if any(k in caption for k in ("hr", "кадр")) else "public")
    for a in files:
        raw, name = download_file(a)
        if not raw:
            send(chat_id=chat_id, user_id=user_id, text="Не смог скачать файл.")
            continue
        send(chat_id=chat_id, user_id=user_id, text=f"Разбираю «{name}» (уровень: {level})…")
        try:
            res = svc("/admin/upload", {"user_id": str(user_id), "filename": name, "level": level,
                                        "b64": base64.b64encode(raw).decode()})
        except Exception as e:
            log(f"! upload {e}")
            send(chat_id=chat_id, user_id=user_id, text="Ошибка загрузки.")
            continue
        if res.get("ok"):
            send(chat_id=chat_id, user_id=user_id,
                 text=(f"Документ добавлен: {name}\nНарезка: {res['mode']}, фрагментов {res['added']}, "
                       f"уровень {res['level']}. Всего в базе {res['total_chunks']}."),
                 buttons=[btn("В меню", "nav:main")])
        else:
            send(chat_id=chat_id, user_id=user_id, text=f"Не принял: {res.get('error')}")
    return True


def handle(upd):
    ut = upd.get("update_type")
    if ut == "bot_started":
        uid = (upd.get("user") or {}).get("user_id")
        send(chat_id=upd.get("chat_id"), text=HELLO, buttons=main_menu(role_of(uid)))
        return
    if ut == "message_callback":
        handle_callback(upd)
        return
    if ut != "message_created":
        return
    msg = upd.get("message") or {}
    sender = msg.get("sender") or {}
    mid = (msg.get("body") or {}).get("mid")
    if mid and str(mid) in SENT_MIDS:
        log(f"skip own message {mid}")
        return
    if sender.get("is_bot"):
        return
    if not sender:
        return
    if str(sender.get("user_id")) == str(bot_id()):
        log("skip message from bot id")
        return
    rcpt = msg.get("recipient") or {}
    chat_id = rcpt.get("chat_id")
    user_id = sender.get("user_id") or rcpt.get("user_id")   # recipient.user_id — это id бота
    role = role_of(user_id)
    text = ((msg.get("body") or {}).get("text") or "").strip()
    log(f"in {user_id} role={role}: {text[:80]!r}")
    if handle_file(msg, chat_id, user_id, role):
        return
    if not text:
        send(chat_id=chat_id, user_id=user_id, text="Пришлите вопрос текстом.")
        return
    handle_text(text, chat_id, user_id, role)


def main():
    import fcntl
    lock = open(os.path.join(BASE, "data", ".bot.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("другой экземпляр уже работает — выходим")
        sys.exit(0)
    if not TOKEN:
        log("нет MAX_TOKEN")
        sys.exit(2)
    log(f"бот: {api('GET', '/me').status_code}")
    try:
        log(f"сервис: {svc('/health', {}, method='GET')}")
    except Exception as e:
        log(f"! сервис недоступен: {e}")
    register_commands()
    scheduler.start(lambda uid, text: send(user_id=uid, text=text), log_fn=log)

    def reminders():
        reminded, dead = set(), set()
        while True:
            try:
                for p in svc("/surveys/pending", {"older_min": 2 if os.environ.get("KORSIK_DEMO") == "1" else 1440},
                             method="GET").get("pending", []):
                    if not str(p["user_id"]).isdigit() or p["user_id"] in dead:
                        continue          # служебные/тестовые и недоступные id пропускаем
                    key = (p["user_id"], p["survey_id"], p["idx"])
                    if key in reminded:
                        continue
                    reminded.add(key)
                    mid_sent = send(user_id=p["user_id"],
                         text=(f"Незаконченный опрос «{TITLES.get(p['survey_id'], p['survey_id'])}» — "
                               f"осталось {p['idx']} вопросов."),
                         buttons=[[btn("Продолжить", "nav:surveys")]])
                    if not mid_sent:
                        dead.add(p["user_id"])
            except Exception as e:
                log(f"[remind] {e}")
            time.sleep(45)

    threading.Thread(target=reminders, daemon=True).start()

    marker, seen = None, set()
    try:
        marker = int(open(MARKER_FILE).read().strip())
    except Exception:
        pass
    log("старт long polling")
    while True:
        params = {"timeout": 30, "limit": 50}
        if marker:
            params["marker"] = marker
        try:
            r = api("GET", "/updates", params=params, timeout=70)
            if r.status_code != 200:
                time.sleep(3)
                continue
            data = r.json()
            for upd in data.get("updates") or []:
                mid = ((upd.get("message") or {}).get("body") or {}).get("mid") or ""
                key = f"{upd.get('update_type')}:{mid}:{((upd.get('callback') or {}).get('callback_id'))}"
                if key in seen:
                    continue
                seen.add(key)
                if len(seen) > 5000:
                    seen = set(list(seen)[-1000:])
                try:
                    handle(upd)
                except Exception as e:
                    log(f"! handle {e}")
            marker = data.get("marker", marker)
            try:
                open(MARKER_FILE, "w").write(str(marker))
            except Exception:
                pass
        except Exception as e:
            log(f"! poll {e}")
            time.sleep(3)
        time.sleep(0.35)


if __name__ == "__main__":
    main()
