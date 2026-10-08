# syntax=docker/dockerfile:1
#
# Punching Bag —— 容器化靶场（T1）
#
#   上游  : https://github.com/tumi8/punching-bag
#   版本  : main @ cfe60b3e25a19113588cb5fe7313dd3da658d7ca   (2026-10-05, "Adding acknowledgments/citations")
#   许可证: AGPL-3.0（上游 LICENSE，全文 661 行）
#
# ⚠️ 许可证更正 —— 本仓库此前把这个工具记为 CC-BY 4.0，是错的。
#    上游仓库里唯一的许可证文件是 GNU AGPL-3.0；源码文件无独立头注，README 无许可证字样。
#    AGPL 与 CC-BY 不是一回事：CC-BY 只需署名，AGPL 是**强 copyleft 且带网络条款（§13）**。
#    对当前阶段的实际影响：
#      - Stage 0/1 的**本地研究使用不触发任何义务**（不分发、不对网络提供服务）；
#      - **分发这个镜像**、或走 TODOS #3 的 **fork 回退路线并分发**，会触发：
#        衍生作品须以 AGPL 发布，且网络服务须提供源码。
#    这直接关系到"将来开源原型代码"的可行性，见 CLAUDE.md「复用或改写第三方工具」段。
#
# 源码**不 vendor 进本仓库**（CLAUDE.md：第三方不进版本库）。构建期从上游克隆，
# 并按写死的 SHA 校验——校验失败即构建失败，而不是"用了个看起来没问题的版本"。

# ────────────────────────────── builder ──────────────────────────────
FROM debian:bookworm-slim AS builder

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        cmake \
        git \
        ca-certificates \
        libpcap-dev \
        libnet-dev \
        nlohmann-json3-dev \
    && rm -rf /var/lib/apt/lists/*

ARG PB_REPO=https://github.com/tumi8/punching-bag.git
ARG PB_COMMIT=cfe60b3e25a19113588cb5fe7313dd3da658d7ca

RUN git clone "${PB_REPO}" /src/punching-bag \
 && git -C /src/punching-bag checkout --detach "${PB_COMMIT}" \
 && test "$(git -C /src/punching-bag rev-parse HEAD)" = "${PB_COMMIT}" \
 && echo "pinned upstream OK: ${PB_COMMIT}"

WORKDIR /src/punching-bag/build
RUN cmake .. -DCMAKE_BUILD_TYPE=Release \
 && make -j"$(nproc)" \
 && test -x ./punchingbag

# ────────────────────────────── runtime ──────────────────────────────
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

# libpcap0.8 / libnet1   —— 运行期共享库（nlohmann/json 是纯头文件，无需运行期包）
# iproute2               —— ip -6 route / ip netns；靶场与探测侧都要用
# iputils-ping           —— T1 的验证手段
# tcpdump                —— Stage 0.2(a) 的对账首选路径：容器 veth 侧自己数包（设计方案 :755），
#                           把「给上游打 C++ 补丁」从关键路径上挪开
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpcap0.8 \
        libnet1 \
        iproute2 \
        iputils-ping \
        tcpdump \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /src/punching-bag/build/punchingbag /usr/local/bin/punchingbag
COPY --from=builder /src/punching-bag/configs /opt/punchingbag/configs

# 构建期断言：动态库真的解析得到。
# 上游用 libnet_init(LIBNET_RAW6, nullptr, ...) 自己选设备，缺库只在**运行时**才炸，
# 而那时报出来的是 "libnet_init() failed" ——一个指不到根因的错误。在这里提前失败。
RUN ldd /usr/local/bin/punchingbag && ! ldd /usr/local/bin/punchingbag | grep -q "not found"

ENTRYPOINT ["/usr/local/bin/punchingbag"]
