#!/usr/bin/env bash
# Install the two token widgets into the Omarchy shell on this machine.
# Copies the plugin folders, asks the shell to rescan, enables them, and pins them
# to bar row 1. Safe to run again. Remove them with: omarchy plugin remove <id>
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dest="${HOME}/.config/omarchy/plugins"
ids=(meshvault.ai-tokens meshvault.cluster-tokens)

for id in "${ids[@]}"; do
  mkdir -p "${dest}/${id}"
  for file in "${here}/plugins/${id}/"*; do
    case "${file}" in
      *__pycache__*) ;;
      *) cp -p "${file}" "${dest}/${id}/" ;;
    esac
  done
  omarchy plugin validate "${dest}/${id}"
done

omarchy-shell shell rescanPlugins || true
sleep 2

for id in "${ids[@]}"; do
  if omarchy plugin list | grep -E "^${id}[[:space:]]+disabled" >/dev/null; then
    omarchy plugin enable "${id}" left
  fi
done

python3 "${here}/pin_left.py"
echo "Done. The bar updates in about 8 seconds."
