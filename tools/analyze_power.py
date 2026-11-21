#!/usr/bin/env python3
"""Analisa a fase P (varredura de potência) do run_campaign.py.

Em cada etapa P_EDxx_SFyy o ED transmitiu sozinho com a potência intercalada pacote a pacote:
o pacote com FCnt f saiu com o índice sweep[f % n]. Por isso a potência de cada pacote PERDIDO
também é conhecida, e o PDR por nível de potência sai exato.

Saídas em --out:
  resultado_potencia.csv  por ED x SF x potência: enviados, recebidos, PDR (IC 95% Wilson), RSSI, SNR
  resumo_potencia.csv     por ED x SF: potência mínima com PDR >= --threshold, atenuação do enlace
                          (potência - RSSI) e inclinação RSSI x potência (~1 dB/dB = medida consistente)

Uso:
    ./analyze_power.py [uplinks.csv] [--log campanha_log.csv] [--positions posicoes.csv] [--out pasta]
"""
import argparse
import csv
import math
import os
import re
from collections import defaultdict

DATA_DIR = os.path.expanduser("~/rak/chirpstack-docker/data/uplinks")


def txp_dbm(index):
    """Potência no rádio para o índice TX_POWER em AU915 (limitada a 22 dBm pelo SX1262)."""
    return min(22, math.floor(30 - 2 * index - 2.15))


def wilson(successes, total, z=1.96):
    if total == 0:
        return None, None
    p = successes / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return 100 * (center - half), 100 * (center + half)


def fmt(value, digits=1):
    return "" if value is None else f"{value:.{digits}f}"


def mean(xs):
    return sum(xs) / len(xs) if xs else None


def read_csv(path):
    """Lê o CSV tolerando arquivos "alinhados" por editor (espaços em nomes e campos)."""
    with open(path, newline="") as f:
        rows = list(csv.reader(f))
    header = [h.strip() for h in rows[0]]
    return [dict(zip(header, (c.strip() for c in r))) for r in rows[1:] if len(r) == len(header)]


def sweeps_from_log(path):
    """Varredura usada em cada etapa, pela observação "txp=..." do campanha_log.csv."""
    if not path or not os.path.exists(path):
        return {}
    out = {}
    for r in read_csv(path):
        m = re.search(r"txp=([\d,]+)", r.get("observacoes", ""))
        if m:
            out[r["step"]] = [int(x) for x in m.group(1).split(",")]
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="?", default=os.path.join(DATA_DIR, "uplinks.csv"))
    ap.add_argument("--log", default=os.path.join(DATA_DIR, "campanha_log.csv"))
    ap.add_argument("--sweep", default="0,4,7,10,12,14", help="varredura usada se a etapa não estiver no log")
    ap.add_argument("--threshold", type=float, default=95, help="PDR mínimo (%%) para a potência mínima")
    ap.add_argument("--positions")
    ap.add_argument("--out", default=".")
    args = ap.parse_args()

    default_sweep = [int(x) for x in args.sweep.split(",")]
    step_sweep = sweeps_from_log(args.log)
    positions = {}
    if args.positions:
        positions = {p["device_name"]: p for p in read_csv(args.positions)}

    by_step_dev = defaultdict(dict)  # (step, ed, sf) -> {fcnt: row}
    for r in read_csv(args.csv):
        if r.get("step", "").startswith("P_") and r.get("f_cnt"):
            by_step_dev[(r["step"], r["device_name"], r["sf"])][int(r["f_cnt"])] = r

    levels = defaultdict(lambda: {"sent": 0, "rx": 0, "rssi": [], "snr": []})  # (ed, sf, idx)
    mismatches = 0
    for (step, ed, sf), frames in by_step_dev.items():
        sweep = step_sweep.get(step, default_sweep)
        for f in range(min(frames), max(frames) + 1):
            idx = sweep[f % len(sweep)]
            g = levels[(ed, sf, idx)]
            g["sent"] += 1
            r = frames.get(f)
            if r is None:
                continue
            g["rx"] += 1
            if r.get("tx_power_index", "") not in ("", str(idx)):
                mismatches += 1  # o pacote declarou outro índice: varredura desalinhada
            for key, col in (("rssi", "rssi_dbm"), ("snr", "snr_db")):
                if r.get(col, "") != "":
                    g[key].append(float(r[col]))

    if not levels:
        raise SystemExit("Nenhuma linha de etapa P (step começando com P_) no CSV.")

    os.makedirs(args.out, exist_ok=True)
    detail_path = os.path.join(args.out, "resultado_potencia.csv")
    with open(detail_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["device_name", "x_m", "andar", "sf", "tx_index", "tx_dbm", "enviados", "recebidos",
                    "pdr_percent", "pdr_ic95_inf", "pdr_ic95_sup", "rssi_medio_dbm", "snr_medio_db"])
        for (ed, sf, idx), g in sorted(levels.items(), key=lambda kv: (kv[0][0], int(kv[0][1]), -txp_dbm(kv[0][2]))):
            lo, hi = wilson(g["rx"], g["sent"])
            pos = positions.get(ed, {})
            w.writerow([ed, pos.get("x_m", ""), pos.get("andar", ""), sf, idx, txp_dbm(idx), g["sent"], g["rx"],
                        fmt(100 * g["rx"] / g["sent"]), fmt(lo), fmt(hi), fmt(mean(g["rssi"])), fmt(mean(g["snr"]))])

    summary_path = os.path.join(args.out, "resumo_potencia.csv")
    by_link = defaultdict(list)
    for (ed, sf, idx), g in levels.items():
        by_link[(ed, sf)].append((txp_dbm(idx), idx, g))
    with open(summary_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["device_name", "x_m", "andar", "sf", "potencia_minima_dbm", "indice_minimo",
                    "atenuacao_media_db", "inclinacao_rssi_db_por_db", "observacao"])
        print(f"{'ED':16} {'SF':>3}  {'pot. mín. (dBm)':>15}  {'atenuação (dB)':>14}  {'inclinação':>10}")
        for (ed, sf), items in sorted(by_link.items(), key=lambda kv: (kv[0][0], int(kv[0][1]))):
            items.sort(key=lambda t: -t[0])  # da maior para a menor potência
            # Potência mínima: a menor a partir da qual TODOS os níveis acima também passam no limiar.
            minimum = None
            for dbm, idx, g in items:
                if 100 * g["rx"] / g["sent"] >= args.threshold:
                    minimum = (dbm, idx)
                else:
                    break
            pts = [(dbm, mean(g["rssi"])) for dbm, _, g in items if len(g["rssi"]) >= 3]
            attenuation = mean([dbm - rssi for dbm, rssi in pts])
            slope = None
            if len(pts) >= 2:
                mx, my = mean([p[0] for p in pts]), mean([p[1] for p in pts])
                sxx = sum((x - mx) ** 2 for x, _ in pts)
                slope = sum((x - mx) * (y - my) for x, y in pts) / sxx if sxx else None
            note = "" if minimum else f"nem a maior potência atingiu {args.threshold:g}%"
            if slope is not None and not 0.7 <= slope <= 1.3:
                note = (note + "; " if note else "") + "RSSI não acompanha a potência (saturação perto do gateway?)"
            pos = positions.get(ed, {})
            w.writerow([ed, pos.get("x_m", ""), pos.get("andar", ""), sf,
                        minimum[0] if minimum else "", minimum[1] if minimum else "",
                        fmt(attenuation), fmt(slope, 2), note])
            print(f"{ed:16} {sf:>3}  {str(minimum[0]) if minimum else '-':>15}  {fmt(attenuation):>14}  {fmt(slope, 2):>10}")

    print(f"\n{len(levels)} grupos ED x SF x potência -> {detail_path}")
    print(f"resumo por ED x SF -> {summary_path}")
    if mismatches:
        print(f"ATENÇÃO: {mismatches} pacotes declararam um índice diferente do esperado pela varredura")


if __name__ == "__main__":
    main()
