# 后端验收版

在 m600 的 `cloud-operator-lab` 中验收持久 Ubuntu VM。门户沿用已有视觉模板，后续接入这些生命周期操作。

## 直接验收

在 `cloud-operator` 仓库执行，本机需已配置 `ssh m600`：

```sh
python3 hack/acceptance.py status
python3 hack/acceptance.py ssh alice
python3 hack/acceptance.py ssh bob
```

两台实例均为 **1 vCPU / 1 GiB RAM / 8 GiB Ceph 系统盘**，登录用户 `cloud`，可用 `sudo`。每台 VM 使用独立 SSH 密钥及 host key。私钥保存在本机 `.local/`，已排除 Git；SSH 通过 m600 转发到稳定的 Kubernetes Service，端到端认证由 guest sshd 完成。

在 Alice 中写入文件，退出后执行停机、启动，再登录查看：

```sh
python3 hack/acceptance.py ssh alice --command 'echo hello | sudo tee /root/my-file'
python3 hack/acceptance.py stop alice
python3 hack/acceptance.py start alice
# 一步重启：python3 hack/acceptance.py restart alice
python3 hack/acceptance.py ssh alice --command 'sudo cat /root/my-file'
```

`stop` 会检查 launcher 正常关机结果；`start` 提交新的 bootID，并等待 guest SSH 就绪。guest 内 `systemctl poweroff` 会正常关闭服务并停机；本版 guest 内 `reboot` 也会停止本次 VMM，重新启动通过上述 `start` 命令执行。

## 新建环境与镜像

两个源码仓库放在同级目录，各自 `make test build`。m600 已预置 Firecracker 1.17.0、jailer、Linux 6.1.188 和 BusyBox 到实验资产目录。

```sh
python3 hack/acceptance.py prepare
python3 hack/acceptance.py images
python3 hack/acceptance.py create alice --os ubuntu-24.04
python3 hack/acceptance.py create bob --os ubuntu-24.04
```

当前开放 Ubuntu 24.04 amd64，目录位于 `images/catalog.json`。发行版原始镜像固定为 ZJU Mirror 的 20261017 构建并校验 SHA-256，下载缓存位于 m600 的实验资产目录。创建 Job 为新 PVC 制作完整根盘，配置 systemd、SSH、公钥、独立身份和 ZJU 软件源。launcher 启动时直接挂载已有盘。

镜像目录中的内核字段记录兼容组合；当前实验 Operator 使用管理员预置的单一 kernel。新增 OS 需先验证组合，再开放目录项。v0.1 缓存发行版归档，每个新盘独立初始化；后续可接入不可变模板与 CSI 克隆。

## 网络

数据路径：guest `192.0.2.2` → Pod 内 TAP/NAT → Cilium → 现有出口。每台 VM 使用独立 Pod netns。DNS 经 `192.0.2.1` 转发到 Pod 配置的 DNS，以适配 Cilium 的 Service socket 转换。SSH 由 Service 转发到 Pod TCP 2222，再接 guest 22。

验收策略允许 DNS、ZJU Mirror 和公网 HTTP/HTTPS；普通私网、节点管理网、Kubernetes API 与跨用户 VM 流量默认拒绝。guest 网卡关闭 IPv6 自动配置，出口规则为 IPv4。集群管理员通过 m600 跳板验收，队员门户授权与独立跳板账号在后续接入。

## 备份恢复

```sh
python3 hack/acceptance.py stop alice
python3 hack/acceptance.py backup alice
python3 hack/acceptance.py restore
python3 hack/acceptance.py ssh alice-restored
python3 hack/acceptance.py stop alice-restored
python3 hack/acceptance.py start alice
```

停机备份写到 m600 本地 `/var/tmp/zjusct-cloud-operator-lab-20261030/backups/`，包含压缩 raw 磁盘、SHA-256、大小及 VM 配置。恢复校验摘要和目标容量，写入全新的 `alice-restored-root` PVC，再启动独立 VM。已有恢复实例保留，重复恢复时需先由管理员明确处置旧实例并选择新目标。正式备份保留策略和异地副本后续配置。

完整检查命令 `python3 hack/acceptance.py check` 会验证双用户 SSH、sudo、DNS/HTTPS、网络隔离、密钥隔离、正常开关机、数据保留及备份恢复，记录到 `evidence/acceptance-results.json`，最后保留 Alice/Bob 运行、恢复实例停止。该检查会创建固定名称的恢复实例，适合首次验收；日常复验使用上面的独立操作命令。

## 部署范围

Operator 常驻为 Deployment，ServiceAccount 权限限定在实验 namespace。CPU/内存和 PVC 有 namespace 配额；launcher 使用当前开发用 privileged Pod 和只读资产 hostPath。代码与实验资源独立于生产 Argo。验收后继续收敛设备权限、接入门户身份和正式存储策略。
