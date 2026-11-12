#!/usr/bin/env bash
# 젯슨이 켜지면 게이트웨이(+ VLM 서버, 가짜 센서)가 자동으로 켜지게 설치한다.
#   bash deploy/install-service.sh                게이트웨이만 설치하고 지금 켬
#   bash deploy/install-service.sh all            VLM 서버 + 가짜 센서 + 게이트웨이
#   bash deploy/install-service.sh vlm            VLM 서버만 (127.0.0.1:8091)
#   bash deploy/install-service.sh fake-sensors   가짜 센서만 (localhost MQTT)
#   bash deploy/install-service.sh remove         셋 다 자동 실행을 끄고 지움
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${PYTHON:-$HOME/mspace-venv/bin/python}"
UNITS=/etc/systemd/system
WHAT="${1:-gateway}"

fill() {  # 서비스 파일의 @...@ 칸을 채워 /etc/systemd/system 에 둔다
  sed -e "s|@USER@|$(id -un)|g" -e "s|@REPO@|$REPO|g" -e "s|@PYTHON@|$PYTHON|g" -e "s|@HOME@|$HOME|g" \
    "$REPO/deploy/$1.service" | sudo tee "$UNITS/$1.service" >/dev/null
}

start() {
  sudo systemctl daemon-reload
  sudo systemctl enable "$1"
  sudo systemctl restart "$1"  # 이미 켜져 있으면 새 설정으로 다시 켠다
  sleep 2
  systemctl --no-pager --lines=0 status "$1" || true
}

if [ "$WHAT" = "remove" ]; then
  for unit in mspace-gateway mspace-vlm mspace-fake-sensors; do
    sudo systemctl disable --now "$unit" 2>/dev/null || true
    sudo rm -f "$UNITS/$unit.service"
  done
  sudo systemctl daemon-reload
  echo "자동 실행을 껐어요 (게이트웨이, VLM 서버, 가짜 센서)."
  exit 0
fi

case "$WHAT" in
  gateway|all|vlm|fake-sensors) ;;
  *) echo "모르는 말이에요: $WHAT (gateway, all, vlm, fake-sensors, remove 중 하나)"; exit 1 ;;
esac

[ -x "$PYTHON" ] || { echo "파이썬을 못 찾았어요: $PYTHON (PYTHON=경로 로 알려 주세요)"; exit 1; }
if [ "$WHAT" = "all" ] && ! grep -Eq '^MQTT_HOST=(localhost|127\.0\.0\.1)$' "$REPO/gateway.env" 2>/dev/null; then
  echo "gateway.env 에 MQTT_HOST=localhost 줄이 없어요. 가짜 센서를 쓸 땐 꼭 넣어요 (교육장 브로커로 안 가게)."
  echo "넣은 뒤 다시: bash deploy/install-service.sh all"
  exit 1
fi

if [ "$WHAT" = "vlm" ] || [ "$WHAT" = "all" ]; then
  for f in "$HOME/llama.cpp/build/bin/llama-server" "$HOME/vlm/Qwen3-VL-2B-Instruct-Q8_0.gguf" \
           "$HOME/vlm/mmproj-Qwen3-VL-2B-Instruct-Q8_0.gguf"; do
    [ -e "$f" ] || { echo "VLM 파일이 없어요: $f"; exit 1; }
  done
  if pgrep -f "llama-server.*--port 8091" >/dev/null; then
    echo "손으로 켠 VLM 서버(8091)를 끌게요 (같은 포트를 둘이 못 써서). 8080은 건드리지 않아요."
    pkill -f "llama-server.*--port 8091" || true
    sleep 1
  fi
  fill mspace-vlm
  start mspace-vlm
fi

if [ "$WHAT" = "fake-sensors" ] || [ "$WHAT" = "all" ]; then
  if pgrep -f "gateway/fake_env_sensors.py" >/dev/null; then
    echo "손으로 켠 가짜 센서를 끌게요 (같은 값이 두 번 올라가지 않게)."
    pkill -f "gateway/fake_env_sensors.py" || true
    sleep 1
  fi
  fill mspace-fake-sensors
  start mspace-fake-sensors
fi

if [ "$WHAT" = "gateway" ] || [ "$WHAT" = "all" ]; then
  if [ ! -f "$REPO/gateway.env" ]; then
    cp "$REPO/deploy/gateway.env.example" "$REPO/gateway.env"
    echo "gateway.env 를 새로 만들었어요. 설정이 맞는지 한 번 보세요: nano $REPO/gateway.env"
  fi
  if pgrep -f "gateway/gateway.py" >/dev/null; then
    echo "손으로 켠 게이트웨이를 끌게요 (카메라를 둘이 같이 못 써서)."
    pkill -f "gateway/gateway.py" || true
    sleep 1
  fi
  fill mspace-gateway
  start mspace-gateway
fi

echo
echo "켜졌어요. 이제 젯슨을 껐다 켜도 알아서 켜져요."
echo "  게이트웨이 출력:  tail -f $REPO/gateway.log"
echo "  VLM 서버 출력:    tail -f $HOME/vlm/vlm.log"
echo "  가짜 센서 출력:   tail -f $REPO/fake_sensors.log"
echo "  다시 켜기:        sudo systemctl restart mspace-gateway   (gateway.env 를 고친 뒤)"
echo "  끄기:             sudo systemctl stop mspace-gateway"
