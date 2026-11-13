idf_component_register(SRCS "fp_image.c"
                       INCLUDE_DIRS "include"
                       REQUIRES fp_proto freertos
                       PRIV_REQUIRES log heap esp_timer esp_rom)
