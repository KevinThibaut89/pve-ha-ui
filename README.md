# HA Manager

A simple web UI for **Proxmox VE High Availability** (PVE 8). It puts everything HA on one page:

- **Status at a glance:** quorum, the HA manager, every node (online, active, idle or **maintenance**) and every guest's HA state.
- **One switch per VM/CT** to add it to HA or remove it. You can also set its wanted state (started, stopped, disabled, ignored) and its group.
- **Move** an HA guest to another node, by live migration or relocation.
- **Node maintenance mode** with one button. HA drains the node before you reboot it, and guests move back afterwards.
- **HA groups** editor: pick nodes, set priorities with +/−, and toggle restricted and no-failback.
- **Bulk actions** for many guests at once, plus search, filters, group-by-node and a recent-activity list.
- **Settings panel** for the cluster address: set the node IPs, test them, and choose how certificates are checked.

It is pure Python standard library (no pip packages) plus one HTML page. You sign in with your normal Proxmox account and Proxmox's own permissions apply. No API token is stored anywhere.

## Install (one line, community-scripts style)

Open the **Shell** of any Proxmox node (in the web UI: *node → Shell*) and paste:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/KevinThibaut89/pve-ha-ui/main/lxc/create-haui-lxc.sh)"
```

A short wizard asks **Default** or **Advanced settings**, plus whether to enable the maintenance button. Then it builds everything and prints the address to open. Nothing needs to be cloned or installed on the node first.

**Default settings** create a small unprivileged Debian 12 container: 1 CPU, 512 MB RAM, 3 GB disk, DHCP on `vmbr0`, next free ID. **Advanced settings** let you pick the ID, resources, storage, bridge and a static IP.

The installer then:

- **Lists every cluster node's IP** in `/etc/haui/haui.toml`, so the UI keeps working when a node is down. You can change them later in **Settings**.
- **Verifies TLS to Proxmox** by trusting the cluster CA (`/etc/pve/pve-root-ca.pem`). If you use custom or ACME certificates, it pins them instead.
- **Serves the UI over HTTPS** with a self-signed certificate on **https://&lt;ct-ip&gt;:8443**.
- **Adds the maintenance key** (optional, on by default): see [How maintenance mode works](#how-maintenance-mode-works).
- **Can protect its own container with HA** (Advanced settings). That only works if the container's disk is on shared or replicated storage.

To skip the questions and accept every default:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/KevinThibaut89/pve-ha-ui/main/lxc/create-haui-lxc.sh)" _ --defaults
```

`--help` lists the other options. To install from a fork or a tag, set `HAUI_REPO=owner/name` and/or `HAUI_REF=v0.2.0`.

### Update

Open the container's console and type:

```bash
update
```

You can also run it from the node: `pct exec <ctid> -- update`. It downloads the latest version, restarts the service, and **rolls back automatically** if the new version doesn't start.

### From a checkout

```bash
git clone https://github.com/KevinThibaut89/pve-ha-ui.git && cd pve-ha-ui
bash lxc/create-haui-lxc.sh                 # install
bash lxc/create-haui-lxc.sh --update <ctid> # push this checkout into an existing container
```

### Manual install (any Debian/Ubuntu box with Python 3.11+)

```bash
sudo useradd --system --home /var/lib/haui haui
sudo mkdir -p /opt/haui /etc/haui /var/lib/haui
sudo cp -r haui systemd /opt/haui/
sudo cp config.example.toml /etc/haui/haui.toml        # then edit hosts / TLS
sudo cp systemd/haui.service /etc/systemd/system/
sudo chown -R haui:haui /etc/haui /var/lib/haui
sudo systemctl enable --now haui
```

## Setting the cluster address

Open **Settings** (the gear icon) to change which Proxmox nodes the UI talks to:

- **Nodes:** one or more IP addresses or hostnames (port 8006 unless you add `:port`). When one node is down, the next one is used. **Add all cluster nodes** fills in every node's IP from the cluster.
- **Test connection** checks each node without signing in. It reports whether the node is reachable, whether its certificate is trusted, and whether it really is a Proxmox API.
- **Certificate check:**
  - **Verify** trusts the cluster CA (if `ca_file` is set, which the installer does) and public CAs.
  - **Pin** trusts exactly the certificates you approved. When a test says "certificate not trusted", click **Trust this certificate** to pin it.
  - **Don't verify** is available but not recommended.
- **Save** is refused unless at least one node answers. Saving signs everyone else out, because Proxmox sign-ins belong to one cluster.

The choice is stored in `/var/lib/haui/settings.json` and overrides `hosts`, `verify_tls` and `fingerprints` from the config file. Delete that file to go back to the config file.

Only Proxmox **administrators** (`Sys.Modify` on `/`) can change these settings, because the target decides where everyone's password is sent. Other users see them read-only.

**First run:** if no node is configured at all, the UI opens on a "Connect to your Proxmox cluster" screen instead of the sign-in page. The installer always configures the nodes, so you only see this screen with a manual install.

If the configured nodes are unreachable, nobody can sign in to fix them. In that case, edit or delete `/var/lib/haui/settings.json` (or `/etc/haui/haui.toml`) and run `systemctl restart haui`.

## Who can do what

The UI uses the logged-in user's Proxmox permissions:

| Action | Proxmox privilege |
|---|---|
| See status | `Sys.Audit` on `/`, and `VM.Audit` to see guests |
| Change HA, move guests, maintenance | `Sys.Console` on `/` (Proxmox's own requirement for HA changes) |
| Change the cluster address in Settings | `Sys.Modify` on `/` |

Users without `Sys.Console` get a read-only view. Two-factor login with TOTP or recovery keys works. WebAuthn-only accounts must use the Proxmox web UI.

## How maintenance mode works

Proxmox 8 has no API call for node maintenance. It is only available as `ha-manager crm-command node-maintenance enable|disable <node>` on the command line. So the UI runs that command over SSH.

The installer creates `/etc/haui/id_ed25519` inside the container and appends one line to `/etc/pve/priv/authorized_keys`. That file is shared by every node, so all nodes accept the key. The line is locked to a single forced command:

```
restrict,command="case $SSH_ORIGINAL_COMMAND in *[!A-Za-z0-9.\ -]*) echo denied >&2; exit 1;; node-maintenance\ enable\ *|node-maintenance\ disable\ *) exec /usr/sbin/ha-manager crm-command $SSH_ORIGINAL_COMMAND;; *) echo denied >&2; exit 1;; esac" ssh-ed25519 AAAA… haui@haui
```

The key can only put a node into maintenance or take it out. It has no shell, no forwarding and no other commands. Node host keys are copied from `/etc/pve/priv/known_hosts`, so there is no trust-on-first-use step.

Before running the command, the server checks three things:
- the node name is a real cluster member
- the user has `Sys.Console`
- the cluster is quorate.

To turn the feature off, remove the `[maintenance]` section from the config and the key's line from `/etc/pve/priv/authorized_keys`.

> Maintenance moves **HA guests only**. Guests not in HA stay where they are, and the confirmation dialog lists them. Wait for "Moving now" to reach 0 before rebooting.

## HA states, briefly

| Wanted state | Meaning |
|---|---|
| **started** | Keep it running. If its node fails, HA restarts it elsewhere. |
| **stopped** | Keep it off, but still managed (it follows node failures and maintenance). |
| **disabled** | Stop it and leave it alone. This is also the way out of `error`. |
| **ignored** | HA keeps the config but takes its hands off completely. |

When you add a guest, it keeps its current power state: a running guest becomes `started` and a stopped one becomes `stopped`. HA never boots a guest you just enabled.

To clear an `error`, set the wanted state to **disabled**, fix the cause, then set it back to **started**.

## Development

```bash
python3 -m unittest discover -s tests -t .
```

The tests run the real server and client against `tests/fake_pve.py`. That file is a fake Proxmox VE 8 cluster served over HTTP, with tickets, CSRF tokens and a simulated HA stack. To work on the UI locally, start it with `python3 -m tests.fake_pve --port 8006`. Then point haui at `http://127.0.0.1:8006` on the setup screen. Any username and password work there.

Layout:

- `haui/pve.py`: PVE API client (ticket auth, CSRF, host failover, CA or fingerprint TLS, connection probe)
- `haui/settings.py`: cluster address and TLS settings (validation, saving)
- `haui/state.py`: merges eight API calls into the one `/api/state` document the UI renders
- `haui/server.py`: JSON API, sessions, input validation, static files, HTTPS
- `haui/maintenance.py`: the SSH maintenance call and the forced `authorized_keys` command
- `haui/static/`: the UI (plain HTML, CSS and JS, no build step)

## Troubleshooting

- **The service fails with `226/NAMESPACE`.** The container needs `nesting=1`, which the installer sets. Otherwise remove the `Protect*` lines from the unit.
- **"certificate fingerprint … is not pinned".** A node's custom certificate was renewed. Update `fingerprints` in `/etc/haui/haui.toml`, or switch to `ca_file`.
- **Maintenance fails with "Permission denied (publickey)".** Check that the key's line is in `/etc/pve/priv/authorized_keys` and that `/root/.ssh/authorized_keys` on each node is a symlink to that file. That is the Proxmox default.
