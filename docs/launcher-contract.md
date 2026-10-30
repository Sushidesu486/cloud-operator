# Operator / launcher protocol v1 and v2

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
- `graceSeconds`: v1 allows 1–15 seconds; v2 allows 1–90 seconds. Include at least
  five extra seconds in the Pod termination grace period for the shutdown handshake.
- Protocol v2 adds `networkNAT: true`: configure IPv4 masquerade inside the Pod netns
  relay guest DNS at 192.0.2.1 to the Pod DNS server,
  and proxy TCP port 2222 to guest port 22. The Operator sets a 60-second guest
  shutdown budget and a 75-second Pod termination budget. The Pod uses its own
  network namespace; Cilium policies control external ingress and egress.
- The development runtime additionally expects `busybox` beside `firecracker`
  for TAP address setup. The image importer is a separate explicit action.

The Operator reserves guest RAM plus runtime overhead, leaves nodeName unset, uses
restartPolicy Never, and injects the root device via CSI `volumeDevices`. VMM and
helper processes stay within the Pod's resource cgroup. Current permissions are
development-only privileged, not part of a production security contract.

`/work/ready` records VMM startup. With v2 `networkNAT`, the `ready` command also
connects to guest port 22 and checks the SSH protocol banner. Operator reports
`SSHReady` only after this probe succeeds.
SIGTERM/SIGINT requests shutdown over Firecracker vsock (guest CID 3, port 1024).
The image must provide cloud-guest-helper or an implementation of the same narrow
protocol. The only accepted request is `{"version":1,"action":"shutdown"}` followed
by a newline. The helper accepts host CID 2 only, syncs/remounts the test root read-only,
replies `accepted\n`, and exits the guest via i8042 reset. It offers no command execution.
On a systemd image, run the helper with `--systemd`: it acknowledges the request
and calls `systemctl --no-block reboot`. systemd stops services and unmounts the
root before the final reset exits Firecracker. The image maps the poweroff service
to the same final reset action.
A guest that ignores shutdown is killed after the grace period. The Pod termination message is JSON:

```json
{"version":1,"reason":"GuestExited","exitCode":0}
```

Reasons are `GuestExited` (0), `RequestedStop` (0), `ForcedStop` (2),
`LauncherFailed` (1), or `UnsupportedHost` (1), with optional `message`.
GuestExited records a successful VMM process exit. Both guest reboot and poweroff
leave the VM stopped in this release; request a new bootID to start it again.
Platform restart is stop followed by a new bootID. Backup uses the stopped VM.

The local `/work/control.sock` is mode 0600. `POST /console` accepts at most 4096
bytes of raw serial input. It is guest console access and must never be exposed
without upstream owner authentication. There is no network control endpoint.

Launcher readiness, stdout guest logs, and termination messages belong to one Pod
run. Operator stores the stable VM identity, bootID, Pod UID and observed state.
The completed bootID must not be treated as a fresh boot when a terminal Pod is
removed. Volume ownership/fencing and user authentication are not launcher duties.
