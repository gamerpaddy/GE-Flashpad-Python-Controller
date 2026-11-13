idf_component_register(SRCS "fp_config.c"
                       INCLUDE_DIRS "include"
                       REQUIRES espressif__cjson nvs_flash
                       PRIV_REQUIRES log)
