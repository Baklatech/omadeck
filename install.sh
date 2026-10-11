#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Ставит omadeck как одну команду: программа, ярлык для лаунчера и иконка.
# На Omarchy ещё добавляет пункт в меню. Зависимостей нет — нужен только python3 (3.11+).
#
#   ./install.sh              в ~/.local, без sudo
#   ./install.sh --system     в /usr/local (спросит sudo)
#   ./install.sh --uninstall  удалить (вместе с --system — из /usr/local)
set -euo pipefail
cd "$(dirname "$0")"

PREFIX=$HOME/.local
SUDO=
ACTION=install
for arg in "$@"; do
  case $arg in
    --system) PREFIX=/usr/local SUDO=sudo ;;
    --uninstall) ACTION=uninstall ;;
    *) echo "usage: $0 [--system] [--uninstall]" >&2; exit 2 ;;
  esac
done
(( EUID == 0 )) && SUDO=

BIN=$PREFIX/bin/omadeck
DESKTOP=$PREFIX/share/applications/omadeck.desktop
ICON=$PREFIX/share/icons/hicolor/scalable/apps/omadeck.svg
MENU=${OMARCHY_MENU_FILE:-$HOME/.config/omarchy/extensions/omarchy-menu.jsonc}
MENU_DEFAULT=/usr/share/omarchy/config/omarchy/extensions/omarchy-menu.jsonc

is_omarchy() { command -v omarchy-menu >/dev/null || [[ -d $HOME/.config/omarchy ]]; }

# Пункт меню лежит между маркерами, поэтому его можно обновлять и убирать,
# не задевая остальное содержимое файла.
backup() { cp "$1" "$1.bak.$(date +%s)"; }

menu_strip() { sed -i '/^\/\/ >>> omadeck >>>$/,/^\/\/ <<< omadeck <<<$/d' "$MENU"; }

menu_remove() {
  [[ -f $MENU ]] && grep -q '^// >>> omadeck >>>$' "$MENU" || return 0
  backup "$MENU"
  menu_strip
  echo "пункт Omadeck убран из меню Omarchy"
}

menu_add() {
  local icon entry
  icon=$(printf '\U000F0428')   # nf-md-presentation
  entry="  \"omadeck\": {\"icon\": \"$icon\", \"label\": \"Omadeck\", \"aliases\": [\"omadeck\", \"slides\", \"deck\", \"presentation\"], \"description\": \"omadeck — презентации в терминале\", \"action\": \"omarchy-launch-or-focus-tui $BIN\"},"
  # уже стоит ровно такой — не трогаем файл и не плодим резервные копии
  if [[ -f $MENU ]] && grep -qxF -- "$entry" "$MENU"; then
    echo "пункт Omadeck уже есть в меню Omarchy"
    return 0
  fi
  if [[ -f $MENU ]]; then
    backup "$MENU"
    menu_strip
  else
    mkdir -p "$(dirname "$MENU")"
    if [[ -f $MENU_DEFAULT ]]; then cp "$MENU_DEFAULT" "$MENU"; else printf '{\n}\n' >"$MENU"; fi
  fi
  if [[ $(head -n1 "$MENU") != "{" ]]; then
    echo "$MENU: первая строка не «{», пункт меню не добавлен" >&2
    return 0
  fi
  sed -i "1a // >>> omadeck >>>\n$entry\n// <<< omadeck <<<" "$MENU"
  echo "пункт Omadeck добавлен в меню Omarchy"
}

refresh_caches() {
  if command -v gtk-update-icon-cache >/dev/null; then
    $SUDO gtk-update-icon-cache -q -t -f "$PREFIX/share/icons/hicolor" 2>/dev/null || true
  fi
  if command -v update-desktop-database >/dev/null; then
    $SUDO update-desktop-database -q "$PREFIX/share/applications" 2>/dev/null || true
  fi
}

if [[ $ACTION == uninstall ]]; then
  $SUDO rm -f "$BIN" "$DESKTOP" "$ICON"
  refresh_caches
  if is_omarchy; then menu_remove; fi
  echo "omadeck удалён из $PREFIX"
  exit 0
fi

if ! command -v python3 >/dev/null; then
  if command -v pacman >/dev/null; then hint="sudo pacman -S python"; else hint="sudo apt install python3"; fi
  echo "нужен python3: $hint" >&2
  exit 1
fi

# в ярлыке — полный путь: у лаунчера в PATH может не быть $PREFIX/bin
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
sed "s|^Exec=.*|Exec=$BIN|" packaging/omadeck.desktop >"$tmp"

$SUDO install -Dm755 omadeck.py "$BIN"
$SUDO install -Dm644 "$tmp" "$DESKTOP"
$SUDO install -Dm644 packaging/omadeck.svg "$ICON"
refresh_caches
if is_omarchy; then menu_add; fi

echo "готово: $BIN — попробуйте: omadeck"
case ":$PATH:" in
  *":$PREFIX/bin:"*) ;;
  *) echo "внимание: $PREFIX/bin нет в PATH — запускайте по полному пути или добавьте его" ;;
esac
