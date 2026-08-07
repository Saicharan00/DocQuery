"""Look at what `find_images` keeps and what it throws away.

The junk thresholds in `ingestion.py` are guesses about visual output, and
guesses about visual output need a human eye. Checking them through the running
app would cost an upload and a paid embedding *per candidate — including the
junk we are trying to delete*. This costs nothing and runs in a second, so the
numbers can be tuned and re-checked freely.

It imports the real `find_images`, so what you see here is exactly what ships.

    apps\\api> .venv\\Scripts\\python.exe ..\\..\\scripts\\inspect_images.py C:\\path\\report.pdf out

Throwaway. Delete it whenever.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "api"))

from app.services.ingestion import find_images  # noqa: E402


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2

    pdf_path = Path(sys.argv[1])
    out_dir = Path(sys.argv[2])
    out_dir.mkdir(parents=True, exist_ok=True)

    def rejected(page_number: int, box, reason: str) -> None:
        print(
            f"  page {page_number:>3}  "
            f"{box.width:>6.0f}x{box.height:<6.0f} at ({box.x0:.0f},{box.y0:.0f})  "
            f"dropped: {reason}"
        )

    print(f"Rejected boxes in {pdf_path.name}:")
    regions = find_images(pdf_path.read_bytes(), "application/pdf", on_reject=rejected)

    seen_per_page: dict[int, int] = {}
    for region in regions:
        position = seen_per_page.get(region.page_number, 0) + 1
        seen_per_page[region.page_number] = position
        name = f"p{region.page_number:02d}-{position:02d}.jpg"
        (out_dir / name).write_bytes(region.jpeg)

    print(f"\nKept {len(regions)} images -> {out_dir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
