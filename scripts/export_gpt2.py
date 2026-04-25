#!/usr/bin/env python3
"""
export_gpt2.py — Export GPT-2 Medium weights to FPGA binary format (FP16).

Outputs:
  weights.bin  — 24 layers packed per HBM layout, FP16 weights + interleaved LN params
  embed.bin    — wte + wpe + ln_f params (FP32, for host-side processing)

Usage:
  pip install torch transformers
  python scripts/export_gpt2.py [--output-dir fpga/data] [--model gpt2-medium]
"""

import argparse
import os
import struct
import sys

import numpy as np

# ---------------------------------------------------------------------------
# RTL Constants (must match defines.vh production config)
# ---------------------------------------------------------------------------
MODEL_DIM   = 1024
NUM_HEADS   = 16
HEAD_DIM    = MODEL_DIM // NUM_HEADS  # 64
F_DIM       = 4096
MAX_SEQ_LEN = 128
NUM_LAYERS  = 24
WE          = 16    # elements per 256-bit word (BUS_WIDTH / DATA_WIDTH)
WORD_BYTES  = 32    # 256 bits = 32 bytes

# Row strides (in 256-bit words)
MODEL_STRIDE = MODEL_DIM // WE   # 64
F_STRIDE     = F_DIM // WE       # 256

# Per-layer weight offsets (in 256-bit words)
LAYER_WQ_OFFSET   = 0
LAYER_WK_OFFSET   = MODEL_DIM * MODEL_DIM // WE                          # 65536
LAYER_WV_OFFSET   = 2 * MODEL_DIM * MODEL_DIM // WE                     # 131072
LAYER_WO_OFFSET   = 3 * MODEL_DIM * MODEL_DIM // WE                     # 196608
LAYER_FFN1_OFFSET = 4 * MODEL_DIM * MODEL_DIM // WE                     # 262144
LAYER_FFN2_OFFSET = LAYER_FFN1_OFFSET + MODEL_DIM * F_DIM // WE         # 524288
LAYER_LN1_OFFSET  = LAYER_FFN2_OFFSET + F_DIM * MODEL_DIM // WE         # 786432
LAYER_LN2_OFFSET  = LAYER_LN1_OFFSET + 2 * MODEL_DIM // WE             # 786560
LAYER_BIAS_QKV_OFFSET  = LAYER_LN2_OFFSET + 2 * MODEL_DIM // WE       # 786688
LAYER_BIAS_PROJ_OFFSET = LAYER_BIAS_QKV_OFFSET + 3 * MODEL_DIM // WE  # 786880
LAYER_BIAS_FFN1_OFFSET = LAYER_BIAS_PROJ_OFFSET + MODEL_DIM // WE     # 786944
LAYER_BIAS_FFN2_OFFSET = LAYER_BIAS_FFN1_OFFSET + F_DIM // WE         # 787200
LAYER_SIZE             = LAYER_BIAS_FFN2_OFFSET + MODEL_DIM // WE      # 787264

# Per-layer bias sizes (in FP16 elements)
BIAS_QKV_LEN  = 3 * MODEL_DIM  # 3072
BIAS_PROJ_LEN = MODEL_DIM      # 1024
BIAS_FFN1_LEN = F_DIM          # 4096
BIAS_FFN2_LEN = MODEL_DIM      # 1024
BIAS_PER_LAYER = BIAS_QKV_LEN + BIAS_PROJ_LEN + BIAS_FFN1_LEN + BIAS_FFN2_LEN  # 9216

TOTAL_WEIGHT_WORDS = LAYER_SIZE * NUM_LAYERS  # 18,880,512 words


# ---------------------------------------------------------------------------
# FP16 Conversion
# ---------------------------------------------------------------------------

def to_fp16_bits(tensor: np.ndarray) -> np.ndarray:
    """Convert FP32 tensor to FP16 bit patterns stored as uint16.

    Returns uint16 array with same shape, containing IEEE 754 half-precision bits.
    """
    return tensor.astype(np.float16).view(np.uint16)


# ---------------------------------------------------------------------------
# Binary Packing (matches RTL HBM word layout)
# ---------------------------------------------------------------------------

def pack_word(elements: np.ndarray) -> bytes:
    """Pack 16 FP16 values (uint16 bit patterns) into 32 bytes (256-bit word).

    Element 0 at bytes [0:2], element 1 at bytes [2:4], ..., element 15 at bytes [30:32].
    """
    assert len(elements) == WE
    return elements.astype(np.uint16).tobytes()


def pack_weight_matrix(buf: bytearray, mat_fp16: np.ndarray, rows: int, cols: int,
                       base_word: int, stride: int):
    """Pack FP16 weight matrix (uint16 bit patterns) into buffer at word offset.

    Row r stored at word addresses [base_word + r*stride .. base_word + r*stride + cols/WE - 1].
    """
    words_per_row = cols // WE
    for r in range(rows):
        row_base = (base_word + r * stride) * WORD_BYTES
        for w in range(words_per_row):
            col_start = w * WE
            elements = mat_fp16[r, col_start:col_start + WE]
            offset = row_base + w * WORD_BYTES
            buf[offset:offset + WORD_BYTES] = pack_word(elements)


def pack_ln_params(buf: bytearray, gamma_fp16: np.ndarray, beta_fp16: np.ndarray,
                   base_word: int):
    """Pack LN gamma/beta as interleaved FP16: gamma[0], beta[0], gamma[1], beta[1], ...

    Each value is a separate FP16 (uint16). Total = 2*dim values = 2*dim/WE words.
    """
    dim = len(gamma_fp16)
    interleaved = np.empty(2 * dim, dtype=np.uint16)
    interleaved[0::2] = gamma_fp16.astype(np.uint16)
    interleaved[1::2] = beta_fp16.astype(np.uint16)
    words = 2 * dim // WE
    for w in range(words):
        elements = interleaved[w * WE:(w + 1) * WE]
        offset = (base_word + w) * WORD_BYTES
        buf[offset:offset + WORD_BYTES] = pack_word(elements)


def pack_bias_vector(buf: bytearray, bias_fp16: np.ndarray, base_word: int):
    """Pack a 1D FP16 bias vector into consecutive 256-bit words at base_word."""
    dim = len(bias_fp16)
    words = dim // WE
    for w in range(words):
        elements = bias_fp16[w * WE:(w + 1) * WE]
        offset = (base_word + w) * WORD_BYTES
        buf[offset:offset + WORD_BYTES] = pack_word(elements)


# ---------------------------------------------------------------------------
# Main Export
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Export GPT-2 Medium weights to FPGA binary")
    parser.add_argument("--model", default="gpt2-medium", help="HuggingFace model name")
    parser.add_argument("--output-dir", default="fpga/data", help="Output directory")
    args = parser.parse_args()

    # Late imports so --help works without torch
    import torch
    from transformers import GPT2LMHeadModel

    os.makedirs(args.output_dir, exist_ok=True)

    # ------------------------------------------------------------------
    # Load model
    # ------------------------------------------------------------------
    print(f"Loading {args.model}...")
    model = GPT2LMHeadModel.from_pretrained(args.model)
    sd = model.state_dict()

    # Validate dimensions
    wte = sd["transformer.wte.weight"].numpy()   # (50257, 1024)
    wpe = sd["transformer.wpe.weight"].numpy()   # (1024, 1024)
    assert wte.shape == (50257, MODEL_DIM), f"wte shape mismatch: {wte.shape}"
    assert wpe.shape == (MAX_SEQ_LEN * 8, MODEL_DIM) or wpe.shape[1] == MODEL_DIM, \
        f"wpe shape: {wpe.shape}"  # GPT-2 medium has 1024 positions
    print(f"  wte: {wte.shape}, wpe: {wpe.shape}")

    # ------------------------------------------------------------------
    # Allocate weight buffer
    # ------------------------------------------------------------------
    total_bytes = TOTAL_WEIGHT_WORDS * WORD_BYTES
    print(f"Allocating weight buffer: {total_bytes / 1e6:.1f} MB "
          f"({TOTAL_WEIGHT_WORDS} words x {WORD_BYTES} bytes)")
    wgt_buf = bytearray(total_bytes)

    # Biases are now packed inline in wgt_buf at LAYER_BIAS_*_OFFSET

    # ------------------------------------------------------------------
    # Pack each layer
    # ------------------------------------------------------------------
    for layer_idx in range(NUM_LAYERS):
        prefix = f"transformer.h.{layer_idx}"
        print(f"  Layer {layer_idx}/{NUM_LAYERS}...", end="", flush=True)

        # --- Load weights and biases ---
        # GPT-2 Conv1D: weight shape is (in_features, out_features)
        # c_attn: (1024, 3072) = concatenated QKV
        c_attn_w = sd[f"{prefix}.attn.c_attn.weight"].numpy()   # (1024, 3072)
        c_attn_b = sd[f"{prefix}.attn.c_attn.bias"].numpy()     # (3072,)
        c_proj_w = sd[f"{prefix}.attn.c_proj.weight"].numpy()    # (1024, 1024)
        c_proj_b = sd[f"{prefix}.attn.c_proj.bias"].numpy()      # (1024,)
        c_fc_w   = sd[f"{prefix}.mlp.c_fc.weight"].numpy()       # (1024, 4096)
        c_fc_b   = sd[f"{prefix}.mlp.c_fc.bias"].numpy()         # (4096,)
        c_proj2_w = sd[f"{prefix}.mlp.c_proj.weight"].numpy()    # (4096, 1024)
        c_proj2_b = sd[f"{prefix}.mlp.c_proj.bias"].numpy()      # (1024,)

        # Split QKV
        W_q = c_attn_w[:, :MODEL_DIM]            # (1024, 1024)
        W_k = c_attn_w[:, MODEL_DIM:2*MODEL_DIM] # (1024, 1024)
        W_v = c_attn_w[:, 2*MODEL_DIM:]           # (1024, 1024)
        W_o = c_proj_w                             # (1024, 1024)
        W_ffn1 = c_fc_w                            # (1024, 4096)
        W_ffn2 = c_proj2_w                         # (4096, 1024)

        # LN params
        ln1_gamma = sd[f"{prefix}.ln_1.weight"].numpy()  # (1024,)
        ln1_beta  = sd[f"{prefix}.ln_1.bias"].numpy()    # (1024,)
        ln2_gamma = sd[f"{prefix}.ln_2.weight"].numpy()  # (1024,)
        ln2_beta  = sd[f"{prefix}.ln_2.bias"].numpy()    # (1024,)

        # --- Convert weights to FP16 bit patterns ---
        W_q_fp16   = to_fp16_bits(W_q)
        W_k_fp16   = to_fp16_bits(W_k)
        W_v_fp16   = to_fp16_bits(W_v)
        W_o_fp16   = to_fp16_bits(W_o)
        W_f1_fp16  = to_fp16_bits(W_ffn1)
        W_f2_fp16  = to_fp16_bits(W_ffn2)

        # Convert LN params to FP16 bit patterns
        ln1_g_fp16 = to_fp16_bits(ln1_gamma)
        ln1_b_fp16 = to_fp16_bits(ln1_beta)
        ln2_g_fp16 = to_fp16_bits(ln2_gamma)
        ln2_b_fp16 = to_fp16_bits(ln2_beta)

        # Convert biases to FP16 bit patterns
        qkv_bias_fp16  = to_fp16_bits(c_attn_b)    # (3072,)
        proj_bias_fp16 = to_fp16_bits(c_proj_b)     # (1024,)
        ffn1_bias_fp16 = to_fp16_bits(c_fc_b)       # (4096,)
        ffn2_bias_fp16 = to_fp16_bits(c_proj2_b)    # (1024,)

        # --- Pack weights into buffer (unchanged layout) ---
        layer_base = layer_idx * LAYER_SIZE

        pack_weight_matrix(wgt_buf, W_q_fp16, MODEL_DIM, MODEL_DIM,
                           layer_base + LAYER_WQ_OFFSET, MODEL_STRIDE)
        pack_weight_matrix(wgt_buf, W_k_fp16, MODEL_DIM, MODEL_DIM,
                           layer_base + LAYER_WK_OFFSET, MODEL_STRIDE)
        pack_weight_matrix(wgt_buf, W_v_fp16, MODEL_DIM, MODEL_DIM,
                           layer_base + LAYER_WV_OFFSET, MODEL_STRIDE)
        pack_weight_matrix(wgt_buf, W_o_fp16, MODEL_DIM, MODEL_DIM,
                           layer_base + LAYER_WO_OFFSET, MODEL_STRIDE)
        pack_weight_matrix(wgt_buf, W_f1_fp16, MODEL_DIM, F_DIM,
                           layer_base + LAYER_FFN1_OFFSET, F_STRIDE)
        pack_weight_matrix(wgt_buf, W_f2_fp16, F_DIM, MODEL_DIM,
                           layer_base + LAYER_FFN2_OFFSET, MODEL_STRIDE)

        pack_ln_params(wgt_buf, ln1_g_fp16, ln1_b_fp16,
                       layer_base + LAYER_LN1_OFFSET)
        pack_ln_params(wgt_buf, ln2_g_fp16, ln2_b_fp16,
                       layer_base + LAYER_LN2_OFFSET)

        # --- Pack biases inline in weight buffer ---
        pack_bias_vector(wgt_buf, qkv_bias_fp16,  layer_base + LAYER_BIAS_QKV_OFFSET)
        pack_bias_vector(wgt_buf, proj_bias_fp16,  layer_base + LAYER_BIAS_PROJ_OFFSET)
        pack_bias_vector(wgt_buf, ffn1_bias_fp16,  layer_base + LAYER_BIAS_FFN1_OFFSET)
        pack_bias_vector(wgt_buf, ffn2_bias_fp16,  layer_base + LAYER_BIAS_FFN2_OFFSET)

        print(" done")

    # ------------------------------------------------------------------
    # Write weights.bin (unchanged layout)
    # ------------------------------------------------------------------
    wgt_path = os.path.join(args.output_dir, "weights.bin")
    with open(wgt_path, "wb") as f:
        f.write(wgt_buf)
    print(f"Wrote {wgt_path}: {len(wgt_buf):,} bytes ({len(wgt_buf)/1e6:.1f} MB)")

    # Biases are now inline in weights.bin (no separate biases.bin needed)

    # ------------------------------------------------------------------
    # Write embed.bin (FP32 arrays for host-side processing)
    # ------------------------------------------------------------------
    # Format:
    #   [4 bytes] vocab_size (uint32)
    #   [4 bytes] model_dim  (uint32)
    #   [4 bytes] max_pos    (uint32) — wpe rows
    #   [4 bytes] reserved   (uint32)
    #   [vocab_size * model_dim * 4 bytes] wte (FP32, row-major)
    #   [max_pos * model_dim * 4 bytes]    wpe (FP32, row-major)
    #   [model_dim * 4 bytes]              ln_f gamma (FP32)
    #   [model_dim * 4 bytes]              ln_f beta  (FP32)

    ln_f_gamma = sd["transformer.ln_f.weight"].numpy()  # (1024,)
    ln_f_beta  = sd["transformer.ln_f.bias"].numpy()    # (1024,)

    vocab_size = wte.shape[0]
    max_pos = wpe.shape[0]

    embed_path = os.path.join(args.output_dir, "embed.bin")
    with open(embed_path, "wb") as f:
        # Header
        f.write(struct.pack("<IIII", vocab_size, MODEL_DIM, max_pos, 0))
        # wte
        f.write(wte.astype(np.float32).tobytes())
        # wpe
        f.write(wpe.astype(np.float32).tobytes())
        # ln_f
        f.write(ln_f_gamma.astype(np.float32).tobytes())
        f.write(ln_f_beta.astype(np.float32).tobytes())

    embed_size = 16 + vocab_size * MODEL_DIM * 4 + max_pos * MODEL_DIM * 4 + MODEL_DIM * 4 * 2
    print(f"Wrote {embed_path}: {embed_size:,} bytes ({embed_size/1e6:.1f} MB)")

    print("\nDone! Files ready for FPGA host application.")
    print(f"  Weight buffer size for XRT: {total_bytes:,} bytes")
    print(f"  Format: FP16 weights + biases, interleaved FP16 LN params")


if __name__ == "__main__":
    main()
