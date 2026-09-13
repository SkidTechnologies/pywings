# pywings

[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](LICENSE)
[![Python Version](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![Pterodactyl Compatibility](https://img.shields.io/badge/Pterodactyl-v1.x_Compatible-059669.svg)](https://pterodactyl.io/)
[![Runtime](https://img.shields.io/badge/Runtime-Rootless_PRoot_%2B_OCI-9333ea.svg)](#features)

A lightweight, rootless Python implementation of the **Pterodactyl Wings** daemon powered by an integrated **OCI container runtime and PRoot sandbox**.

`pywings` provides a complete drop-in replacement for standard Wings, allowing you to run Pterodactyl game servers without root privileges, without Linux user namespaces, and without the Docker daemon. It pulls OCI/Docker images directly from container registries (Docker Hub, GHCR, Quay, etc.), builds local root filesystems, and runs them sandboxed inside userspace using PRoot (`-0` root emulation).

## Features

- **Full Pterodactyl Wings API Compatibility**: Supports all `/api/*` endpoints used by the Pterodactyl Panel.
- **Custom OCI Registry Engine**: Pulls image manifests, manifest lists, and content-addressable layer blobs directly from OCI/Docker HTTP V2 registries with multi-arch platform resolution.
- **Safe Rootfs Assembly**: Sequentially unpacks OCI layers with AUFS/OCI whiteout deletion (`.wh.<file>` and `.wh..wh..opq`), path traversal protection, and content caching.
- **Userspace PRoot Sandbox Runtime**: Runs containers using PRoot with root emulation (`-0`), rootfs jail (`-r`), sandboxed bind mounts, and process group lifecycle management without requiring Linux user namespaces or Docker.
- **Host Escape & Reverse Shell Protection**: Jails processes inside their container rootfs and only mounts permitted directories (`/home/container`, `/mnt/server`, `/mnt/install`), preventing escape to the host filesystem.
- **Live Real-Time WebSocket Console**: Live bidirectional WebSocket console streaming stdout/stderr, stats, power actions, and install logs.
- **Full Egg Configuration Parser**: Automatically updates server files (`server.properties`, JSON, YAML, INI, text configs) and resolves environment variables.
- **Complete Installation Lifecycle**: Runs egg install scripts, binds `/mnt/server` and `/mnt/install`, and notifies Panel (`/api/remote/servers/{uuid}/install`) to mark servers as installed.
- **Power Lifecycle & Done-Line Matching**: Implements `starting` -> `running` -> `stopping` -> `offline` states with egg startup done matchers and graceful shutdown.
- **Crash Detection & Auto-Restart**: Detects unexpected process crashes and automatically recovers servers with loop protection.
- **Integrated SFTP Server**: Built-in SFTP server on port `2022` authenticating directly against the Panel (`/api/remote/sftp`).
- **Backups & Node Transfers**: Full support for `.tar.gz` backups, restore, and inter-node server transfers.

## Requirements

- Python 3.10+
- `proot` binary installed (either in `PATH`, at `/home/container/.tools/proot`, or specified via `PROOT_PATH` environment variable)

## Installation

```bash
git clone https://github.com/SkidTechnologies/pywings.git
cd pywings

pip install -r requirements.txt
```

## Configuration

Copy `config.example.yml` to `config.yml` or paste the configuration generated from the Pterodactyl Panel:

```bash
cp config.example.yml config.yml
```

Edit `config.yml` with your node credentials, Panel URL, and tokens.

## Running

### Development Mode
```bash
python app.py
```

### Production Mode (Gunicorn)
```bash
gunicorn -w 1 -k gthread --threads 10 -b 0.0.0.0:8080 wsgi:app
```

## License

This project is licensed under the **GNU Affero General Public License v3.0** (AGPL-3.0). See the [LICENSE](LICENSE) file for the full license text.

