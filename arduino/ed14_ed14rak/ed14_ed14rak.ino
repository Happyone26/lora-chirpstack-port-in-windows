  #include <Arduino.h>
  #include <LoRaWan-RAK4630.h>
  #include <SPI.h>

  // ==== PREENCHER com as chaves do device cadastrado no ChirpStack ====
  // Este sketch é reaproveitado como "template" para cada novo device: troque o DevEUI
  // (e o AppKey, se não for compartilhado) e o texto "#5" na linha 52 antes de gravar.
  uint8_t nodeDeviceEUI[8] = {0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00}; // ED14RAK
  uint8_t nodeAppEUI[8]    = {0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00};
  uint8_t nodeAppKey[16]   = {0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00};

  // Sub-band AU915 (ChirpStack au915_0 = subband 1 = canais 0-7 + 64)
  #define LORAWAN_SUBBAND 1

  #define LORAWAN_APP_DATA_BUFF_SIZE 64
  #define LORAWAN_APP_TX_DUTYCYCLE   5000
  // Modo comandado por downlink (enviado pelo ChirpStack) na porta LORAWAN_SF_CMD_PORT:
  //   1 byte 0x07..0x0C   -> MEDINDO: dados na porta LORAWAN_DATA_PORT a cada
  //                          LORAWAN_APP_TX_DUTYCYCLE, nesse SF
  //   0x00 [+ 0x07..0x0C] -> STANDBY: so uma consulta curta na porta LORAWAN_STANDBY_PORT a
  //                          cada LORAWAN_STANDBY_POLL_MS, no SF indicado (sem 2o byte: mantem)
  // Class A so recebe downlink logo apos um uplink, por isso o standby nao fica mudo.
  // Em AU915, DR = 12 - SF (DR_5 = SF7 ... DR_0 = SF12). Comandos valem a partir do proximo
  // envio. Apos reset o device volta ao estado definido em LORAWAN_START_STANDBY.
  #define LORAWAN_SF_CMD_PORT 10
  #define LORAWAN_DATA_PORT 2
  #define LORAWAN_STANDBY_PORT 3
  #define LORAWAN_STANDBY_POLL_MS 10000
  #define LORAWAN_START_STANDBY 1 // 1 = comeca em standby (campanha); 0 = comeca medindo em SF7
  // Jitter: cada intervalo e sorteado em base +- jitter, para dois EDs nao ficarem transmitindo
  // sempre no mesmo instante (em 07/10 pares sincronizados colidiram em ~1/8 dos pacotes).
  #define LORAWAN_APP_TX_JITTER_MS 500
  #define LORAWAN_STANDBY_POLL_JITTER_MS 1000
  // Se o join falhar (JOINREQ_NBTRIALS tentativas), tenta de novo apos 30-40 s, indefinidamente.
  #define LORAWAN_JOIN_RETRY_MS 30000
  #define LORAWAN_JOIN_RETRY_JITTER_MS 10000
  static volatile bool join_retry_pending = false;
  // Troca de potencia por downlink na porta LORAWAN_TXP_CMD_PORT: 1 byte = indice TX_POWER 0..14.
  // AU915: potencia = 30 - 2*indice - 2,15 dBm, limitada a 22 dBm pelo SX1262 (0, 1 e 2 dao 22 dBm;
  // 3 = 21, 4 = 19, 5 = 17, 6 = 15, 7 = 13 ... 14 = -1 dBm). Padrao 0 = potencia maxima.
  #define LORAWAN_TXP_CMD_PORT 11
  #define LORAWAN_TXP_MAX_INDEX 14
  static volatile int8_t pending_txp = -1;
  static uint8_t current_txp = 0;
  // Varredura de potencia (porta 11 com 2..15 bytes = lista de indices): o pacote de DADOS com
  // FCnt f sai com lista[f % tamanho], intercalando as potencias pacote a pacote (a potencia de
  // um pacote perdido fica conhecida pelo FCnt). 1 byte na porta 11 volta para potencia fixa.
  #define LORAWAN_TXP_MAX_SWEEP 15
  static uint8_t txp_sweep[LORAWAN_TXP_MAX_SWEEP];
  static uint8_t txp_sweep_len = 0;
  static uint8_t pending_sweep[LORAWAN_TXP_MAX_SWEEP];
  static volatile int8_t pending_sweep_len = -1; // -1 = sem mudanca, 0 = desligar varredura
  // Bateria (base WisBlock): tensao lida em WB_A0 atraves do divisor da base. VBAT_DIVIDER_COMP
  // vem do exemplo oficial RAK4631; se a leitura divergir de um multimetro, ajuste aqui.
  #define PIN_VBAT WB_A0
  #define VBAT_MV_PER_LSB (0.73242188F) // referencia 3,0 V / 4096 (12 bits)
  #define VBAT_DIVIDER_COMP (1.73F)
  #define BATTERY_READ_MS 60000
  // LED verde (so sem USB): pisca rapido ate o join, devagar depois - mostra no campo se ligou/conectou
  #define LED_BLINK_JOINED_MS 2000
  #define LED_BLINK_JOINING_MS 500
  #define LED_BLINK_ON_MS 50
  // No pacote de medicao (1 byte): nivel da bateria 0..100 %, ou este valor quando alimentado por USB
  #define BATTERY_BYTE_USB 101
  static volatile uint16_t battery_mv = 0;
  static volatile uint8_t battery_pct = 0;
  static volatile int8_t pending_sf = -1;
  static volatile int8_t pending_poll_sf = -1;
  static volatile bool pending_standby = false;
  static bool in_standby = LORAWAN_START_STANDBY;
  static uint8_t current_sf = 7;
  static uint8_t poll_sf = 7;

  #define JOINREQ_NBTRIALS 3               

  static uint32_t count = 0;
  static uint32_t count_fail = 0;

  static uint8_t m_lora_app_data_buffer[LORAWAN_APP_DATA_BUFF_SIZE];
  static lmh_app_data_t m_lora_app_data = {m_lora_app_data_buffer, 0, 0, 0, 0};

  TimerEvent_t appTimer;

  static void lorawan_has_joined_handler(void);
  static void lorawan_join_failed_handler(void);
  static void lorawan_rx_handler(lmh_app_data_t *app_data);
  static void lorawan_confirm_class_handler(DeviceClass_t Class);
  static void send_lora_frame(void);
  static void tx_lora_periodic_handler(void);

  // ADR desligado e DR_5 fixo (SF7/BW125 em AU915) - sem adaptacao
  static lmh_param_t lora_param_init = {
      LORAWAN_ADR_OFF, DR_5, LORAWAN_PUBLIC_NETWORK,
      JOINREQ_NBTRIALS, LORAWAN_DEFAULT_TX_POWER, LORAWAN_DUTYCYCLE_OFF};

  // VBUS presente = placa ligada no USB (alimentacao externa)
  static bool usb_connected(void)
  {
      return (NRF_POWER->USBREGSTATUS & POWER_USBREGSTATUS_VBUSDETECT_Msk) != 0;
  }

  static uint16_t read_battery_mv(void)
  {
      uint32_t sum = 0;
      for (uint8_t i = 0; i < 16; i++)
          sum += analogRead(PIN_VBAT);
      return (uint16_t)((sum / 16.0F) * VBAT_MV_PER_LSB * VBAT_DIVIDER_COMP);
  }

  // Curva de descarga tipica de Li-ion 18650 3,7 V (celula usada nos EDs), interpolada entre
  // os pontos. Mais fiel no meio da faixa que a reta dos exemplos RAK (que subestima ~3,8-4,0 V).
  static const uint16_t BATT_CURVE_MV[] = {3270, 3610, 3690, 3710, 3730, 3750, 3770, 3790, 3800, 3820, 3840,
                                           3850, 3870, 3910, 3950, 3980, 4020, 4080, 4110, 4150, 4200};
  static const uint8_t BATT_CURVE_PCT[] = {0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50,
                                           55, 60, 65, 70, 75, 80, 85, 90, 95, 100};
  static uint8_t battery_mv_to_pct(uint16_t mv)
  {
      const uint8_t n = sizeof(BATT_CURVE_MV) / sizeof(BATT_CURVE_MV[0]);
      if (mv <= BATT_CURVE_MV[0])
          return 0;
      if (mv >= BATT_CURVE_MV[n - 1])
          return 100;
      uint8_t i = 1;
      while (mv > BATT_CURVE_MV[i])
          i++;
      return BATT_CURVE_PCT[i - 1] + (uint32_t)(mv - BATT_CURVE_MV[i - 1]) * (BATT_CURVE_PCT[i] - BATT_CURVE_PCT[i - 1]) /
                                         (BATT_CURVE_MV[i] - BATT_CURVE_MV[i - 1]);
  }

  static void update_battery(void)
  {
      battery_mv = read_battery_mv();
      battery_pct = battery_mv_to_pct(battery_mv);
      Serial.printf("Bateria: %u mV (%u%%)%s\n", battery_mv, battery_pct, usb_connected() ? " - USB conectado" : "");
  }

  // Nivel para o DevStatusAns do LoRaWAN (ChirpStack): 0 = alimentacao externa, 1..254 = nivel
  static uint8_t lorawan_battery_level(void)
  {
      if (usb_connected())
          return 0;
      return 1 + (uint16_t)battery_pct * 253 / 100;
  }

  static lmh_callback_t lora_callbacks = {
      lorawan_battery_level, BoardGetUniqueId, BoardGetRandomSeed,
      lorawan_rx_handler, lorawan_has_joined_handler,
      lorawan_confirm_class_handler, lorawan_join_failed_handler};

  void setup()
  {
      Serial.begin(115200);
      time_t timeout = millis();
      while (!Serial)
      {
          if ((millis() - timeout) < 5000) delay(100);
          else break;
      }

      Serial.println("=====================================");
      Serial.println("Teste RAK4631 #14 (ED14RAK) - OTAA / AU915 / ChirpStack");
      Serial.println("=====================================");

      pinMode(LED_GREEN, OUTPUT);
      digitalWrite(LED_GREEN, !LED_STATE_ON);
      analogReference(AR_INTERNAL_3_0);
      analogReadResolution(12);
      delay(1);
      update_battery();

      uint32_t err_code = lora_rak4630_init();
      if (err_code != 0)
          Serial.printf("lora_rak4630_init falhou - %d\n", err_code);
      // Chaves - essas funcoes retornam void nesta versao da lib
      lmh_setDevEui(nodeDeviceEUI);
      lmh_setAppEui(nodeAppEUI);
      lmh_setAppKey(nodeAppKey);

      // Semente do sorteio diferente em cada placa (ID unico do chip + DevEUI); com a mesma
      // semente todas sorteariam a mesma sequencia de intervalos e o jitter nao separaria nada.
      uint32_t seed = BoardGetRandomSeed();
      for (uint8_t i = 0; i < 8; i++)
          seed = seed * 31 + nodeDeviceEUI[i];
      randomSeed(seed);

      err_code = lmh_init(&lora_callbacks, lora_param_init, true, CLASS_A, LORAMAC_REGION_AU915);
      if (err_code != 0)
      {
          Serial.printf("lmh_init falhou - %d\n", err_code);
          return;
      }

      // Use lmh_setSubBandChannels OU lmh_setSingleChannelGateway, nunca os dois
      if (!lmh_setSubBandChannels(LORAWAN_SUBBAND))
          Serial.println("lmh_setSubBandChannels falhou. Subband errado?");

      appTimer.timerNum = 3;
      TimerInit(&appTimer, tx_lora_periodic_handler);

      Serial.println("Iniciando JOIN (OTAA)...");
      lmh_join();
  }

  void loop()
  {
      // A LoRa roda nos callbacks da lib; aqui so bateria e LED.
      static uint32_t last_battery = 0, last_blink = 0;
      uint32_t now = millis();
      if (now - last_battery >= BATTERY_READ_MS)
      {
          last_battery = now;
          update_battery();
      }
      uint32_t blink_ms = lmh_join_status_get() == LMH_SET ? LED_BLINK_JOINED_MS : LED_BLINK_JOINING_MS;
      if (!usb_connected() && now - last_blink >= blink_ms)
      {
          last_blink = now;
          digitalWrite(LED_GREEN, LED_STATE_ON);
          delay(LED_BLINK_ON_MS);
          digitalWrite(LED_GREEN, !LED_STATE_ON);
      }
      delay(20);
  }

  // Proximo intervalo: base do modo atual +- jitter sorteado
  static uint32_t next_interval(void)
  {
      uint32_t base = in_standby ? LORAWAN_STANDBY_POLL_MS : LORAWAN_APP_TX_DUTYCYCLE;
      uint32_t jitter = in_standby ? LORAWAN_STANDBY_POLL_JITTER_MS : LORAWAN_APP_TX_JITTER_MS;
      return base - jitter + random(2 * jitter + 1);
  }

  static void tx_lora_periodic_handler(void)
  {
      if (join_retry_pending)
      {
          // O resultado chega em lorawan_has_joined_handler / lorawan_join_failed_handler,
          // que rearmam o timer.
          join_retry_pending = false;
          Serial.println("Nova tentativa de JOIN (OTAA)...");
          lmh_join();
          return;
      }
      send_lora_frame();
      // Intervalo do proximo envio depende do modo ja atualizado por send_lora_frame()
      TimerSetValue(&appTimer, next_interval());
      TimerStart(&appTimer);
  }

  static void send_lora_frame(void)
  {
      if (lmh_join_status_get() != LMH_SET)
      {
          Serial.println("Ainda sem join, aguardando...");
          return;
      }

      if (pending_standby)
      {
          pending_standby = false;
          in_standby = true;
          if (pending_poll_sf >= 0)
          {
              poll_sf = pending_poll_sf;
              pending_poll_sf = -1;
          }
          Serial.printf(">>> STANDBY: consulta a cada %d s em SF%d\n", LORAWAN_STANDBY_POLL_MS / 1000, poll_sf);
      }
      else if (pending_sf >= 0)
      {
          current_sf = pending_sf;
          pending_sf = -1;
          in_standby = false;
          Serial.printf(">>> MEDINDO em SF%d (DR_%d)\n", current_sf, 12 - current_sf);
      }
      if (pending_sweep_len >= 0)
      {
          txp_sweep_len = pending_sweep_len;
          memcpy(txp_sweep, pending_sweep, txp_sweep_len);
          pending_sweep_len = -1;
          if (txp_sweep_len > 0)
              Serial.printf(">>> VARREDURA DE POTENCIA: %d niveis\n", txp_sweep_len);
      }
      if (pending_txp >= 0)
      {
          current_txp = pending_txp;
          pending_txp = -1;
          Serial.printf(">>> POTENCIA: indice TX_POWER_%d\n", current_txp);
      }
      uint8_t tx_sf = in_standby ? poll_sf : current_sf;
      uint8_t tx_txp = current_txp;
      if (!in_standby && txp_sweep_len > 0)
      {
          MibRequestConfirm_t mib;
          mib.Type = MIB_UPLINK_COUNTER;
          LoRaMacMibGetRequestConfirm(&mib); // = FCnt deste pacote (so incrementa depois do envio)
          tx_txp = txp_sweep[mib.Param.UpLinkCounter % txp_sweep_len];
      }
      lmh_datarate_set(12 - tx_sf, false);
      lmh_tx_power_set(tx_txp); // reaplicado a cada envio, para nao voltar ao padrao apos rejoin

      m_lora_app_data.port = in_standby ? LORAWAN_STANDBY_PORT : LORAWAN_DATA_PORT;
      if (in_standby)
      {
          // Consulta: tensao da bateria (mV, 2 bytes), nivel (%), flags (bit0 = USB) e indice de potencia
          m_lora_app_data.buffsize = 6;
          m_lora_app_data.buffer[4] = current_txp;
          m_lora_app_data.buffer[5] = txp_sweep_len;
          m_lora_app_data.buffer[0] = battery_mv >> 8;
          m_lora_app_data.buffer[1] = battery_mv & 0xFF;
          m_lora_app_data.buffer[2] = battery_pct;
          m_lora_app_data.buffer[3] = usb_connected() ? 0x01 : 0x00;
      }
      else
      {
          // Medicao: 1 byte = nivel da bateria, ou USB. Com controle de potencia em uso (varredura ou
          // indice != 0) vai um 2o byte com o indice usado neste pacote; sem ele, o tamanho (e o
          // tempo no ar) fica igual ao da campanha de SF.
          m_lora_app_data.buffsize = (txp_sweep_len > 0 || tx_txp != 0) ? 2 : 1;
          m_lora_app_data.buffer[0] = usb_connected() ? BATTERY_BYTE_USB : battery_pct;
          m_lora_app_data.buffer[1] = tx_txp;
      }

      lmh_error_status error = lmh_send(&m_lora_app_data, LMH_UNCONFIRMED_MSG);
      if (error == LMH_SUCCESS)
      {
          count++;
          Serial.printf("%s enviado (total=%lu, SF%d)\n", in_standby ? "Consulta standby" : "Pacote de teste", count, tx_sf);
      }
      else
      {
          count_fail++;
          Serial.printf("Falha ao enviar (total falhas=%lu), erro=%d\n", count_fail, error);
      }
  }
  static void lorawan_has_joined_handler(void)
  {
      Serial.println(">>> JOIN OK! Conexao com o gateway/ChirpStack estabelecida.");
      lmh_class_request(CLASS_A);
      TimerSetValue(&appTimer, next_interval());
      TimerStart(&appTimer);
  }

  static void lorawan_join_failed_handler(void)
  {
      Serial.println(">>> FALHA NO JOIN.");
      Serial.println("Verifique: DevEUI/AppEUI/AppKey corretos, subband compativel com o gateway,");
      Serial.println("gateway com uplink habilitado na banda AU915, e se o device esta 'ativo' no ChirpStack.");
      uint32_t wait_ms = LORAWAN_JOIN_RETRY_MS + random(LORAWAN_JOIN_RETRY_JITTER_MS + 1);
      Serial.printf("Nova tentativa de JOIN em %lu s.\n", wait_ms / 1000);
      join_retry_pending = true;
      TimerSetValue(&appTimer, wait_ms);
      TimerStart(&appTimer);
  }

  static void lorawan_rx_handler(lmh_app_data_t *app_data)
  {
      Serial.printf("Downlink recebido - porta %d, %d bytes: ", app_data->port, app_data->buffsize);
      for (uint8_t i = 0; i < app_data->buffsize; i++)
          Serial.printf("%02X ", app_data->buffer[i]);
      Serial.println();

      if (app_data->port == LORAWAN_SF_CMD_PORT && app_data->buffsize >= 1)
      {
          uint8_t cmd = app_data->buffer[0];
          if (app_data->buffsize == 1 && cmd >= 7 && cmd <= 12)
          {
              pending_standby = false;
              pending_sf = cmd;
              Serial.printf(">>> Comando: medir em SF%d (aplicado no proximo envio)\n", cmd);
          }
          else if (cmd == 0x00 && (app_data->buffsize == 1 ||
                   (app_data->buffsize == 2 && app_data->buffer[1] >= 7 && app_data->buffer[1] <= 12)))
          {
              pending_sf = -1;
              if (app_data->buffsize == 2)
                  pending_poll_sf = app_data->buffer[1];
              pending_standby = true;
              Serial.println(">>> Comando: standby (aplicado no proximo envio)");
          }
          else
              Serial.println(">>> Comando invalido (esperado SF 7-12, ou 00 [+ SF da consulta])");
      }
      else if (app_data->port == LORAWAN_TXP_CMD_PORT && app_data->buffsize >= 1 &&
               app_data->buffsize <= LORAWAN_TXP_MAX_SWEEP)
      {
          bool ok = true;
          for (uint8_t i = 0; i < app_data->buffsize; i++)
              ok = ok && app_data->buffer[i] <= LORAWAN_TXP_MAX_INDEX;
          if (!ok)
              Serial.println(">>> Comando de potencia invalido (indices 0-14)");
          else if (app_data->buffsize == 1)
          {
              pending_txp = app_data->buffer[0];
              pending_sweep_len = 0;
              Serial.printf(">>> Comando: potencia fixa TX_POWER_%d (aplicado no proximo envio)\n", app_data->buffer[0]);
          }
          else
          {
              memcpy(pending_sweep, app_data->buffer, app_data->buffsize);
              pending_sweep_len = app_data->buffsize;
              Serial.printf(">>> Comando: varredura de potencia com %d niveis (aplicada no proximo envio)\n", app_data->buffsize);
          }
      }
  }

  static void lorawan_confirm_class_handler(DeviceClass_t Class)
  {
      Serial.printf("Classe confirmada: %c\n", "ABC"[Class]);
      lmh_class_request(Class);
  }
