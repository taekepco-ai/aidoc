"""
Qwen2.5-0.5B 한국어 파인튜닝 스크립트 (LoRA)

사용법:
  1. install.bat 실행 (최초 1회)
  2. train_data.json에 학습 데이터 추가
  3. python train.py
  4. python export_onnx.py
"""
import json
import os
import ssl
import torch

# SSL 인증서 검증 우회 (사내망 등 SSL 문제 해결)
os.environ["CURL_CA_BUNDLE"] = ""
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
os.environ["REQUESTS_CA_BUNDLE"] = ""
ssl._create_default_https_context = ssl._create_unverified_context

import requests
requests.packages.urllib3.disable_warnings()
old_request = requests.Session.request
def patched_request(self, *args, **kwargs):
    kwargs["verify"] = False
    return old_request(self, *args, **kwargs)
requests.Session.request = patched_request

# httpx도 SSL 우회
try:
    import httpx
    _original_client_init = httpx.Client.__init__
    def _patched_client_init(self, *args, **kwargs):
        kwargs["verify"] = False
        _original_client_init(self, *args, **kwargs)
    httpx.Client.__init__ = _patched_client_init
except ImportError:
    pass

from pathlib import Path
from transformers import AutoModelForCausalLM, AutoTokenizer, TrainingArguments, Trainer
from peft import LoraConfig, get_peft_model, TaskType
from torch.utils.data import Dataset


# ============================================================
# 설정
# ============================================================
MODEL_NAME = "Qwen/Qwen2.5-0.5B-Instruct"  # 베이스 모델
DATA_PATH = Path(__file__).parent / "train_data.json"
OUTPUT_DIR = Path(__file__).parent / "output"
EPOCHS = 3
BATCH_SIZE = 4
LEARNING_RATE = 2e-4
MAX_LENGTH = 512


# ============================================================
# 데이터셋
# ============================================================
class ChatDataset(Dataset):
    def __init__(self, data, tokenizer, max_length):
        self.samples = []
        for item in data:
            # Qwen 챗 포맷으로 변환
            messages = [
                {"role": "system", "content": "당신은 한국어에 능통한 AI 어시스턴트입니다. 항상 한국어로 답변하세요."},
                {"role": "user", "content": item["instruction"]},
                {"role": "assistant", "content": item["output"]},
            ]
            text = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=False)
            encoded = tokenizer(text, truncation=True, max_length=max_length, padding="max_length", return_tensors="pt")
            input_ids = encoded["input_ids"].squeeze()
            attention_mask = encoded["attention_mask"].squeeze()
            # labels = input_ids (자기회귀 학습)
            labels = input_ids.clone()
            # 패딩 토큰은 loss 계산에서 제외
            labels[labels == tokenizer.pad_token_id] = -100
            self.samples.append({
                "input_ids": input_ids,
                "attention_mask": attention_mask,
                "labels": labels,
            })

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        return self.samples[idx]


# ============================================================
# 메인
# ============================================================
def main():
    print("=" * 50)
    print("  Qwen2.5-0.5B 한국어 파인튜닝")
    print("=" * 50)

    # GPU 확인
    device = "cuda" if torch.cuda.is_available() else "cpu"
    if device == "cuda":
        print(f"GPU: {torch.cuda.get_device_name(0)}")
        print(f"VRAM: {torch.cuda.get_device_properties(0).total_mem / 1024**3:.1f}GB")
    else:
        print("GPU가 없습니다. CPU로 학습합니다. (느릴 수 있음)")

    # 모델 & 토크나이저 로드
    print("\n모델 로딩 중...")
    tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    model = AutoModelForCausalLM.from_pretrained(
        MODEL_NAME,
        torch_dtype=torch.float16 if device == "cuda" else torch.float32,
        trust_remote_code=True,
    )

    # LoRA 설정
    print("LoRA 적용 중...")
    lora_config = LoraConfig(
        task_type=TaskType.CAUSAL_LM,
        r=16,               # LoRA rank
        lora_alpha=32,       # LoRA alpha
        lora_dropout=0.05,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
    )
    model = get_peft_model(model, lora_config)
    model.print_trainable_parameters()

    # 데이터 로드
    print(f"\n학습 데이터 로딩: {DATA_PATH}")
    with open(DATA_PATH, "r", encoding="utf-8") as f:
        raw_data = json.load(f)
    print(f"학습 데이터: {len(raw_data)}개")

    dataset = ChatDataset(raw_data, tokenizer, MAX_LENGTH)

    # 학습 설정
    training_args = TrainingArguments(
        output_dir=str(OUTPUT_DIR / "checkpoints"),
        num_train_epochs=EPOCHS,
        per_device_train_batch_size=BATCH_SIZE,
        learning_rate=LEARNING_RATE,
        fp16=(device == "cuda"),
        logging_steps=1,
        save_strategy="epoch",
        report_to="none",
        remove_unused_columns=False,
    )

    # 학습 시작
    print(f"\n학습 시작! (에폭: {EPOCHS}, 배치: {BATCH_SIZE})")
    print("-" * 50)
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=dataset,
    )
    trainer.train()

    # 저장
    merged_dir = OUTPUT_DIR / "merged_model"
    print(f"\n모델 병합 & 저장 중: {merged_dir}")

    # LoRA를 베이스 모델에 병합
    model = model.merge_and_unload()
    model.save_pretrained(str(merged_dir))
    tokenizer.save_pretrained(str(merged_dir))

    print("\n" + "=" * 50)
    print("학습 완료!")
    print(f"모델 저장 위치: {merged_dir}")
    print("다음 단계: python export_onnx.py")
    print("=" * 50)


if __name__ == "__main__":
    main()
