"""
=======================
Download Norway DEM (DTM or DOM) from Kartverket's WCS service in tiled chunks.

Source WCS
----------
  DTM: https://wcs.geonorge.no/skwms1/wcs.hoyde-dtm-nhm-25833
        coverage id: nhm_dtm_topo_25833
  DOM: https://wcs.geonorge.no/skwms1/wcs.hoyde-dom-nhm-25833
        coverage id: nhm_dom_topo_25833

"""


import argparse
import os
import sys
import time
from pathlib import Path

import requests
from tqdm import tqdm
import signal
import subprocess

# Global flag set by SIGINT handler so blocking loops can stop promptly
STOP = False

def _sigint_handler(signum, frame):
    global STOP
    STOP = True
    print("\nReceived Ctrl-C, stopping...")

signal.signal(signal.SIGINT, _sigint_handler)

# ── WCS endpoints ────────────────────────────────────────────────────────────
WCS_BASE = {
    "dtm": "https://wcs.geonorge.no/skwms1/wcs.hoyde-dtm-nhm-25833",
    "dom": "https://wcs.geonorge.no/skwms1/wcs.hoyde-dom-nhm-25833",
}
COVERAGE_ID = {
    "dtm": "nhm_dtm_topo_25833",   # verified from GetCapabilities
    "dom": "nhm_dom_topo_25833",   # verified from GetCapabilities
}

# ── Norway mainland bbox in EPSG:25833 ───────────────────────────────────────
# minx,miny,maxx,maxy (rounded conservatively)
NORWAY_BBOX_25833 = (-77_000, 6_448_000, 1_117_000, 7_945_000)

# ── Compound CRS string (used when writing GeoTIFF authority tags) ────────────
# GDAL will write the vertical datum into the GeoTIFF via gdal_translate
# when we set the output SRS to the compound code.
COMPOUND_CRS = "EPSG:25833+5941"  # horizontal=UTM33N, vertical=NN2000
COMPOUND_CRS_EPSG = 5972  # EPSG code for the compound CRS (for gdalwarp)


# ─────────────────────────────────────────────────────────────────────────────
def build_getcoverage_url(
    base_url: str,
    coverage: str,
    bbox: tuple,
    res: float,
    fmt: str = "GeoTIFF",
    version: str = "1.0.0",
) -> str:
    """Build a WCS 1.0.0 GetCoverage URL."""
    minx, miny, maxx, maxy = bbox
    width  = round((maxx - minx) / res)
    height = round((maxy - miny) / res)
    return (
        f"{base_url}?"
        f"SERVICE=WCS&VERSION={version}&REQUEST=GetCoverage"
        f"&COVERAGE={coverage}"
        f"&CRS=EPSG:25833"
        f"&BBOX={minx},{miny},{maxx},{maxy}"
        f"&WIDTH={width}&HEIGHT={height}"
        f"&FORMAT={fmt}"
    )


def tile_bbox(minx, miny, maxx, maxy, tile_size):
    """Yield (col, row, tile_bbox) tuples covering the full bbox."""
    x = minx
    col = 0
    while x < maxx:
        x1 = min(x + tile_size, maxx)
        y = miny
        row = 0
        while y < maxy:
            y1 = min(y + tile_size, maxy)
            yield col, row, (x, y, x1, y1)
            y = y1
            row += 1
        x = x1
        col += 1


def download_tile(
    url: str,
    out_path: Path,
    timeout: int = 300,
    retries: int = 3,
    retry_delay: float = 5.0,
) -> bool:
    """Download a single WCS tile. Returns True on success."""
    global STOP
    for attempt in range(1, retries + 1):
        resp = None
        try:
            resp = requests.get(url, timeout=timeout, stream=True)
            if resp.status_code != 200:
                print(
                    f"  ✗ HTTP {resp.status_code} on attempt {attempt}: "
                    f"{resp.text[:200]}"
                )
                time.sleep(retry_delay)
                continue

            ctype = resp.headers.get("Content-Type", "")
            if "xml" in ctype or "html" in ctype:
                # Server returned an error document instead of raster
                body = resp.content[:500].decode("utf-8", errors="replace")
                print(f"  ✗ Server returned error XML/HTML on attempt {attempt}: {body}")
                time.sleep(retry_delay)
                continue

            out_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = out_path.with_suffix(".tmp")
            with open(tmp_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    f.write(chunk)
                    # allow graceful stop on Ctrl-C between chunks
                    if STOP:
                        try:
                            tmp_path.unlink(missing_ok=True)
                        except Exception:
                            pass
                        return False
            tmp_path.rename(out_path)
            return True

        except KeyboardInterrupt:
            # Ensure we mark global stop and clean up temporary file/connection
            STOP = True
            print("  Received Ctrl-C, aborting download...")
            try:
                if resp is not None:
                    resp.close()
            except Exception:
                pass
            try:
                tmp_path = out_path.with_suffix(".tmp")
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
            return False

        except requests.exceptions.Timeout:
            print(f"  ✗ Timeout on attempt {attempt}")
            # sleep in small increments so Ctrl-C is responsive
            slept = 0.0
            step = 0.1
            while slept < retry_delay:
                if STOP:
                    return False
                time.sleep(step)
                slept += step
        except requests.exceptions.RequestException as e:
            print(f"  ✗ Request error on attempt {attempt}: {e}")
            slept = 0.0
            step = 0.1
            while slept < retry_delay:
                if STOP:
                    return False
                time.sleep(step)
                slept += step

    return False


# ─────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Download Kartverket DTM/DOM tiles from WCS (EPSG:25833+5941)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--type", choices=["dtm", "dom"], default="dtm",
        help="DEM type: dtm (terrain) or dom (surface). Default: dtm"
    )
    parser.add_argument(
        "--res", type=float, default=10.0,
        help="Pixel resolution in metres. 1, 10, or 50 available. Default: 10"
    )
    parser.add_argument(
        "--bbox", type=float, nargs=4,
        metavar=("MINX", "MINY", "MAXX", "MAXY"),
        default=list(NORWAY_BBOX_25833),
        help=(
            "Bounding box in EPSG:25833 (UTM33N metres). "
            "Default: full mainland Norway"
        ),
    )
    parser.add_argument(
        "--tile-size", type=float, default=25_000.0,
        help="Tile size in metres (square). Default: 25000 (25×25 km)"
    )
    parser.add_argument(
        "--out", type=Path, default=Path(f"{Path(__file__).resolve().parent}\\kartverket_dem"),
        help="Output directory for tiles. Default: ./kartverket_dem"
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Skip tiles that already exist on disk"
    )

    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print URLs without downloading"
    )
    parser.add_argument(
        "--timeout", type=int, default=300,
        help="Request timeout in seconds. Default: 300"
    )
    parser.add_argument(
        "--retries", type=int, default=3,
        help="Retry attempts per tile. Default: 3"
    )
    args = parser.parse_args()

    dem_type   = args.type
    res        = args.res
    minx, miny, maxx, maxy = args.bbox
    tile_size  = args.tile_size
    out_dir    = args.out
    resume     = args.resume
    dry_run    = args.dry_run

    base_url   = WCS_BASE[dem_type]
    coverage   = COVERAGE_ID[dem_type]

    # ── Sanity check: warn about large requests ───────────────────────────────
    total_w = maxx - minx
    total_h = maxy - miny
    total_px = int(total_w / res) * int(total_h / res)
    approx_gb = total_px * 4 / 1e9  # Float32
    n_tiles = sum(1 for _ in tile_bbox(minx, miny, maxx, maxy, tile_size))

    print(f"\n{'─'*60}")
    print(f"  DEM type   : {dem_type.upper()} ({COMPOUND_CRS})")
    print(f"  Resolution : {res}m")
    print(f"  BBox       : ({minx}, {miny}) → ({maxx}, {maxy}) [EPSG:25833]")
    print(f"  Tile size  : {tile_size/1000:.0f}×{tile_size/1000:.0f} km")
    print(f"  Tiles      : {n_tiles}")
    print(f"  ~Size      : {approx_gb:.1f} GB (uncompressed Float32)")
    print(f"  Output     : {out_dir.resolve()}")
    print(f"{'─'*60}\n")

    if not dry_run and approx_gb > 100:
        print(
            f"⚠  Total uncompressed size ~{approx_gb:.0f} GB. "
            "Make sure you have enough disk space."
        )
        try:
            ans = input("Continue? [y/N] ").strip().lower()
        except EOFError:
            ans = "y"  # non-interactive
        if ans != "y":
            sys.exit(0)

    # ── Download ──────────────────────────────────────────────────────────────
    out_dir.mkdir(parents=True, exist_ok=True)

    tiles = list(tile_bbox(minx, miny, maxx, maxy, tile_size))
    ok = 0
    skip = 0
    fail = 0

    try:
        for col, row, tbbox in tqdm(tiles, desc="Tiles", unit="tile"):
            if STOP:
                print("Stopping download loop due to Ctrl-C")
                break

            tx0, ty0, tx1, ty1 = tbbox
            fname = (
                f"{dem_type}_{res:.0f}m_"
                f"col{col:03d}_row{row:03d}_"
                f"{int(tx0)}_{int(ty0)}.tif"
            )
            out_path = out_dir / fname

            if resume and out_path.exists() and out_path.stat().st_size > 0:
                skip += 1
                continue

            url = build_getcoverage_url(base_url, coverage, tbbox, res)

            if dry_run:
                print(f"  [dry-run] {fname}\n    → {url}\n")
                ok += 1
                continue

            success = download_tile(
                url, out_path,
                timeout=args.timeout,
                retries=args.retries,
            )
            if success:
                ok += 1
            else:
                fail += 1
                print(f"  ✗ FAILED: {fname}")

            if STOP:
                break

    except KeyboardInterrupt:
        print("\nKeyboardInterrupt received, exiting...")

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'─'*60}")
    print(f"  Done. OK={ok}  Skipped={skip}  Failed={fail}")
    if fail:
        print("  Re-run with --resume to retry failed tiles.")
    print(f"{'─'*60}")

    if not dry_run and ok > 0 and not fail:
        vrt_path = out_dir / f"{dem_type}_{res:.0f}m.vrt"
        print(f"\n  Suggested merge command:")
        print(f"  gdalbuildvrt {vrt_path} {out_dir}/*.tif")
        print(
            f"  gdal_translate -of GTiff -co COMPRESS=DEFLATE "
            f"-co BIGTIFF=YES {vrt_path} {out_dir / (dem_type + f'_{res:.0f}m_merged.tif')}"
        )
        print()
    
    # ── Gdal functions ───────────────────────────────────────────────────────────────
    osgeo4w_bat = r"C:\OSGeo4W\OSGeo4W.bat"

    # Step 1: Build VRT
    vrt_path = out_dir / f"{dem_type}_{res:.0f}m.vrt"
    inner_cmd = fr'gdalbuildvrt -overwrite "{vrt_path}" "{out_dir}/*.tif"'
    cmd = f'cmd /c "{osgeo4w_bat} && {inner_cmd}"'
    print(f"Running command: {cmd}")
    proc = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    print("Return code:", proc.returncode)
    print("STDOUT:\n", proc.stdout)
    print("STDERR:\n", proc.stderr)

    # Step 2: Translate to GTiff
    translated_tif = out_dir / f"{dem_type}_{res:.0f}m_merged.tif"
    inner_cmd = fr'gdal_translate -of GTiff -co TILED=YES -co COMPRESS=DEFLATE -co BIGTIFF=YES "{vrt_path}" "{translated_tif}"'
    cmd = f'cmd /c "{osgeo4w_bat} && {inner_cmd}"'
    print(f"Running command: {cmd}")
    proc = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    print("Return code:", proc.returncode)
    print("STDOUT:\n", proc.stdout)
    print("STDERR:\n", proc.stderr)

    # Step 3: Change crs to compound with vertical datum
    reprojected_tif = out_dir / f"EPSG{COMPOUND_CRS_EPSG}_{dem_type}_{res:.0f}m_merged.tif"
    inner_cmd = fr'gdalwarp -t_srs EPSG:{COMPOUND_CRS_EPSG} "{translated_tif}" "{reprojected_tif}"'
    cmd = f'cmd /c "{osgeo4w_bat} && {inner_cmd}"'
    print(f"Running command: {cmd}")
    proc = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    print("Return code:", proc.returncode)
    print("STDOUT:\n", proc.stdout)
    print("STDERR:\n", proc.stderr)

    #Step 4: Make .gtx with gdal_translate (copies metadata and applies compression)
    gtx_path = out_dir / f"EPSG{COMPOUND_CRS_EPSG}_{dem_type}_{res:.0f}m_merged.gtx"
    inner_cmd = fr'gdal_translate -of GTX "{reprojected_tif}" "{gtx_path}"'
    cmd = f'cmd /c "{osgeo4w_bat} && {inner_cmd}"'
    print(f"Running command: {cmd}")
    proc = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    print("Return code:", proc.returncode)
    print("STDOUT:\n", proc.stdout)
    print("STDERR:\n", proc.stderr)

    # Cleanup intermediate files
    # Comment out the following lines if you want to keep the VRT and merged GeoTIFF
    try:
        (out_dir / f"{dem_type}_{res:.0f}m.vrt").unlink(missing_ok=True)
        (out_dir / f"{dem_type}_{res:.0f}m_merged.tif").unlink(missing_ok=True)
        (out_dir / f"EPSG{COMPOUND_CRS_EPSG}_{dem_type}_{res:.0f}m_merged.tif").unlink(missing_ok=True)
        (out_dir / f"EPSG{COMPOUND_CRS_EPSG}_{dem_type}_{res:.0f}m_merged.gtx").unlink(missing_ok=True)
    except Exception as e:
        print(f"Warning: failed to clean up intermediate files: {e}") 



if __name__ == "__main__":
    main()