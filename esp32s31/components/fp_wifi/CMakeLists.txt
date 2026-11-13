idf_component_register(SRCS "fp_wifi.c"
                       INCLUDE_DIRS "include"
                       REQUIRES fp_config esp_wifi esp_netif esp_event
                       PRIV_REQUIRES log freertos lwip)
