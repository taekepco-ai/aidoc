"""
학습된 모델을 ONNX로 변환 (onnxruntime 불필요)

사용법: python export_onnx.py
"""
import os
import ssl
import shutil

os.environ["CURL_CA_BUNDLE"] = ""
os.environ["REQUESTS_CA_BUNDLE"] = ""
ssl._create_default_https_context = ssl._create_unverified_context

import requests
requests.packages.urllib3.disable_warnings()
old_request = requests.Session.request
def patched_request(self, *args, **kwargs):
    kwargs["verify"] = False
    return old_request(self, *args, **kwargs)
requests.Session.request = patched_request

try:
    import httpx
    _orig = httpx.Client.__init__
    def _patch(self, *a, **kw):
        kw["verify"] = False
        _orig(self, *a, **kw)
    httpx.Client.__init__ = _patch
except ImportError:
    pass

import torch
from pathlib import Path
from transformers import AutoModelForCausalLM, AutoTokenizer


MERGED_DIR = Path(__file__).parent / "output" / "merged_model"
TEMP_DIR = Path("C:/slm_temp")
BROWSER_MODEL_DIR = Path(__file__).parent.parent / "models" / "onnx-community" / "Qwen2.5-0.5B-Instruct"


class ModelWrapper(torch.nn.Module):
    """ONNX export용 래퍼 - forward 시그니처 단순화"""
    def __init__(self, model):
        super().__init__()
        self.model = model

    def forward(self, input_ids, attention_mask):
        outputs = self.model(input_ids=input_ids, attention_mask=attention_mask)
        return outputs.logits


def main():
    print("=" * 50)
    print("  ONNX Export")
    print("=" * 50)

    if not MERGED_DIR.exists():
        print(f"Error: {MERGED_DIR} not found.")
        print("Run python train.py first.")
        return

    # 한글 경로 우회
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    temp_merged = TEMP_DIR / "merged_model"
    if temp_merged.exists():
        shutil.rmtree(temp_merged)
    print("Copying to temp path (Korean path workaround)...")
    shutil.copytree(str(MERGED_DIR), str(temp_merged))

    # 모델 로드 (float16으로 로드하여 단일 ONNX 파일 생성)
    print(f"\nLoading model...")
    tokenizer = AutoTokenizer.from_pretrained(str(temp_merged), trust_remote_code=True)
    model = AutoModelForCausalLM.from_pretrained(
        str(temp_merged),
        torch_dtype=torch.float16,
        trust_remote_code=True,
    )
    model.eval()

    wrapper = ModelWrapper(model)

    # ONNX 변환
    onnx_path = TEMP_DIR / "model.onnx"
    print(f"\nExporting to ONNX...")

    dummy = tokenizer("Hello world", return_tensors="pt")
    input_ids = dummy["input_ids"]
    attention_mask = dummy["attention_mask"]

    # float16 모델에 맞게 입력도 CPU에서 처리
    wrapper = wrapper.cpu()

    with torch.no_grad():
        torch.onnx.export(
            wrapper,
            (input_ids, attention_mask),
            str(onnx_path),
            input_names=["input_ids", "attention_mask"],
            output_names=["logits"],
            dynamic_axes={
                "input_ids": {0: "batch", 1: "sequence"},
                "attention_mask": {0: "batch", 1: "sequence"},
                "logits": {0: "batch", 1: "sequence"},
            },
            opset_version=17,
            do_constant_folding=True,
        )

    size_mb = onnx_path.stat().st_size / 1024 / 1024
    print(f"ONNX export done: {size_mb:.1f}MB")

    # 브라우저 모델 폴더로 복사
    print(f"\nCopying to browser model folder...")
    BROWSER_MODEL_DIR.mkdir(parents=True, exist_ok=True)
    onnx_subdir = BROWSER_MODEL_DIR / "onnx"
    onnx_subdir.mkdir(exist_ok=True)

    for f in ["config.json", "tokenizer.json", "tokenizer_config.json", "generation_config.json"]:
        src = temp_merged / f
        if src.exists():
            shutil.copy2(src, BROWSER_MODEL_DIR / f)
            print(f"  Copied: {f}")

    dst = onnx_subdir / "model_q4f16.onnx"
    shutil.copy2(onnx_path, dst)
    print(f"  Copied: model.onnx -> onnx/model_q4f16.onnx ({size_mb:.1f}MB)")

    # 외부 데이터 파일도 복사 (2GB 초과 시 자동 생성됨)
    for ext_file in TEMP_DIR.glob("*.onnx.data"):
        shutil.copy2(ext_file, onnx_subdir / ext_file.name)
        print(f"  Copied: {ext_file.name}")
    for ext_file in TEMP_DIR.glob("onnx__*"):
        shutil.copy2(ext_file, onnx_subdir / ext_file.name)
        print(f"  Copied external data: {ext_file.name}")

    # 임시 폴더 정리
    shutil.rmtree(TEMP_DIR)
    print("\nTemp files cleaned up.")

    print("\n" + "=" * 50)
    print("Done! Run SlmServer.exe to test.")
    print("=" * 50)


if __name__ == "__main__":
    main()
