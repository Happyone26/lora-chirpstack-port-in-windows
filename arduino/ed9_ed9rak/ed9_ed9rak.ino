  #include <Arduino.h>
  #include <LoRaWan-RAK4630.h>
  #include <SPI.h>

  // ==== PREENCHER com as chaves do device cadastrado no ChirpStack ====
  // Este sketch é reaproveitado como "template" para cada novo device: troque o DevEUI
  // (e o AppKey, se não for compartilhado) e o texto "#5" na linha 52 antes de gravar.
  uint8_t nodeDeviceEUI[8] = {0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00}; // ED9RAK
  uint8_t nodeAppEUI[8]    = {0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00};
  uint8_t nodeAppKey[16]   = {0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00};

  // Sub-band AU915 (ChirpStack au915_0 = subband 1 = canais 0-7 + 64)
  #define LORAWAN_SUBBAND 1

  #define LORAWAN_APP_DATA_BUFF_SIZE 64
  #define LORAWAN_APP_TX_DUTYCYCLE   5000
  // Troca de SF por downlink (enviado pelo ChirpStack): porta LORAWAN_SF_CMD_PORT,
  // payload de 1 byte com o SF desejado (0x07 a 0x0C). Em AU915, DR = 12 - SF
  // (DR_5 = SF7 ... DR_0 = SF12). O novo SF vale a partir do proximo envio.
  // Apos reset/rejoin o device volta ao SF7 (DR_5) definido em lora_param_init.
  #define LORAWAN_SF_CMD_PORT 10
  static volatile int8_t pending_dr = -1;
  static uint8_t current_sf = 7;

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

  static lmh_callback_t lora_callbacks = {
      BoardGetBatteryLevel, BoardGetUniqueId, BoardGetRandomSeed,
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
      Serial.println("Teste RAK4631 #9 (ED9RAK) - OTAA / AU915 / ChirpStack");
      Serial.println("=====================================");

      uint32_t err_code = lora_rak4630_init();
      if (err_code != 0)
          Serial.printf("lora_rak4630_init falhou - %d\n", err_code);
      // Chaves - essas funcoes retornam void nesta versao da lib
      lmh_setDevEui(nodeDeviceEUI);
      lmh_setAppEui(nodeAppEUI);
      lmh_setAppKey(nodeAppKey);

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
      // callbacks da lib fazem o trabalho; nada necessário aqui
  }

  static void tx_lora_periodic_handler(void)
  {
      TimerSetValue(&appTimer, LORAWAN_APP_TX_DUTYCYCLE);
      TimerStart(&appTimer);
      send_lora_frame();
  }

  static void send_lora_frame(void)
  {
      if (lmh_join_status_get() != LMH_SET)
      {
          Serial.println("Ainda sem join, aguardando...");
          return;
      }

      if (pending_dr >= 0)
      {
          lmh_datarate_set((uint8_t)pending_dr, false);
          current_sf = 12 - pending_dr;
          pending_dr = -1;
          Serial.printf(">>> SF alterado para SF%d (DR_%d)\n", current_sf, 12 - current_sf);
      }

      m_lora_app_data.port = 2;
      m_lora_app_data.buffsize = 1;
      m_lora_app_data.buffer[0] = 0xAA;

      lmh_error_status error = lmh_send(&m_lora_app_data, LMH_UNCONFIRMED_MSG);
      if (error == LMH_SUCCESS)
      {
          count++;
          Serial.printf("Pacote de teste enviado (total=%lu, SF%d)\n", count, current_sf);
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
      TimerSetValue(&appTimer, LORAWAN_APP_TX_DUTYCYCLE);
      TimerStart(&appTimer);
  }

  static void lorawan_join_failed_handler(void)
  {
      Serial.println(">>> FALHA NO JOIN.");
      Serial.println("Verifique: DevEUI/AppEUI/AppKey corretos, subband compativel com o gateway,");
      Serial.println("gateway com uplink habilitado na banda AU915, e se o device esta 'ativo' no ChirpStack.");
  }

  static void lorawan_rx_handler(lmh_app_data_t *app_data)
  {
      Serial.printf("Downlink recebido - porta %d, %d bytes: ", app_data->port, app_data->buffsize);
      for (uint8_t i = 0; i < app_data->buffsize; i++)
          Serial.printf("%02X ", app_data->buffer[i]);
      Serial.println();

      if (app_data->port == LORAWAN_SF_CMD_PORT)
      {
          uint8_t sf = app_data->buffsize == 1 ? app_data->buffer[0] : 0;
          if (sf >= 7 && sf <= 12)
          {
              pending_dr = 12 - sf;
              Serial.printf(">>> Comando de SF recebido: SF%d (aplicado no proximo envio)\n", sf);
          }
          else
              Serial.println(">>> Comando de SF invalido (esperado 1 byte, 7 a 12)");
      }
  }

  static void lorawan_confirm_class_handler(DeviceClass_t Class)
  {
      Serial.printf("Classe confirmada: %c\n", "ABC"[Class]);
      lmh_class_request(Class);
  }
