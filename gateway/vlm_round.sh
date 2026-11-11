#!/bin/bash
# 10판 (exp 전용): VLM 두 개를 차례로 띄워 1등 설정(옮김 빼기 1.0) 위에 얹어 채점한다. 젯슨에서 돌린다.
# llama-server는 127.0.0.1에서만 열어서 젯슨 밖에서는 못 들어온다. 사진은 젯슨 밖으로 나가지 않는다.
# 준비: ~/llama.cpp/build/bin/llama-server, ~/vlm/ 안의 모델 파일 4개
set -u
cd ~/mspace-device || exit 1
LLAMA=~/llama.cpp/build/bin/llama-server
B="YOLO_MODEL=gateway/yolov8s.onnx DESK_PHOTO=1 DESK_AREA=0.25,0.22,0.64,0.83 ROOM_ITEMS=remote:1 DESK_LIGHT=ssim DESK_THRESH=25 DESK_MOVE=1 DESK_MOVE_SAME=1.0"
V="VLM_URL=http://127.0.0.1:8080 VLM_CACHE=$HOME/vlm/cache.json TIDY_TIMEOUT=7200"

run_model() {
  name=$1; model=$2; mmproj=$3
  echo "=== $name 시작 $(date +%H:%M)"
  "$LLAMA" -m ~/vlm/"$model" --mmproj ~/vlm/"$mmproj" --host 127.0.0.1 --port 8080 -t 6 -c 8192 > ~/vlm/"$name".log 2>&1 &
  pid=$!
  for _ in $(seq 120); do curl -sf http://127.0.0.1:8080/health > /dev/null && break; sleep 5; done
  if ! curl -sf http://127.0.0.1:8080/health > /dev/null; then
    echo "$name 서버가 안 떴어요. ~/vlm/$name.log 를 보세요"; kill "$pid" 2>/dev/null; return
  fi
  printf '%s\n' \
    "$name VLM만 | $B $V VLM_NAME=$name VLM_RULE=vlm" \
    "$name 둘다 남음일때만 | $B $V VLM_NAME=$name VLM_RULE=and" \
    "$name 하나라도 남음이면 | $B $V VLM_NAME=$name VLM_RULE=or" > ~/AI-go-do/candidates.txt
  ~/mspace-venv/bin/python gateway/tidy_eval.py compare ~/AI-go-do
  kill "$pid"; wait "$pid" 2>/dev/null
  echo "=== $name 끝 $(date +%H:%M)"
}

run_model smol SmolVLM-500M-Instruct-Q8_0.gguf mmproj-SmolVLM-500M-Instruct-Q8_0.gguf
run_model qwen Qwen3-VL-2B-Instruct-Q8_0.gguf mmproj-Qwen3-VL-2B-Instruct-Q8_0.gguf
echo "=== 10판 다 끝남 $(date +%H:%M)"
