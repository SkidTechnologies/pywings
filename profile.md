<div align="center">

# SkidTechnologies

### *Next-Generation Python Ports of Pterodactyl Panel & Wings with Rootless Container Virtualization*

[![GitHub followers](https://img.shields.io/github/followers/SkidTechnologies?label=Follow%20Organization&style=for-the-badge&logo=github&color=21262d)](https://github.com/SkidTechnologies)
[![Python Version](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![Port of Pterodactyl](https://img.shields.io/badge/Port%20of-Pterodactyl%20v1.x-007acc?style=for-the-badge&logo=pterodactyl&logoColor=white)](https://pterodactyl.io)
[![Runtime](https://img.shields.io/badge/Container%20Runtime-Native%20OCI%20%2B%20PRoot-7c3aed?style=for-the-badge&logo=linux&logoColor=white)](#-key-innovations)
[![License: CC0 1.0](https://img.shields.io/badge/License-CC0_1.0-22c55e.svg?style=for-the-badge)](https://creativecommons.org/publicdomain/zero/1.0/)

<br/>

<p align="center">
  <b>SkidTechnologies</b> is an open-source engineering initiative creating a <b>high-performance, 100% rootless Python port of the Pterodactyl ecosystem</b>.
  <br/>
  We deliver 1:1 drop-in compatibility with official Pterodactyl eggs, API specifications, and control-plane protocols — completely independent of the Docker daemon and root privileges.
</p>

</div>

---

## 🌌 The Ecosystem

```text
                     ┌──────────────────────────────────────────────┐
                     │               SkidTechnologies               │
                     │       Next-Gen Pterodactyl Python Port       │
                     └──────────────────────┬───────────────────────┘
                                            │
                     ┌──────────────────────┴──────────────────────┐
                     ▼                                             ▼
           ┌────────────────────┐                        ┌────────────────────┐
           │      pypanel       │ ◄════════════════════► │      pywings       │
           │  Pterodactyl Panel │        REST API        │  Pterodactyl Wings │
           │    (Pure Python)   │     WebSockets (JWT)   │  (Rootless Daemon) │
           └────────────────────┘                        └─────────┬──────────┘
                     │                                             │
          ┌──────────┴──────────┐                        ┌─────────┴──────────┐
          ▼                     ▼                        ▼                    ▼
   [Web Dashboard]      [Node Orchestrator]       [Native OCI Engine]   [PRoot Sandbox]
   Reactive UI & API    Multi-Node Manager        Direct Registry Pull  Userspace Root (-0)
```

---

## 🚀 Flagship Projects

### 🦅 [pywings](https://github.com/SkidTechnologies/pywings)
> **Direct Python port of the Pterodactyl Wings daemon powered by an in-house rootless OCI engine & PRoot sandbox.**

- **Zero Root & Zero Docker Daemon:** Runs game servers without `sudo`, without `/var/run/docker.sock`, and without host kernel namespace privileges.
- **Native OCI Registry Engine:** Pulls image manifests, manifest lists, and content-addressable layer blobs directly from Docker Hub, GHCR, Quay, and private registries via HTTPS.
- **Userspace PRoot Sandbox:** Emulates root (`-0`) inside an isolated rootfs jail (`-r`) with host escape protection, sandboxed mounts, and universal egg support (Java, Node.js, Python, Rust, Go, Alpine, Ubuntu, Debian).
- **100% Pterodactyl API Compatibility:** All 39 Wings HTTP routes, real-time WebSocket console, instant zero-lock process tree kill, live CPU/RAM/Network telemetry, and full backup/transfer pipelines.
- **Embedded SFTP Subsystem:** Integrated SSH/SFTP server on port `2022` with real-time Panel credential validation and activity audit logging.
- **Automated Self-Updater:** Integrated background worker that detects upstream GitHub releases and applies zero-downtime in-place daemon restarts.

---

### 🖥️ [pypanel](https://github.com/SkidTechnologies/pypanel)
> **Pure Python re-implementation of the Pterodactyl Panel control plane.**

- **Eliminates PHP & Queue Headaches:** Replaces complex PHP-FPM, Composer, and Redis worker setups with a fast, modern, and maintainable Python backend.
- **1:1 Pterodactyl Data Model:** Compatible with standard Pterodactyl databases, nodes, egg configurations, sub-users, and permissions.
- **Bi-Directional Interoperability:** Controls both `pywings` nodes and official Go-based Wings nodes seamlessly.
- **Egg Ecosystem Support:** Parses standard Pterodactyl Egg exports, environment variables, startup commands, and configuration file matchers out of the box.
- **Reactive Terminal & Metrics:** Real-time console streaming, power control, millisecond server telemetry, and interactive web dashboard.

---

## 💡 Key Innovations

```text
  Traditional Wings (Go)                       pywings (Python)
┌─────────────────────────────────┐          ┌─────────────────────────────────┐
│ Host Root Privileges Required   │          │ 100% Unprivileged Userspace     │
│ Docker Daemon (/var/run/docker) │   VS     │ Embedded OCI Registry Client    │
│ Linux Kernel cgroups Privileges │          │ PRoot Sandbox Runtime (-0)      │
│ Fails on Shared Hosting / LXC   │          │ Deploys Anywhere Linux Runs     │
└─────────────────────────────────┘          └─────────────────────────────────┘
```

- 🔒 **Zero-Root Sandboxing:** Traditional Wings requires host root access and `/var/run/docker.sock`. `pywings` operates entirely in user-space, making it safe for shared infrastructure.
- ⚡ **Direct HTTPS Registry Pulling:** No local container tools required. `pywings` directly resolves multi-arch manifests and pulls layers from OCI registries into a content-addressable cache.
- 📱 **Runs Anywhere:** Deployable on unprivileged LXC/Proxmox, nested Docker containers, free-tier cloud instances, shared hosting, and Android Termux.
- 🎯 **Plug & Play Interoperability:** Mix and match freely — connect `pypanel` to official Go Wings, or connect `pywings` to an official Pterodactyl PHP Panel.
- 🛑 **Zero-Lock Instant Kill:** Nuclear process termination scanning `/proc` by process groups, working directory, and environment tags, preventing zombie port binders and hung servers.

---

## 🛠️ Technology Stack

<div align="center">

| Layer | Technology | Purpose |
| :--- | :--- | :--- |
| **Foundation** | Pterodactyl Protocol v1.x | 1:1 compatibility with Panel APIs, WebSocket events, and Egg schemas |
| **Language Core** | Python 3.10+ | Clean, asynchronous, and easily deployable codebase |
| **Container Engine** | PRoot + OCI Engine | Userspace execution with fake root (`-0`), rootfs jail, and no daemon |
| **Storage & Caching** | Content-Addressable SHA256 | Layer deduplication and fast local rootfs reconstruction |
| **Control Plane** | Flask / WSGI / Gunicorn | High-throughput REST API with JWT authorization |
| **Live Streaming** | WebSocket | Interactive ANSI terminal, command dispatch, and millisecond telemetry |
| **File Subsystem** | Paramiko & Native Filesystem | Port `2022` SFTP server with panel ACLs and safe path-traversal guards |

</div>

---

## 🌐 Deployable Environments

`pywings` and `pypanel` empower game hosting in environments where Docker was previously impossible:

- 🐧 **Unprivileged LXC / Proxmox Containers** (No nesting or `/dev/tun` requirements)
- 🐳 **Nested Docker Nodes** (Run Pterodactyl inside a container without Docker-in-Docker `dind`)
- ☁️ **Shared Hosting & Restricted VPS** (No `sudo` or `root` credentials needed)
- 📱 **Android / Termux Environments** (Edge devices and ARM64 single-board computers)
- 🏢 **Air-Gapped & Enterprise Linux** (Debian, Ubuntu, AlmaLinux, Rocky Linux, Alpine)

---

## 🤝 Community & Contributing

We welcome community contributions, bug reports, and optimizations!

- **Report an Issue:** Found a bug or compatibility issue? Open an issue on [pywings Issues](https://github.com/SkidTechnologies/pywings/issues) or [pypanel Issues](https://github.com/SkidTechnologies/pypanel/issues).
- **Submit a Pull Request:** Fork the repository, create your feature branch, and submit a PR.
- **Join Discussions:** Share your thoughts, custom egg configurations, and deployment setups.

---

<div align="center">

### 📄 License

Both `pywings` and `pypanel` are released under permissive open-source licenses. See repository license files for specifics.

<sub>Built with ❤️ by **SkidTechnologies**. Proudly inspired by and compatible with the open-source foundation of the **[Pterodactyl Project](https://pterodactyl.io)**.</sub>

</div>
