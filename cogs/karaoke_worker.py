"""Isolated optional karaoke engines; run with OUTPUT_DIR INPUT_WAV arguments."""

import argparse
import os
from pathlib import Path


def freeze_batch_norm(model):
    """Use fixed-statistics inference, including on GPUs with broken BatchNorm kernels."""
    import torch
    from torchvision.ops.misc import FrozenBatchNorm2d

    for name, module in list(model.named_modules()):
        if isinstance(module, torch.nn.BatchNorm2d):
            frozen = FrozenBatchNorm2d(module.num_features, eps=module.eps)
            for field in ("weight", "bias", "running_mean", "running_var"):
                value = getattr(module, field)
                if value is not None:
                    setattr(frozen, field, value.detach())
            frozen = frozen.to(device=module.running_mean.device, dtype=module.running_mean.dtype)
            model.set_submodule(name, frozen)


def separate_demucs(output: Path, source: Path, device: str) -> None:
    from demucs.separate import main

    try:
        shifts = int(os.getenv("KARAOKE_SHIFTS", "1"))
        overlap = float(os.getenv("KARAOKE_OVERLAP", "0.25"))
    except ValueError:
        raise SystemExit("KARAOKE_SHIFTS must be a non-negative integer and KARAOKE_OVERLAP must be a number from 0 (inclusive) to 1 (exclusive).") from None
    if shifts < 0 or not 0 <= overlap < 1:
        raise SystemExit("KARAOKE_SHIFTS must be non-negative and KARAOKE_OVERLAP must be from 0 (inclusive) to 1 (exclusive).")
    print(f"Demucs htdemucs: shifts={shifts}; overlap={overlap}", flush=True)
    main([
        "--two-stems", "vocals", "-n", "htdemucs", "--device", device,
        "--shifts", str(shifts), "--overlap", str(overlap),
        "--out", str(output), str(source),
    ])
    (output / "htdemucs" / source.stem / "no_vocals.wav").replace(output / "no_vocals.wav")


def separate_mdx(output: Path, source: Path, device: str) -> None:
    import torch
    from audio_separator.separator import Separator

    model_directory = os.getenv("KARAOKE_MODEL_DIR") or str(Path.home() / ".cache" / "discord-vc-bot" / "models")
    separator = Separator(
        model_file_dir=model_directory,
        output_dir=str(output),
        output_format="WAV",
        use_soundfile=True,
        use_autocast=device == "cuda",
        mdx_params={
            "hop_length": 1024,
            "segment_size": 128,
            "overlap": 0.25,
            "batch_size": 1,
            "enable_denoise": False,
        },
    )
    # This segment size deliberately selects native PyTorch conversion, not ONNX Runtime.
    # Apply the requested device before load_model constructs the architecture.
    separator.torch_device = torch.device(device)
    separator.torch_device_mps = None
    separator.load_model("UVR-MDX-NET-Inst_Main.onnx")
    model = separator.model_instance.model_run
    if not isinstance(model, torch.nn.Module) or next(model.parameters()).device.type != device:
        raise SystemExit("MDX did not load on the requested native PyTorch device; no alternate backend was attempted.")
    freeze_batch_norm(model)
    print(f"MDX native PyTorch: device={device}; model=UVR-MDX-NET-Inst_Main.onnx; precision={separator.effective_precision}", flush=True)
    separator.separate(str(source), custom_output_names={"Vocals": "vocals", "Instrumental": "no_vocals"})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("input_wav", type=Path)
    args = parser.parse_args()
    backend = os.getenv("KARAOKE_BACKEND", "mdx").strip().lower()
    requested = os.getenv("KARAOKE_DEVICE", "auto").strip().lower()
    if backend not in {"demucs", "mdx"}:
        raise SystemExit("KARAOKE_BACKEND must be demucs or mdx.")
    if requested not in {"auto", "cpu", "cuda"}:
        raise SystemExit("KARAOKE_DEVICE must be auto, cpu, or cuda. Vulkan is not supported; use compatible CUDA/ROCm PyTorch for GPU inference.")
    import torch

    available = torch.cuda.is_available()
    if requested == "cuda" and not available:
        raise SystemExit("KARAOKE_DEVICE=cuda requires compatible CUDA/ROCm PyTorch and GPU drivers; no CPU fallback was attempted.")
    device = "cuda" if requested == "cuda" or (requested == "auto" and available) else "cpu"
    print(f"Karaoke backend={backend}; device={device}; requested={requested}; torch={torch.__version__}; HIP={torch.version.hip}", flush=True)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if backend == "demucs":
        separate_demucs(args.output_dir, args.input_wav, device)
    else:
        separate_mdx(args.output_dir, args.input_wav, device)


if __name__ == "__main__":
    main()
