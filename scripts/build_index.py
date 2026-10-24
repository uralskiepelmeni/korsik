# -*- coding: utf-8 -*-
"""Сборка индекса: считает эмбеддинги для data/kb.jsonl.

  python scripts/build_index.py

Результат:
  data/index.json  — чанки с метаданными (документ, пункт, страница, уровень)
  data/emb.npy     — матрица эмбеддингов (N × 1024), косинусная близость

Перезапускать после изменения data/kb.jsonl.
"""
import json
import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "data")


def main():
    import numpy as np
    import torch
    from transformers import AutoModel, AutoTokenizer

    kb_path = os.path.join(DATA, "kb.jsonl")
    if not os.path.exists(kb_path):
        raise SystemExit("нет data/kb.jsonl")

    kb = [json.loads(line) for line in open(kb_path, encoding="utf-8") if line.strip()]
    model_id = os.environ.get("KORSIK_EMB_MODEL", "BAAI/bge-m3")
    device = os.environ.get("KORSIK_DEVICE") or ("cuda" if torch.cuda.is_available() else "cpu")
    print(f"чанков: {len(kb)} · модель: {model_id} · устройство: {device}")

    tok = AutoTokenizer.from_pretrained(model_id)
    model = AutoModel.from_pretrained(model_id, dtype=torch.float16 if device == "cuda" else torch.float32)
    model = model.to(device).eval()

    texts = [f'{c["title"]} · {c["clause"]}. {c["text"]}' for c in kb]
    out = []
    with torch.no_grad():
        for i in range(0, len(texts), 16):
            batch = texts[i:i + 16]
            enc = tok(batch, padding=True, truncation=True, max_length=512, return_tensors="pt").to(device)
            h = model(**enc).last_hidden_state[:, 0].float()
            out.append(torch.nn.functional.normalize(h, dim=-1).cpu().numpy())
    emb = np.concatenate(out)

    np.save(os.path.join(DATA, "emb.npy"), emb)
    json.dump(kb, open(os.path.join(DATA, "index.json"), "w", encoding="utf-8"), ensure_ascii=False)
    print(f"готово: {emb.shape[0]} векторов размерности {emb.shape[1]}")


if __name__ == "__main__":
    main()
