idf_component_register(SRCS "fp_session.c"
                       INCLUDE_DIRS "include"
                       REQUIRES fp_proto lwip freertos
                       PRIV_REQUIRES log esp_timer)
