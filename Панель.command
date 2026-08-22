#!/bin/bash
# flowbatch — панель управления очередями. Двойной клик по этому файлу.
# macOS-аналог «Панель.bat».
cd "$(dirname "$0")" || exit 1

PY=""
for cand in .venv/bin/python venv/bin/python; do
  if [ -x "$cand" ]; then PY="$cand"; break; fi
done
if [ -z "$PY" ]; then
  echo "Не найден venv."
  echo "Создай его:  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  read -r -p "Enter — закрыть" _
  exit 1
fi

# Прибить прошлую панель на этом порту, если осталась висеть.
# Chrome с CDP-портом 9222 НЕ трогаем: прогоны переживают перезапуск панели.
if lsof -ti tcp:8765 >/dev/null 2>&1; then
  echo "Останавливаю прошлую панель на 8765..."
  lsof -ti tcp:8765 | xargs kill -9 2>/dev/null
fi

echo
echo "Панель: http://127.0.0.1:8765"
echo "Браузер с порталом Flow поднимается кнопкой в панели (или ./launch-chrome.sh)."
echo "Не закрывай это окно — закроешь, панель остановится."
echo
( sleep 1 && open "http://127.0.0.1:8765" ) &
exec "$PY" -m flowbatch.cli ui
