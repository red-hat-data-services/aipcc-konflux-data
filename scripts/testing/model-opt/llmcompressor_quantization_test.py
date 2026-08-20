#!/usr/bin/env python3
"""
W8A8 Quantization Test: Disk Size & Parameter Memory Reduction

Validates that W8A8 quantization produces a model with:
* Smaller disk size
* Smaller parameter memory footprint (int8 vs bfloat16/float16)

Steps:
1. Load the original model (CPU), measure parameter memory footprint
2. Quantize with SmoothQuant + GPTQ (W8A8)
3. Load quantized model, validate dtypes + measure parameter memory footprint
4. Compare disk size (original vs quantized)
5. Compare parameter memory footprint (original vs quantized)

Usage: python llmcompressor_quantization_test.py <model-name> <output-dir>

Exit codes:
* 0: quantization succeeded and improvements detected
* 1: quantization failed or no improvement found
"""
import os
import sys
import shutil
import torch
from transformers import AutoModelForCausalLM
from llmcompressor.modifiers.smoothquant import SmoothQuantModifier
from llmcompressor.modifiers.quantization import GPTQModifier
from llmcompressor import oneshot


def log(msg):
    print(f"[PY] {msg}", flush=True)


def fail(msg):
    print(f"[PY][ERROR] {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def dir_size(path):
    if os.path.isfile(path):
        return os.path.getsize(path)

    total = 0
    for dp, _, fs in os.walk(path):
        for f in fs:
            fp = os.path.join(dp, f)
            if os.path.isfile(fp):
                total += os.path.getsize(fp)
    return total


def measure_param_memory(model):
    """Compute total parameter memory from actual tensor dtypes.

    Returns (total_bytes, dtype_breakdown) where dtype_breakdown is a dict
    mapping dtype string to {"count": int, "bytes": int}.
    """
    breakdown = {}
    total_bytes = 0
    for _, param in model.named_parameters():
        dtype_str = str(param.dtype)
        nbytes = param.numel() * param.element_size()
        entry = breakdown.setdefault(dtype_str, {"count": 0, "bytes": 0})
        entry["count"] += param.numel()
        entry["bytes"] += nbytes
        total_bytes += nbytes
    return total_bytes, breakdown


def main():
    if len(sys.argv) != 3:
        print("Usage: llmcompressor_quantization_test.py <model-name> <output-dir>")
        sys.exit(1)

    model_name = sys.argv[1]
    output_dir = sys.argv[2]
    original_dir = f"/models/{model_name}"

    log(f"Requested model: {model_name}")
    log(f"torch={torch.__version__}, cuda={torch.cuda.is_available()}")

    # -------------------------------------------------------------------
    # Prepare output directory and verify original model exists
    # -------------------------------------------------------------------
    os.makedirs(output_dir, exist_ok=True)

    for item in os.listdir(output_dir):
        p = os.path.join(output_dir, item)
        try:
            if os.path.isdir(p):
                shutil.rmtree(p)
            else:
                os.remove(p)
        except Exception as e:
            fail(f"Failed to delete {p}: {e}")

    if not os.path.isdir(original_dir):
        fail(f"Original model not found at {original_dir}. "
             f"Model should be pre-downloaded by download-models task.")

    log(f"Using original model from: {original_dir}")

    original_size_mb = dir_size(original_dir) / 1e6
    log(f"Original model size on disk: {original_size_mb:.2f} MB")

    if original_size_mb == 0:
        fail("Original model directory is empty")

    # -------------------------------------------------------------------
    # Step 1: Load original model and measure parameter memory
    # -------------------------------------------------------------------
    log("=" * 60)
    log("STEP 1: Measuring parameter memory for ORIGINAL model")
    log("=" * 60)

    try:
        model_orig = AutoModelForCausalLM.from_pretrained(original_dir)
    except Exception as e:
        fail(f"Failed to load original model from {original_dir}: {e}")

    original_mem_bytes, original_dtypes = measure_param_memory(model_orig)
    original_mem_mb = original_mem_bytes / (1024 ** 2)

    log("Original model dtype distribution:")
    total_params = sum(d["count"] for d in original_dtypes.values())
    for dtype, info in original_dtypes.items():
        pct = 100.0 * info["count"] / total_params if total_params else 0.0
        log(f"  {dtype}: {info['count']:,} params, {info['bytes'] / (1024**2):.2f} MB ({pct:.1f}%)")
    log(f"Original parameter memory: {original_mem_mb:.2f} MB")

    del model_orig

    # -------------------------------------------------------------------
    # Step 2: Quantize model
    # -------------------------------------------------------------------
    log("=" * 60)
    log("STEP 2: Quantizing model (W8A8) - SINGLE PASS")
    log("=" * 60)

    recipe = [
        SmoothQuantModifier(smoothing_strength=0.8),
        GPTQModifier(
            scheme="W8A8",
            targets="Linear",
            ignore=["lm_head"],
        ),
    ]

    log("Starting oneshot quantization (SmoothQuant + GPTQ W8A8)")

    try:
        oneshot(
            model=original_dir,
            dataset="open_platypus",
            recipe=recipe,
            output_dir=output_dir,
            max_seq_length=1024,
            num_calibration_samples=128,
        )
    except Exception as e:
        fail(f"Quantization failed: {e}")

    # -------------------------------------------------------------------
    # Step 3: Load quantized model, validate dtypes + parameter memory
    # -------------------------------------------------------------------
    log("=" * 60)
    log("STEP 3: Validating quantization (dtype + parameter memory)")
    log("=" * 60)

    log("Loading quantized model")

    try:
        model_q = AutoModelForCausalLM.from_pretrained(output_dir)
    except Exception as e:
        fail(f"Failed to load quantized model from {output_dir}: {e}")

    quant_mem_bytes, quant_dtypes = measure_param_memory(model_q)
    quant_mem_mb = quant_mem_bytes / (1024 ** 2)

    log("Quantized model dtype distribution:")
    total_params_q = sum(d["count"] for d in quant_dtypes.values())
    for dtype, info in quant_dtypes.items():
        pct = 100.0 * info["count"] / total_params_q if total_params_q else 0.0
        log(f"  {dtype}: {info['count']:,} params, {info['bytes'] / (1024**2):.2f} MB ({pct:.1f}%)")
    log(f"Quantized parameter memory: {quant_mem_mb:.2f} MB")

    int_params = sum(
        info["count"] for dtype, info in quant_dtypes.items() if "int" in dtype
    )

    if int_params <= 0:
        fail("Quantization failed: no integer parameters detected")

    log(f"✔ Integer parameters detected: {int_params:,}")

    del model_q

    # -------------------------------------------------------------------
    # Step 4: Measure disk size reduction
    # -------------------------------------------------------------------
    log("=" * 60)
    log("STEP 4: Measuring DISK SIZE reduction")
    log("=" * 60)

    quant_size_mb = dir_size(output_dir) / 1e6
    log(f"Quantized model size on disk: {quant_size_mb:.2f} MB")

    if not quant_size_mb < original_size_mb:
        fail(
            "Quantized model is NOT smaller on disk — "
            f"original={original_size_mb:.2f} MB, "
            f"quantized={quant_size_mb:.2f} MB"
        )

    size_ratio = original_size_mb / quant_size_mb if quant_size_mb > 0 else 0
    size_reduction_pct = ((original_size_mb - quant_size_mb) / original_size_mb * 100) if original_size_mb > 0 else 0

    log(f"Disk size: {original_size_mb:.2f} MB → {quant_size_mb:.2f} MB ({size_ratio:.2f}x smaller)")
    log("✔ Disk size reduction confirmed")

    # -------------------------------------------------------------------
    # Step 5: Assert parameter memory reduction
    # -------------------------------------------------------------------
    log("=" * 60)
    log("STEP 5: Measuring PARAMETER MEMORY reduction")
    log("=" * 60)

    if not quant_mem_bytes < original_mem_bytes:
        fail(
            "Quantized model parameters are NOT smaller — "
            f"original={original_mem_mb:.2f} MB, "
            f"quantized={quant_mem_mb:.2f} MB"
        )

    mem_ratio = original_mem_bytes / quant_mem_bytes if quant_mem_bytes > 0 else 0
    mem_reduction_pct = ((original_mem_bytes - quant_mem_bytes) / original_mem_bytes * 100) if original_mem_bytes > 0 else 0

    log(f"Parameter memory: {original_mem_mb:.2f} MB → {quant_mem_mb:.2f} MB ({mem_ratio:.2f}x smaller)")
    log(f"Parameter memory reduction: {mem_reduction_pct:.1f}%")
    log("✔ Parameter memory reduction confirmed")

    # -------------------------------------------------------------------
    # Final summary
    # -------------------------------------------------------------------
    log("=" * 60)
    log("TEST SUMMARY")
    log("=" * 60)
    log(f"✔ Quantization successful ({int_params:,} int params)")
    log(f"✔ Disk size: {original_size_mb:.2f} MB → {quant_size_mb:.2f} MB ({size_ratio:.2f}x smaller)")
    log(f"✔ Param memory: {original_mem_mb:.2f} MB → {quant_mem_mb:.2f} MB ({mem_ratio:.2f}x smaller, {mem_reduction_pct:.1f}% reduction)")
    log("=" * 60)
    log("✔ W8A8 quantization test PASSED!")
    log("=" * 60)


if __name__ == "__main__":
    main()
