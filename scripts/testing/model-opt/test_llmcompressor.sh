#!/usr/bin/env bash
set -euo pipefail

IMAGE="${1:-}"
MODEL_NAME="${2:-}"
MODEL_OUT_DIR="${3:-}"

if [[ -z "${IMAGE}" || -z "${MODEL_NAME}" || -z "${MODEL_OUT_DIR}" ]]; then
  echo "[ERROR] Usage: $0 <container-image> <model-name> <model-out-dir>" >&2
  exit 1
fi

REMOTE_SCRIPT_DIR="/home/${USER}/scripts"
REMOTE_MODELS_DIR="/home/${USER}/models"

CONTAINER_OUT_DIR="/out"

echo "=================================================="
echo " Model-opt environment validation"
echo " Image: ${IMAGE}"
echo " Model: ${MODEL_NAME}"
echo " Host Output dir: ${MODEL_OUT_DIR}"
echo " Container Output dir: ${CONTAINER_OUT_DIR}"
echo "=================================================="

run_in_container() {
  podman run --rm \
    --device nvidia.com/gpu=all \
    -v "${REMOTE_SCRIPT_DIR}:/scripts:z" \
    -v "${MODEL_OUT_DIR}:/out:z" \
    -v "${REMOTE_MODELS_DIR}:/models:z" \
    "${IMAGE}" sh -ceu "$1"
}

echo
echo "=== Python version ==="
run_in_container 'python3 --version'

run_in_container '
python3 - <<EOF
import sys
major, minor = sys.version_info[:2]
if (major, minor) < (3, 9):
    raise SystemExit("✗ Python >= 3.9 required")
EOF
'

echo
echo "=== llmcompressor ==="
run_in_container '
python3 - <<EOF
import llmcompressor
print("llmcompressor version:", llmcompressor.__version__)
EOF
'

echo
echo "=== torch ==="
run_in_container '
python3 - <<EOF
import torch
print("torch version:", torch.__version__)
print("CUDA available:", torch.cuda.is_available())
print("CUDA build:", torch.version.cuda)
EOF
'

echo
echo "=== transformers ==="
run_in_container '
python3 - <<EOF
import transformers
print("transformers version:", transformers.__version__)
EOF
'

echo
echo "=== GPU Info inside container ==="
run_in_container "nvidia-smi"

echo
echo "✓ Model-opt environment looks good"

echo
echo "=== Preparing container output directory ==="
run_in_container "mkdir -p '${CONTAINER_OUT_DIR}'"

echo
echo "=== Cleaning container output directory ==="
run_in_container "rm -rf ${CONTAINER_OUT_DIR:?}/*"

echo
echo "=== llmcompressor W8A8 Unified Test (Size + VRAM) ==="
run_in_container "python3 /scripts/testing/model-opt/llmcompressor_quantization_test.py '${MODEL_NAME}' '${CONTAINER_OUT_DIR}'"

echo
echo "✓ llmcompressor quantization test PASSED"

echo
echo "=== Quantization artifacts on VM ==="
ls -lah "${MODEL_OUT_DIR}"
