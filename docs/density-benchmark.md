# 单 CPU MicroVM 密度实验

实验入口：`python3 hack/density.py prepare`、`run`、`cleanup`。数据写入 `evidence/density-results.json`。独立 namespace 为 `cloud-density-lab`，验收环境中的 Alice/Bob 保留。

## 实测结果（2026-10-30）

10 台共享一个逻辑 CPU 已通过轻负载测试，20 台也通过。建议试点先按 10 台/逻辑 CPU 配置；轻负载池可以进一步采用 20 台，并按实际总请求率控制准入。

| 指标 | 1 台 | 10 台 | 20 台 |
|---|---:|---:|---:|
| 空闲 CPU（核） | 0.013 | 0.104 | 0.234 |
| 空闲内存当前用量，含缓存（GiB） | 0.63 | 6.28 | 12.57 |
| 空闲匿名内存（GiB） | 0.31 | 3.05 | 6.16 |
| 网页总计 100 RPS，P95（ms） | 3.0 | 8.6 | 11.0 |
| 网页总计 500 RPS，P95（ms） | 4.2 | 235.6 | 503.5 |
| Agent 总计 40 RPS，P95（ms） | 257.9 | 267.4 | 270.2 |
| Agent 总计 100 RPS，P95（ms） | 257.7 | 274.2 | 281.6 |

表中的请求率是整组 VM 的总量。单台网页 100 RPS 使用 40 秒复测，其余主矩阵每档 20 秒；上述各档请求全部成功。全部容器的采样 CPU affinity 均为 63，所有正式样本中 CFS 节流和 OOM kill 计数均为 0。

Agent 模拟 API 自带 250 ms 等待。10 台与 20 台在总计 100 RPS 时分别使用约 0.50、0.61 核；对应 P99 为 468、505 ms。网页总计 100 RPS 时，10 台与 20 台的 P99 为 216、221 ms。

高压边界：10 台的 Agent 总计 300 RPS 虽全部完成，P95 已到 3.20 秒。20 台在提高发压进程文件描述符上限至 65536 后复测，6000 个请求中 2991 个触发 5 秒超时（49.85%）；成功响应的 P95 为 4.88 秒，被测容器合计使用约 0.94 核。此档已经过载。

首次单台网页 100 RPS 出现长尾，P95 为 276 ms；停止其余 VM 后做了 40 秒复测，P95 为 2.95 ms、P99 为 22.1 ms。两轮原始数据均保留，容量规划采用实际业务的延迟目标，并预留节点后台任务的算力。

## 配置建议

- 将 guest vCPU 与宿主 CPU request 分开：轻量规格可先采用 1 vCPU、100m CPU request，允许使用空闲 CPU；20 台/核的轻负载池再评估 50m request。CPU limit 可保留 1，配合池总量和准入控制。
- 当前 Ubuntu 规格从 512 MiB guest 内存起步；按 launcher 和网络组件计入总预算。实测每台约 310 MiB 匿名内存、640 MiB 含缓存用量，运行实际 Agent 工具后按应用峰值调整。
- 每 VM 平均请求率需要与实例数一起计量：10 台、总计 100 RPS 对应每台 10 RPS；20 台、总计 100 RPS 对应每台 5 RPS。
- Ceph 同 namespace 的 raw-block PVC 克隆已在本次 20 台实验中验证，可用于后续镜像模板分发。

原始数据：[density-results.json](../evidence/density-results.json)。

## 方法

- m600 的 CPU 63 是被测计算预算：所有 VMM、launcher、HTTP 转发进程和 readiness probe 都绑定到这个逻辑 CPU；1、10、20 台依次增加。每台 guest 看到 1 个 vCPU。
- 使用已有 Ubuntu 24.04 根盘，512 MiB guest 内存；每台独立 8 GiB Ceph RBD 克隆。保留原系统服务。
- launcher 的 request/limit 为 10m/1 CPU、640 MiB 内存；转发进程为 5m/1 CPU、32/64 MiB 内存。CPU 绑定提供这一组 VM 的共享算力边界，各进程可使用空闲时间。
- HTTP 客户端和模拟模型 API 在 m601，避免占用被测 CPU。网络经过真实 Pod/Cilium、Pod 内 TCP 转发和 guest 网络栈。
- 网页场景：Python asyncio 服务，约 4 KiB JSON 响应，HTTP/1.1 短连接；总请求率 100、500 RPS。
- Agent 场景：guest 调用另一个节点上的模拟 API，等待 250 ms，读取约 16 KiB JSON，再消耗 1 ms 进程 CPU 做本地处理；总请求率 40、100、300 RPS。
- 各负载持续 20 秒，使用按时发起请求的开放式负载，单请求超时 5 秒；记录客户端排程延迟，便于识别发压端瓶颈。
- CPU、内存、节流与 OOM 从对应 Pod 容器的宿主 cgroup 读取；CPU 63 与 SMT 同胞 CPU 31 仍由节点系统共享，后台任务也会参与竞争。容量分析使用 cgroup CPU 时间。
- 内存同时报告 cgroup 当前用量和匿名页：前者包含可回收文件缓存，后者主要体现 guest/VMM 的驻留内存。

准备阶段发现转发容器 100m 的独立限额会给单台基线增加节流；正式对比改为 1 CPU 上限，所有组统一由 CPU 63 绑定共享总算力。

这是远程模型 API 加轻量编排的 Agent 工作负载。实际服务容量还取决于请求率、每请求本地 CPU 工作量、外部 API 延迟和每个实例的内存需求。
