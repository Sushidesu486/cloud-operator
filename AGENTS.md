# cloud-operator

- 管理持久 CloudVM；节点选择交给 Kubernetes 默认 scheduler。不得用 nodeName 绕过调度。
- root PVC 独立于 VM 生命周期，禁止自动格式化或在删除 VM 时级联删除磁盘。
- 核心测试围绕幂等启动、正常关机、旧 writer 未退出、磁盘归属；不重复测试代码实现细节。
- Go launcher 在独立的 cloud-launcher 仓库；只通过 docs/launcher-contract.md 的版本化协议对接。
- hack/lab.py 仅操作标记的 cloud-operator-lab 和测试 CRD；准备、测试、回收均显式执行。
- 当前 --development 产生 privileged Pod 和只读资产 hostPath；不称作生产安全实现。
- 不修改生产 Argo，不 push、发布镜像或创建远端仓库，除非用户明确要求。
