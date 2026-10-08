#!/usr/bin/env python3
"""Analisa o uplinks.csv do exporter por etapa da campanha: PDR, perda por colisão medida,
referência teórica (Pure ALOHA) e sobreposições observadas no ar.

Perda por colisão medida (sem modelo):
    P_col = 1 - PDR(ED, SF, etapa com N EDs) / PDR(mesmo ED e SF, etapa em que ele estava sozinho)
A etapa "sozinho" é qualquer etapa em que só aquele ED transmitiu (N = 1). Se houver mais de uma
para o mesmo ED e SF, elas são somadas.

Sobreposições observadas: pares de pacotes RECEBIDOS no mesmo gateway, canal e SF cujos intervalos
[tmst, tmst + ToA] se cruzam - ou seja, ao menos um sobreviveu a uma sobreposição real (efeito captura).

Uso:
    ./analyze_collisions.py [uplinks.csv] [--positions posicoes.csv] [--out pasta]

posicoes.csv (opcional): device_name,x_m,andar   (ex.: ED3RAK,12.5,1)
Saída: <out>/resultado_por_etapa.csv e <out>/sobreposicoes.csv
"""
import argparse
import csv
import math
import os
from collections import defaultdict
from datetime import datetime

DEFAULT_CSV = os.path.expanduser("~/rak/chirpstack-docker/data/uplinks/uplinks.csv")
TMST_WRAP = 2 ** 32
# Pacotes mais distantes que isso no relógio do exporter nunca se sobrepõem (maior ToA ~1,5 s);
# também evita comparar tmst de voltas diferentes do contador de 32 bits (~71,6 min).
OVERLAP_WALL_WINDOW_S = 10


def wilson(successes, total, z=1.96):
    if total == 0:
        return None, None
    p = successes / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return 100 * (center - half), 100 * (center + half)


def fnum(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fmt(value, digits=1):
    return "" if value is None else f"{value:.{digits}f}"


def load_rows(path):
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["_wall"] = datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")).timestamp()
        r["_fcnt"] = int(r["f_cnt"]) if r["f_cnt"] else None
        r["_gap"] = int(r["fcnt_gap"]) if r["fcnt_gap"] else 0
        r["_tmst"] = int(r["tmst_us"]) if r["tmst_us"] else None
        r["_toa_us"] = (fnum(r["toa_ms"]) or 0) * 1000
    rows.sort(key=lambda r: r["_wall"])
    return rows


def find_overlaps(step_rows):
    """Pares recebidos no mesmo gateway/canal/SF com intervalos no ar se cruzando."""
    pairs = []
    # Uma mesma transmissão registrada duas vezes (mesmo ED, FCnt e tmst) entra uma vez só;
    # retransmissões confirmadas têm outro tmst e continuam contando (ocupam o ar de novo).
    unique = {(r["dev_eui"], r["_fcnt"], r["_tmst"]): r for r in step_rows}
    candidates = sorted((r for r in unique.values() if r["_tmst"] is not None and r["frequency_hz"]),
                        key=lambda r: r["_wall"])
    for i, a in enumerate(candidates):
        for b in candidates[i + 1:]:
            if b["_wall"] - a["_wall"] > OVERLAP_WALL_WINDOW_S:
                break
            if (a["dev_eui"] == b["dev_eui"] or a["gateway_id"] != b["gateway_id"]
                    or a["frequency_hz"] != b["frequency_hz"] or a["sf"] != b["sf"]):
                continue
            # Diferença de tmst com sinal, tratando a volta do contador de 32 bits.
            delta = (b["_tmst"] - a["_tmst"]) % TMST_WRAP
            if delta >= TMST_WRAP // 2:
                delta -= TMST_WRAP
            first, second, gap_us = (a, b, delta) if delta >= 0 else (b, a, -delta)
            if gap_us < first["_toa_us"]:
                pairs.append((first, second, gap_us))
    return pairs


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="?", default=DEFAULT_CSV)
    ap.add_argument("--positions")
    ap.add_argument("--out", default=".")
    args = ap.parse_args()

    rows = [r for r in load_rows(args.csv) if r["step"]]
    if not rows:
        raise SystemExit("Nenhuma linha com etapa (coluna step) no CSV.")

    positions = {}
    if args.positions:
        with open(args.positions, newline="") as f:
            positions = {p["device_name"]: p for p in csv.DictReader(f)}

    by_step = defaultdict(list)
    for r in rows:
        by_step[r["step"]].append(r)

    groups = {}  # (step, device, sf) -> estatísticas
    overlap_rows = []
    for step, step_rows in by_step.items():
        n_eds = len({r["dev_eui"] for r in step_rows})
        seen_first = set()
        for r in step_rows:
            key = (step, r["device_name"], r["sf"])
            g = groups.setdefault(key, {"n_eds": n_eds, "fcnts": set(), "lost": 0,
                                        "rssi": [], "snr": [], "aloha": [], "overlaps": 0, "drssi": []})
            # O buraco do 1º pacote de cada ED na etapa pertence ao período anterior a ela.
            if r["dev_eui"] in seen_first:
                g["lost"] += r["_gap"]
            seen_first.add(r["dev_eui"])
            if r["_fcnt"] is not None:
                g["fcnts"].add(r["_fcnt"])  # retransmissões (mesmo FCnt) contam uma vez
            for field, col in (("rssi", "rssi_dbm"), ("snr", "snr_db"), ("aloha", "aloha_ref_percent")):
                v = fnum(r[col])
                if v is not None:
                    g[field].append(v)
        for first, second, gap_us in find_overlaps(step_rows):
            drssi = abs((fnum(first["rssi_dbm"]) or 0) - (fnum(second["rssi_dbm"]) or 0))
            for r in (first, second):
                g = groups[(step, r["device_name"], r["sf"])]
                g["overlaps"] += 1
                g["drssi"].append(drssi)
            overlap_rows.append({"step": step, "sf": first["sf"], "frequency_hz": first["frequency_hz"],
                                 "ed_a": first["device_name"], "f_cnt_a": first["f_cnt"], "rssi_a": first["rssi_dbm"],
                                 "ed_b": second["device_name"], "f_cnt_b": second["f_cnt"], "rssi_b": second["rssi_dbm"],
                                 "sobreposicao_ms": f"{(first['_toa_us'] - gap_us) / 1000:.1f}",
                                 "delta_rssi_db": f"{drssi:.1f}"})

    # PDR de referência: mesmo ED e SF em etapas em que ele estava sozinho.
    alone = defaultdict(lambda: [0, 0])
    for (step, dev, sf), g in groups.items():
        if g["n_eds"] == 1:
            alone[(dev, sf)][0] += len(g["fcnts"])
            alone[(dev, sf)][1] += len(g["fcnts"]) + g["lost"]

    os.makedirs(args.out, exist_ok=True)
    out_path = os.path.join(args.out, "resultado_por_etapa.csv")
    cols = ["step", "n_eds", "device_name", "x_m", "andar", "sf", "recebidos", "perdidos",
            "pdr_percent", "pdr_ic95_inf", "pdr_ic95_sup", "pdr_sozinho_percent", "p_col_medida_percent",
            "aloha_ref_media_percent", "rssi_medio_dbm", "snr_medio_db",
            "sobreposicoes_recebidas", "delta_rssi_medio_db"]
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for (step, dev, sf), g in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1], int(kv[0][2] or 0))):
            rx, lost = len(g["fcnts"]), g["lost"]
            pdr = 100 * rx / (rx + lost) if rx + lost else None
            lo, hi = wilson(rx, rx + lost)
            a_rx, a_tot = alone.get((dev, sf), (0, 0))
            pdr_alone = 100 * a_rx / a_tot if a_tot else None
            p_col = 100 * (1 - pdr / pdr_alone) if pdr is not None and pdr_alone else None
            mean = lambda xs: sum(xs) / len(xs) if xs else None
            pos = positions.get(dev, {})
            w.writerow({"step": step, "n_eds": g["n_eds"], "device_name": dev,
                        "x_m": pos.get("x_m", ""), "andar": pos.get("andar", ""), "sf": sf,
                        "recebidos": rx, "perdidos": lost, "pdr_percent": fmt(pdr),
                        "pdr_ic95_inf": fmt(lo), "pdr_ic95_sup": fmt(hi),
                        "pdr_sozinho_percent": fmt(pdr_alone), "p_col_medida_percent": fmt(p_col),
                        "aloha_ref_media_percent": fmt(mean(g["aloha"]), 2),
                        "rssi_medio_dbm": fmt(mean(g["rssi"])), "snr_medio_db": fmt(mean(g["snr"])),
                        "sobreposicoes_recebidas": g["overlaps"], "delta_rssi_medio_db": fmt(mean(g["drssi"]))})

    ov_path = os.path.join(args.out, "sobreposicoes.csv")
    with open(ov_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["step", "sf", "frequency_hz", "ed_a", "f_cnt_a", "rssi_a",
                                          "ed_b", "f_cnt_b", "rssi_b", "sobreposicao_ms", "delta_rssi_db"])
        w.writeheader()
        w.writerows(overlap_rows)

    print(f"{len(groups)} grupos (etapa x ED x SF) -> {out_path}")
    print(f"{len(overlap_rows)} sobreposições recebidas -> {ov_path}")


if __name__ == "__main__":
    main()
