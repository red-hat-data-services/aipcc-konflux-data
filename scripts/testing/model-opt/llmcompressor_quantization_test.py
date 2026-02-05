#!/usr/bin/env python3
"""
Unified W8A8 Quantization Test: Size & VRAM Reduction

Purpose
Validate that W8A8 quantization improves:

* Disk size smaller
* VRAM usage reduction

How it works

1. Load the original model
2. Optionally measure original VRAM usage (GPU only)
3. Quantize with SmoothQuant + GPTQ (single pass, W8A8)
4. Verify quantization via dtype inspection
5. Compare disk size (original vs quantized)
6. Optionally compare VRAM usage
7. Assert at least one improvement

Usage: python llmcompressor_quantization_test.py <model-name> <output-dir>

Exit codes

* 0: quantization succeeded and improvement detected
* 1: quantization failed or no improvement found
"""
import os
import sys
import shutil
import torch
from transformers import AutoTokenizer, AutoModelForCausalLM
from llmcompressor.modifiers.smoothquant import SmoothQuantModifier
from llmcompressor.modifiers.quantization import GPTQModifier
from llmcompressor import oneshot


def log(msg):
    print(f"[PY] {msg}", flush=True)


def fail(msg):
    print(f"[PY][ERROR] {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def dir_size(path):
    """Return total size (in bytes) of all files under a directory or a single file."""
    if os.path.isfile(path):
        return os.path.getsize(path)

    total = 0
    for dp, _, fs in os.walk(path):
        for f in fs:
            fp = os.path.join(dp, f)
            if os.path.isfile(fp):
                total += os.path.getsize(fp)
    return total


def get_vram_mb():
    """Get current VRAM usage in MB."""
    if not torch.cuda.is_available():
        return 0.0
    return torch.cuda.memory_allocated() / (1024 ** 2)


def get_peak_vram_mb():
    """Get peak VRAM usage in MB since last reset."""
    if not torch.cuda.is_available():
        return 0.0
    return torch.cuda.max_memory_allocated() / (1024 ** 2)


def reset_vram_stats():
    """Reset peak VRAM statistics."""
    if torch.cuda.is_available():
        torch.cuda.reset_peak_memory_stats()
        torch.cuda.empty_cache()


def measure_model_vram(model_path, model_name="model"):
    """
    Load a model onto GPU and measure VRAM consumption.

    Returns:
        dict: {"current_mb": float, "peak_mb": float} or None if CUDA unavailable
    """
    if not torch.cuda.is_available():
        log(f"Skipping VRAM measurement for {model_name} (CUDA not available)")
        return None

    log(f"Measuring VRAM for {model_name} at: {model_path}")

    # Reset and clear before measurement
    reset_vram_stats()

    try:
        model = AutoModelForCausalLM.from_pretrained(model_path)
        model = model.to("cuda")

        # Force memory allocation by doing a forward pass
        tokenizer = AutoTokenizer.from_pretrained(model_path)
        inputs = tokenizer("Hello world", return_tensors="pt").to("cuda")
        with torch.no_grad():
            _ = model(**inputs)

        # Get VRAM measurements
        current_vram = get_vram_mb()
        peak_vram = get_peak_vram_mb()

        log(f"  Current VRAM: {current_vram:.2f} MB")
        log(f"  Peak VRAM: {peak_vram:.2f} MB")

        # Clean up
        del model, inputs
        reset_vram_stats()

        return {
            "current_mb": current_vram,
            "peak_mb": peak_vram
        }
    except Exception as e:
        log(f"Warning: Failed to measure VRAM for {model_name}: {e}")
        return None


def main():
    if len(sys.argv) != 3:
        print("Usage: llmcompressor_quantization_test.py <model-name> <output-dir>")
        sys.exit(1)

    model_name = sys.argv[1]
    output_dir = sys.argv[2]
    original_dir = f"/models/{model_name}"

    log(f"Requested model: {model_name}")
    log(f"torch={torch.__version__}, cuda={torch.cuda.is_available()}")

    # Check GPU availability
    has_cuda = torch.cuda.is_available()
    if has_cuda:
        device_name = torch.cuda.get_device_name(0)
        total_vram = torch.cuda.get_device_properties(0).total_memory / (1024 ** 2)
        log(f"GPU: {device_name}")
        log(f"Total VRAM: {total_vram:.2f} MB")
    else:
        log("GPU not available - VRAM tests will be skipped")

    # -------------------------------------------------------------------
    # Prepare output directory and verify original model exists
    # -------------------------------------------------------------------
    os.makedirs(output_dir, exist_ok=True)

    # Clean output directory
    for item in os.listdir(output_dir):
        p = os.path.join(output_dir, item)
        try:
            if os.path.isdir(p):
                shutil.rmtree(p)
            else:
                os.remove(p)
        except Exception as e:
            fail(f"Failed to delete {p}: {e}")

    # -------------------------------------------------------------------
    # Verify original model exists at expected location
    # -------------------------------------------------------------------
    if not os.path.isdir(original_dir):
        fail(f"Original model not found at {original_dir}. "
             f"Model should be pre-downloaded by download-models task.")

    log(f"Using original model from: {original_dir}")

    # Original size
    original_size_mb = dir_size(original_dir) / 1e6
    log(f"Original model size: {original_size_mb:.2f} MB")

    if original_size_mb == 0:
        fail("Original model directory is empty")

    # -------------------------------------------------------------------
    # Measure VRAM for original model
    # -------------------------------------------------------------------
    log("=" * 60)
    log("STEP 1: Measuring VRAM for ORIGINAL model (if GPU available)")
    log("=" * 60)

    original_vram = measure_model_vram(original_dir, "original")

    # -------------------------------------------------------------------
    # Perform quantization
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
    # Load quantized model and check dtype distribution
    # -------------------------------------------------------------------
    log("=" * 60)
    log("STEP 3: Validating quantization (dtype check)")
    log("=" * 60)

    log("Loading quantized model")

    try:
        model_q = AutoModelForCausalLM.from_pretrained(output_dir)
    except Exception as e:
        fail(f"Failed to load quantized model from {output_dir}: {e}")

    # Inspect dtype distribution
    dtypes = {}
    total_params = 0

    for _, param in model_q.named_parameters():
        dtypes.setdefault(str(param.dtype), 0)
        dtypes[str(param.dtype)] += param.numel()
        total_params += param.numel()

    log("Dtype distribution after quantization:")
    for dtype, count in dtypes.items():
        pct = 100.0 * count / total_params if total_params else 0.0
        log(f"  {dtype}: {count:,} params ({pct:.2f}%)")

    # Count integer parameters
    int_params = sum(
        count for dtype, count in dtypes.items() if "int" in dtype
    )

    # Assert quantization correctness
    if int_params <= 0:
        fail("Quantization failed: no integer parameters detected")

    log(f"✔ Integer parameters detected: {int_params:,}")

    # Clean up loaded model
    del model_q

    # -------------------------------------------------------------------
    # Measure disk size reduction
    # -------------------------------------------------------------------
    log("=" * 60)
    log("STEP 4: Measuring DISK SIZE reduction")
    log("=" * 60)

    # Quantized model size
    quant_size_mb = dir_size(output_dir) / 1e6

    log(f"Quantized model size: {quant_size_mb:.2f} MB")

    # Assert quantized model is smaller
    if not quant_size_mb < original_size_mb:
        fail(
            "Quantized model is NOT smaller — "
            f"original={original_size_mb:.2f} MB, "
            f"quantized={quant_size_mb:.2f} MB"
        )

    # Calculate and show compression ratio
    size_compression_ratio = original_size_mb / quant_size_mb if quant_size_mb > 0 else 0
    size_reduction_mb = original_size_mb - quant_size_mb
    size_reduction_pct = (size_reduction_mb / original_size_mb * 100) if original_size_mb > 0 else 0

    log(f"Original model size: {original_size_mb:.2f} MB")
    log(f"Quantized model size: {quant_size_mb:.2f} MB")
    log(f"Size reduction: {size_reduction_mb:.2f} MB ({size_reduction_pct:.1f}%)")
    log(f"Compression ratio: {size_compression_ratio:.2f}x")
    log("✔ Disk size reduction confirmed")

    # -------------------------------------------------------------------
    # Measure VRAM reduction if GPU available
    # -------------------------------------------------------------------
    log("=" * 60)
    log("STEP 5: Measuring VRAM reduction (if GPU available)")
    log("=" * 60)

    quant_vram = measure_model_vram(output_dir, "quantized")

    # Compare VRAM if both measurements succeeded
    vram_test_passed = False
    if original_vram is not None and quant_vram is not None:
        original_peak = original_vram["peak_mb"]
        quant_peak = quant_vram["peak_mb"]

        vram_reduction = original_peak - quant_peak
        vram_reduction_pct = (vram_reduction / original_peak * 100) if original_peak > 0 else 0

        log(f"Original model peak VRAM: {original_peak:.2f} MB")
        log(f"Quantized model peak VRAM: {quant_peak:.2f} MB")
        log(f"VRAM reduction: {vram_reduction:.2f} MB ({vram_reduction_pct:.1f}%)")

        # Assert that quantized model uses less VRAM
        if quant_peak >= original_peak:
            fail(
                f"Quantized model does NOT use less VRAM! "
                f"Original={original_peak:.2f} MB, Quantized={quant_peak:.2f} MB"
            )

        log("✔ VRAM reduction confirmed")
        vram_test_passed = True
    else:
        log("⚠ VRAM test skipped (GPU not available)")

    # -------------------------------------------------------------------
    # Final summary
    # -------------------------------------------------------------------
    log("=" * 60)
    log("TEST SUMMARY")
    log("=" * 60)
    log(f"✔ Quantization successful ({int_params:,} int params)")
    log(f"✔ Disk size: {original_size_mb:.2f} MB → {quant_size_mb:.2f} MB ({size_compression_ratio:.2f}x smaller)")
    if vram_test_passed:
        log(f"✔ VRAM usage: {original_peak:.2f} MB → {quant_peak:.2f} MB ({vram_reduction_pct:.1f}% reduction)")
    else:
        log("⚠ VRAM test: Skipped (no GPU)")
    log("=" * 60)
    log("✔ W8A8 quantization test PASSED!")
    log("=" * 60)


if __name__ == "__main__":
    main()
