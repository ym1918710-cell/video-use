"""Create a full-length person matte locally with macOS Vision."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path


HELPER_DIR = Path(__file__).parent
SWIFT_SOURCE = HELPER_DIR / "person_mask.swift"
CACHE_BINARY = Path("/private/tmp/video-use-person-mask")


def compile_masker() -> Path:
    """Compile only when the checked-in Swift source is newer than the cache."""
    if not shutil.which("swiftc"):
        raise RuntimeError("macOS Swift/Vision is required for local person masking.")
    if not CACHE_BINARY.exists() or CACHE_BINARY.stat().st_mtime < SWIFT_SOURCE.stat().st_mtime:
        subprocess.run([
            "swiftc", str(SWIFT_SOURCE), "-o", str(CACHE_BINARY),
            "-framework", "AVFoundation", "-framework", "Vision",
            "-framework", "CoreImage", "-framework", "ImageIO",
            "-framework", "UniformTypeIdentifiers",
        ], check=True)
    return CACHE_BINARY


def build_person_mask(video: Path, output: Path, fps: float = 30, quality: str = "balanced") -> Path:
    if quality not in {"fast", "balanced", "accurate"}:
        raise ValueError("quality must be fast, balanced, or accurate")
    if fps <= 0:
        raise ValueError("fps must be positive")
    output.parent.mkdir(parents=True, exist_ok=True)
    binary = compile_masker()
    with tempfile.TemporaryDirectory(prefix="video-use-mask-", dir=output.parent) as temporary:
        frames = Path(temporary)
        subprocess.run([str(binary), str(video), str(frames), str(fps), quality], check=True)
        subprocess.run([
            "ffmpeg", "-y", "-framerate", str(fps), "-i", str(frames / "mask_%06d.png"),
            "-c:v", "ffv1", "-pix_fmt", "gray", "-an", str(output),
        ], check=True)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a local macOS Vision person matte")
    parser.add_argument("video", type=Path)
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--fps", type=float, default=30)
    parser.add_argument("--quality", choices=("fast", "balanced", "accurate"), default="balanced")
    args = parser.parse_args()
    result = build_person_mask(args.video.resolve(), args.output.resolve(), args.fps, args.quality)
    print(f"person mask → {result}")


if __name__ == "__main__":
    main()
