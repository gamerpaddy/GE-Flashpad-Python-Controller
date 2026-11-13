cmake_minimum_required(VERSION 3.22)

# Needs ESP-IDF v6.1 (first release with the esp32s31 target):
#   idf.py --preview set-target esp32s31
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(flashpad_esp32s31)
