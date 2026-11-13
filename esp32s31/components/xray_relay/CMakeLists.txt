idf_component_register(SRCS "xray_relay.c"
                       INCLUDE_DIRS "include"
                       REQUIRES esp_driver_gpio esp_timer
                       PRIV_REQUIRES log)
