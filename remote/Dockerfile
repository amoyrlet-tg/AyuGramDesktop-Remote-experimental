ARG BUILD_IMAGE=ghcr.io/telegramdesktop/tdesktop/centos_env:latest
FROM ${BUILD_IMAGE} AS builder
USER root
WORKDIR /opt/ayugram-source
COPY . .
ARG API_ID
ARG API_HASH
ARG BUILD_JOBS=2
ENV CMAKE_BUILD_TYPE=Debug
RUN test -n "$API_ID" && test -n "$API_HASH" \
    && cd Telegram \
    && ./configure.sh -D CMAKE_BUILD_TYPE=Debug -D TDESKTOP_API_ID="$API_ID" \
       -D TDESKTOP_API_HASH="$API_HASH" -D AYUGRAM_BUILD_REMOTE_ENGINE=ON \
       -D DESKTOP_APP_DISABLE_AUTOUPDATE=ON -D DESKTOP_APP_DISABLE_CRASH_REPORTS=ON \
    && cmake --build ../out --config Debug --target AyuGramRemoteEngine --parallel "$BUILD_JOBS"
RUN install -Dm755 out/remote/AyuGramRemoteEngine /opt/ayugram/AyuGramRemoteEngine

FROM ${BUILD_IMAGE}
USER root
WORKDIR /opt/ayugram
COPY --from=builder /opt/ayugram/AyuGramRemoteEngine ./AyuGramRemoteEngine
COPY remote/ayuremote ./ayuremote
COPY Telegram/SourceFiles/mtproto/scheme ./schema
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10000:10000
ENTRYPOINT ["python3", "-m", "ayuremote.server", "--state", "/data", "--schema", "/opt/ayugram/schema", "--engine", "/opt/ayugram/AyuGramRemoteEngine"]
