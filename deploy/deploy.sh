#!/usr/bin/env bash
set -euxo pipefail

# where the repo lives on the server
REPO_DIR="/root/mmo-services"
UNIT_SRC="${REPO_DIR}/deploy/chat.service"
UNIT_DST="/etc/systemd/system/chat.service"
UNIT_SRC_PROFILES="${REPO_DIR}/deploy/profiles.service"
UNIT_DST_PROFILES="/etc/systemd/system/profiles.service"
UNIT_SRC_BGUTIL="${REPO_DIR}/deploy/bgutil.service"
UNIT_DST_BGUTIL="/etc/systemd/system/bgutil.service"

# install system deps
apt-get update -y
apt-get install -y python3 python3-pip
apt-get install -y python3 python3-pip ffmpeg

# install Node.js 20
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt-get install -y nodejs

# virtualenv + python deps
python3 -m venv "${REPO_DIR}/venv"
"${REPO_DIR}/venv/bin/pip" install -r "${REPO_DIR}/redistrb.txt"

# install bgutil POT provider for yt-dlp YouTube bot detection bypass
BGUTIL_VERSION="1.3.1"
BGUTIL_DIR="/root/bgutil-ytdlp-pot-provider"

if [ ! -d "$BGUTIL_DIR" ]; then
    git clone --single-branch --branch $BGUTIL_VERSION \
        https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git $BGUTIL_DIR
fi

cd "$BGUTIL_DIR/server"
npm ci
npx tsc
cd "$REPO_DIR"

# install systemd units
cp "${UNIT_SRC}" "${UNIT_DST}"
cp "${UNIT_SRC_PROFILES}" "${UNIT_DST_PROFILES}"
cp "${UNIT_SRC_BGUTIL}" "${UNIT_DST_BGUTIL}"

systemctl daemon-reload

systemctl enable --now bgutil
systemctl enable --now chat
systemctl enable --now profiles

systemctl restart --now bgutil
systemctl restart --now chat
systemctl restart --now profiles

systemctl status bgutil --no-pager || true
systemctl status chat --no-pager || true
systemctl status profiles --no-pager || true