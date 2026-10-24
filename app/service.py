# -*- coding: utf-8 -*-
"""Корсик v4 — документы разного уровня доступа, роли, загрузка файлов, панель.
Без вызова HR: бот отвечает только по документам, доступным роли пользователя.

  POST /ask              {"user_id","text"}                 → ответы по доступным документам
  GET  /documents?user_id=                                  → документы, видимые роли
  POST /admin/upload     {"user_id","filename","b64","title","level"}
  POST /admin/document   {"user_id","title","text","level"}
  GET  /digest?days=7
  GET  /whoami?user_id=
  POST /roles/set        {"user_id","role","admin_id"}
  GET  /panel?token=
  GET  /health
"""
import base64
import io
import json
import os
import re
import sqlite3
import time




BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(BASE, "data", "index.json")
EMB = os.path.join(BASE, "data", "emb.npy")

DB = os.environ.get("KORSIK_DB", os.path.join(BASE, "data", "korsik.db"))
PANEL_TOKEN = os.environ.get("KORSIK_PANEL_TOKEN", "change-me")
THRESHOLD = float(os.environ.get("KORSIK_THRESHOLD", "0.60"))
TOP_K = 8
REFINE_MIN, REFINE_PART, REFINE_GAIN = 900, 600, 0.02

ROLES = ("employee", "manager", "hr", "admin")
ROLE_RANK = {"employee": 1, "manager": 1, "hr": 2, "admin": 3}
LEVEL_RANK = {"public": 1, "hr": 2, "admin": 3}
DEFAULT_LEVELS = {"pvr": "public", "anticorruption": "public", "quality": "public", "secrecy": "hr"}

STATE = {}

SURVEYS = {
    "day3": {
        "title": "Опрос 3-го рабочего дня",
        "intro": "Три коротких вопроса о первых днях.",
        "questions": [
            {"id": "q1", "type": "single", "text": "Были ли вопросы при оформлении документов?",
             "options": ["Всё понятно", "Были вопросы", "Возникли трудности с документами"]},
            {"id": "q2", "type": "single", "text": "Как вас встретили коллеги?",
             "options": ["Отлично, помогли", "Нормально", "Почти никто не помогал"]},
            {"id": "q3", "type": "single", "text": "Понятны ли задачи на испытательный срок?",
             "options": ["Да, понятны", "Не совсем", "Пока ничего не понял"]},
        ],
    },
    "week2": {
        "title": "Чек-лист двух недель",
        "intro": "Два вопроса да/нет и семь оценок от 1 до 10.",
        "questions": [
            {"id": "q1", "type": "yesno", "text": "Понятны ли задачи, которые перед вами ставят?"},
            {"id": "q2", "type": "yesno", "text": "Достаточно ли информации, чтобы выполнять работу?"},
            {"id": "q3", "type": "scale", "text": "Доступность руководителя"},
            {"id": "q4", "type": "scale", "text": "Качество обратной связи от руководителя"},
            {"id": "q5", "type": "scale", "text": "Поддержка коллег"},
            {"id": "q6", "type": "scale", "text": "Настройка доступов и техники"},
            {"id": "q7", "type": "scale", "text": "Своевременность выдачи доступов"},
            {"id": "q8", "type": "scale", "text": "Поддержка HR по рабочим вопросам"},
            {"id": "q9", "type": "scale", "text": "Поддержка HR при трудоустройстве"},
        ],
    },
    "month": {
        "title": "Опрос через месяц",
        "intro": "Как дела с настроением и поддержкой.",
        "questions": [
            {"id": "q1", "type": "single", "text": "С каким настроением приходите на работу?",
             "options": ["С хорошим", "Нейтрально", "Безразличие", "Уже устал, задачи валятся"]},
            {"id": "q2", "type": "multi", "text": "Кто вам помогает? (можно несколько)",
             "options": ["Руководитель", "Коллеги", "Наставник", "HR", "Никто"]},
            {"id": "q3", "type": "single", "text": "Что помогло бы ускорить адаптацию?",
             "options": ["Больше обучения", "Понятнее задачи", "Больше поддержки руководителя", "Другое"]},
        ],
    },
    "final": {
        "title": "Итоговый опрос (конец испытательного срока)",
        "intro": "Что было сложным, остаётесь ли вы, и что улучшить.",
        "questions": [
            {"id": "q1", "type": "single", "text": "Что было самым сложным?",
             "options": ["Задачи и процессы", "Отношения в команде", "Доступы и техника", "Другое"]},
            {"id": "q2", "type": "single", "text": "Планируете продолжить работу?",
             "options": ["Да", "Не уверен", "Нет, задумываюсь об уходе"]},
            {"id": "q3", "type": "scale", "text": "Оценка: руководитель"},
            {"id": "q4", "type": "scale", "text": "Оценка: команда"},
            {"id": "q5", "type": "scale", "text": "Оценка: задачи"},
            {"id": "q6", "type": "scale", "text": "Оценка: доступы и техника"},
            {"id": "q7", "type": "scale", "text": "Оценка: поддержка HR"},
            {"id": "q8", "type": "scale", "text": "Оценка: условия труда"},
            {"id": "q9", "type": "scale", "text": "Оценка: обучение"},
            {"id": "q10", "type": "scale", "text": "Оценка: атмосфера"},
            {"id": "q11", "type": "scale", "text": "Оценка: организация работы"},
            {"id": "q12", "type": "text", "text": "Что порекомендуете улучшить?"},
        ],
    },
}
REASON_OPTIONS = {"q1": None}

THEMES = {
    "dostupy": "доступы и техника",
    "zadachi": "задачи и процессы",
    "rukovoditel": "руководитель",
    "kollegi": "коллеги и команда",
    "hr": "поддержка HR",
    "nagruzka": "нагрузка и переработки",
    "uhod": "намерение уйти",
}


def db():
    conn = sqlite3.connect(DB, timeout=30, isolation_level="IMMEDIATE")
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=FULL")
    except Exception:
        pass
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users(user_id TEXT PRIMARY KEY, role TEXT NOT NULL DEFAULT 'employee',
                                     name TEXT, created REAL);
    CREATE TABLE IF NOT EXISTS docs(id INTEGER PRIMARY KEY AUTOINCREMENT, doc_id TEXT, title TEXT,
                                    chunks INTEGER, level TEXT, added_by TEXT, added_at REAL);
    CREATE TABLE IF NOT EXISTS tickets(
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, text TEXT, kind TEXT DEFAULT 'no_answer',
        status TEXT DEFAULT 'open', answer TEXT, answered_by TEXT, created REAL, updated REAL);
    CREATE TABLE IF NOT EXISTS custom_surveys(
        id TEXT PRIMARY KEY, title TEXT, intro TEXT, questions TEXT, created_by TEXT, created REAL);
    CREATE TABLE IF NOT EXISTS seen_callbacks(callback_id TEXT PRIMARY KEY, ts REAL);
    CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, user_id TEXT,
                                      kind TEXT, payload TEXT);
    """)

    conn.executescript("""
    CREATE TABLE IF NOT EXISTS survey_runs(
        user_id TEXT, survey_id TEXT, idx INTEGER DEFAULT 0, status TEXT DEFAULT 'active',
        started REAL, finished REAL, PRIMARY KEY(user_id, survey_id));
    CREATE TABLE IF NOT EXISTS survey_answers(
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, survey_id TEXT, qid TEXT,
        value TEXT, reason TEXT, themes TEXT, ts REAL);
    """)
    # чистим возможные дубли и навешиваем уникальность
    try:
        conn.execute("DELETE FROM survey_answers WHERE id NOT IN "
                     "(SELECT MAX(id) FROM survey_answers GROUP BY user_id, survey_id, qid)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_survey_answer "
                     "ON survey_answers(user_id, survey_id, qid)")
    except Exception as e:
        print("dedupe init:", e, flush=True)
    conn.commit()
    conn.close()


def get_role(user_id):
    conn = db()
    row = conn.execute("SELECT role FROM users WHERE user_id=?", (str(user_id),)).fetchone()
    conn.close()
    return row["role"] if row else "employee"


def set_role(user_id, role):
    conn = db()
    conn.execute("INSERT INTO users(user_id, role, created) VALUES(?,?,?) "
                 "ON CONFLICT(user_id) DO UPDATE SET role=excluded.role", (str(user_id), role, time.time()))
    conn.commit()
    conn.close()


def event(user_id, kind, payload=""):
    conn = db()
    conn.execute("INSERT INTO events(ts,user_id,kind,payload) VALUES(?,?,?,?)",
                 (time.time(), str(user_id), kind, str(payload)[:500]))
    conn.commit()
    conn.close()


def load_state():
    if STATE.get("ready"):
        return STATE
    import numpy as np
    import torch
    from transformers import AutoModel, AutoTokenizer

    init_db()
    kb = json.load(open(INDEX, encoding="utf-8"))
    for c in kb:                                   # проставляем уровни доступа
        c.setdefault("level", DEFAULT_LEVELS.get(c.get("doc_id"), "public"))
    emb = np.load(EMB)
    emb_model = os.environ.get("KORSIK_EMB_MODEL", "BAAI/bge-m3")
    verifier = os.environ.get("KORSIK_VERIFIER", "vllm-sr/d3-nano")
    device = os.environ.get("KORSIK_DEVICE") or ("cuda" if torch.cuda.is_available() else "cpu")
    etok = AutoTokenizer.from_pretrained(emb_model)
    em = AutoModel.from_pretrained(emb_model, dtype=torch.float16 if device == "cuda" else torch.float32)
    em = em.to(device).eval()
    # dtype верификатора: на GPU bf16, на CPU fp32 — но при нехватке памяти
    # можно задать KORSIK_VERIFIER_DTYPE=bfloat16 (модель займёт вдвое меньше)
    try:
        import accelerate  # noqa: F401  — нужен коду d3-nano (передаёт device_map внутри)
    except ImportError:
        raise RuntimeError("не установлен accelerate — выполните: pip install -r requirements.txt")
    vd = os.environ.get("KORSIK_VERIFIER_DTYPE") or ("bfloat16" if device == "cuda" else "float32")
    vdtype = {"bfloat16": torch.bfloat16, "float16": torch.float16, "float32": torch.float32}[vd]
    print(f"верификатор: {verifier}, dtype={vd}, устройство={device}", flush=True)
    d3 = AutoModel.from_pretrained(verifier, trust_remote_code=True, dtype=vdtype)
    d3 = d3.to(device).eval()
    STATE.update({"kb": kb, "emb": emb, "etok": etok, "em": em, "d3": d3,
                  "torch": torch, "np": np, "device": device, "ready": True})
    return STATE


def embed_texts(texts):
    import torch
    S = load_state()
    out = []
    with torch.no_grad():
        for i in range(0, len(texts), 16):
            enc = S["etok"](texts[i:i + 16], padding=True, truncation=True, max_length=512,
                            return_tensors="pt").to(S["device"])
            h = S["em"](**enc).last_hidden_state[:, 0].float()
            out.append(torch.nn.functional.normalize(h, dim=-1).cpu().numpy())
    return S["np"].concatenate(out)


def d3_ask(state, questions):
    S = load_state()
    try:
        return S["d3"].system_one(state=state[:3000], questions=questions).get("answers", {})
    except Exception as e:
        print("d3 error:", e, flush=True)
        return {}


def d3_score(question, fragment):
    a = d3_ask(f"Вопрос сотрудника: {question}\n\nФрагмент документа: {fragment[:1800]}",
               {"ok": {"type": "noul", "instructions": "Этот фрагмент содержит ответ на вопрос сотрудника"}})
    try:
        return float(a["ok"]["noul"])
    except Exception:
        return 0.0


def scope(text):
    a = d3_ask(text, {
        "work": {"type": "noul", "instructions": ("Сообщение — вопрос сотрудника о работе, правилах компании, "
                                                  "документах, отпусках, больничных, дисциплине, адаптации "
                                                  "или защите информации")},
        "off": {"type": "noul", "instructions": ("Сообщение — посторонняя просьба: погода, рецепт, стихи, анекдот, "
                                                 "пример по математике, фильм, код, совет не по работе")},
        "problem": {"type": "noul", "instructions": ("Сообщение — жалоба на условия РАБОТЫ в компании: доступы, "
                                                     "руководитель, задачи, коллеги, нагрузка, рабочее место, "
                                                     "оборудование, кадровые процедуры. Не еда, не вкус, "
                                                     "не личные дела")}})
    try:
        w, o = round(float(a["work"]["noul"]), 3), round(float(a["off"]["noul"]), 3)
        pr = round(float(a["problem"]["noul"]), 3)
    except Exception:
        return {"work": None, "off": None, "problem": None, "verdict": "work"}
    off_topic = o >= 0.5 and o > 2 * max(w, pr)
    # в HR уходит только явная рабочая проблема: и «проблема», и хоть какой-то рабочий контекст
    escalate = (pr >= 0.6 and w >= 0.15) and not off_topic
    return {"work": w, "off": o, "problem": pr,
            "verdict": "off" if off_topic else "work", "escalate": escalate}


def neg_flag(text):
    a = d3_ask(text, {"neg": {"type": "noul", "instructions": "Сообщение выражает недовольство или проблему"}})
    try:
        return round(float(a["neg"]["noul"]), 3)
    except Exception:
        return None


QW = ("сколько", "когда", "можно", "где", "во сколько", "кто", "как", "что", "какой",
      "какая", "какие", "почему", "зачем", "обязательно", "положено", "нужно")



def all_surveys():
    out = dict(SURVEYS)
    try:
        conn = db()
        for r in conn.execute("SELECT * FROM custom_surveys").fetchall():
            out[r["id"]] = {"title": r["title"], "intro": r["intro"] or "",
                            "questions": json.loads(r["questions"])}
        conn.close()
    except Exception as e:
        print("all_surveys:", e, flush=True)
    return out


def hr_recipients():
    conn = db()
    rows = conn.execute("SELECT user_id FROM users WHERE role IN ('hr','admin')").fetchall()
    conn.close()
    return [r["user_id"] for r in rows]


def themes_of(text):
    """Мульти-метки по свободному тексту: возвращает список ключей THEMES."""
    if not text or len(text.strip()) < 3:
        return []
    qs = {k: {"type": "noul", "instructions": f"Текст касается темы: {v}"} for k, v in THEMES.items()}
    a = d3_ask(text, qs)
    out = []
    for k in THEMES:
        try:
            if float(a[k]["noul"]) >= 0.6:
                out.append(k)
        except Exception:
            continue
    return out


def split_questions(text):
    t = text.strip()
    parts = [p.strip() for p in re.split(r"(?<=[?])\s+", t) if p.strip()]
    if len(parts) <= 1:
        chunks = re.split(r",?\s+и\s+|;\s*", t)
        if len(chunks) > 1 and all(any(w in c.lower() for w in QW) for c in chunks if len(c) > 8):
            parts = [c.strip() for c in chunks if len(c.strip()) > 8]
    out = [p if p.endswith("?") else p + "?" for p in parts if len(p) >= 4]
    return out or [t if t.endswith("?") else t + "?"]


def split_parts(text, size=REFINE_PART, overlap=100):
    words = text.split()
    out, i = [], 0
    while i < len(words):
        chunk, ln, j = [], 0, i
        while j < len(words) and ln < size:
            chunk.append(words[j]); ln += len(words[j]) + 1; j += 1
        out.append(" ".join(chunk))
        if j >= len(words):
            break
        i = max(i + 1, j - max(1, int(overlap / 6)))
    return out


def _hint_docs(q):
    """Какие документы явно названы в вопросе (по корням слов, а не по точной форме)."""
    q = q.lower()
    keys = []
    if ("кодекс" in q and "труд" in q) or "тк рф" in q or "ткрф" in q or " тк " in q:
        keys.append("трудовой кодекс")
    if "пвтр" in q or ("правила" in q and ("распоряд" in q or "внутренн" in q)):
        keys.append("правила внутреннего трудового распорядка")
    if "антикоррупц" in q:
        keys.append("антикоррупционная политика")
    if "коммерческ" in q and "тайн" in q:
        keys.append("коммерческая тайна")
    if ("политик" in q and ("качеств" in q or "эколог" in q)):
        keys.append("политика в области качества")
    return keys


def doc_filter(question, kb, allowed):
    """Если в вопросе прямо назван документ — ищем только по нему."""
    keys = _hint_docs(question)
    if not keys:
        return None
    idx = [i for i, c in enumerate(kb)
           if any(k in (c.get("title") or "").lower() for k in keys)
           and LEVEL_RANK.get(c.get("level", "public"), 1) <= allowed]
    return idx or None


def answer_one(question, role="employee", refine=True):
    S = load_state()
    allowed = ROLE_RANK.get(role, 1)
    qe = embed_texts([question])
    sims = (qe @ S["emb"].T)[0]
    restricted = doc_filter(question, S["kb"], allowed)
    if restricted:
        order = sorted(restricted, key=lambda j: -float(sims[j]))[:TOP_K]
    else:
        order = [j for j in S["torch"].tensor(sims).argsort(descending=True).tolist()
                 if LEVEL_RANK.get(S["kb"][j].get("level", "public"), 1) <= allowed][:TOP_K]

    best = {"score": 0.0, "chunk": None, "quote": None}
    for j in order:
        c = S["kb"][j]
        sc = d3_score(question, c["text"])
        quote = c["text"]
        if refine and len(c["text"]) > REFINE_MIN:
            for p in split_parts(c["text"]):
                ps = d3_score(question, p)
                if ps > sc + REFINE_GAIN:
                    sc, quote = ps, p
        if sc > best["score"]:
            best = {"score": sc, "chunk": c, "quote": quote}

    if best["score"] < THRESHOLD:
        kw = re.sub(r"\b(могу|можно|ли|я|мы|мне|меня|мой|моя|мои|что|как|если|или|а|и|на|по|за|в|во|с|со|у|о|об|это|такое|нужно|должен)\b",
                    " ", question.lower())
        kw = " ".join(kw.split())
        if len(kw) > 8:
            qe2 = embed_texts([kw])
            sims2 = (qe2 @ S["emb"].T)[0]
            if restricted:
                order2 = sorted(restricted, key=lambda j: -float(sims2[j]))[:5]
            else:
                order2 = [j for j in S["torch"].tensor(sims2).argsort(descending=True).tolist()
                          if LEVEL_RANK.get(S["kb"][j].get("level", "public"), 1) <= allowed][:5]
            for j in order2:
                c2 = S["kb"][j]
                sc2 = d3_score(question, c2["text"])
                if sc2 > best["score"]:
                    best = {"score": sc2, "chunk": c2, "quote": c2["text"]}

    c = best["chunk"]
    if c is None or best["score"] < THRESHOLD:
        return {"status": "escalate", "score": round(best["score"], 3), "question": question,
                "reason": "в доступных документах нет ответа"}
    return {"status": "answer", "score": round(best["score"], 3), "question": question,
            "doc": c["title"], "clause": c["clause"], "page": c.get("page"), "doc_id": c.get("doc_id"),
            "level": c.get("level", "public"), "quote": best["quote"].strip()[:1200],
            "refined": best["quote"] != c["text"]}


def _sniff(raw):
    """Тип файла по содержимому, а не по имени: MAX часто не отдаёт расширение."""
    head = raw[:8]
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK"):
        try:
            if b"word/document.xml" in raw[:4000]:
                return "docx"
            if b"ppt/slides/" in raw[:8000] or b"ppt/presentation.xml" in raw[:8000]:
                return "pptx"
        except Exception:
            pass
    return ""


def extract_text(filename, raw):
    name = (filename or "").lower()
    kind = _sniff(raw)
    if kind:
        name = "file." + kind
    if name.endswith(".pdf"):
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw))
        text = "\n".join((p.extract_text() or "") for p in reader.pages)
        if len(text.strip()) < 200:          # скан без текстового слоя → OCR
            try:
                import pytesseract
                from pdf2image import convert_from_bytes
                pages = convert_from_bytes(raw, dpi=200)
                parts = []
                for i, img in enumerate(pages[:200], 1):
                    parts.append(f"[стр. {i}] " + pytesseract.image_to_string(img, lang="rus+eng"))
                ocr = "\n".join(parts)
                print(f"OCR: {len(pages)} стр., {len(ocr)} симв.", flush=True)
                if len(ocr.strip()) > len(text.strip()):
                    return ocr
            except Exception as e:
                print("OCR error:", e, flush=True)
        return text
    if name.endswith(".docx"):
        import docx
        return "\n".join(p.text for p in docx.Document(io.BytesIO(raw)).paragraphs)
    if name.endswith(".pptx"):
        from pptx import Presentation
        prs = Presentation(io.BytesIO(raw))
        out = []
        for i, s in enumerate(prs.slides, 1):
            texts = [sh.text_frame.text for sh in s.shapes if sh.has_text_frame]
            out.append(f"[слайд {i}] " + " ".join(texts))
        return "\n".join(out)
    try:
        return raw.decode("utf-8", errors="ignore")
    except Exception:
        return ""


def ingest_text(title, text, user_id, level="public", doc_id=None):
    text = (text or "").strip()
    if len(text) < 20:
        return {"ok": False, "error": "текст пустой или короче 20 символов"}
    if level not in LEVEL_RANK:
        level = "public"
    doc_id = doc_id or (re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:24] or f"doc{int(time.time())}")
    found = re.findall(r"(?m)^\s*(\d+(?:\.\d+)+)\.?\s+(.+?)(?=\n\s*\d+(?:\.\d+)+\.?\s|\Z)", text, re.S)
    chunks = []
    if found:
        for clause, body in found:
            chunks.append({"cid": f"{doc_id}#{clause}", "doc_id": doc_id, "title": title, "clause": clause,
                           "page": None, "level": level, "text": f"{clause}. {body.strip()}"[:1600]})
        mode = "по пунктам"
    else:
        parts = split_parts(text, 1200, 150)
        for i, p in enumerate(parts, 1):
            chunks.append({"cid": f"{doc_id}#фрагмент{i}", "doc_id": doc_id, "title": title,
                           "clause": f"фрагмент {i}", "page": None, "level": level, "text": p[:1600]})
        mode = f"окнами по 1200 ({len(parts)})"

    S = load_state()
    S["kb"].extend(chunks)
    S["emb"] = S["np"].concatenate([S["emb"], embed_texts(
        [f'{c["title"]} · {c["clause"]}. {c["text"]}' for c in chunks])])
    for c in S["kb"]:
        c.setdefault("level", DEFAULT_LEVELS.get(c.get("doc_id"), "public"))
    json.dump(S["kb"], open(INDEX, "w"), ensure_ascii=False)
    S["np"].save(EMB, S["emb"])
    conn = db()
    conn.execute("INSERT INTO docs(doc_id,title,chunks,level,added_by,added_at) VALUES(?,?,?,?,?,?)",
                 (doc_id, title, len(chunks), level, str(user_id), time.time()))
    conn.commit()
    conn.close()
    pass
    event(user_id, "doc_added", f"{title} [{level}]: {len(chunks)} {mode}")
    return {"ok": True, "doc_id": doc_id, "added": len(chunks), "mode": mode, "level": level,
            "total_chunks": len(S["kb"]), "clauses": [c["clause"] for c in chunks][:15]}


def create_app():
    from fastapi import Body, FastAPI
    from fastapi.responses import HTMLResponse

    init_db()
    api = FastAPI(title="Корсик", docs_url=None, redoc_url=None, openapi_url=None)

    @api.get("/health")
    def health():
        S = load_state()
        return {"ok": True, "chunks": len(S["kb"]), "threshold": THRESHOLD}

    @api.get("/whoami")
    def whoami(user_id: str = ""):
        return {"user_id": user_id, "role": get_role(user_id)}

    @api.post("/roles/set")
    def roles_set(payload: dict = Body(...)):
        uid, role, admin = str(payload.get("user_id")), payload.get("role"), str(payload.get("admin_id") or "")
        if role not in ROLES:
            return {"ok": False, "error": f"роль из {ROLES}"}
        if admin and get_role(admin) not in ("admin", "hr"):
            return {"ok": False, "error": "нет прав"}
        set_role(uid, role)
        event(admin, "role_set", f"{uid} → {role}")
        return {"ok": True, "user_id": uid, "role": role}

    @api.get("/documents")
    def documents(user_id: str = ""):
        S = load_state()
        role = get_role(user_id) if user_id else "employee"
        allowed = ROLE_RANK.get(role, 1)
        agg = {}
        for c in S["kb"]:
            lvl = c.get("level", "public")
            if LEVEL_RANK.get(lvl, 1) > allowed:
                continue
            d = agg.setdefault(c["doc_id"], {"title": c["title"], "clauses": 0, "level": lvl})
            d["clauses"] += 1
        return {"role": role, "docs": [{"doc_id": k, **v} for k, v in agg.items()],
                "total_chunks": sum(d["clauses"] for d in agg.values())}

    @api.post("/admin/upload")
    def upload(payload: dict = Body(...)):
        uid = str(payload.get("user_id") or "")
        role = get_role(uid)
        level = payload.get("level") or "public"
        if role not in ("hr", "admin"):
            return {"ok": False, "error": "загружать документы может только кадровая служба"}
        if LEVEL_RANK.get(level, 1) > ROLE_RANK.get(role, 1):
            return {"ok": False, "error": f"роль {role} не может создавать документы уровня {level}"}
        raw = base64.b64decode(payload.get("b64") or "")
        text = extract_text(payload.get("filename") or "", raw)
        title = (payload.get("title") or "").strip() or (payload.get("filename") or "").strip() or "Документ"
        if title.lower().endswith((".pdf", ".docx", ".pptx", ".txt")):
            title = title.rsplit(".", 1)[0]
        res = ingest_text(title, text, uid, level)
        res["extracted_chars"] = len(text)
        return res

    @api.post("/admin/document")
    def add_document(payload: dict = Body(...)):
        uid = str(payload.get("user_id") or "")
        role = get_role(uid)
        level = payload.get("level") or "public"
        if role not in ("hr", "admin"):
            return {"ok": False, "error": "добавлять документы может только кадровая служба"}
        if LEVEL_RANK.get(level, 1) > ROLE_RANK.get(role, 1):
            return {"ok": False, "error": f"роль {role} не может создавать документы уровня {level}"}
        return ingest_text(payload.get("title") or "Без названия", payload.get("text") or "", uid, level)

    @api.post("/ask")
    def ask(payload: dict = Body(...)):
        text = (payload.get("text") or "").strip()
        user_id = str(payload.get("user_id") or "anon")
        if not text:
            return {"replies": [], "error": "empty text"}
        role = get_role(user_id)
        sc = scope(text)
        subs = split_questions(text)
        replies = [answer_one(q, role) for q in subs]
        neg = neg_flag(text)
        answered_n = sum(1 for r in replies if r["status"] == "answer")
        ticket_id = None
        if sc.get("escalate") and answered_n == 0:
            conn = db()
            cur = conn.execute("INSERT INTO tickets(user_id,text,kind,created,updated) VALUES(?,?,?,?,?)",
                               (user_id, text[:2000], "no_answer", time.time(), time.time()))
            conn.commit()
            ticket_id = cur.lastrowid
            conn.close()
        payload = json.dumps({"role": role, "work": sc.get("verdict"), "subs": len(subs),
                              "answers": answered_n, "neg": neg,
                              "themes": themes_of(text) if sc.get("verdict") == "work" else []},
                             ensure_ascii=False)
        event(user_id, "ask", payload)
        return {"replies": replies, "n_subquestions": len(subs), "negative": neg,
                "work": sc, "work_related": sc.get("verdict") != "off", "role": role,
                "ticket_id": ticket_id, "hr_ids": hr_recipients() if ticket_id else []}

    @api.get("/users")
    def users(role: str = ""):
        conn = db()
        rows = conn.execute("SELECT user_id, role FROM users" + (" WHERE role=?" if role else ""),
                            ((role,) if role else ())).fetchall()
        conn.close()
        return {"users": [dict(r) for r in rows]}

    @api.get("/tickets")
    def tickets(status: str = "open", user_id: str = ""):
        conn = db()
        if user_id:
            rows = conn.execute("SELECT * FROM tickets WHERE user_id=? ORDER BY id DESC LIMIT 30",
                                (str(user_id),)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM tickets WHERE status=? ORDER BY id ASC LIMIT 50",
                                (status,)).fetchall()
        conn.close()
        return {"tickets": [dict(r) for r in rows]}

    @api.get("/tickets/{tid}")
    def ticket_one(tid: int):
        conn = db()
        r = conn.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
        conn.close()
        return {"ticket": dict(r) if r else None}

    @api.post("/tickets/answer")
    def ticket_answer(payload: dict = Body(...)):
        tid = int(payload.get("id") or 0)
        hr = str(payload.get("hr_id") or "")
        if get_role(hr) not in ("hr", "admin"):
            return {"ok": False, "error": "нет прав"}
        conn = db()
        row = conn.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
        if not row:
            conn.close()
            return {"ok": False, "error": "нет такого обращения"}
        conn.execute("UPDATE tickets SET answer=?, answered_by=?, status='answered', updated=? WHERE id=?",
                     (payload.get("answer") or "", hr, time.time(), tid))
        conn.commit()
        conn.close()
        return {"ok": True, "id": tid, "user_id": row["user_id"], "answer": payload.get("answer")}

    @api.post("/tickets/close")
    def ticket_close(payload: dict = Body(...)):
        tid = int(payload.get("id") or 0)
        hr = str(payload.get("hr_id") or "")
        if get_role(hr) not in ("hr", "admin"):
            return {"ok": False, "error": "нет прав"}
        conn = db()
        conn.execute("UPDATE tickets SET status='closed', updated=? WHERE id=?", (time.time(), tid))
        conn.commit()
        conn.close()
        return {"ok": True, "id": tid}

    @api.post("/surveys/create")
    def survey_create(payload: dict = Body(...)):
        uid = str(payload.get("user_id") or "")
        if get_role(uid) not in ("hr", "admin"):
            return {"ok": False, "error": "нет прав"}
        qs = payload.get("questions") or []
        if not qs:
            return {"ok": False, "error": "нужен хотя бы один вопрос"}
        sid = f"custom_{int(time.time()) % 100000}"
        title = payload.get("title") or "Опрос от кадровой службы"
        conn = db()
        conn.execute("INSERT INTO custom_surveys(id,title,intro,questions,created_by,created) VALUES(?,?,?,?,?,?)",
                     (sid, title, payload.get("intro") or "", json.dumps(qs, ensure_ascii=False), uid, time.time()))
        conn.commit()
        conn.close()
        event(uid, "survey_created", f"{sid}: {title} ({len(qs)} вопр.)")
        return {"ok": True, "survey_id": sid, "title": title, "questions": len(qs)}

    @api.post("/seen")
    def seen(payload: dict = Body(...)):
        cid = str(payload.get("callback_id") or "")
        if not cid:
            return {"new": True}
        init_db()
        conn = db()
        try:
            conn.execute("INSERT INTO seen_callbacks(callback_id, ts) VALUES(?,?)", (cid, time.time()))
            conn.commit()
            new = True
        except Exception:
            new = False
        try:
            conn.execute("DELETE FROM seen_callbacks WHERE ts < ?", (time.time() - 86400,))
            conn.commit()
        finally:
            conn.close()
        return {"new": new}

    @api.post("/admin/delete_doc")
    def delete_doc(payload: dict = Body(...)):
        uid = str(payload.get("user_id") or "")
        if get_role(uid) not in ("hr", "admin"):
            return {"ok": False, "error": "нет прав"}
        doc_id = str(payload.get("doc_id") or "")
        S = load_state()
        keep = [c for c in S["kb"] if c.get("doc_id") != doc_id]
        removed = len(S["kb"]) - len(keep)
        if not removed:
            return {"ok": False, "error": "документ не найден"}
        S["kb"] = keep
        S["emb"] = embed_texts([f'{c["title"]} · {c["clause"]}. {c["text"]}' for c in keep])
        json.dump(S["kb"], open(INDEX, "w"), ensure_ascii=False)
        S["np"].save(EMB, S["emb"])
        conn = db()
        conn.execute("DELETE FROM docs WHERE doc_id=?", (doc_id,))
        conn.commit()
        conn.close()
        pass
        return {"ok": True, "removed": removed, "left": len(S["kb"])}

    @api.post("/admin/rename")
    def rename(payload: dict = Body(...)):
        uid = str(payload.get("user_id") or "")
        if get_role(uid) not in ("hr", "admin"):
            return {"ok": False, "error": "нет прав"}
        doc_id, title = str(payload.get("doc_id") or ""), (payload.get("title") or "").strip()
        if not doc_id or not title:
            return {"ok": False, "error": "нужны doc_id и title"}
        S = load_state()
        n = 0
        for c in S["kb"]:
            if c.get("doc_id") == doc_id:
                c["title"] = title
                n += 1
        json.dump(S["kb"], open(INDEX, "w"), ensure_ascii=False)
        conn = db()
        conn.execute("UPDATE docs SET title=? WHERE doc_id=?", (title, doc_id))
        conn.commit()
        conn.close()
        pass
        return {"ok": True, "doc_id": doc_id, "title": title, "chunks": n}

    @api.post("/admin/purge")
    def purge(payload: dict = Body(...)):
        """Удаляет тестовые данные: пользователей и/или опросы."""
        uid = str(payload.get("user_id") or "")
        if get_role(uid) not in ("hr", "admin"):
            return {"ok": False, "error": "нет прав"}
        users = [str(u) for u in (payload.get("users") or [])]
        sids = [str(x) for x in (payload.get("survey_ids") or [])]
        conn = db()
        cur = conn.cursor()
        removed = {}
        if users:
            m = ",".join("?" * len(users))
            for t in ("survey_answers", "survey_runs", "tickets", "events"):
                cur.execute(f"DELETE FROM {t} WHERE user_id IN ({m})", users)
                removed[t] = cur.rowcount
            cur.execute(f"DELETE FROM users WHERE user_id IN ({m})", users)
        if sids:
            m = ",".join("?" * len(sids))
            cur.execute(f"DELETE FROM survey_answers WHERE survey_id IN ({m})", sids)
            cur.execute(f"DELETE FROM survey_runs WHERE survey_id IN ({m})", sids)
            cur.execute(f"DELETE FROM custom_surveys WHERE id IN ({m})", sids)
            removed["custom_surveys"] = cur.rowcount
        conn.commit()
        conn.close()
        pass
        return {"ok": True, "removed": removed}

    @api.get("/scope")
    def scope_dbg(text: str = ""):
        return {"text": text, "scope": scope(text)}

    @api.get("/themes")
    def themes(text: str = ""):
        return {"text": text, "themes": themes_of(text)}

    @api.get("/digest")
    def digest(days: float = 7):
        conn = db()
        since = time.time() - days * 86400
        ev = conn.execute("SELECT * FROM events WHERE ts>=?", (since,)).fetchall()
        dc = conn.execute("SELECT * FROM docs ORDER BY id DESC LIMIT 10").fetchall()
        conn.close()
        asks = []
        for e in ev:
            if e["kind"] != "ask":
                continue
            try:
                asks.append(json.loads(e["payload"] or "{}"))
            except Exception:
                asks.append({})
        answered = sum(1 for a in asks if a.get("answers"))
        negs = [a.get("neg") for a in asks if a.get("neg") is not None]
        upset = sum(1 for n in negs if n >= 0.7)
        neg_share = round(sum(1 for n in negs if n >= 0.7) / len(negs), 3) if negs else 0.0
        theme_counts = {}
        for a in asks:
            for t in a.get("themes") or []:
                theme_counts[t] = theme_counts.get(t, 0) + 1
        reason_themes = {}
        conn2 = db()
        for r in conn2.execute("SELECT themes FROM survey_answers WHERE themes IS NOT NULL AND themes!=''").fetchall():
            for t in (r["themes"] or "").split(","):
                if t:
                    reason_themes[t] = reason_themes.get(t, 0) + 1
        conn2.close()
        top_themes = [{"key": THEMES.get(k, k), "n": v}
                      for k, v in sorted(reason_themes.items(), key=lambda x: -x[1])[:5]]
        full_themes = [{"key": k, "n": v} for k, v in
                       sorted(theme_counts.items(), key=lambda x: -x[1])[:5]]
        conn = db()
        runs = conn.execute("SELECT COUNT(*) c FROM survey_runs WHERE status='done'").fetchone()["c"]
        srows = conn.execute("SELECT COUNT(*) c FROM survey_answers").fetchone()["c"]
        conn.close()
        conn3 = db()
        t_open = conn3.execute("SELECT COUNT(*) c FROM tickets WHERE status='open'").fetchone()["c"]
        t_all = conn3.execute("SELECT COUNT(*) c FROM tickets").fetchone()["c"]
        conn3.close()
        return {"days": days, "tickets_open": t_open, "tickets_total": t_all,
                "surveys_done": runs, "survey_answers": srows,
                "negative_share": neg_share, "upset_users": upset, "top_themes": full_themes,
                "top_reason_themes": top_themes,
                "messages": len(asks), "answered": answered,
                "no_answer": len(asks) - answered,
                "docs_recent": [dict(d) for d in dc],
                "summary": (f"За {days:g} дн.: вопросов {len(asks)}, с ответом {answered}, "
                            f"без ответа {len(asks) - answered}. Опросов пройдено: {runs}, "
                            f"ответов в опросах: {srows}. Обращений в HR: {t_all} (открытых {t_open}). Недовольных: {upset} "
                            f"({neg_share * 100:.0f}% сообщений). "
                            + (f"Частые темы жалоб: {', '.join(t['key'] + ' ' + str(t['n']) for t in top_themes)}."
                               if top_themes else ""))}

    @api.get("/panel", response_class=HTMLResponse)
    def panel(token: str = ""):
        if token != PANEL_TOKEN:
            return HTMLResponse("<h1>403</h1>", status_code=403)
        conn = db()
        dc = conn.execute("SELECT * FROM docs ORDER BY id DESC LIMIT 20").fetchall()
        us = conn.execute("SELECT * FROM users ORDER BY created DESC LIMIT 50").fetchall()
        ev = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT 40").fetchall()
        conn.close()
        drows = "".join(f"<tr><td>{d['title']}</td><td>{d['chunks']}</td><td>{d['level']}</td>"
                        f"<td>{d['added_by']}</td></tr>" for d in dc)
        urows = "".join(f"<tr><td>{u['user_id']}</td><td>{u['role']}</td></tr>" for u in us)
        erows = "".join(f"<tr><td>{e['kind']}</td><td>{e['user_id']}</td><td>{(e['payload'] or '')[:90]}</td></tr>"
                        for e in ev)
        html = f"""<html><head><meta charset="utf-8"><title>Корсик — панель</title>
<style>body{{font-family:system-ui;margin:24px;background:#0f1115;color:#e6e6e6}}
h2{{margin-top:32px}}table{{border-collapse:collapse;width:100%}}
td,th{{border:1px solid #2a2f3a;padding:6px 8px;font-size:13px;text-align:left}}
th{{background:#171b24}}</style></head><body>
<h1>Корсик — панель</h1>
<h2>Документы ({len(dc)})</h2><table><tr><th>Название</th><th>Фрагментов</th><th>Уровень</th><th>Добавил</th></tr>{drows}</table>
<h2>Пользователи ({len(us)})</h2><table><tr><th>user_id</th><th>роль</th></tr>{urows}</table>
<h2>События ({len(ev)})</h2><table><tr><th>тип</th><th>кто</th><th>детали</th></tr>{erows}</table>
</body></html>"""
        return HTMLResponse(html)


    @api.get("/surveys")
    def surveys(user_id: str = ""):
        conn = db()
        runs = {r["survey_id"]: dict(r) for r in
                conn.execute("SELECT * FROM survey_runs WHERE user_id=?", (str(user_id),)).fetchall()}
        conn.close()
        out = []
        for sid, s in all_surveys().items():
            r = runs.get(sid)
            out.append({"survey_id": sid, "title": s["title"], "intro": s["intro"],
                        "questions": len(s["questions"]),
                        "status": (r or {}).get("status", "not_started"),
                        "progress": (r or {}).get("idx", 0)})
        return {"surveys": out}

    @api.post("/surveys/start")
    def survey_start(payload: dict = Body(...)):
        uid, sid = str(payload.get("user_id") or ""), payload.get("survey_id")
        if sid not in all_surveys():
            return {"ok": False, "error": "нет такого опроса"}
        conn = db()
        row = conn.execute("SELECT status FROM survey_runs WHERE user_id=? AND survey_id=?",
                           (uid, sid)).fetchone()
        if row and row["status"] == "done" and not payload.get("force"):
            conn.close()
            return {"ok": True, "done": True, "already": True,
                    "text": "Вы уже проходили этот опрос — ответы сохранены. Повторно проходить не нужно."}
        conn.execute("INSERT INTO survey_runs(user_id, survey_id, idx, status, started) "
                     "VALUES(?,?,0,'active',?) ON CONFLICT(user_id, survey_id) DO UPDATE SET status='active'",
                     (uid, sid, time.time()))
        conn.commit()
        conn.close()
        return survey_question(uid, sid, 0)

    @api.post("/surveys/answer")
    def survey_answer(payload: dict = Body(...)):
        uid = str(payload.get("user_id") or "")
        sid = payload.get("survey_id")
        qid = payload.get("qid")
        if sid not in all_surveys():
            return {"ok": False, "error": "нет такого опроса"}
        qs = all_surveys()[sid]["questions"]
        idx = next((i for i, q in enumerate(qs) if q["id"] == qid), None)
        if idx is None:
            return {"ok": False, "error": "нет такого вопроса"}
        value = str(payload.get("value") or "")
        reason = payload.get("reason")
        low_flag = (qs[idx]["type"] == "scale" and value.isdigit() and int(value) < 8) or \
                   (qs[idx]["type"] == "yesno" and value.lower() in ("нет", "no", "0"))
        if reason:
            conn = db()
            th = ",".join(themes_of(str(reason)))
            conn.execute("UPDATE survey_answers SET reason=?, themes=? WHERE id=(SELECT MAX(id) FROM survey_answers "
                         "WHERE user_id=? AND survey_id=? AND qid=?)",
                         (str(reason)[:500], th, uid, sid, qid))
            conn.commit()
            conn.close()
        else:
            conn = db()
            th = ",".join(themes_of(value)) if qs[idx]["type"] == "text" else ""
            conn.execute("INSERT INTO survey_answers(user_id, survey_id, qid, value, themes, ts) "
                         "VALUES(?,?,?,?,?,?) ON CONFLICT(user_id, survey_id, qid) DO UPDATE SET "
                         "value=excluded.value, themes=excluded.themes, ts=excluded.ts",
                         (uid, sid, qid, value[:300], th, time.time()))
            conn.execute("UPDATE survey_runs SET idx=? WHERE user_id=? AND survey_id=?", (idx + 1, uid, sid))
            conn.commit()
            conn.close()
        if low_flag and not reason:
            return {"ok": True, "need_reason": True, "qid": qid,
                    "prompt": "Спасибо. Что именно мешает? Напишите коротко."}
        return survey_question(uid, sid, idx + 1)

    def survey_question(uid, sid, idx):
        qs = all_surveys()[sid]["questions"]
        if idx >= len(qs):
            conn = db()
            conn.execute("UPDATE survey_runs SET status='done', finished=? WHERE user_id=? AND survey_id=?",
                         (time.time(), uid, sid))
            conn.commit()
            conn.close()
            return {"ok": True, "done": True,
                    "text": "Спасибо! Ответы сохранены, кадровая служба их увидит в сводке."}
        q = qs[idx]
        conn = db()
        conn.execute("UPDATE survey_runs SET idx=? WHERE user_id=? AND survey_id=?", (idx, uid, sid))
        conn.commit()
        conn.close()
        return {"ok": True, "done": False, "survey_id": sid, "idx": idx, "total": len(qs),
                "qid": q["id"], "type": q["type"], "text": q["text"], "options": q.get("options", [])}

    @api.get("/surveys/pending")
    def surveys_pending(older_min: float = 15):
        conn = db()
        rows = conn.execute("SELECT * FROM survey_runs WHERE status='active' AND started<=?",
                            (time.time() - older_min * 60,)).fetchall()
        conn.close()
        return {"pending": [{"user_id": r["user_id"], "survey_id": r["survey_id"], "idx": r["idx"],
                             "started": r["started"]} for r in rows]}

    @api.get("/surveys/results")
    def survey_results(survey_id: str = "", user_id: str = ""):
        if get_role(user_id) not in ("hr", "admin"):
            return {"ok": False, "error": "результаты видит только кадровая служба"}
        conn = db()
        rows = conn.execute("SELECT * FROM survey_answers WHERE survey_id=?", (survey_id,)).fetchall()
        conn.close()
        agg = {}
        for r in rows:
            a = agg.setdefault(r["qid"], {"answers": [], "reasons": []})
            a["answers"].append(r["value"])
            if r["reason"]:
                a["reasons"].append(r["reason"])
        return {"survey_id": survey_id, "answers": len(rows), "by_question": agg}

    return api


def run():
    import uvicorn
    host = os.environ.get("KORSIK_HOST", "0.0.0.0")
    port = int(os.environ.get("KORSIK_PORT", "8000"))
    print(f"Корсик: сервис на http://{host}:{port} (индекс: {INDEX})")
    uvicorn.run(create_app(), host=host, port=port, log_level="info")


if __name__ == "__main__":
    run()
