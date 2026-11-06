#!/usr/bin/env bash
# 게이트웨이를 젯슨 부팅 때 자동으로 켜지게 설치한다.
#   bash deploy/install-service.sh            설치하고 지금 켬
#   bash deploy/install-service.sh remove     자동 실행을 끄고 지움
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${PYTHON:-$HOME/mspace-venv/bin/python}"
UNIT=/etc/systemd/system/mspace-gateway.service

if [ "${1:-}" = "remove" ]; then
  sudo systemctl disable --now mspace-gateway || true
  sudo rm -f "$UNIT"
  sudo systemctl daemon-reload
  echo "자동 실행을 껐어요."
  exit 0
fi

[ -x "$PYTHON" ] || { echo "파이썬을 못 찾았어요: $PYTHON (PYTHON=경로 로 알려 주세요)"; exit 1; }
if [ ! -f "$REPO/gateway.env" ]; then
  cp "$REPO/deploy/gateway.env.example" "$REPO/gateway.env"
  echo "gateway.env 를 새로 만들었어요. 설정이 맞는지 한 번 보세요: nano $REPO/gateway.env"
fi
if pgrep -f "gateway/gateway.py" >/dev/null; then
  echo "손으로 켠 게이트웨이를 끌게요 (카메라를 둘이 같이 못 써서)."
  pkill -f "gateway/gateway.py" || true
  sleep 1
fi

sed -e "s|@USER@|$(id -un)|g" -e "s|@REPO@|$REPO|g" -e "s|@PYTHON@|$PYTHON|g" \
  "$REPO/deploy/mspace-gateway.service" | sudo tee "$UNIT" >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now mspace-gateway
sleep 2
systemctl --no-pager --lines=0 status mspace-gateway || true
echo
echo "켜졌어요. 이제 젯슨을 껐다 켜도 알아서 켜져요."
echo "  화면 출력 보기:  tail -f $REPO/gateway.log"
echo "  다시 켜기:       sudo systemctl restart mspace-gateway   (gateway.env 를 고친 뒤)"
echo "  끄기:            sudo systemctl stop mspace-gateway"
