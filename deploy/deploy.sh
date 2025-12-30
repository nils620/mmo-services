#!/usr/bin/env bash
set -euxo pipefail

# where the repo lives on the server
REPO_DIR="/root/mmo-services"
UNIT_SRC="${REPO_DIR}/deploy/chat.service"
UNIT_DST="/etc/systemd/system/chat.service"
UNIT_SRC_PROFILES="${REPO_DIR}/deploy/profiles.service"
UNIT_DST_PROFILES="/etc/systemd/system/profiles.service"

# install deps (comment out if you already did)
apt-get update -y
apt-get install -y python3 python3-pip

# (optional) virtualenv
python3 -m venv "${REPO_DIR}/venv"
"${REPO_DIR}/venv/bin/pip" install -r "${REPO_DIR}/requirements.txt"

# or plain system Python:
pip3 install -r "${REPO_DIR}/redistrb.txt"

# install the systemd unit
cp "${UNIT_SRC}" "${UNIT_DST}"
cp "$UNIT_SRC_PROFILES" "$UNIT_DST_PROFILES"

systemctl daemon-reload
systemctl enable --now chat
systemctl status chat --no-pager || true
systemctl enable --now profiles
systemctl status profiles
