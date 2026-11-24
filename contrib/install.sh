#!/bin/sh
# Install Iris as a systemd user service on 127.0.0.1:8090 (no root). Run from the repository root.
# ./contrib/install.sh           install or update
# ./contrib/install.sh --remove  stop and remove the service (keeps the inbox and the settings)
set -eu
units="$HOME/.config/systemd/user"
code="$HOME/.local/share/iris"
conf="$HOME/.config/iris"
if [ "${1:-}" = "--remove" ]; then
    systemctl --user disable --now iris.service 2>/dev/null || true
    rm -f "$units/iris.service"
    rm -rf "$code/iris"
    systemctl --user daemon-reload
    echo "Iris removed (inbox kept in ~/.local/state/iris, settings in $conf)."
    exit 0
fi
mkdir -p "$units" "$code" "$conf"
rm -rf "$code/iris"
cp -r iris "$code/iris"
find "$code/iris" -name '__pycache__' -prune -exec rm -rf {} +
[ -e "$conf/config.toml" ] || cp contrib/config.example.toml "$conf/config.toml"
cp contrib/iris.service "$units/"
systemctl --user daemon-reload
systemctl --user enable iris.service
systemctl --user restart iris.service
echo "Iris runs on http://127.0.0.1:8090/ (settings: $conf/config.toml)."
echo "Point your tools at it, e.g. THEMIS_NTFY_URL=http://127.0.0.1:8090/themis"
