idf_component_register(SRCS "fp_capture.c"
                       INCLUDE_DIRS "include"
                       REQUIRES fp_proto fp_config fp_dark fp_eth fp_image fp_session xray_relay
                       PRIV_REQUIRES log esp_timer freertos)
