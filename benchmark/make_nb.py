"""Собирает Colab-ноутбук для замера задержек на T4: наш верификатор против Qwen 3.5."""
import ast
import json
import os

cells = []


def md(text):
    cells.append({"cell_type": "markdown", "metadata": {}, "source": text.splitlines(keepends=True)})


def code(text):
    # в Colab строки с ! и % — команды оболочки, для проверки синтаксиса их убираем
    check = "\n".join("" if ln.lstrip().startswith(("!", "%")) else ln for ln in text.splitlines())
    ast.parse(check)
    cells.append({"cell_type": "code", "metadata": {}, "execution_count": None, "outputs": [],
                  "source": text.splitlines(keepends=True)})


md("""# Замер задержки: наш верификатор против Qwen 3.5 2B и 4B

Один и тот же вопрос, один и тот же контекст, одна и та же видеокарта (**T4**), одна и та же точность (float16).

**Перед запуском:** `Среда выполнения` → `Сменить среду выполнения` → **T4 GPU**, затем `Среда выполнения` → `Выполнить все`.

Ноутбук сам:
1. скачивает базу из репозитория (199 фрагментов);
2. считает эмбеддинги bge-m3 и время поиска;
3. замеряет наш путь: проверка одного фрагмента верификатором и полный цикл (8 проверок);
4. замеряет Qwen 3.5 2B и Qwen 3.5 4B на том же вопросе с тем же контекстом;
5. печатает таблицу в миллисекундах и сохраняет `latency_t4.csv`.

Модели грузятся по одной, память освобождается между замерами.
Оценка времени: 10–15 минут, из них большая часть — скачивание весов.""")

code('''# 1. Проверка, что видеокарта действительно T4
!nvidia-smi --query-gpu=name,memory.total --format=csv
import torch, platform
print("torch:", torch.__version__, "| python:", platform.python_version())
assert torch.cuda.is_available(), "Нет GPU: Среда выполнения → Сменить среду выполнения → T4 GPU"
print("GPU:", torch.cuda.get_device_name(0))
print("поддержка bfloat16:", torch.cuda.is_bf16_supported())''')

code('''# 2. Зависимости (torch в Colab уже стоит)
!pip -q install "transformers>=5.17.0" accelerate safetensors requests 2>&1 | tail -2
import transformers
print("transformers:", transformers.__version__)''')

code('''# 3. Вопрос, контекст и настройки замера
import time, json, statistics, requests

QUESTION = "Какой испытательный срок устанавливается при приёме на работу?"
KB_URL = "https://raw.githubusercontent.com/uralskiepelmeni/korsik/main/data/kb.jsonl"

EMB_MODEL = "BAAI/bge-m3"           # наш поиск
VERIFIER = "vllm-sr/d3-nano"        # наша модель-верификатор
QWEN_SMALL = "Qwen/Qwen3.5-2B"
QWEN_LARGE = "Qwen/Qwen3.5-4B"

WARMUP = 3            # прогревочные прогоны, не идут в статистику
RUNS = 10             # замеров на схему
MAX_NEW_TOKENS = 64   # столько же, сколько нужно короткому ответу по документу

DEVICE = "cuda"
DTYPE = torch.float16     # T4 не умеет bfloat16 — честное сравнение в fp16

def human(ms):
    return f"{ms:.0f} мс"

def stats(times_ms):
    return {
        "median": statistics.median(times_ms),
        "mean": statistics.mean(times_ms),
        "p90": sorted(times_ms)[max(0, int(len(times_ms) * 0.9) - 1)],
        "min": min(times_ms),
        "max": max(times_ms),
    }

def free():
    import gc, torch
    gc.collect(); torch.cuda.empty_cache()
    print("занято видеопамяти:", round(torch.cuda.memory_allocated() / 2**30, 2), "ГБ")''')

code('''# 4. База знаний и наш поиск: bge-m3 + 199 фрагментов
import numpy as np
from transformers import AutoTokenizer, AutoModel

kb = [json.loads(l) for l in requests.get(KB_URL, timeout=60).text.splitlines() if l.strip()]
print("фрагментов в базе:", len(kb))

etok = AutoTokenizer.from_pretrained(EMB_MODEL)
em = AutoModel.from_pretrained(EMB_MODEL, dtype=DTYPE).to(DEVICE).eval()

def embed(texts):
    with torch.no_grad():
        enc = etok(texts, padding=True, truncation=True, max_length=512,
                   return_tensors="pt").to(DEVICE)
        h = em(**enc).last_hidden_state[:, 0].float()
        return torch.nn.functional.normalize(h, dim=-1).cpu().numpy()

t0 = time.perf_counter()
E = embed([f'{c["title"]} · {c["clause"]}. {c["text"]}' for c in kb])
print(f"индекс: {E.shape} за {(time.perf_counter()-t0):.1f} с")

# сразу фиксируем контекст для Qwen: топ-1 фрагмент по косинусу (как в пайплайне)
qv = embed([QUESTION])[0]
TOP1 = int(np.argmax(E @ qv))
CONTEXT = kb[TOP1]["text"]
print("контекст для Qwen:", kb[TOP1]["title"], "|", kb[TOP1]["clause"])''')

code('''# 5. Наш путь: проверка фрагментов верификатором
#    bge-m3 освобождаем, чтобы модели не жили в памяти одновременно
del em, etok
free()

from transformers import AutoModel as AM
d3 = AM.from_pretrained(VERIFIER, trust_remote_code=True, dtype=DTYPE).to(DEVICE).eval()

def d3_score(question, fragment):
    text = "Вопрос сотрудника: " + question + "  Фрагмент документа: " + fragment[:1800]
    r = d3.system_one(state=text,
                      questions={"ok": {"type": "noul",
                                        "instructions": "Этот фрагмент содержит ответ на вопрос сотрудника"}})
    return float(r["answers"]["ok"]["noul"])

for _ in range(WARMUP):
    d3_score(QUESTION, kb[0]["text"])

# 5.1 одна проверка «вопрос + фрагмент»
one = []
for _ in range(RUNS):
    i = int(np.random.randint(0, len(kb)))
    t0 = time.perf_counter()
    d3_score(QUESTION, kb[i]["text"])
    one.append((time.perf_counter() - t0) * 1000)

# 5.2 восемь проверок — как в рабочем пайплайне
eight = []
for _ in range(RUNS):
    t0 = time.perf_counter()
    for i in np.random.randint(0, len(kb), 8):
        d3_score(QUESTION, kb[int(i)]["text"])
    eight.append((time.perf_counter() - t0) * 1000)

VERIFIER_ONE = stats(one)
VERIFIER_EIGHT = stats(eight)
print("одна проверка:", human(VERIFIER_ONE["median"]), "| восемь:", human(VERIFIER_EIGHT["median"]))

del d3
free()''')

code('''# 6. Qwen 3.5: тот же вопрос, тот же контекст, генерация ответа
from transformers import AutoTokenizer as AT, AutoModelForCausalLM

PROMPT = " ".join([
    "Ответь на вопрос сотрудника, опираясь только на фрагмент документа.",
    "Если ответа нет — скажи, что в документе этого нет.",
    "",
    "Фрагмент:",
    CONTEXT,
    "",
    "Вопрос: " + QUESTION,
])

results = {"verifier_one": VERIFIER_ONE, "verifier_eight": VERIFIER_EIGHT}

for name, mid in (("qwen_2b", QWEN_SMALL), ("qwen_4b", QWEN_LARGE)):
    print()
    print("=== " + mid + " ===")
    tok = AT.from_pretrained(mid)
    model = AutoModelForCausalLM.from_pretrained(mid, dtype=DTYPE).to(DEVICE).eval()
    msgs = [{"role": "user", "content": PROMPT}]
    prompt_text = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
    enc = tok(prompt_text, return_tensors="pt").to(DEVICE)

    with torch.no_grad():
        for _ in range(WARMUP):
            model.generate(**enc, max_new_tokens=MAX_NEW_TOKENS, do_sample=False)

    times, tps, sample = [], [], ""
    for _ in range(RUNS):
        torch.cuda.synchronize()
        t0 = time.perf_counter()
        out = model.generate(**enc, max_new_tokens=MAX_NEW_TOKENS, do_sample=False)
        torch.cuda.synchronize()
        dt = (time.perf_counter() - t0) * 1000
        times.append(dt)
        n_new = out.shape[1] - enc["input_ids"].shape[1]
        tps.append(n_new / (dt / 1000))
        if not sample:
            sample = tok.decode(out[0][enc["input_ids"].shape[1]:], skip_special_tokens=True)

    s = stats(times)
    s["tokens_per_s"] = statistics.median(tps)
    results[name] = s
    print("медиана", human(s["median"]), "| p90", human(s["p90"]), "|", round(s["tokens_per_s"], 1), "ток/с")
    print("пример ответа:", sample[:300].replace(chr(10), " "))

    del model, tok
    free()''')

code('''# 7. Итоговая таблица
rows = [
    ("Наш верификатор: 1 проверка", results["verifier_one"]),
    ("Наш пайплайн: 8 проверок", results["verifier_eight"]),
    ("Qwen 3.5 2B (генерация)", results["qwen_2b"]),
    ("Qwen 3.5 4B (генерация)", results["qwen_4b"]),
]

print(f"{'схема':32}{'медиана':>10}{'p90':>10}{'мин':>9}{'макс':>9}")
print("-" * 70)
for name, s in rows:
    print(f"{name:32}{human(s['median']):>10}{human(s['p90']):>10}{human(s['min']):>9}{human(s['max']):>9}")

for name, s in rows:
    if "tokens_per_s" in s:
        print(name + ":", round(s["tokens_per_s"], 1), "токенов/с")

import csv
with open("latency_t4.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["схема", "медиана_мс", "p90_мс", "мин_мс", "макс_мс", "токенов_в_с"])
    for name, s in rows:
        w.writerow([name, round(s["median"]), round(s["p90"]), round(s["min"]), round(s["max"]),
                    round(s.get("tokens_per_s", 0), 1)])

print()
print("сохранено: latency_t4.csv")
print("Вопрос для всех схем:", QUESTION)
print("Прогонов на схему:", RUNS, "| прогрев:", WARMUP, "| GPU:", torch.cuda.get_device_name(0))''')

nb = {
    "cells": cells,
    "metadata": {
        "colab": {"provenance": [], "gpuType": "T4"},
        "kernelspec": {"name": "python3", "display_name": "Python 3"},
        "language_info": {"name": "python"},
        "accelerator": "GPU",
    },
    "nbformat": 4,
    "nbformat_minor": 0,
}

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "latency_t4.ipynb")
json.dump(nb, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("ноутбук:", out, "| ячеек:", len(cells))
