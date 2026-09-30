# RAK4631 + RAK7371 + ChirpStack (AU915)

Stack completa de teste LoRaWAN: um gateway RAK7371 (concentrador SX1302) rodando o packet forwarder oficial da Semtech, um ou mais end-devices RAK4631 (nRF52840 + SX1262), e um ChirpStack v4 com observabilidade via Prometheus/Grafana/Loki, tudo em AU915 sub-band 1.

Este repositório é uma versão portátil, preparada para instalação em um notebook **Windows 11**. Todas as chaves LoRaWAN (DevEUI/AppKey) foram removidas dos sketches — você precisa gerar/preencher as suas próprias no ChirpStack antes de gravar qualquer device.

## Arquitetura

```
RAK7371 (USB) ──┐
                ├─► lora_pkt_fwd (WSL2, protocolo Semtech UDP, porta 1700)
                │        │
                │        ▼
RAK4631 x N ────┼──► (rádio LoRa) ──► gateway-bridge (Docker) ──► ChirpStack (Docker)
  (USB só p/           AU915                                         │
   flash/monitor)                                                    ▼
                                                          MQTT (mosquitto) ──► mqtt-stats-exporter
                                                                                  │        │
                                                                                  ▼        ▼
                                                                            Prometheus   CSV
                                                                                  │
                                                                                  ▼
                                                                              Grafana
```

- **Região/banda:** AU915, sub-band 1 (`au915_0` no ChirpStack = canais 0-7 + canal 64, 915.2–916.6MHz + 500kHz em 915.9MHz)
- **Firmware dos devices:** `LORAWAN_SUBBAND 1`, `LORAMAC_REGION_AU915`, Classe A, ADR desligado, DR fixo em `DR_5` (SF7/BW125), mensagens confirmadas, envio a cada 5s
- **Gateway ID (fixo, do exemplo Semtech):** `AA555A0000000000`

## Pré-requisitos (Windows 11)

1. **Docker Desktop** com o backend **WSL2** habilitado (Settings → General → "Use the WSL 2 based engine")
2. Uma distro Linux no WSL2 (recomendado: Ubuntu, via `wsl --install -d Ubuntu`)
3. **usbipd-win** — permite passar o RAK7371 (dispositivo USB) do Windows pro WSL2. Instalar via `winget install usbipd`
4. **Arduino IDE** (versão 2.x), instalado nativamente no Windows (não dentro do WSL2 — precisa de acesso direto à porta COM/USB)
5. **Git** e **GitHub CLI** (`gh`), se for versionar mudanças

## 1. Clonar este repositório

```powershell
git clone <url-deste-repo>
cd rak4631-chirpstack-portable
```

## 2. Subir o ChirpStack + observabilidade (Docker)

```powershell
cd chirpstack-docker
docker compose up -d
```

Isso sobe: `chirpstack`, `chirpstack-gateway-bridge` (UDP, porta 1700), `postgres`, `redis`, `mosquitto`, `mqtt-stats-exporter`, `prometheus`, `grafana`, `loki`, `promtail`.

**Portas expostas no host:**

| Porta | Serviço |
|---|---|
| 8080 | ChirpStack (web UI + API) |
| 8090 | ChirpStack REST API |
| 3000 | Grafana |
| 9090 | Prometheus |
| 1883 | MQTT (Mosquitto) |
| 1700/udp | Gateway Bridge (packet forwarder) |
| 3100 | Loki |
| 9080 | Promtail |

Login padrão do ChirpStack: `admin` / `admin`. Login padrão do Grafana: `admin` / `admin`. **Troque essas senhas se este ambiente sair da fase de teste.**

A região `au915_0` já vem habilitada por padrão em `configuration/chirpstack/chirpstack.toml` — não precisa mexer nisso.

## 3. Gateway RAK7371 (via WSL2)

O `lora_pkt_fwd` (packet forwarder) é um binário Linux que fala com o concentrador via USB (libusb) — não roda nativamente no Windows. O caminho é rodá-lo dentro do WSL2, com o dispositivo USB passado via `usbipd`.

### 3.1. Passar o USB do RAK7371 pro WSL2

No **PowerShell como Administrador**:

```powershell
usbipd list
# anote o BUSID do dispositivo RAK7371 (geralmente aparece como "USB Serial" ou similar)
usbipd bind --busid <BUSID>
usbipd attach --wsl --busid <BUSID>
```

Depois de conectar, dentro do WSL2 (`wsl`), confirme que apareceu:
```bash
ls /dev/ttyACM*
```

Nota: toda vez que desconectar/reconectar o RAK7371 fisicamente, ou reiniciar o Windows, é preciso rodar `usbipd attach` de novo — ele não persiste automaticamente.

### 3.2. Clonar e compilar o sx1302_hal (dentro do WSL2)

```bash
sudo apt update && sudo apt install -y build-essential libusb-1.0-0-dev git
git clone https://github.com/Lora-net/sx1302_hal.git
cd sx1302_hal
git checkout V2.1.0
make
```

### 3.3. Aplicar a configuração AU915 já pronta

```bash
cp <caminho-do-repo>/gateway/global_conf.json packet_forwarder/global_conf.json
```

Esse arquivo já está configurado com: `com_path: /dev/ttyACM0`, `server_address: localhost`, portas 1700, e as frequências AU915 sub-band 1 (`radio_0` em 915600000Hz, `radio_1` em 916300000Hz, canais 0-7 + canal de 500kHz em 915900000Hz).

**Importante:** `server_address` está como `localhost` — como o WSL2 tem sua própria interface de rede, isso só funciona se o ChirpStack (Docker) também estiver acessível em `localhost` de dentro do WSL2. No Docker Desktop com integração WSL2 habilitada (padrão), isso geralmente funciona sem ajuste. Se não funcionar, troque `server_address` para o IP do Windows host visível de dentro do WSL2 (`cat /etc/resolv.conf` mostra o nameserver, que costuma ser o IP do host).

### 3.4. Rodar o packet forwarder

```bash
cd packet_forwarder
./lora_pkt_fwd
```

Deixe rodando num terminal — ele imprime estatísticas de recepção periodicamente. `Ctrl+C` pra parar.

## 4. Arduino IDE (Windows nativo)

1. Preferences → "Additional Boards Manager URLs" → adicionar a URL do board package da RAKwireless (RAK4630/WisBlock)
2. Boards Manager → instalar o pacote **RAKwireless nRF52 Boards**
3. Library Manager → instalar **SX126x-Arduino**
4. Selecionar a placa correta (RAK4630) e a porta COM do device conectado

## 5. Cadastrar os devices no ChirpStack

Acesse `http://localhost:8080`:

1. **Gateways** → Add gateway, com o Gateway ID `AA555A0000000000` (deve aparecer sozinho como "seen recently" assim que o `lora_pkt_fwd` estiver rodando e mandando stats)
2. **Applications** → criar uma aplicação de teste
3. Dentro da aplicação, **Device profiles** → criar um perfil com região **AU915**, classe A
4. **Devices** → Add device, escolher o perfil criado. O DevEUI pode ser gerado automaticamente (ícone de dado ao lado do campo)
5. Abrir o device criado → aba **OTAA keys** → gerar/copiar o **Application key**

## 6. Preencher e gravar os sketches

Em `arduino/loratestefuncional/loratestefuncional.ino` (primeiro device) ou `arduino/loratestefuncional2/loratestefuncional2.ino` (usado como template pros demais — copie a pasta pra cada device novo, ou reedite antes de cada gravação):

```cpp
uint8_t nodeDeviceEUI[8] = { /* preencher com o DevEUI do ChirpStack */ };
uint8_t nodeAppEUI[8]    = {0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00}; // pode deixar zerado
uint8_t nodeAppKey[16]   = { /* preencher com o Application key do ChirpStack */ };
```

Compile e grave via Arduino IDE. Abra o Serial Monitor (115200 baud) pra acompanhar o join.

**Se a gravação falhar com "Serial port could not be opened"**: o board reseta pra modo bootloader (DFU) durante a gravação, e às vezes a porta demora a reaparecer. Desconecte/reconecte o cabo USB e tente de novo; se persistir, aperte o botão RESET da placa duas vezes rápido pra forçar o modo DFU manualmente antes de mandar gravar.

## 7. Verificar

- **Serial Monitor**: deve mostrar `>>> JOIN OK!` seguido de envios periódicos a cada 5s
- **ChirpStack → LoRa Frames** (na tela do device): deve mostrar os uplinks chegando
- **Grafana** (`http://localhost:3000`, dashboard "ChirpStack - Device Metrics"): RSSI, SNR, PDR, vazão, taxa de colisão estimada, devices ativos, e um painel de log/payload
- **CSV**: cada uplink real gera uma linha em `chirpstack-docker/data/uplinks/uplinks.csv`, com PDR, Time-on-Air, e taxa de colisão estimada (modelo Pure ALOHA, ver comentários em `mqtt-stats-exporter/index.js`)

## Troubleshooting conhecido

- **"failed to ping the concentrator MCU" / "wrong ACK type"**: o `com_path` do `global_conf.json` está apontando pra porta errada — normalmente acontece quando um RAK4631 conecta ANTES do gateway e rouba `/dev/ttyACM0`. Solução: pare o `lora_pkt_fwd`, desconecte os RAK4631, confirme que o gateway está em `/dev/ttyACM0` (`ls /dev/ttyACM*`), suba o `lora_pkt_fwd`, só depois reconecte os end-devices.
- **"Device or resource busy" ao abrir a porta do gateway**: outro processo (geralmente um Serial Monitor esquecido aberto) está com a porta aberta. No Linux/WSL2: `fuser -v /dev/ttyACM0` mostra quem está segurando.
- **Device nunca aparece no ChirpStack apesar de RF boa (CRC_OK nos logs do `lora_pkt_fwd`)**: quase sempre é mismatch de região entre o Device Profile do ChirpStack e o firmware/gateway. Confirme que todos os três (firmware `LORAMAC_REGION_AU915`, Device Profile no ChirpStack, e o sub-band configurado) usam AU915 sub-band 1 (`LORAWAN_SUBBAND 1` no firmware corresponde aos canais 0-7 do `au915_0`).
- **Painéis do Grafana mostrando dado "congelado" de um device desligado**: já tratado no código (o `mqtt-stats-exporter` limpa métricas de devices inativos depois de `LORA_ACTIVE_WINDOW_SEC` segundos, configurável no `docker-compose.yml`, default 20s).
