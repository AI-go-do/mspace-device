"""DINOv2-small을 ONNX로 내보낸다 (맥에서 한 번, DESK_LIGHT=dino 실험용).

    python3 gateway/export_dino.py   → dinov2_small.onnx (약 85MB) → 젯슨 ~/mspace-device/gateway/ 로 복사

입력 크기는 desk_diff.DINO_SIZE (폭 518, 높이 448)와 같아야 한다. 모델: facebookresearch/dinov2 (Apache-2.0).
"""
import torch

WIDTH, HEIGHT = 518, 448


class Patches(torch.nn.Module):
    """사진 → 14×14칸마다 특징 (1, 32×37, 384)."""

    def __init__(self, model):
        super().__init__()
        self.model = model

    def forward(self, pixel_values):
        return self.model.forward_features(pixel_values)["x_norm_patchtokens"]


def main():
    model = Patches(torch.hub.load("facebookresearch/dinov2", "dinov2_vits14").eval())
    x = torch.zeros(1, 3, HEIGHT, WIDTH)
    with torch.no_grad():
        print("특징 모양:", tuple(model(x).shape))
        args = {"input_names": ["pixel_values"], "output_names": ["patches"], "opset_version": 17}
        try:
            torch.onnx.export(model, x, "dinov2_small.onnx", dynamo=False, **args)
        except TypeError:  # 옛 torch에는 dynamo 인자가 없다
            torch.onnx.export(model, x, "dinov2_small.onnx", **args)
    print("저장: dinov2_small.onnx")


if __name__ == "__main__":
    main()
