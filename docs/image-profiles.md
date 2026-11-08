# Image profiles

What each image produced by this pipeline is for, and what goes in it.

All images are built from the Ubuntu 24.04 (Noble) cloud image with Packer
(`qemu` builder, `accelerator = "tcg"`) and provisioned with Ansible. Profiles
are **layered**: every image starts from `base`, then adds one or more profile
roles. That keeps the shared hardening in one place and lets a combined image
(e.g. automation + troubleshooting) be built by listing two roles.

```
noble cloud image
  └── base                (hardened, minimal, cloud-init, operator CLI)
        ├── netauto       (network automation workstation / runner)
        ├── nettools      (network troubleshooting & diagnostics)
        ├── netlab        (virtual network labs: containerlab, FRR)   [optional]
        └── netmon        (telemetry / flow / syslog collector)      [optional]
```

| Profile    | Who uses it                          | Shape                         | Size target |
|------------|--------------------------------------|-------------------------------|-------------|
| `base`     | everything builds on it              | headless server               | ≤ 1.5 GB    |
| `netauto`  | NetDevOps engineers, CI runners      | headless, SSH + VS Code Remote| ≤ 3 GB      |
| `nettools` | NOC / field engineers, incident work | headless, jump box / probe    | ≤ 2.5 GB    |
| `netlab`   | training, pre-change testing         | needs nested containers, RAM  | ≤ 4 GB      |
| `netmon`   | always-on collector                  | long-running services         | ≤ 3 GB      |

---

## 1. `base` — hardened foundation

**Theme:** a boring, secure, predictable Linux host. No network-specific tools.

**Contents**

- **Boot/provisioning:** cloud-init (NoCloud + common datasources), qemu-guest-agent,
  growpart, serial console enabled (`console=ttyS0`) so the image can be managed
  headless and under TCG.
- **Access:** OpenSSH with key-only auth, root login disabled, an `ops` admin user
  created by cloud-init, sudo with logging.
- **Hardening:** CIS Ubuntu 24.04 Level 1 Server baseline, applied by Ansible and
  **checked with `oscap`** (SSG profile `xccdf_org.ssgproject.content_profile_cis_level1_server`).
  auditd, AppArmor enforcing, nftables default-deny inbound except SSH,
  unattended-upgrades for security, chrony time sync, journald persistent with size cap.
- **Operator CLI:** `vim`, `tmux`, `git`, `curl`, `wget`, `jq`, `yq`, `rsync`,
  `less`, `htop`, `tree`, `unzip`, `bash-completion`, `ca-certificates`.
- **Trimmed:** snapd, cloud-guest-utils extras, motd-news, popularity-contest removed.
- **Image metadata:** `/etc/os-pipeline/build-info.json` (profile list, git SHA,
  build date, package manifest hash) so any running VM can say what it is.

---

## 2. `netauto` — network automation

**Theme:** a ready-to-go *automation workstation and CI runner*. You SSH in (or attach
VS Code Remote-SSH), clone your automation repo, and every library, collection and
linter is already there, pinned and version-consistent.

### Python toolchain (`/opt/netauto/venv`, Python 3.12, pinned via `requirements.lock`)

| Area                     | Packages |
|--------------------------|----------|
| Device connectivity      | `netmiko`, `scrapli` (+ `scrapli-netconf`, `scrapli-community`), `paramiko`, `ncclient` |
| Multi-vendor abstraction | `napalm` (+ community drivers as needed) |
| Orchestration frameworks | `nornir`, `nornir-netmiko`, `nornir-napalm`, `nornir-scrapli`, `nornir-utils`, `nornir-jinja2` |
| Testing / validation     | `pyats[library]` + Genie parsers, `pybatfish`, `pytest`, `pytest-html` |
| Parsing                  | `textfsm` + `ntc-templates`, `ttp`, `ciscoconfparse2`, `genie` |
| Model-driven / telemetry | `pygnmi`, `ncclient`, `pyang`, `xmltodict` |
| Source of truth          | `pynetbox`, `pynautobot` |
| Data / templating        | `jinja2`, `netaddr`, `pyyaml`, `ruamel.yaml`, `jsonschema`, `rich`, `typer` |
| API clients              | `requests`, `httpx` |

### Ansible

- `ansible-core` (pinned) + `ansible-lint`, `molecule`.
- Collections (pinned in `requirements.yml`): `ansible.netcommon`, `ansible.utils`,
  `cisco.ios`, `cisco.iosxr`, `cisco.nxos`, `arista.eos`, `junipernetworks.junos`,
  `vyos.vyos`, `netbox.netbox`, `community.general`.
- Sensible `/etc/ansible/ansible.cfg`: `network_cli` persistent connections, longer
  command timeouts, fact caching to `~/.cache/ansible`.

### Standalone tools

- **gNMI / telemetry:** `gnmic`, `gnoic`.
- **Model browsing:** `pyang`; YANG models for the vendors above under `/opt/yang`.
- **IaC:** `terraform` (or OpenTofu) for controller/cloud-network providers.
- **Repo hygiene:** `pre-commit`, `yamllint`, `ansible-lint`, `ruff`, `shellcheck`.
- **Config transfer:** `tftp-hpa` client, `lftp`; an *opt-in* `tftpd-hpa`/SCP drop
  directory (`/srv/configs`) disabled by default.
- **Optional, off by default:** Batfish server container (needs docker/podman; ~1.5 GB).

### Configuration

- `~/.ssh/config` drop-in that enables **legacy KEX/ciphers only for a
  `Host legacy-*` pattern** (old IOS/ASA gear), keeping modern defaults everywhere else.
- `/etc/profile.d/netauto.sh` activates the venv and sets `ANSIBLE_*` / `NTC_TEMPLATES_DIR`.
- Starter skeleton at `/etc/skel/netauto-starter/` (inventory, group_vars, a sample
  Nornir and Ansible playbook) so new users see a working layout.
- Secrets are **never baked in**: credentials come from cloud-init, environment, or
  ansible-vault at run time.

### Smoke test (what CI checks)

`ansible-galaxy collection list` shows every pinned collection;
`python -c "import netmiko, napalm, nornir, scrapli, pybatfish, genie"` succeeds;
`gnmic version`; a Nornir run against a `netlab` FRR node if one is available.

---

## 3. `nettools` — network troubleshooting

**Theme:** the *toolbag you'd want plugged into a broken network*. Drop it on a jump
host, a hypervisor next to the problem, or a laptop VM, and every layer from cable to
TLS can be inspected without installing anything.

### By layer

| Layer / task                | Tools |
|-----------------------------|-------|
| L1/L2 link & neighbors      | `ethtool`, `lldpd` (`lldpcli`), `bridge-utils`, `vlan`, `iproute2` (`ip`, `bridge`, `ss`), `arping`, `arp-scan`, `macchanger` |
| Serial / console            | `picocom`, `minicom`, `screen`, `setserial`, USB-serial drivers (for out-of-band console cables) |
| L3 reachability & path      | `mtr-tiny`, `traceroute`, `tcptraceroute`, `fping`, `iputils-ping`, `ndisc6` (`rdisc6`, `tracert6`), `paris-traceroute` |
| Addressing / subnetting     | `ipcalc`, `sipcalc`, `ipv6calc` |
| DHCP / DNS                  | `isc-dhcp-client`, `dhcpdump`, `dhcping`, `bind9-dnsutils` (`dig`, `nslookup`), `ldnsutils` (`drill`), `knot-dnsutils` (`kdig`, DoT/DoH), `whois` |
| Packet capture & analysis   | `tcpdump`, `tshark`, `termshark`, `ngrep`, `wireshark-common` (`editcap`, `mergecap`), `tcpreplay`, `python3-scapy` |
| Throughput & latency        | `iperf3`, `iperf` (v2, for old peers), `netperf`, `sockperf`, `nuttcp` |
| Ports & services            | `nmap` (+ `ncat`), `netcat-openbsd`, `socat`, `hping3`, `masscan` (opt-in) |
| Live traffic per interface  | `iftop`, `nload`, `bmon`, `nethogs`, `vnstat`, `sysstat` (`sar -n DEV`) |
| Firewall / NAT / state      | `nftables`, `iptables`, `conntrack`, `ipset` |
| HTTP / TLS                  | `curl`, `httpie`, `openssl`, `sslscan`, `testssl.sh`, `mitmproxy` (opt-in) |
| SNMP                        | `snmp` (`snmpwalk`, `snmpget`), `snmp-mibs-downloader` + vendor MIBs in `/usr/share/snmp/mibs` |
| Routing protocols (passive) | `frr` installed but **disabled** — can be enabled to peer BGP/OSPF read-only and see what a neighbor advertises |
| Wireless (if hardware)      | `iw`, `wavemon`, `aircrack-ng` (opt-in) |

### Helper scripts (`/usr/local/bin`, shipped in this repo)

- `netcheck` — one-shot health report: interfaces & link state, default route, DNS
  resolution, gateway/8.8.8.8/1.1.1.1 ping, MTU path test (`ping -M do` sweep), NTP
  offset. Output as text or `--json`.
- `capture` — wraps `tcpdump` with a ring buffer (`-C/-W`), sane snaplen, and writes to
  `/var/captures/` with timestamped names; prints the `tshark` command to read it back.
- `portcheck host port[,port]` — TCP/UDP reachability via `nc`/`hping3` with clear output.
- `mtu-probe host` — finds path MTU (IPv4 and IPv6).

### Configuration

- `tcpdump`/`tshark` usable by the `netops` group without sudo (`wireshark` group +
  capabilities on `dumpcap`).
- Capture storage on its own path with a size cap and a systemd tmpfiles cleanup timer.
- Default-off: anything that listens (`tftpd`, `iperf3 -s`, FRR, `mitmproxy`) is
  installed but disabled; enable per incident.
- Offensive/noisy tools (`masscan`, `aircrack-ng`, `mitmproxy`, `macchanger`) live in an
  **opt-in variable** (`nettools_include_active_tools: false`) so the default image is
  safe to drop into production networks.

### Smoke test

Each tool on the list resolves on `$PATH`; `netcheck --json` returns valid JSON inside
QEMU user networking; `tcpdump -D` lists interfaces as the `netops` user.

---

## 4. `netlab` — virtual network lab (optional)

**Theme:** stand up a multi-vendor topology on one VM to test automation before it
touches production. Pairs naturally with `netauto`.

- `containerlab`, `docker-ce` (or podman), `frr` container images pre-pulled.
- Pre-pulled open images: FRR, Nokia SR Linux, Arista cEOS **placeholder** (license —
  user must import), plain Alpine/Ubuntu host nodes.
- Example topologies in `/opt/netlab/topologies/` (2-router BGP, spine-leaf EVPN with
  FRR/SR Linux) wired to the `netauto` starter inventory.
- Needs ≥ 8 GB RAM and 4 vCPU; **slow under TCG** — fine for build/smoke tests here,
  intended to be run on a KVM host.

## 5. `netmon` — collector (optional)

**Theme:** an always-on box that *receives* network data.

- Telemetry: `gnmic` in collector mode, Telegraf (SNMP + gNMI inputs).
- Metrics & dashboards: Prometheus + `snmp_exporter` + `blackbox_exporter`, Grafana.
- Flows: `nfdump`/`nfcapd` (NetFlow/IPFIX) or `pmacct`.
- Logs: `rsyslog`/`syslog-ng` listening on 514/udp+tcp with per-host files and rotation.
- All services as systemd units with config in `/etc/netmon/`, data on a separate disk.

---

## Repository layout (proposed)

```
packer/
  noble.pkr.hcl            # qemu builder, accelerator = "tcg", var "profiles"
ansible/
  site.yml                 # applies base + selected profile roles
  roles/
    base/  netauto/  nettools/  netlab/  netmon/
  requirements.yml         # pinned collections
profiles/
  netauto/requirements.in  # -> requirements.lock (pip-compile)
  nettools/scripts/        # netcheck, capture, portcheck, mtu-probe
tests/
  vm-smoke/                # boot -> cloud-init -> SSH -> per-profile checks
  oscap/                   # tailoring file + report output
cloud-init/
  user-data.example  meta-data.example
docs/
  image-profiles.md        # this file
```

## Verification per image

1. `packer validate` + `ansible-lint` + `yamllint` (fast, every commit).
2. Packer build under TCG (background, logs to `build/logs/`).
3. Boot the result with QEMU TCG + cloud-init seed → wait for SSH on `:2222`.
4. Run the profile's smoke test over SSH.
5. `oscap xccdf eval` against the image → HTML report saved under `build/reports/`.

## Open decisions

- **Which first?** Recommendation: `base` → `nettools` (all apt packages, quick win) →
  `netauto` (pinning the Python/Ansible stack takes longer).
- **Combined image?** Many teams want `netauto + nettools` in one box; the layering
  supports it, but shipping it as a separate named image is a choice.
- **Vendor content** (cEOS, IOS XRd, vendor MIBs/YANG under restrictive licenses) can't be
  redistributed — supply import hooks rather than baking them in.
- **Container variants:** `nettools` and `netauto` also make good OCI images
  (`docker run --net=host`), cheaper to test than VMs; worth producing both from the
  same package lists.
