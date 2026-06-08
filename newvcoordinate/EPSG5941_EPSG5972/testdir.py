from pathlib import Path
import subprocess
import os
import sys

current_dir = Path(__file__).resolve().parent
print(f"Current directory: {current_dir}")

dem_type = "DTM"
res = 10

out_dir = current_dir / "kartverket_dem_test_output"
print(f"Output directory: {out_dir}")
print()

tif_path = out_dir / f"{dem_type}_{res:.0f}m_merged.tif"

# ──────────────────────────────────────────────────────────────────────────
# APPROACH 1: Use GDAL Python bindings (FASTEST - no subprocess needed)
# ──────────────────────────────────────────────────────────────────────────
try:
    from osgeo import gdal
    
    print("✓ Using GDAL Python bindings (no subprocess)")
    ds = gdal.Open(str(tif_path))
    if ds:
        print(f"GDAL Info:")
        print(f"  Driver: {ds.GetDriver().ShortName}")
        print(f"  Size: {ds.RasterXSize}x{ds.RasterYSize}")
        print(f"  Bands: {ds.RasterCount}")
        print(f"  Projection: {ds.GetProjection()}")
        print(f"  GeoTransform: {ds.GetGeoTransform()}")
        ds = None  # Close dataset
    else:
        print(f"ERROR: Could not open {tif_path}")
    print()

except ImportError:
    print("⚠ GDAL Python bindings not available, falling back to subprocess...\n")
    
    # ──────────────────────────────────────────────────────────────────────
    # APPROACH 2: Set OSGeo4W environment once, then call gdalinfo directly
    # ──────────────────────────────────────────────────────────────────────
    
    osgeo4w_bat = r"C:\OSGeo4W\OSGeo4W.bat"
    osgeo4w_bin = r"C:\OSGeo4W\bin"
    
    # Extract environment variables from OSGeo4W.bat (run once)
    env = os.environ.copy()
    try:
        env_cmd = f'cmd /c "{osgeo4w_bat} && set"'
        result = subprocess.run(env_cmd, shell=True, capture_output=True, text=True)
        
        # Parse output and update environment
        for line in result.stdout.split('\n'):
            if '=' in line:
                key, val = line.split('=', 1)
                env[key] = val
        
        print("✓ Environment variables loaded from OSGeo4W.bat")
    except Exception as e:
        print(f"⚠ Could not parse OSGeo4W env, using direct call: {e}")
    
    # Now call gdalinfo directly (no batch file needed for subsequent calls)
    gdalinfo_exe = osgeo4w_bin / "gdalinfo.exe"
    cmd = [str(gdalinfo_exe), str(tif_path)]
    
    print(f"Running: {' '.join(cmd)}")
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    print("Return code:", proc.returncode)
    print("STDOUT:\n", proc.stdout)
    if proc.stderr:
        print("STDERR:\n", proc.stderr)
    print()