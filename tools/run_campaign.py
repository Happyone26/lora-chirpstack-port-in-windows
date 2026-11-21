#!/usr/bin/env python3
"""Roda a campanha de medição comandando os EDs por downlink (firmware com modo standby).

Sequências:
  S  - cada ED sozinho em SF7..SF12 (16 x 6 = 96 etapas, --packets-alone pacotes): é a referência
       "PDR sozinho" da perda por colisão medida (analyze_collisions.py)
  A  - EDs entram um a um, todos os ativos no mesmo SF: N = 1..16 x SF7..SF12 (96 etapas)
  B  - SFs misturados só com os 2 primeiros EDs: SF7..12 x SF7..12 (36 etapas)
  SAB - S, depois A, depois B (padrão); qualquer combinação de letras vale (ex.: S, AB)
  P  - potência: cada ED sozinho, em cada SF de --p-sfs (padrão SF7 a SF12), com VARREDURA de potência
       intercalada pacote a pacote (--txp-sweep, padrão índices 0,4,7,10,12,14 = 22/19/13/7/3/-1 dBm):
       o pacote com FCnt f sai com sweep[f % n]. Dura --packets-per-level x n pacotes por etapa.
       Fora da fase P os EDs ativos recebem potência fixa 0 (22 dBm), desfazendo qualquer varredura.

Em cada etapa: EDs ativos recebem o SF (1 byte), os demais recebem standby (00 + SF da consulta,
escolhido fora dos SFs da etapa). Quando todos confirmam no CSV do exporter, a etapa é gravada em
data/uplinks/current_step e dura até cada ED ativo TRANSMITIR --packets pacotes (avanço do FCnt,
então perdas não prolongam a etapa). Cada etapa vai para data/uplinks/campanha_log.csv.

Uso:
    export CHIRPSTACK_TOKEN=<API key do tenant>
    ./run_campaign.py --dry-run                 # mostra o plano e a duração estimada
    ./run_campaign.py                            # A + B com ED1..ED16
    ./run_campaign.py --plan A --eds 1-8         # só A, com ED1..ED8 (nessa ordem de entrada)
    ./run_campaign.py --start-at A_N05_SF07      # retoma a partir de uma etapa
    ./run_campaign.py --plan P --eds 1-8         # campanha de potência com ED1..ED8
"""
import argparse
import base64
import csv
import math
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from set_sf import APPLICATION_ID, SF_CMD_PORT, TXP_CMD_PORT, call, device_names_from_args  # noqa: E402

DATA_DIR = os.path.expanduser("~/rak/chirpstack-docker/data/uplinks")
SFS = range(7, 13)
FIXED_MAX_POWER = (0,)  # índice TX_POWER_0 fixo (22 dBm), o padrão do firmware


def build_plan(plan, eds, packets, packets_alone, sweep, p_sfs, packets_per_level):
    """Etapas como (rótulo, {ed: sf}, pacotes por ED, potência: índice fixo (i,) ou varredura)."""
    steps = []
    for phase in plan:
        if phase == "S":
            # Ordem ED a ED: cada placa sai do standby uma vez só (ativar leva até ~15 s).
            for i, ed in enumerate(eds, 1):
                for sf in SFS:
                    steps.append((f"S_ED{i:02d}_SF{sf:02d}", {ed: sf}, packets_alone, FIXED_MAX_POWER))
        elif phase == "A":
            for n in range(1, len(eds) + 1):
                for sf in SFS:
                    steps.append((f"A_N{n:02d}_SF{sf:02d}", {ed: sf for ed in eds[:n]}, packets, FIXED_MAX_POWER))
        elif phase == "B":
            e1, e2 = eds[0], eds[1]
            for sf1 in SFS:
                for sf2 in SFS:
                    steps.append((f"B_SF{sf1:02d}-{sf2:02d}", {e1: sf1, e2: sf2}, packets, FIXED_MAX_POWER))
        elif phase == "P":
            for i, ed in enumerate(eds, 1):
                for sf in p_sfs:
                    steps.append((f"P_ED{i:02d}_SF{sf:02d}", {ed: sf}, packets_per_level * len(sweep), tuple(sweep)))
    return steps


def txp_dbm(index):
    """Potência no rádio para o índice TX_POWER em AU915 (limitada a 22 dBm pelo SX1262)."""
    return min(22, math.floor(30 - 2 * index - 2.15))


def power_ok(txp, row):
    """O pacote saiu com a potência pedida? (2º byte do payload, coluna tx_power_index)"""
    value = row.get("tx_power_index", "")
    if txp == FIXED_MAX_POWER:
        return value in ("", "0")
    return value != "" and row["f_cnt"] != "" and int(value) == txp[int(row["f_cnt"]) % len(txp)]


def poll_sf_for(step_sfs):
    """SF das consultas de standby: o menor (menos tempo no ar) que não está na etapa."""
    return next(sf for sf in SFS if sf not in step_sfs)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class CsvTail:
    """Lê só as linhas novas do uplinks.csv do exporter."""

    def __init__(self, path):
        self.path = path
        with open(path, newline="") as f:
            self.header = next(csv.reader([f.readline()]))
            f.seek(0, os.SEEK_END)
            self.pos = f.tell()
        self.partial = ""

    def read(self):
        with open(self.path, newline="") as f:
            f.seek(self.pos)
            chunk = f.read()
            self.pos = f.tell()
        lines = (self.partial + chunk).split("\n")
        self.partial = lines.pop()  # linha ainda sendo escrita
        rows = []
        for r in csv.reader(lines):
            if len(r) == len(self.header):
                rows.append(dict(zip(self.header, r)))
        return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plan", default="SAB", help="letras das fases, na ordem: S, A, B, P (padrão SAB)")
    ap.add_argument("--eds", nargs="+", default=["1-16"], help="EDs na ordem de entrada (ex.: 1-16, ou 3 1 2)")
    ap.add_argument("--packets", type=int, default=100, help="pacotes transmitidos por ED ativo nas etapas A e B")
    ap.add_argument("--packets-alone", type=int, default=50, help="pacotes por etapa na fase S (ED sozinho)")
    ap.add_argument("--txp-sweep", default="0,4,7,10,12,14", help="fase P: índices TX_POWER da varredura")
    ap.add_argument("--p-sfs", default="7,8,9,10,11,12", help="fase P: SFs medidos")
    ap.add_argument("--packets-per-level", type=int, default=20, help="fase P: pacotes por nível de potência")
    ap.add_argument("--interval", type=float, default=5.0, help="intervalo de envio do firmware (s), para estimativas")
    ap.add_argument("--confirm-timeout", type=float, default=180, help="espera (s) por confirmação antes de reenviar")
    ap.add_argument("--attempts", type=int, default=3, help="reenvios antes de seguir sem o ED")
    ap.add_argument("--stall-timeout", type=float, default=300,
                    help="sem pacote novo de um ED por esse tempo (s), a etapa segue sem ele")
    ap.add_argument("--start-at", help="rótulo da etapa a partir da qual começar")
    ap.add_argument("--data-dir", default=DATA_DIR)
    ap.add_argument("--poll", type=float, default=1.0, help="(teste) intervalo de leitura do CSV")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    eds = device_names_from_args(args.eds)
    if not args.plan or set(args.plan) - set("SABP"):
        sys.exit("--plan aceita só as letras S, A, B e P")
    sweep = [int(x) for x in args.txp_sweep.split(",")]
    p_sfs = [int(x) for x in args.p_sfs.split(",")]
    if not 2 <= len(sweep) <= 15 or any(not 0 <= x <= 14 for x in sweep) or any(sf not in SFS for sf in p_sfs):
        sys.exit("--txp-sweep: 2 a 15 índices entre 0 e 14; --p-sfs: SFs entre 7 e 12")
    steps = build_plan(args.plan, eds, args.packets, args.packets_alone, sweep, p_sfs, args.packets_per_level)
    if args.start_at:
        labels = [s[0] for s in steps]
        if args.start_at not in labels:
            sys.exit(f"Etapa {args.start_at} não existe no plano.")
        steps = steps[labels.index(args.start_at):]

    est_min = sum(n * args.interval + 30 for _, _, n, _ in steps) / 60
    print(f"Plano {args.plan}: {len(steps)} etapas, EDs na ordem {eds}")
    print(f"Duração estimada: ~{est_min / 60:.1f} h (envio a cada {args.interval:g} s + ~30 s de troca por etapa)")
    if args.dry_run:
        for label, sf_map, n, txp in steps:
            power = "22 dBm" if txp == FIXED_MAX_POWER else "varredura " + "/".join(f"{txp_dbm(i)}" for i in txp) + " dBm"
            print(f"  {label}: " + ", ".join(f"{d}=SF{sf}" for d, sf in sf_map.items())
                  + f" | {power} | {n} pacotes | demais em standby (consulta SF{poll_sf_for(set(sf_map.values()))})")
        return
    if "CHIRPSTACK_TOKEN" not in os.environ:
        sys.exit("Defina CHIRPSTACK_TOKEN com a API key do tenant.")

    devices = {d["name"]: d for d in call("GET", f"/devices?applicationId={APPLICATION_ID}&limit=100")["result"]}
    missing = [e for e in eds if e not in devices]
    if missing:
        sys.exit(f"Não existem no ChirpStack: {missing}")
    eui = {e: devices[e]["devEui"] for e in eds}
    name_by_eui = {v: k for k, v in eui.items()}
    print("Último uplink no ChirpStack:")
    for e in eds:
        print(f"  {e:16} {devices[e].get('lastSeenAt') or 'nunca'}")

    step_file = os.path.join(args.data_dir, "current_step")
    log_path = os.path.join(args.data_dir, "campanha_log.csv")
    tail = CsvTail(os.path.join(args.data_dir, "uplinks.csv"))
    new_log = not os.path.exists(log_path)
    log = open(log_path, "a", newline="")
    logw = csv.writer(log)
    if new_log:
        logw.writerow(["step", "inicio_utc", "fim_utc", "eds_ativos", "sfs", "poll_sf", "pacotes_por_ed", "observacoes"])

    def set_step(label):
        with open(step_file, "w") as f:
            f.write(label + "\n")

    def send(ed, commands):
        """commands = [(porta, payload), ...]: uma limpeza da fila e todos enfileirados em ordem
        (Class A entrega um por uplink, então SF e potência chegam em uplinks seguidos)."""
        call("DELETE", f"/devices/{eui[ed]}/queue")
        for port, payload in commands:
            call("POST", f"/devices/{eui[ed]}/queue", {"queueItem": {
                "confirmed": False, "fPort": port, "data": base64.b64encode(bytes(payload)).decode()}})

    commanded = {}  # ed -> estado já comandado: ("sf", n) ou ("standby", poll_sf)
    commanded_txp = {}  # ed -> potência já comandada: (i,) fixa ou tupla de varredura
    absent = set()  # EDs que não confirmaram: seguem recebendo comandos, mas ninguém espera por eles
    try:
        for label, sf_map, packets, txp in steps:
            poll_sf = poll_sf_for(set(sf_map.values()))
            wanted = {ed: ("sf", sf_map[ed]) if ed in sf_map else ("standby", poll_sf) for ed in eds}
            set_step("")  # pacotes da troca de etapa não entram em nenhuma etapa
            notes = [] if txp == FIXED_MAX_POWER else ["txp=" + ",".join(map(str, txp))]
            print(f"\n[{now_iso()}] {label}: ativos " + ", ".join(f"{d}=SF{s}" for d, s in sf_map.items())
                  + ("" if txp == FIXED_MAX_POWER else f" | varredura de potência {list(txp)}"))

            # 1) Comanda quem precisa mudar e espera confirmação no CSV.
            pending = {ed for ed in eds if commanded.get(ed) != wanted[ed]
                       or (ed in sf_map and commanded_txp.get(ed) != txp)}
            for attempt in range(1, args.attempts + 1):
                for ed in sorted(pending):
                    kind, value = wanted[ed]
                    commands = []
                    if commanded.get(ed) != wanted[ed] or attempt > 1:
                        commands.append((SF_CMD_PORT, [value] if kind == "sf" else [0, value]))
                    if ed in sf_map and (commanded_txp.get(ed) != txp or attempt > 1):
                        commands.append((TXP_CMD_PORT, list(txp)))
                    send(ed, commands)
                    commanded[ed] = wanted[ed]
                    if ed in sf_map:
                        commanded_txp[ed] = txp
                to_wait = pending - absent
                sent_at = time.time()
                deadline = sent_at + args.confirm_timeout
                last_data, at_target, confirmed = {}, set(), set()
                while time.time() < deadline:
                    for r in tail.read():
                        ed = name_by_eui.get(r["dev_eui"])
                        if ed is None:
                            continue
                        last_data[ed] = time.time()
                        if wanted[ed] == ("sf", int(r["sf"] or 0)) and power_ok(txp, r):
                            at_target.add(ed)
                    # Ativo: já mandou dado no SF e na potência pedidos. Standby: parou de mandar dados (as
                    # consultas da porta 3 não chegam ao CSV) por mais de 2 intervalos.
                    quiet = lambda ed: time.time() - max(last_data.get(ed, 0), sent_at) > 2 * args.interval + 2
                    confirmed = {ed for ed in pending
                                 if (ed in at_target if wanted[ed][0] == "sf" else quiet(ed))}
                    if confirmed >= to_wait:
                        break
                    time.sleep(args.poll)
                absent -= confirmed  # ausente que respondeu volta ao normal
                pending -= confirmed
                if not pending - absent:
                    break
                print(f"  sem confirmação (tentativa {attempt}/{args.attempts}): {sorted(pending - absent)}")
            if pending - absent:
                print(f"  seguindo sem {sorted(pending - absent)}; nas próximas etapas eles recebem o comando sem espera")
                absent |= pending
            if pending:
                notes.append("sem confirmacao: " + " ".join(sorted(pending)))

            # 2) Mede: conta pacotes transmitidos (avanço do FCnt) por ED ativo.
            set_step(label)
            start = now_iso()
            first, last, resent = {}, {}, set()
            active = [ed for ed in sf_map if ed not in pending]
            progress_at = {ed: time.time() for ed in active}
            while True:
                for r in tail.read():
                    ed = name_by_eui.get(r["dev_eui"])
                    if ed is None or not r["f_cnt"]:
                        continue
                    if ed not in sf_map:
                        if ed not in resent:  # um reenvio de standby por etapa basta
                            notes.append(f"{ed} enviou dados fora da etapa")
                            send(ed, [(SF_CMD_PORT, [0, poll_sf])])
                            resent.add(ed)
                        continue
                    if r["sf"] != str(sf_map[ed]) or not power_ok(txp, r):
                        continue
                    if ed in absent:  # voltou a transmitir no SF certo: entra na próxima etapa
                        absent.discard(ed)
                        continue
                    f = int(r["f_cnt"])
                    first.setdefault(ed, f)
                    last[ed] = f
                    progress_at[ed] = time.time()
                for ed in list(active):
                    if time.time() - progress_at[ed] > args.stall_timeout:
                        notes.append(f"{ed} parou de transmitir")
                        print(f"\n  {ed} sem pacotes há {args.stall_timeout:.0f} s; etapa segue sem ele")
                        active.remove(ed)
                sent = {ed: last[ed] - first[ed] + 1 if ed in first else 0 for ed in active}
                print(f"\r  {label}: " + " ".join(f"{ed.replace('rak4630-sensor1', 'ED1')}={n}" for ed, n in sent.items()),
                      end="", flush=True)
                if all(n >= packets for n in sent.values()):
                    break
                time.sleep(args.poll)
            print()
            logw.writerow([label, start, now_iso(), " ".join(sf_map), " ".join(map(str, sf_map.values())), poll_sf,
                           " ".join(f"{ed}:{n}" for ed, n in sent.items()), "; ".join(dict.fromkeys(notes))])
            log.flush()
    except KeyboardInterrupt:
        print("\nInterrompido. Para retomar use --start-at com a etapa atual.")
    finally:
        set_step("")
        log.close()
    # Fim: todos em standby e de volta à potência fixa padrão.
    for ed in eds:
        send(ed, [(SF_CMD_PORT, [0, 7]), (TXP_CMD_PORT, list(FIXED_MAX_POWER))])
    print("Campanha encerrada; todos os EDs foram mandados para standby.")


if __name__ == "__main__":
    main()
