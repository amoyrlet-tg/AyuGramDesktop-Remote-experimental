if (WIN32 OR APPLE)
    message(FATAL_ERROR "The experimental remote engine currently targets Linux VPS hosts.")
endif()

add_executable(AyuGramRemoteEngine)
init_non_host_target(AyuGramRemoteEngine)
set_target_properties(AyuGramRemoteEngine PROPERTIES AUTOMOC ON
    RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/remote)
target_precompile_headers(AyuGramRemoteEngine PRIVATE ${src_loc}/ayu/remote/daemon_pch.h)
target_include_directories(AyuGramRemoteEngine PRIVATE ${src_loc})
target_compile_definitions(AyuGramRemoteEngine PRIVATE TDESKTOP_API_ID=${TDESKTOP_API_ID})
nice_target_sources(AyuGramRemoteEngine ${src_loc} PRIVATE
    ayu/remote/daemon_main.cpp
    ayu/remote/daemon_pch.h
    ayu/remote/daemon_runtime.cpp
    ayu/remote/mtp_runtime.h
    ayu/remote/transport.cpp
    ayu/remote/transport.h
    core/core_settings_proxy.cpp
    mtproto/config_loader.cpp
    mtproto/connection_abstract.cpp
    mtproto/connection_abstract.h
    mtproto/connection_http.cpp
    mtproto/connection_http.h
    mtproto/connection_resolving.cpp
    mtproto/connection_resolving.h
    mtproto/connection_tcp.cpp
    mtproto/connection_tcp.h
    mtproto/facade.cpp
    mtproto/mtp_instance.cpp
    mtproto/mtp_instance.h
    mtproto/session.cpp
    mtproto/session.h
    mtproto/session_private.cpp
    mtproto/session_private.h
    mtproto/special_config_request.cpp
    mtproto/special_config_request.h
    storage/serialize_common.cpp
)
target_link_libraries(AyuGramRemoteEngine PRIVATE
    tdesktop::td_mtproto
    tdesktop::td_scheme
    desktop-app::lib_base
    desktop-app::lib_crl
    desktop-app::lib_rpl
    desktop-app::lib_tl
    desktop-app::external_qt
    desktop-app::external_openssl
    desktop-app::external_zlib
    desktop-app::external_kcoreaddons
)
