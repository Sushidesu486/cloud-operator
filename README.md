# cloud-operator

仓库：https://github.com/Sushidesu486/cloud-operator

管理运行在 Kubernetes 上的持久 Firecracker VM。一个 `CloudVM` 保存长期身份和磁盘引用；一个 launcher Pod 表示一次运行。复用默认 scheduler、PVC/Ceph CSI，launcher 在独立仓库 `cloud-launcher`。

当前是 **v0 开发实验**：只支持管理员预置的 root PVC、单一允许节点、共享 CPU 小规格和显式启动。没有自动故障迁移、镜像目录、租户门户授权或生产权限配置。

## 构建

Go 1.25+；依赖与校验锁定在 go.mod/go.sum，controller-runtime 与 Kubernetes 1.35 API 对齐。

协作开发时将两个仓库放在同级目录，分别构建；没有跨仓库 Go 模块依赖。

```sh
git clone git@github.com:Sushidesu486/cloud-operator.git
git clone git@github.com:Sushidesu486/cloud-launcher.git
make -C cloud-launcher test build
make -C cloud-operator test build
```

在本仓库内执行：

```sh
make test
make build
make generate # 修改 API 后重新生成 DeepCopy 和 CRD
```

## 运行模型

`spec.powerState: Running` 和递增的 `spec.bootID` 构成显式启动请求。同一个 bootID 重试只创建一个 Pod。正常 guest 退出后保持停止；再次启动应增加 bootID。改变 CPU/内存不会自动重启，新的规格在下一次 bootID 生效。

`Stopped` 会请求删除当前 Pod，由 SIGTERM 进入 launcher 的正常关机路径；20 秒 Pod 宽限期内 launcher 最多等待 12 秒 guest 关机。无法正常关机时 launcher 报告 ForcedStop。当前停止 Pod 随删除消失，Operator 的 Stopped 只表达运行对象已回收，不承诺文件系统已干净卸载；集成验证另外检查 launcher 关机结果。

Pod 使用 `restartPolicy: Never`。缺失的已观测 Pod 标记 Unknown，不自动重启；旧 Pod 仍在 Terminating 时不启动下一台。v0 不提供跨节点 fencing；不能通过强删 Pod/VolumeAttachment 恢复失联机器。

root PVC 必须为同 namespace 的 raw block 卷，带 `cloud.zjusct.io/owner`。Operator 在 PVC 上以 resourceVersion 条件更新 `cloud.zjusct.io/claimed-by`，防止另一 CloudVM 使用；删除 VM 后保留卷和预约，需管理员确认旧 writer 已停止再显式释放。标签由受信任管理面设置，不是完整的多租户授权系统；不要给普通用户任意 Pod/PVC/CloudVM 写权限。

状态包含 Pending / Running / Stopping / Stopped / Failed / Blocked / Unknown。Running 只表示 VMM 启动，不代表 SSH 或 guest 应用已就绪。所有 VMM/辅助进程计入 launcher Pod 的 CPU/内存，内存 request/limit 是 guest 大小加 128 MiB 实验开销预算。

## m600 集成实验

实验脚本会创建一个**集群级 CRD**，但只允许操作带实验标签的同名对象；Operator 使用两小时有效、仅限 `cloud-operator-lab` 的 ServiceAccount token，没有生产 namespace 或 secrets 权限。实验不由 Argo 接管。

前置条件：本机可 SSH m600；m600 有管理员 kubectl 用于 bootstrap；现有 `ceph-block`；已验证 Firecracker 1.17.0 / Linux 6.1.188 / BusyBox 资产位于脚本 SOURCE 指定目录；两个仓库已构建 Linux/amd64 二进制。源码可分开放置，通过 `--launcher` 指定另一个仓库。

```sh
python3 hack/lab.py prepare --launcher ../cloud-launcher
python3 hack/lab.py check
python3 hack/lab.py cleanup
```

prepare 给一个新建 256 MiB 测试 PVC 制作 BusyBox 根盘；初始化 Job 拒绝覆盖已有签名。check 启动临时 Operator，验证默认调度、启动/停止、根盘写入后再次启动、guest 主动关机不重启、CRD 校验、删除 VM 保盘，然后停止 Operator。结果在 `evidence/live-results.json`，日志和凭据不入 Git。cleanup 会删除这个**可丢弃实验**的 namespace/PVC 和实验 CRD，不删除其他实验或生产资源；远端临时资产另行按准确目录回收。

当前运行器需要显式 `--development`，使用 privileged Pod 和只读 hostPath 资产，**不适合开放给普通用户**。未实现的下一步：KVM/TUN 最小权限、普通发行版镜像与 SSH、CNI 出口/服务入口、优雅停止结果的持久记录、租户准入与配额。

协议见 [launcher-contract](docs/launcher-contract.md)。
