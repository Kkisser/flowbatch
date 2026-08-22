#!/bin/bash
# Запускает отдельный Chrome с CDP-портом для flowbatch (macOS).
# Отдельный профиль обязателен: Chrome 136+ игнорирует remote-debugging на дефолтном.
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/chrome-flow-profile" \
  "https://labs.google/fx/ru/tools/flow"
