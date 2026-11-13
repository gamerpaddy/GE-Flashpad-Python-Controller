idf_component_register(SRCS "main.c"
                       INCLUDE_DIRS "."
                       REQUIRES fp_proto fp_config fp_eth fp_image fp_session fp_capture fp_dark fp_wifi web_ui xray_relay
                       PRIV_REQUIRES esp_netif esp_event esp_timer heap nvs_flash)
