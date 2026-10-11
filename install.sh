#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Ставит omadeck как одну команду. Зависимостей нет — нужен только python3 (3.11+).
#
#   ./install.sh              в ~/.local/bin, без sudo
#   ./install.sh --system     в /usr/local/bin (спросит sudo)
#   ./install.sh --uninstall  удалить (вместе с --system — из /usr/local/bin)
set -euo pipefail
cd "$(dirname "$0")"

DIR=$HOME/.local/bin
SUDO=
ACTION=install
for arg in "$@"; do
  case $arg in
    --system) DIR=/usr/local/bin SUDO=sudo ;;
    --uninstall) ACTION=uninstall ;;
    *) echo "usage: $0 [--system] [--uninstall]" >&2; exit 2 ;;
  esac
done
(( EUID == 0 )) && SUDO=

if [[ $ACTION == uninstall ]]; then
  $SUDO rm -f "$DIR/omadeck"
  echo "удалено: $DIR/omadeck"
else
  $SUDO install -Dm755 omadeck.py "$DIR/omadeck"
  echo "установлено: $DIR/omadeck — попробуйте: omadeck demo"
fi
