#!/usr/bin/env bash
# create-haui-lxc.sh — build a Proxmox LXC container that runs HA Manager.
#
# Run this ON A PROXMOX NODE, as root. One line, community-scripts style:
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/KevinThibaut89/pve-ha-ui/main/lxc/create-haui-lxc.sh)"
#
# or from a checkout of this repo:
#
#   bash lxc/create-haui-lxc.sh                 # interactive wizard (whiptail)
#   bash lxc/create-haui-lxc.sh --defaults      # non-interactive, all defaults
#   bash lxc/create-haui-lxc.sh --update 123    # push this checkout into CT 123
#
# Without a checkout around it, the script downloads the app from GitHub
# (override with HAUI_REPO=owner/name and HAUI_REF=branch-or-tag).
#
# What it does:
#   1. Downloads the Debian 12 standard CT template (if not already cached).
#   2. Creates an unprivileged container and copies the app to /opt/haui.
#   3. Writes /etc/haui/haui.toml listing every cluster node's IP, and trusts
#      the cluster CA (or pins node certificates if you use custom ones).
#   4. Creates a self-signed HTTPS certificate for the web UI.
#   5. Optionally (default: yes) creates an SSH key for the maintenance-mode
#      button and adds it to /etc/pve/priv/authorized_keys, locked to the one
#      command `ha-manager crm-command node-maintenance enable|disable <node>`.
#   6. Enables the `haui` systemd service.

set -euo pipefail

# ------------------------------------------------------------ pretty output
if [[ -t 1 ]]; then
    C_GRN=$'\e[1;32m'; C_YLW=$'\e[1;33m'; C_RED=$'\e[1;31m'; C_CYN=$'\e[36m'; C_OFF=$'\e[0m'
else
    C_GRN=""; C_YLW=""; C_RED=""; C_CYN=""; C_OFF=""
fi
LOG="$(mktemp /tmp/haui-lxc.XXXXXX.log)"

log()  { echo -e "${C_GRN}==>${C_OFF} $*"; }
warn() { echo -e "${C_YLW}WARNING:${C_OFF} $*" >&2; }
die()  { echo -e "${C_RED}ERROR:${C_OFF} $*" >&2; exit 1; }
abort(){ echo -e "\n${C_YLW}Aborted by user.${C_OFF}"; exit 1; }

# run <description> <command...> — run a step quietly with a spinner and a
# ✔/✖ result line; full output goes to $LOG and is shown on failure.
FRAMES=('⠋' '⠙' '⠹' '⠸' '⠼' '⠴' '⠦' '⠧' '⠇' '⠏')
run() {
    local desc="$1"; shift
    echo "----- $desc -----" >>"$LOG"
    if [[ -t 1 ]]; then
        ("$@") >>"$LOG" 2>&1 &
        local pid=$! i=0
        while kill -0 "$pid" 2>/dev/null; do
            printf '\r %s%s%s %s' "$C_YLW" "${FRAMES[i++ % 10]}" "$C_OFF" "$desc"
            sleep 0.1
        done
        if wait "$pid"; then
            printf '\r %s✔%s %s \n' "$C_GRN" "$C_OFF" "$desc"
        else
            printf '\r %s✖%s %s \n' "$C_RED" "$C_OFF" "$desc"
            echo; tail -n 25 "$LOG" >&2
            die "step failed — full log: $LOG"
        fi
    else
        echo "==> $desc"
        "$@" >>"$LOG" 2>&1 || { tail -n 25 "$LOG" >&2; die "step failed — full log: $LOG"; }
    fi
}

header() {
    cat <<EOF
${C_CYN}  _   _    _      __  __
 | | | |  / \\    |  \\/  | __ _ _ __   __ _  __ _  ___ _ __
 | |_| | / _ \\   | |\\/| |/ _\` | '_ \\ / _\` |/ _\` |/ _ \\ '__|
 |  _  |/ ___ \\  | |  | | (_| | | | | (_| | (_| |  __/ |
 |_| |_/_/   \\_\\ |_|  |_|\\__,_|_| |_|\\__,_|\\__, |\\___|_|
                                           |___/${C_OFF}
 Proxmox VE High Availability UI — LXC installer
EOF
}

# ---------------------------------------------------------------- defaults
CTID=""                    # empty -> next free ID from the cluster
HOSTNAME="haui"
STORAGE=""                 # empty -> auto-detect / wizard picker
TEMPLATE_STORAGE="local"
BRIDGE="vmbr0"
IP="dhcp"
GW=""
DISK_GB="3"
MEMORY_MB="512"
CORES="1"
START_ON_BOOT=1
MAINTENANCE=1              # create + authorize the maintenance SSH key
PROTECT_HA=0               # add the CT itself to HA (needs shared storage)
UPDATE_CTID=""

usage() {
    header
    cat <<EOF

Usage: bash lxc/create-haui-lxc.sh [options]

Run with NO options on a terminal to get the interactive wizard.

Options (any option switches to non-interactive mode):
  --defaults            accept every default (no questions)
  --ctid N              container ID            (default: next free ID)
  --hostname NAME       CT hostname             (default: ${HOSTNAME})
  --storage NAME        rootfs storage          (default: auto-detect)
  --template-storage N  template storage        (default: ${TEMPLATE_STORAGE})
  --bridge NAME         network bridge          (default: ${BRIDGE})
  --ip CIDR|dhcp        IP config               (default: ${IP})
  --gw IP               gateway (with static --ip)
  --disk GB             rootfs size             (default: ${DISK_GB})
  --memory MB           RAM                     (default: ${MEMORY_MB})
  --cores N             CPU cores               (default: ${CORES})
  --no-maintenance      skip the SSH key (hides the maintenance buttons)
  --ha                  protect the HA Manager CT itself with HA
  --no-onboot           do not start the CT at host boot
  --update CTID         copy this checkout into an existing CT and restart
  -h, --help            show this help
EOF
    exit "${1:-0}"
}

# -------------------------------------------------------------------- args
INTERACTIVE=1
[[ $# -gt 0 ]] && INTERACTIVE=0
{ [[ -t 0 && -t 1 ]] && command -v whiptail >/dev/null; } || INTERACTIVE=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --defaults)         shift ;;
        --ctid)             CTID="$2"; shift 2 ;;
        --hostname)         HOSTNAME="$2"; shift 2 ;;
        --storage)          STORAGE="$2"; shift 2 ;;
        --template-storage) TEMPLATE_STORAGE="$2"; shift 2 ;;
        --bridge)           BRIDGE="$2"; shift 2 ;;
        --ip)               IP="$2"; shift 2 ;;
        --gw)               GW="$2"; shift 2 ;;
        --disk)             DISK_GB="$2"; shift 2 ;;
        --memory)           MEMORY_MB="$2"; shift 2 ;;
        --cores)            CORES="$2"; shift 2 ;;
        --no-maintenance)   MAINTENANCE=0; shift ;;
        --ha)               PROTECT_HA=1; shift ;;
        --no-onboot)        START_ON_BOOT=0; shift ;;
        --update)           UPDATE_CTID="$2"; shift 2 ;;
        -h|--help)          usage 0 ;;
        *) echo "Unknown option: $1" >&2; usage 1 ;;
    esac
done

# ------------------------------------------------------------------ checks
[[ $EUID -eq 0 ]] || die "run as root on a Proxmox node"
command -v pct   >/dev/null || die "'pct' not found — run this on a Proxmox VE node"
command -v pveam >/dev/null || die "'pveam' not found — run this on a Proxmox VE node"
command -v pvesh >/dev/null || die "'pvesh' not found — run this on a Proxmox VE node"

HAUI_REPO="${HAUI_REPO:-KevinThibaut89/pve-ha-ui}"
HAUI_REF="${HAUI_REF:-main}"

# The forced command for the maintenance key in /etc/pve/priv/authorized_keys:
# only "node-maintenance enable|disable <node>" gets through. Must stay
# identical to haui.maintenance.AUTHORIZED_KEYS_COMMAND (a test checks this).
FORCED_CMD='case $SSH_ORIGINAL_COMMAND in *[!A-Za-z0-9.\ -]*) echo denied >&2; exit 1;; node-maintenance\ enable\ *|node-maintenance\ disable\ *) exec /usr/sbin/ha-manager crm-command $SSH_ORIGINAL_COMMAND;; *) echo denied >&2; exit 1;; esac'

# Use the checkout this script lives in, or download the app (curl | bash case).
SCRIPT_PATH="${BASH_SOURCE[0]:-}"
REPO_ROOT=""
if [[ -n "$SCRIPT_PATH" && -f "$SCRIPT_PATH" ]]; then
    REPO_ROOT="$(cd "$(dirname "$SCRIPT_PATH")/.." && pwd)"
    [[ -f "$REPO_ROOT/pyproject.toml" && -d "$REPO_ROOT/haui" ]] || REPO_ROOT=""
fi
if [[ -z "$REPO_ROOT" ]]; then
    command -v curl >/dev/null || die "'curl' is needed to download HA Manager"
    REPO_ROOT="$(mktemp -d /tmp/haui-src.XXXXXX)"
    trap 'rm -rf "$REPO_ROOT"' EXIT
    download() {
        curl -fsSL "https://github.com/${HAUI_REPO}/archive/${HAUI_REF}.tar.gz" \
            | tar -xz -C "$REPO_ROOT" --strip-components=1
    }
    run "Downloading HA Manager (${HAUI_REPO}@${HAUI_REF})" download
    [[ -d "$REPO_ROOT/haui" ]] || die "the download does not look like HA Manager — check HAUI_REPO/HAUI_REF"
fi

push_source() {
    local ct="$1" tarball
    tarball="$(mktemp /tmp/haui-src.XXXXXX.tar.gz)"
    tar -C "$REPO_ROOT" -czf "$tarball" \
        --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' \
        --exclude='*.egg-info' --exclude='.claude' --exclude='tests' \
        haui systemd lxc config.example.toml pyproject.toml README.md
    pct push "$ct" "$tarball" /tmp/haui-src.tar.gz
    rm -f "$tarball"
    pct exec "$ct" -- bash -c "rm -rf /opt/haui.new && mkdir -p /opt/haui.new \
        && tar -C /opt/haui.new -xzf /tmp/haui-src.tar.gz && rm /tmp/haui-src.tar.gz \
        && rm -rf /opt/haui && mv /opt/haui.new /opt/haui"
}

# ------------------------------------------------------------------ update
if [[ -n "$UPDATE_CTID" ]]; then
    header
    pct status "$UPDATE_CTID" &>/dev/null || die "CT $UPDATE_CTID does not exist"
    run "Copying HA Manager source to CT ${UPDATE_CTID}" push_source "$UPDATE_CTID"
    run "Restarting the haui service" pct exec "$UPDATE_CTID" -- bash -c \
        "cp /opt/haui/systemd/haui.service /etc/systemd/system/haui.service \
         && install -m 755 /opt/haui/lxc/haui-update /usr/local/bin/haui-update \
         && { [[ -e /usr/bin/update ]] || ln -s /usr/local/bin/haui-update /usr/bin/update; } \
         && systemctl daemon-reload && systemctl restart haui"
    log "Updated. $(pct exec "$UPDATE_CTID" -- systemctl is-active haui 2>/dev/null || true)"
    exit 0
fi

NEXTID="$(pvesh get /cluster/nextid 2>/dev/null)"

mapfile -t ROOTFS_STORAGES < <(pvesm status -content rootdir 2>/dev/null \
                               | awk 'NR>1 && $3=="active" {print $1, $2}')
[[ ${#ROOTFS_STORAGES[@]} -gt 0 ]] \
    || die "no active storage supports container disks (rootdir) — check 'pvesm status'"

# Cluster nodes as "name ip" lines (a standalone node has no cluster entry).
# Parsed with core Perl, which every Proxmox node has.
mapfile -t NODES < <(pvesh get /cluster/status --output-format json 2>/dev/null | perl -MJSON::PP -e '
    my $items = JSON::PP::decode_json(join("", <STDIN>));
    for my $i (@$items) { print "$i->{name} $i->{ip}\n" if $i->{type} eq "node" && $i->{ip} }
')
[[ ${#NODES[@]} -gt 0 ]] || die "could not read the cluster node list (pvesh get /cluster/status)"

# ------------------------------------------------------------------ wizard
WT="whiptail --backtitle HA-Manager-LXC"

wizard() {
    header
    $WT --title "HA Manager LXC" --yesno \
"This creates a small LXC container running HA Manager,
a simple web UI for Proxmox High Availability.

Cluster nodes found: ${#NODES[@]}

Proceed?" 12 60 || abort

    local mode
    mode="$($WT --title "Settings" --menu "Choose an option:" 12 60 2 \
        "1" "Default Settings  (CT ${NEXTID}, 1 CPU, 512 MB, DHCP)" \
        "2" "Advanced Settings" \
        3>&1 1>&2 2>&3)" || abort

    if [[ "$mode" == "2" ]]; then
        while :; do
            CTID="$($WT --title "Container ID" --inputbox "Container ID:" 10 60 "$NEXTID" \
                3>&1 1>&2 2>&3)" || abort
            [[ "$CTID" =~ ^[0-9]+$ ]] || continue
            pct status "$CTID" &>/dev/null || break
            $WT --title "Container ID" --msgbox "CT $CTID already exists — pick another ID." 8 60
        done
        HOSTNAME="$($WT --title "Hostname" --inputbox "Container hostname:" 10 60 "$HOSTNAME" \
            3>&1 1>&2 2>&3)" || abort
        DISK_GB="$($WT --title "Disk" --inputbox "Root disk size (GB):" 10 60 "$DISK_GB" \
            3>&1 1>&2 2>&3)" || abort
        CORES="$($WT --title "CPU" --inputbox "CPU cores:" 10 60 "$CORES" 3>&1 1>&2 2>&3)" || abort
        MEMORY_MB="$($WT --title "Memory" --inputbox "RAM (MB):" 10 60 "$MEMORY_MB" \
            3>&1 1>&2 2>&3)" || abort

        if [[ ${#ROOTFS_STORAGES[@]} -eq 1 ]]; then
            STORAGE="${ROOTFS_STORAGES[0]%% *}"
        else
            local items=() s
            for s in "${ROOTFS_STORAGES[@]}"; do items+=("${s%% *}" "${s#* }"); done
            STORAGE="$($WT --title "Storage" --menu \
                "Storage for the container disk (pick shared storage if you want HA to protect this CT):" \
                18 60 "${#ROOTFS_STORAGES[@]}" "${items[@]}" 3>&1 1>&2 2>&3)" || abort
        fi

        BRIDGE="$($WT --title "Network" --inputbox "Bridge:" 10 60 "$BRIDGE" 3>&1 1>&2 2>&3)" || abort
        local ipmode
        ipmode="$($WT --title "IP address" --menu "IP configuration:" 12 60 2 \
            "dhcp" "Automatic (DHCP)" "static" "Static IP" 3>&1 1>&2 2>&3)" || abort
        if [[ "$ipmode" == "static" ]]; then
            IP="$($WT --title "Static IP" --inputbox "IP address in CIDR form (e.g. 192.168.2.40/24):" \
                10 60 "" 3>&1 1>&2 2>&3)" || abort
            GW="$($WT --title "Gateway" --inputbox "Gateway IP:" 10 60 "" 3>&1 1>&2 2>&3)" || abort
        fi

        if $WT --title "Autostart" --yesno "Start the container when this node boots?" 8 60; then
            START_ON_BOOT=1
        else
            START_ON_BOOT=0
        fi
        if $WT --title "Protect with HA" --defaultno --yesno \
"Add this container itself to HA, so the UI survives a node failure?

Only works if its disk is on SHARED storage (Ceph, NFS, ...)
or replicated ZFS." 11 60; then
            PROTECT_HA=1
        fi
    fi

    if $WT --title "Maintenance button" --yesno \
"Enable the node maintenance button?

Proxmox 8 only offers maintenance mode on the command line, so the
UI needs an SSH key. It is added to /etc/pve/priv/authorized_keys
and locked to ONE command:

  ha-manager crm-command node-maintenance enable|disable <node>

Nothing else can be run with it." 16 70; then
        MAINTENANCE=1
    else
        MAINTENANCE=0
    fi
    confirm_summary
}

confirm_summary() {
    [[ -z "$CTID" ]] && CTID="$NEXTID"
    [[ -z "$STORAGE" ]] && STORAGE="${ROOTFS_STORAGES[0]%% *}"
    $WT --title "Ready to build" --yesno \
"Create this container?

  CT ID        : ${CTID}
  Hostname     : ${HOSTNAME}
  Resources    : ${CORES} CPU / ${MEMORY_MB} MB RAM / ${DISK_GB} GB disk
  Storage      : ${STORAGE}
  Network      : ${BRIDGE}, ${IP}$( [[ -n "$GW" ]] && echo " gw ${GW}" )
  Maintenance  : $( [[ $MAINTENANCE -eq 1 ]] && echo "yes (restricted SSH key)" || echo "no" )
  HA-protected : $( [[ $PROTECT_HA -eq 1 ]] && echo "yes" || echo "no" )
  On boot      : $( [[ $START_ON_BOOT -eq 1 ]] && echo "yes" || echo "no" )" 19 66 || abort
}

if [[ $INTERACTIVE -eq 1 ]]; then
    wizard
else
    header
fi

# ------------------------------------------------------- resolve leftovers
[[ "$IP" != "dhcp" && -z "$GW" ]] && die "--ip $IP is static; a gateway is required (--gw)"

if [[ -z "$STORAGE" ]]; then
    STORAGE="${ROOTFS_STORAGES[0]%% *}"
    log "Auto-selected rootfs storage: $STORAGE"
else
    printf '%s\n' "${ROOTFS_STORAGES[@]}" | awk '{print $1}' | grep -qx "$STORAGE" \
        || die "storage '$STORAGE' does not exist or cannot hold container disks"
fi

[[ -z "$CTID" ]] && CTID="$NEXTID"
pct status "$CTID" &>/dev/null && die "CT $CTID already exists"

# ---------------------------------------------------------------- template
TEMPLATE="$(pveam list "$TEMPLATE_STORAGE" 2>/dev/null \
            | awk '{print $1}' | grep -o 'debian-12-standard.*' | sort -V | tail -n1 || true)"
if [[ -z "$TEMPLATE" ]]; then
    run "Updating CT template catalogue" pveam update
    TEMPLATE="$(pveam available --section system 2>/dev/null \
                | awk '{print $2}' | grep '^debian-12-standard' | sort -V | tail -n1)"
    [[ -n "$TEMPLATE" ]] || die "no debian-12-standard template offered by pveam"
    run "Downloading template ${TEMPLATE}" pveam download "$TEMPLATE_STORAGE" "$TEMPLATE"
fi
TEMPLATE_REF="${TEMPLATE_STORAGE}:vztmpl/${TEMPLATE##*/}"

# ------------------------------------------------------------------ create
NET0="name=eth0,bridge=${BRIDGE},firewall=0"
if [[ "$IP" == "dhcp" ]]; then NET0+=",ip=dhcp"; else NET0+=",ip=${IP},gw=${GW}"; fi

run "Creating CT ${CTID} (${HOSTNAME}, unprivileged)" \
    pct create "$CTID" "$TEMPLATE_REF" \
        --hostname "$HOSTNAME" \
        --unprivileged 1 \
        --features nesting=1 \
        --ostype debian \
        --rootfs "${STORAGE}:${DISK_GB}" \
        --memory "$MEMORY_MB" \
        --swap 0 \
        --cores "$CORES" \
        --net0 "$NET0" \
        --onboot "$START_ON_BOOT" \
        --tags "haui;ha" \
        --description "HA Manager — web UI for Proxmox VE High Availability (https://<ip>:8443)"

run "Starting CT ${CTID}" pct start "$CTID"

wait_for_boot() {
    local state _
    for _ in $(seq 1 30); do
        state="$(pct exec "$CTID" -- systemctl is-system-running 2>/dev/null || true)"
        [[ "$state" == "running" || "$state" == "degraded" ]] && return 0
        sleep 2
    done
    return 0
}
wait_for_net() {
    local _
    for _ in $(seq 1 30); do
        pct exec "$CTID" -- sh -c 'hostname -I 2>/dev/null | grep -q "[0-9]"' && return 0
        sleep 2
    done
    warn "container has no IP yet — continuing, but apt may fail"
    return 0
}
run "Waiting for the container to boot" wait_for_boot
run "Waiting for network" wait_for_net

run "Installing Debian packages" \
    pct exec "$CTID" -- bash -c "
        set -euo pipefail
        export DEBIAN_FRONTEND=noninteractive
        apt-get -qq update
        apt-get -qq install -y --no-install-recommends python3 openssh-client openssl ca-certificates curl
    "

run "Copying HA Manager source to /opt/haui" push_source "$CTID"

# --------------------------------------------------------------- config
build_config() {
    local cfg ip_list tls_lines custom=0 n name ip
    ip_list="$(printf '"%s", ' $(printf '%s\n' "${NODES[@]}" | awk '{print $2}'))"
    ip_list="[${ip_list%, }]"

    for n in "${NODES[@]}"; do
        name="${n%% *}"
        [[ -f "/etc/pve/nodes/${name}/pveproxy-ssl.pem" ]] && custom=1
    done

    if [[ $custom -eq 0 ]]; then
        pct push "$CTID" /etc/pve/pve-root-ca.pem /etc/haui/pve-root-ca.pem
        tls_lines=$'verify_tls = true\nca_file = "/etc/haui/pve-root-ca.pem"'
    else
        # Custom/ACME certificates: pin each node's current certificate.
        local fps="" cert
        for n in "${NODES[@]}"; do
            name="${n%% *}"
            cert="/etc/pve/nodes/${name}/pveproxy-ssl.pem"
            [[ -f "$cert" ]] || cert="/etc/pve/nodes/${name}/pve-ssl.pem"
            fps+="\"$(openssl x509 -noout -fingerprint -sha256 -in "$cert" | cut -d= -f2)\", "
        done
        tls_lines="verify_tls = true"$'\n'"# Pinned node certificates (custom certs found). Re-run with --update after renewing them."$'\n'"fingerprints = [${fps%, }]"
    fi

    cfg="$(mktemp /tmp/haui-cfg.XXXXXX)"
    cat >"$cfg" <<EOF
# Written by create-haui-lxc.sh — see /opt/haui/config.example.toml for all options.
hosts = ${ip_list}
${tls_lines}

listen = "0.0.0.0:8443"
tls_cert = "/etc/haui/tls.crt"
tls_key = "/etc/haui/tls.key"
EOF
    if [[ $MAINTENANCE -eq 1 ]]; then
        cat >>"$cfg" <<'EOF'

[maintenance]
ssh_key = "/etc/haui/id_ed25519"
ssh_user = "root"
ssh_known_hosts = "/var/lib/haui/known_hosts"
EOF
    fi
    pct push "$CTID" "$cfg" /etc/haui/haui.toml
    rm -f "$cfg"
}

setup_ct() {
    pct exec "$CTID" -- bash -c "
        set -euo pipefail
        useradd --system --home /var/lib/haui --shell /usr/sbin/nologin haui 2>/dev/null || true
        mkdir -p /etc/haui /var/lib/haui
    "
    build_config
    pct exec "$CTID" -- bash -c "
        set -euo pipefail
        cd /etc/haui
        if [[ ! -f tls.key ]]; then
            ip=\$(hostname -I | awk '{print \$1}')
            openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
                -keyout tls.key -out tls.crt -subj \"/CN=\$(hostname)\" \
                -addext \"subjectAltName=DNS:\$(hostname),IP:\${ip:-127.0.0.1}\" 2>/dev/null
        fi
        if [[ $MAINTENANCE -eq 1 && ! -f id_ed25519 ]]; then
            ssh-keygen -q -t ed25519 -N '' -C \"haui@\$(hostname)\" -f id_ed25519
        fi
        chown -R haui:haui /etc/haui /var/lib/haui
        chmod 600 /etc/haui/tls.key /etc/haui/id_ed25519 2>/dev/null || true
        cp /opt/haui/systemd/haui.service /etc/systemd/system/haui.service
        install -m 755 /opt/haui/lxc/haui-update /usr/local/bin/haui-update
        # community-scripts convention: type 'update' in the container console
        [[ -e /usr/bin/update ]] || ln -s /usr/local/bin/haui-update /usr/bin/update
        mkdir -p /etc/haui && printf 'HAUI_REPO=%s\nHAUI_REF=%s\n' '${HAUI_REPO}' '${HAUI_REF}' > /etc/haui/source
        systemctl daemon-reload
        systemctl enable --now haui
    "
}
run "Configuring HA Manager" setup_ct

# ------------------------------------------------------- maintenance key
authorize_key() {
    local pub line keys=/etc/pve/priv/authorized_keys
    pub="$(pct exec "$CTID" -- cat /etc/haui/id_ed25519.pub)"
    line="restrict,command=\"${FORCED_CMD}\" ${pub}"
    if ! grep -qF "${pub}" "$keys" 2>/dev/null; then
        echo "$line" >>"$keys"
    fi
    # Trust the nodes' existing host keys (no trust-on-first-use).
    if [[ -f /etc/pve/priv/known_hosts ]]; then
        pct push "$CTID" /etc/pve/priv/known_hosts /var/lib/haui/known_hosts
        pct exec "$CTID" -- chown haui:haui /var/lib/haui/known_hosts
    fi
}
if [[ $MAINTENANCE -eq 1 ]]; then
    run "Authorizing the maintenance key (restricted to node-maintenance)" authorize_key
fi

if [[ $PROTECT_HA -eq 1 ]]; then
    run "Adding CT ${CTID} to HA" ha-manager add "ct:${CTID}" --state started --comment "HA Manager UI"
fi

# ----------------------------------------------------------------- summary
sleep 2
CT_IP="$(pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}' || true)"
STATUS="$(pct exec "$CTID" -- systemctl is-active haui 2>/dev/null || true)"

echo
log "Done. Container ${CTID} is up."
cat <<EOF

  Open      : https://${CT_IP:-<ct-ip>}:8443   (self-signed certificate — accept it once)
  Sign in   : with any Proxmox account, e.g. root / realm "Linux PAM"
  Service   : haui (${STATUS})
  Nodes     : ${#NODES[@]} ($(printf '%s\n' "${NODES[@]}" | awk '{print $1}' | paste -sd, -))
  Maintenance button : $([[ $MAINTENANCE -eq 1 ]] && echo "enabled (key in /etc/pve/priv/authorized_keys)" || echo "disabled")

  Update later: open the container's console and type   update
               (or from this node: pct exec $CTID -- update)

  Useful commands (on this node):
    pct exec $CTID -- journalctl -u haui -f             # follow logs
    pct exec $CTID -- nano /etc/haui/haui.toml          # edit config, then:
    pct exec $CTID -- systemctl restart haui
EOF
