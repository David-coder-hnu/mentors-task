# harness 侧运行环境 —— `src/` 在这里跑（T2）。
#
# 为什么单独一个镜像、而不是在宿主上跑：探测层用 `AF_INET6/SOCK_RAW/
# IPPROTO_ICMPV6`，需要 CAP_NET_RAW **且必须与靶场处在同一个二层**
# （T1 的结论：上游 `pcap_open_live` 是 promisc=0，回包必须 L2 直达靶机）。
# Windows 宿主做不到这两件事，所以 harness 与 Punching Bag 一样住在 compose 网络里。
#
# 源码**不进镜像**——由 compose 把仓库 bind mount 到 /w。这样改代码不用重建镜像，
# 而 CI 里可以用同一个镜像配上不同的 mount（T17）。
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# iproute2 是**运行期真的需要**的，不是顺手装的：探测前缀不在网桥上，
# 得靠 `ip -6 route add … via 靶场` 把 L2 下一跳指到靶机，否则包发不出去
# （原因见 `docker/compose.yml` 顶部第 2 条）。
# tcpdump 也是**运行期真的需要**的，理由比 iproute2 更重（T2(b)）：
# 当靶场账本说「我把 20000 个回包都发出去了」（三层全清）而我们的套接字只收到
# 4755 个时，中间少掉的那 15000 个**在任何应用层日志里都不留痕**。剩下的可能
# 有三条，且症状完全相同（都表现为「这个地址没回包」）：
#     ① 靶场根本没发出去（`libnet_errors` 记账覆盖不到的那部分）
#     ② 发了，但在 bridge/veth 上丢了
#     ③ 到了本容器的 eth0，被内核在我们读到之前丢掉了（套接字接收队列溢出）
# 只有在这段路上抓包能把三条分开。**靠推理分不开**——这正是 v7 记的那类错：
# 错不在文档里，在文档与靶场之间。
RUN apt-get update \
 && apt-get install --no-install-recommends -y iproute2 tcpdump \
 && rm -rf /var/lib/apt/lists/*

# 只装测试运行器。**运行期的 `src/` 不依赖任何第三方包**——这是刻意的：
# 探测层手写原始套接字就是为了不把不确定性引进「可回放」这条硬约束
# （见 `src/adapter/iface.py` 的 docstring）。
RUN pip install --no-cache-dir pytest==9.1.1

WORKDIR /w
