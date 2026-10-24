# -*- coding: utf-8 -*-
"""Планировщик этапов адаптации: календарь из ТЗ + демо-режим (1 минута = 1 день).

Сотрудники хранятся в employees.json, отметки об отправке — там же, поэтому
после перезапуска ничего не дублируется.
"""
import json
import os
import threading
import time
from datetime import date, datetime, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STORE = os.path.join(BASE, "data", "employees.json")
DEMO = os.environ.get("KORSIK_DEMO", "0") == "1"
TICK = 20          # секунд между проверками
TZ_OFFSET = 3      # МСК

# (день адаптации, время, id, текст)
CALENDAR = [
    (-2, "10:00", "pre_docs",
     "До выхода два дня. Список документов для оформления: паспорт, СНИЛС, ИНН, трудовая книжка (или сведения), "
     "диплом{photo}. Если чего-то нет — напишите, подскажу, что делать."),
    (-1, "10:00", "pre_ready",
     "Завтра первый рабочий день. Волнуетесь — это нормально. Возьмите с собой документы, "
     "мы встретим вас на ресепшн. Вопросы можно задавать мне в любое время."),
    (1, "09:00", "d1_hello",
     "Добро пожаловать! Сегодня первый день. Первым делом — оформить пропуск и получить доступы."),
    (1, "11:00", "d1_map",
     "Карта офиса: ресепшн на 1 этаже, ваше рабочее место подскажет руководитель. "
     "Кухня и кофе — на каждом этаже."),
    (1, "12:00", "d1_equip",
     "Проверьте технику: ноутбук, доступ к почте и корпоративному порталу. Если что-то не работает — напишите, "
     "я подскажу порядок получения."),
    (1, "15:00", "d1_company",
     "Немного о компании: «Группа ПОЛИПЛАСТИК» — производитель полимерных трубопроводных систем. "
     "Про качество и экологию есть отдельная политика — могу процитировать."),
    (1, "18:00", "d1_end",
     "Первый день позади. Если остались вопросы по оформлению, доступам или порядкам — спросите сейчас."),
    (2, "10:00", "d2_intro",
     "Сегодня знакомство с командой. Расскажите о себе пару слов — что вам близко по опыту."),
    (3, "09:00", "d3_plan",
     "Адаптационный план готовит руководитель. Уточните у него задачи на испытательный срок — "
     "а я подскажу, что написано в правилах про испытание."),
    (3, "17:30", "d3_survey",
     "Прошло три дня. Ответьте на три коротких вопроса — команда /survey, опрос «3-й рабочий день»."),
    (14, "10:00", "d14_survey",
     "Две недели в компании. Пройдите чек-лист из девяти вопросов — команда /survey, «Через 2 недели». "
     "Если что-то идёт не так, лучше сказать сейчас."),
    (30, "09:00", "d30_month",
     "Первый месяц позади. Как ощущения? Опрос «Через месяц» — в /survey."),
    (51, "09:01", "d51_survey",
     "Скоро встреча с рекрутером. Пока — опрос о настроении и поддержке: /survey."),
    (60, "09:00", "d60_final",
     "Подходит к концу испытательный срок. Итоговый опрос — /survey, «Конец испытательного срока»."),
]

PHOTO = {"GTP": ", фото 3×4"}

# микро-плейбук руководителя: (день адаптации, id, текст)
MANAGER_CALENDAR = [
    (-1, "m_pre", "Завтра выходит новый сотрудник. Три вещи: встретить в 09:50 у ресепшн, "
                  "показать рабочее место, представить команде в первые два часа."),
    (1, "m_d1", "Первый день новичка. Три вещи: убедиться, что доступы и техника работают, "
                "дать первую небольшую задачу, закончить день коротким разговором."),
    (3, "m_d3", "Третий день. Три вещи: согласовать адаптационный план на испытательный срок, "
                "назначить наставника из команды, договориться о встрече раз в неделю."),
    (30, "m_d30", "Месяц. Три вещи: провести встречу по итогам месяца, дать обратную связь по задачам, "
                  "спросить, что мешает работать."),
]


def load():
    if not os.path.exists(STORE):
        return {}
    try:
        return json.load(open(STORE, encoding="utf-8"))
    except Exception:
        return {}


def save(data):
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STORE)


def add_employee(user_id, platform="GrPP", start="", name=""):
    data = load()
    data[str(user_id)] = {"name": name or str(user_id), "platform": platform, "start": start,
                          "added_ts": time.time(), "sent": [], "paused": False}
    save(data)
    return data[str(user_id)]


def set_paused(user_id, paused):
    data = load()
    if str(user_id) in data:
        data[str(user_id)]["paused"] = paused
        save(data)
        return True
    return False


def stage_day(emp):
    if DEMO:
        return int((time.time() - emp.get("added_ts", time.time())) // 60) + 1
    start = emp.get("start")
    if not start:
        return None
    try:
        d0 = date.fromisoformat(start)
    except Exception:
        return None
    return (date.today() - d0).days + 1


def due_events(emp):
    day = stage_day(emp)
    if day is None:
        return []
    sent = set(emp.get("sent") or [])
    out = []
    for d, tm, eid, text in CALENDAR:
        if d != day or eid in sent:
            continue
        if not DEMO:
            now = datetime.now(timezone.utc).astimezone()
            hh, mm = tm.split(":")
            local_now = (datetime.now(timezone.utc).timestamp() + TZ_OFFSET * 3600)
            cur = time.gmtime(local_now)
            if (cur.tm_hour, cur.tm_min) < (int(hh), int(mm)):
                continue
        out.append((eid, text))
    if DEMO:
        return out[:1]   # в демо — по одному сообщению за тик, чтобы не заваливать чат
    return out


def manager_events(emp):
    day = stage_day(emp)
    if day is None:
        return []
    sent = set(emp.get("sent") or [])
    out = []
    for d, eid, text in MANAGER_CALENDAR:
        if d == day and eid not in sent:
            out.append((eid, text))
    return out[:1] if DEMO else out


def set_manager(employee_id, manager_id):
    data = load()
    if str(employee_id) not in data:
        return False
    data[str(employee_id)]["manager"] = str(manager_id)
    save(data)
    return True


def start(send_fn, log_fn=print):
    """Запускает фоновый поток. send_fn(user_id, text) — отправка сообщения."""

    def loop():
        log_fn(f"[sched] старт, демо={'да' if DEMO else 'нет'}")
        while True:
            try:
                data = load()
                changed = False
                for uid, emp in list(data.items()):
                    if emp.get("paused"):
                        continue
                    mgr = emp.get("manager")
                    if mgr:
                        for eid, text in manager_events(emp):
                            send_fn(mgr, text)
                            emp.setdefault("sent", []).append(eid)
                            changed = True
                            log_fn(f"[sched] руководителю {mgr} отправлено {eid}")
                    for eid, text in due_events(emp):
                        plat = emp.get("platform") or "GrPP"
                        msg = text.replace("{photo}", PHOTO.get(plat, ""))
                        send_fn(uid, msg)
                        emp.setdefault("sent", []).append(eid)
                        changed = True
                        log_fn(f"[sched] {uid} отправлено {eid} (день {stage_day(emp)})")
                if changed:
                    save(data)
            except Exception as e:
                log_fn(f"[sched] ошибка: {e}")
            time.sleep(TICK)

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t
