# Operator / launcher protocol v1

This document is copied into both independent repositories. They do not import one
another's Go modules. Incompatible changes require a new protocol version.

Operator starts `cloud-launcher run` with `CLOUD_LAUNCHER_CONFIG` containing one JSON
object. Unknown fields, trailing JSON, invalid versions and unsafe relative paths
are rejected before launch. Only the trusted Operator supplies paths and binaries;
tenant-facing APIs must not pass arbitrary launcher configuration through.

```json
{"version":1,"id":"vm","vcpus":1,"memoryMiB":128,"firecracker":"/assets/firecracker","jailer":"/assets/jailer","kernel":"/assets/vmlinux","rootDevice":"/dev/vm-root","workDir":"/work","graceSeconds":12}
```

- `id`: jail-local identifier, `[a-z0-9][a-z0-9-]{0,31}`; not the user's name.
- `vcpus`: 1–4; `memoryMiB`: 128–4096 for this development implementation.
- `firecracker`, `jailer`, `kernel`: absolute paths to administrator-pinned assets.
- `rootDevice`: an existing initialized writable block device. No format operation.
- `workDir`: per-run ephemeral directory, not a persistent volume or host root.
- `graceSeconds`: 1–15; must fit inside the Pod termination grace period.
- The development runtime additionally expects `busybox` beside `firecracker`
  for TAP address setup. The image importer is a separate explicit action.

The Operator reserves guest RAM plus runtime overhead, leaves nodeName unset, uses
restartPolicy Never, and injects the root device via CSI `volumeDevices`. VMM and
helper processes stay within the Pod's resource cgroup. Current permissions are
development-only privileged, not part of a production security contract.

`/work/ready` means the VMM started, not that SSH or guest services are ready.
SIGTERM/SIGINT requests shutdown over Firecracker vsock (guest CID 3, port 1024).
The image must provide cloud-guest-helper or an implementation of the same narrow
protocol. The only accepted request is `{"version":1,"action":"shutdown"}` followed
by a newline. The helper accepts host CID 2 only, syncs/remounts the test root read-only,
replies `accepted\n`, and exits the guest via i8042 reset. It offers no command execution.
This BusyBox shutdown path is not a general distribution's service manager.
A guest that ignores shutdown is killed after the grace period. The Pod termination message is JSON:

```json
{"version":1,"reason":"GuestExited","exitCode":0}
```

Reasons are `GuestExited` (0), `RequestedStop` (0), `ForcedStop` (2),
`LauncherFailed` (1), or `UnsupportedHost` (1), with optional `message`.
GuestExited describes a clean VMM process exit, not a proof of filesystem or
application consistency. v0 does not distinguish guest reboot from poweroff;
both may require a new explicit bootID.

The local `/work/control.sock` is mode 0600. `POST /console` accepts at most 4096
bytes of raw serial input. It is guest console access and must never be exposed
without upstream owner authentication. There is no network control endpoint.

Launcher readiness, stdout guest logs, and termination messages belong to one Pod
run. Operator stores the stable VM identity, bootID, Pod UID and observed state.
The completed bootID must not be treated as a fresh boot when a terminal Pod is
removed. Volume ownership/fencing and user authentication are not launcher duties.
