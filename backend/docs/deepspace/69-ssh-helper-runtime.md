# 69. SSH Helper Runtime, Remote Sandbox, and Stream Security

The SSH infrastructure (`ssh.py`, `ssh_fs.py`, `ssh_terminal.py`, `ssh_runtime.py`)
provides secure remote execution, filesystem access, and terminal sessions over SSH.

## Use case

- A workflow runs commands on a reviewed remote host.
- A skill reads/writes files on a remote server via SSH.
- A terminal session connects to a remote shell.

## Why it matters

- SSH is a privileged access path; stream security and host verification are critical.
- The helper runtime isolates remote execution from the main process.
- Remote sandbox applies the same containment as local sandbox.

## How it works

### SSH connection (`ssh.py`)

```python
class SSHConnection:
    def __init__(self, host: SSHHost):
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.RejectPolicy())
        self.client.connect(
            host.hostname,
            port=host.port,
            username=host.username,
            key_filename=host.key_path,
            password=host.password,
            timeout=host.timeout,
        )
        # Verify host key against known_hosts or host.fingerprint
```

**Host verification:**
- `RejectPolicy()` — fails on unknown host key.
- `known_hosts` file or `host.fingerprint` (SHA256) for TOFU.
- `host.verify_fingerprint` setting controls strictness.

### Stream security (`ssh/stream_security.py`)

All SSH channels (exec, shell, sftp) use a security layer:

```python
class SecureChannel:
    def __init__(self, channel: paramiko.Channel):
        self.channel = channel
        # 1. Disable PTY allocation for non-interactive channels
        # 2. Set channel window size limits
        # 3. Enable keepalive
        channel.setblocking(False)
        channel.settimeout(30)
```

**Security measures:**
| Measure | Purpose |
|---|---|
| No PTY for exec/sftp | Prevents terminal injection |
| Window size limits | Prevents memory exhaustion |
| Keepalive (30s) | Detects dead connections |
| Command allowlist (optional) | Restricts executable commands |

### SSH helper runtime (`ssh_runtime.py`)

The helper runtime is a separate process that runs on the remote host (via SSH)
and provides a local-like API for file/terminal/subprocess operations.

**Deployment:**
```bash
# On remote host
pip install deepspace-ssh-helper
deepspace-ssh-helper --port 0 --token <secret>
```

**Protocol:**
- JSON-RPC over SSH stdout/stdin (stdin=commands, stdout=responses)
- Each request: `{ "id": 1, "method": "fs.read", "params": {...} }`
- Each response: `{ "id": 1, "result": {...} }` or `{ "id": 1, "error": {...} }`

**Methods exposed:**
| Method | Local equivalent |
|---|---|
| `fs.read` | `fs_read` |
| `fs.write` | `fs_write` |
| `fs.list` | `fs_glob` / `fs_list` |
| `fs.stat` | `fs_stat` |
| `terminal.open` | `terminal_open` |
| `terminal.read` | `terminal_read` |
| `terminal.write` | `terminal_write` |
| `subprocess.exec` | `bash` / `sandbox_execute` |

**Security:**
- Helper binds to localhost only (SSH tunnel forwards local port).
- Token authentication (shared secret via SSH command).
- Commands executed under restricted user (no sudo).

### Remote sandbox (`ssh/sandbox-ssh.py`)

The remote sandbox applies the same containment as local sandbox but via
the helper runtime:

```python
class RemoteSandbox:
    def execute(self, spec: SandboxSpec) -> SandboxResult:
        # 1. Forward spec to helper via JSON-RPC
        # 2. Helper runs local sandbox (linux-namespace/windows-job)
        # 3. Result streamed back over SSH
```

**Containment parity:**
| Feature | Local | Remote (via helper) |
|---|---|---|
| PID namespace | ✅ | ✅ (on remote) |
| Network isolation | ✅ | ✅ (on remote) |
| Mount isolation | ✅ | ✅ (on remote) |
| Timeout | ✅ | ✅ |
| Process tree kill | ✅ | ✅ |

### SSH filesystem (`ssh_fs.py`)

Tools: `ssh_fs_read`, `ssh_fs_write`, `ssh_fs_list`, `ssh_fs_delete`,
`ssh_fs_stat`, `ssh_fs_read_range`, `ssh_fs_read_text`, `ssh_fs_write_bytes`,
`ssh_fs_move`, `ssh_fs_edit`, `ssh_fs_patch`.

All operations go through the helper runtime's `fs.*` methods.  Path
containment enforced on remote (same as local `fs.py`).

### SSH terminal (`ssh_terminal.py`)

Tools: `ssh_terminal_open`, `ssh_terminal_read`, `ssh_terminal_write`,
`ssh_terminal_signal`, `ssh_terminal_close`.

Opens a PTY on the remote host via helper runtime.  Output sanitized
identically to local terminal (see Chapter 64).

### Stream security details

**Channel types:**
| Channel | PTY | Use case | Security |
|---|---|---|---|
| `exec` | No | `ssh_run`, `bash` | No PTY injection |
| `shell` | Yes | `ssh_terminal_open` | Sanitized output |
| `sftp` | No | `ssh_fs_*` | Binary protocol |

**Encryption:** SSHv2 with AES-GCM / ChaCha20-Poly1305 (paramiko default).
**Key exchange:** curve25519-sha256, ecdh-sha2-nistp256.
**Host key:** ed25519, rsa-sha2-256, rsa-sha2-512.

### Settings

| Setting | Type | Default | Effect |
|---|---|---|---|
| `deepspace_ssh_runtime_enabled` | `bool` | `false` | Enable SSH tools. |
| `deepspace_ssh_helper_path` | `str` | `""` | Path to helper binary (for deployment). |
| `deepspace_ssh_helper_token` | `str` | `""` | Shared secret for helper auth. |
| `deepspace_ssh_known_hosts` | `str` | `"~/.ssh/known_hosts"` | Host key verification file. |
| `deepspace_ssh_verify_fingerprint` | `bool` | `true` | Require fingerprint match. |
| `deepspace_ssh_command_allowlist` | `str` | `""` | CSV of allowed remote commands. |

## What to watch for

- **Helper runtime must match DeepSpace version** — protocol changes break compatibility.
- **SSH multiplexing** — reuse single connection for multiple channels (paramiko does this automatically).
- **Timeouts** — helper requests have 30s default; increase for long-running commands.
- **Token rotation** — helper token should be rotated periodically; use secret manager.
- **Host key rotation** — maintain `known_hosts` or use `deepspace_ssh_verify_fingerprint` with pinned SHA256.

## Related chapters

- [Terminals and the Remote Runtime](14-terminals-and-remote-runtime.md) — SSH terminal and fs tools.
- [Terminal Session Lifecycle](64-terminal-session-lifecycle.md) — sanitize rules.
- [The Sandbox](13-the-sandbox.md) — local sandbox parity.
