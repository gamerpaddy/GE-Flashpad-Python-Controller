idf_component_register(SRCS "fp_dark.c"
                       INCLUDE_DIRS "include"
                       REQUIRES fp_image fp_proto esp_partition nvs_flash
                       PRIV_REQUIRES log esp_timer freertos)
