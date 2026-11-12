"""VLM(사진을 보고 말로 답하는 AI)으로 정리 확인을 한 번 더 본다 (10판 1등: Qwen3-VL-2B, VLM_RULE=or, 87.2%).

젯슨에서 llama.cpp의 llama-server를 띄워 두고(127.0.0.1에서만), 입실 사진과 퇴실 사진 두 장을 보내
"새로 생긴 물건이 있나?"를 YES/NO로 묻는다. 사진은 젯슨 안에서만 오가고 밖으로 나가지 않는다.

VLM_URL   llama-server 주소 (예: http://127.0.0.1:8080). 없으면 VLM을 쓰지 않는다
VLM_RULE  YOLO 결과와 합치는 방법 (기본 or)
          vlm: VLM 답만 쓴다 (VLM 혼자 실력 재기)
          and: 둘 다 "남음"일 때만 남음 (잘못 신고 줄이기)
          or : 둘 중 하나라도 "남음"이면 남음 (놓침 줄이기)
VLM_SIDE  보내기 전 사진 긴 변 크기 (기본 768)
VLM_EQ    1이면 어두운 사진을 밝게 펴서 보낸다 (불 꺼진 방)
게이트웨이는 퇴실 뒤 첫 확인(FIRST) 때만 묻고, 카메라 스레드를 막지 않게 따로 묻는다 (room_camera.ask_vlm_then_finish).
VLM_CACHE 답을 적어 둘 파일 (채점 전용). 같은 사진·질문·모델이면 다시 묻지 않아 후보를 여러 개 돌려도 빠르다.
          사진은 적지 않고, 사진의 해시와 답·걸린 시간만 적는다
"""
import base64
import hashlib
import json
import math
import os
import time
import urllib.request

RULES = ("vlm", "and", "or")
SIDE = 768
TIMEOUT = 300  # 채점(tidy_eval)용. 젯슨 CPU에서 한 번에 30초쯤이라 넉넉히 기다린다
GATEWAY_TIMEOUT = 90  # 게이트웨이용. 오래 기다리는 사이 다음 예약이 퇴실하면 core가 앞 예약 결과를 안 받는다. 넘으면 YOLO 결과만 보낸다

PROMPT = (
    "Image 1 is a study room right after the guest checked in. "
    "Image 2 is the same room, from the same camera, after the guest left. "
    "Ignore changes in lighting, shadows and brightness. "
    "Ignore a TV remote control that was moved or flipped, and chairs that were moved. "
    "Ignore objects that were in image 1 but are gone in image 2. "
    "Is there any object in image 2 that was NOT in image 1, such as a cup, bottle, phone, bag, "
    "laptop, paper, trash or food, on the desk, chair or floor? Look carefully at small and far objects. "
    "Answer with only one word: YES or NO."
)


def prepare(img, side=SIDE, eq=False):
    """사진(BGR) → JPEG 바이트. 긴 변을 side로 줄이고, eq면 밝기를 편다."""
    import cv2
    h, w = img.shape[:2]
    scale = side / max(h, w)
    if scale < 1:
        img = cv2.resize(img, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA)
    if eq:
        lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
        lab[:, :, 0] = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(lab[:, :, 0])
        img = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
    ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        raise RuntimeError("사진을 JPEG로 못 바꿨어요")
    return jpg.tobytes()


def parse(text):
    """VLM 답 → True(새 물건 있음) / False(없음) / None(알아듣지 못함)."""
    words = (text or "").strip().upper().replace(".", " ").replace(",", " ").split()
    for w in words:
        if w in ("YES", "Y"):
            return True
        if w in ("NO", "N"):
            return False
    return None


# 환경변수 프록시(http_proxy 등)를 쓰지 않는 전송. 127.0.0.1로 보내도 프록시를 거치면 사진이 젯슨 밖으로 나간다
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def yes_probability(choice):
    """첫 단어가 YES일 확률 (0~1). llama-server가 준 첫 토큰 후보(top_logprobs)에서 YES·NO 몫만 비교한다.

    이 사진 두 장에 대해 VLM이 "새 물건 있음"을 얼마나 확신했나. 후보가 없으면(옛 서버) None.
    """
    try:
        tops = choice["logprobs"]["content"][0]["top_logprobs"]  # 고른 토큰 하나만으로는 반대쪽 몫을 몰라서 안 쓴다
    except (KeyError, IndexError, TypeError):
        return None
    yes = no = 0.0
    for t in tops:
        try:
            word = str(t["token"]).strip().upper()
            p = math.exp(float(t["logprob"]))
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if word in ("YES", "Y"):
            yes += p
        elif word in ("NO", "N"):
            no += p
    if yes + no <= 0:
        return None
    return round(yes / (yes + no), 3)


def post(url, body, timeout=TIMEOUT):
    """(답 글자, YES일 확률 또는 None)."""
    req = urllib.request.Request(url.rstrip("/") + "/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with DIRECT.open(req, timeout=timeout) as res:
        data = json.loads(res.read().decode())
    choice = data["choices"][0]
    return choice["message"]["content"], yes_probability(choice)


def model_name(url, timeout=10):
    """서버가 띄운 모델 이름 (캐시 열쇠와 기록용). 못 읽으면 빈 문자열."""
    try:
        with DIRECT.open(url.rstrip("/") + "/v1/models", timeout=timeout) as res:
            data = json.loads(res.read().decode())
        return data["data"][0]["id"]
    except Exception:
        return ""


def image_part(jpg):
    return {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpg).decode()}}


class VlmCheck:
    def __init__(self, url, rule="or", side=SIDE, eq=False, cache=None, send=post, clock=time.perf_counter,
                 timeout=TIMEOUT):
        if rule not in RULES:
            raise ValueError(f"VLM_RULE는 {', '.join(RULES)} 중 하나예요: {rule!r}")
        self.url, self.rule, self.side, self.eq = url, rule, side, eq
        self.cache_path = cache
        self.send, self.clock = send, clock
        self.model = model_name(url) if send is post else "test"
        self.timeout = timeout
        if send is post:
            self.send = lambda u, body: post(u, body, timeout=timeout)
        self.cache = {}
        if cache and os.path.exists(cache):
            with open(cache, encoding="utf-8") as f:
                self.cache = json.load(f)

    def key(self, a, b):
        h = hashlib.sha256()
        for part in (self.model, PROMPT, str(self.side), str(self.eq)):
            h.update(part.encode() + b"\0")
        h.update(a)
        h.update(b)
        return h.hexdigest()

    def prepare(self, img):
        """화면 한 장 → 이 설정(VLM_SIDE·VLM_EQ)으로 줄인 JPEG. 게이트웨이는 입실 전 사진을 이 크기로만 메모리에 둔다."""
        return prepare(img, self.side, self.eq)

    def ask(self, before, after):
        """(새 물건 있음? True/False/None, 걸린 초, 캐시에서 꺼냈나)."""
        return self.ask_prepared(self.prepare(before), self.prepare(after))

    def ask_jpeg(self, a, b):
        """prepare로 만든 JPEG 두 장으로 묻는다 (게이트웨이). (답, 걸린 초, YES일 확률 또는 None)."""
        new_item, seconds, _, yes = self.ask_with_yes(a, b)
        return new_item, seconds, yes

    def ask_prepared(self, a, b):
        """(답, 걸린 초, 캐시에서 꺼냈나)."""
        return self.ask_with_yes(a, b)[:3]

    def ask_with_yes(self, a, b):
        """(답, 걸린 초, 캐시에서 꺼냈나, YES일 확률 또는 None). 확률을 바로 돌려줘서 두 스레드가 물어도 안 섞인다."""
        key = self.key(a, b)
        if key in self.cache and "yes" in self.cache[key]:  # 확률을 적기 전(#70 전) 캐시는 다시 묻는다
            hit = self.cache[key]
            return parse(hit["text"]), hit["seconds"], True, hit.get("yes")
        body = {"messages": [{"role": "user", "content": [
                    {"type": "text", "text": "Image 1:"}, image_part(a),
                    {"type": "text", "text": "Image 2:"}, image_part(b),
                    {"type": "text", "text": PROMPT}]}],
                "temperature": 0, "max_tokens": 5, "logprobs": True, "top_logprobs": 10}
        started = self.clock()
        reply = self.send(self.url, body)
        seconds = self.clock() - started
        text, yes = reply if isinstance(reply, tuple) else (reply, None)
        if self.cache_path:
            self.cache[key] = {"text": text, "seconds": round(seconds, 3), "yes": yes}
            tmp = self.cache_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.cache, f)
            os.replace(tmp, self.cache_path)
        return parse(text), seconds, False, yes

    def combine(self, status, new_item):
        """YOLO 결과(status)와 VLM 답을 합친다. VLM이 알아듣지 못한 답이면 YOLO 결과 그대로."""
        if new_item is None:
            return status
        vlm = "LEFT_ITEMS" if new_item else "CLEAN"
        if self.rule == "vlm":
            return vlm
        if self.rule == "and":
            return status if new_item else "CLEAN"  # VLM도 "있다"고 해야 남음
        return status if status != "CLEAN" else vlm  # or


def is_local(url):
    """젯슨 안(127.0.0.1·localhost·::1) 주소인가. 사진이 밖으로 나가지 않게 이 주소에만 보낸다."""
    from urllib.parse import urlparse
    try:
        return urlparse(url).hostname in ("127.0.0.1", "localhost", "::1")
    except ValueError:
        return False


def from_env(env):
    url = (env.get("VLM_URL") or "").strip()
    if not url:
        return None
    if not is_local(url):
        raise ValueError(f"VLM_URL은 젯슨 안(127.0.0.1)만 돼요. 사진이 밖으로 나가지 않게 멈춰요: {url}")
    return VlmCheck(url, rule=(env.get("VLM_RULE") or "or").strip().lower(),
                    side=int(env.get("VLM_SIDE") or SIDE), eq=env.get("VLM_EQ") == "1",
                    cache=(env.get("VLM_CACHE") or None))
