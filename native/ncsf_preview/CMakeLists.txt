cmake_minimum_required(VERSION 3.16)
project(rpgds_ncsf_preview LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

file(GLOB SSEQ_PLAYER_SOURCES "SSEQPlayer/*.cpp")
add_executable(rpgds_ncsf_preview main.cpp ${SSEQ_PLAYER_SOURCES})
target_include_directories(rpgds_ncsf_preview PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}"
    "${CMAKE_CURRENT_SOURCE_DIR}/SSEQPlayer"
)
if(MINGW)
    target_link_options(rpgds_ncsf_preview PRIVATE -static -static-libgcc -static-libstdc++)
endif()
