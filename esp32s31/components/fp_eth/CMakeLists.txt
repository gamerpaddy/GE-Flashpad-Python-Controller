idf_component_register(SRCS "fp_eth.c"
                       INCLUDE_DIRS "include"
                       REQUIRES esp_eth esp_netif esp_event fp_config
                       PRIV_REQUIRES log esp_driver_gpio)
