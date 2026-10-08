#!/usr/bin/env python3
"""Enfileira no ChirpStack o downlink de troca de SF para um ou mais EDs.

O firmware (sketches edN_*) escuta a porta 10: payload de 1 byte com o SF (7 a 12).
Como os EDs sao Class A, o downlink so e entregue na janela RX logo apos o
proximo uplink do device; o novo SF vale a partir do envio seguinte.

Uso:
    export CHIRPSTACK_TOKEN=<API key criada em ChirpStack > API keys>
    ./set_sf.py 9 1            # ED1 -> SF9
    ./set_sf.py 12 1 3 5-8     # ED1, ED3, ED5..ED8 -> SF12
    ./set_sf.py 7 all          # todos os devices da aplicacao -> SF7
    ./set_sf.py 10 ED4RAK      # pelo nome exato no ChirpStack

Numeros sao mapeados para nomes: 1 -> rak4630-sensor1, N -> EDNRAK.
"""
import base64
import json
import os
import sys
import urllib.request

API = os.environ.get("CHIRPSTACK_API", "http://localhost:8090/api")
APPLICATION_ID = os.environ.get("CHIRPSTACK_APP_ID", "d8c97e04-9ecc-467e-98dd-63758a184baf")
SF_CMD_PORT = 10


def call(method, path, body=None):
    req = urllib.request.Request(
        API + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": "Bearer " + os.environ["CHIRPSTACK_TOKEN"],
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


def device_names_from_args(args):
    names = []
    for arg in args:
        if "-" in arg and all(p.isdigit() for p in arg.split("-", 1)):
            lo, hi = map(int, arg.split("-", 1))
            nums = range(lo, hi + 1)
        elif arg.isdigit():
            nums = [int(arg)]
        else:
            names.append(arg)
            continue
        names += ["rak4630-sensor1" if n == 1 else f"ED{n}RAK" for n in nums]
    return names


def main():
    if len(sys.argv) < 3 or not sys.argv[1].isdigit() or not 7 <= int(sys.argv[1]) <= 12:
        sys.exit(__doc__)
    if "CHIRPSTACK_TOKEN" not in os.environ:
        sys.exit("Defina CHIRPSTACK_TOKEN com uma API key do ChirpStack.")
    sf = int(sys.argv[1])

    devices = call("GET", f"/devices?applicationId={APPLICATION_ID}&limit=100")["result"]
    eui_by_name = {d["name"]: d["devEui"] for d in devices}
    names = list(eui_by_name) if sys.argv[2:] == ["all"] else device_names_from_args(sys.argv[2:])

    payload = base64.b64encode(bytes([sf])).decode()
    for name in names:
        eui = eui_by_name.get(name)
        if eui is None:
            print(f"{name}: nao existe no ChirpStack, ignorado")
            continue
        # Limpa comandos antigos ainda nao entregues, para o device nao aplicar um SF velho.
        call("DELETE", f"/devices/{eui}/queue")
        call("POST", f"/devices/{eui}/queue",
             {"queueItem": {"confirmed": False, "fPort": SF_CMD_PORT, "data": payload}})
        print(f"{name} ({eui}): SF{sf} enfileirado")


if __name__ == "__main__":
    main()
