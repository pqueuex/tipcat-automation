#!/usr/bin/env python3
"""Create instance-specific notebooks by copying and modifying the base template."""
import json


def replace_cell_source_by_prefix(notebook: dict, prefix: str, new_source_lines: list):
    """Replace first code cell whose source starts with prefix."""
    for cell in notebook.get('cells', []):
        source = cell.get('source', [])
        if source and isinstance(source, list) and source[0].startswith(prefix):
            cell['source'] = new_source_lines
            return True
    return False

# Read the existing notebook
with open('TipCat_Pipeline_Manager.ipynb') as f:
    nb = json.load(f)

# === Create phonecases version ===
nb_phonecases = json.loads(json.dumps(nb))  # Deep copy
# Modify title cell (index 0)
nb_phonecases['cells'][0]['source'] = [
    "# TipCat Pipeline Manager — Phone Cases\n",
    "Interactive notebook for managing phone case designs and running pipeline steps."
]
# Modify config cell (index 1)
config_code = """import json
import os
import subprocess
from google.cloud import storage
from datetime import datetime
from pathlib import Path

# Configuration
CONFIG_NAME = "tipcat-phonecases"
GCP_PROJECT = "tipcat-automation"
GCS_BUCKET = "tipcat-product-designs"
CLOUD_RUN_JOB = "tipcat-phonecases-pipeline"
CLOUD_RUN_REGION = "us-central1"

storage_client = storage.Client(project=GCP_PROJECT)
bucket = storage_client.bucket(GCS_BUCKET)
print(f"✓ Connected to GCP project: {GCP_PROJECT}")
print(f"✓ GCS bucket: {GCS_BUCKET}")
print(f"✓ Config: {CONFIG_NAME}")
print(f"✓ Cloud Run job: {CLOUD_RUN_JOB}")"""
nb_phonecases['cells'][1]['source'] = config_code.split('\n')
upload_code = """def sync_local_designs_to_gcs(local_dir, archive_existing=True, clear_existing=True, include_subdirs=False):
    \"\"\"Upload local PNGs to gs://<bucket>/designs/ from notebook runtime.\"\"\"
    local_path = Path(local_dir).expanduser().resolve()
    if not local_path.exists() or not local_path.is_dir():
        raise ValueError(f"Local directory not found: {local_path}")

    pattern = '**/*.png' if include_subdirs else '*.png'
    local_files = sorted(local_path.glob(pattern))
    local_files = [p for p in local_files if p.is_file()]

    if not local_files:
        raise ValueError(f"No PNG files found in: {local_path}")

    existing_design_blobs = [
        b for b in bucket.list_blobs(prefix='designs/')
        if b.name.lower().endswith('.png') and b.name.count('/') == 1
    ]

    print(f"Local PNGs found: {len(local_files)}")
    print(f"Current GCS top-level designs: {len(existing_design_blobs)}")

    if archive_existing and existing_design_blobs:
        ts = datetime.utcnow().strftime('%Y%m%d-%H%M%S')
        archive_prefix = f"archive/designs-backup-{ts}/"
        print(f"\\n📦 Archiving existing designs to gs://{GCS_BUCKET}/{archive_prefix}")
        for blob in existing_design_blobs:
            target_name = archive_prefix + blob.name.split('/')[-1]
            bucket.copy_blob(blob, bucket, new_name=target_name)

    if clear_existing and existing_design_blobs:
        print(f"🧹 Deleting {len(existing_design_blobs)} existing designs from gs://{GCS_BUCKET}/designs/")
        for blob in existing_design_blobs:
            blob.delete()

    print(f"\\n☁️ Uploading {len(local_files)} local PNGs...")
    uploaded = []
    for f in local_files:
        dest_name = f"designs/{f.name}"
        blob = bucket.blob(dest_name)
        blob.upload_from_filename(str(f))
        uploaded.append(dest_name)

    print(f"✅ Upload complete: {len(uploaded)} files")
    print("Run refresh_inventory() next to regenerate product_list.json")
    return uploaded

# Example:
# sync_local_designs_to_gcs('/content/designs', archive_existing=True, clear_existing=True)"""
nb_phonecases['cells'].insert(2, {
    'cell_type': 'code',
    'metadata': {'language': 'python'},
    'source': upload_code.split('\n')
})
# Update run_step function
run_step_code = """def run_step(step=1, limit=None, verbose=True, config=CONFIG_NAME):
    \"\"\"Execute a single pipeline step via Cloud Run.\"\"\"
    args = [
        f"--config={config}",
        f"--step={step}"
    ]
    
    if limit and int(limit) > 0:
        args.append(f"--limit={int(limit)}")
    
    if verbose:
        args.append("--verbose")
    
    cmd = [
        "gcloud", "run", "jobs", "execute", CLOUD_RUN_JOB,
        f"--region={CLOUD_RUN_REGION}",
        f"--project={GCP_PROJECT}",
        f"--args={','.join(args)}"
    ]
    
    print(f"\\n🚀 Running Step {step}...")
    print(f"Command: {' '.join(cmd)}")
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    print(f"Exit code: {result.returncode}")
    
    if result.stdout:
        print("\\n📋 Output:")
        print(result.stdout[:2000])
    
    if result.stderr:
        print("\\n❌ Errors:")
        print(result.stderr[:2000])
    
    if result.returncode == 0:
        print(f"\\n✓ Step {step} completed successfully")
    
    return result

# Example usage:
# result = run_step(step=1, limit=2, verbose=True)"""
replace_cell_source_by_prefix(nb_phonecases, 'def run_step(', run_step_code.split('\n'))

# Update list_designs function to top-level designs only
list_designs_code = """def list_designs(prefix=\"designs/\"):
    \"\"\"List all PNG designs in GCS bucket (top-level only).\"\"\"
    blobs = list(bucket.list_blobs(prefix=prefix))
    designs = [
        b.name for b in blobs
        if b.name.lower().endswith(\".png\") and b.name.count(\"/\") == 1
    ]
    designs = sorted(designs)
    print(f\"\\n📁 Found {len(designs)} PNG designs:\")
    for name in designs[:20]:
        print(f\"  - {name}\")
    if len(designs) > 20:
        print(f\"  ... and {len(designs) - 20} more\")
    return designs

print(\"\\n=== Scanning for designs ===\")
designs = list_designs()"""
replace_cell_source_by_prefix(nb_phonecases, 'def list_designs(', list_designs_code.split('\n'))

with open('TipCat_Pipeline_Manager_phonecases.ipynb', 'w') as f:
    json.dump(nb_phonecases, f, indent=1)

# === Create mousepads version ===
nb_mousepads = json.loads(json.dumps(nb))  # Deep copy
# Modify title cell (index 0)
nb_mousepads['cells'][0]['source'] = [
    "# TipCat Pipeline Manager — Mouse Pads\n",
    "Interactive notebook for managing mouse pad designs and running pipeline steps."
]
# Modify config cell (index 1)
config_code_mp = """import json
import os
import subprocess
from google.cloud import storage
from datetime import datetime
from pathlib import Path

# Configuration
CONFIG_NAME = "tipcat-mousepads"
GCP_PROJECT = "tipcat-automation"
GCS_BUCKET = "tipcat-mousepads"
CLOUD_RUN_JOB = "tipcat-mousepads-pipeline"
CLOUD_RUN_REGION = "us-central1"

storage_client = storage.Client(project=GCP_PROJECT)
bucket = storage_client.bucket(GCS_BUCKET)
print(f"✓ Connected to GCP project: {GCP_PROJECT}")
print(f"✓ GCS bucket: {GCS_BUCKET}")
print(f"✓ Config: {CONFIG_NAME}")
print(f"✓ Cloud Run job: {CLOUD_RUN_JOB}")"""
nb_mousepads['cells'][1]['source'] = config_code_mp.split('\n')
nb_mousepads['cells'].insert(2, {
    'cell_type': 'code',
    'metadata': {'language': 'python'},
    'source': upload_code.split('\n')
})
# Update run_step and list_designs functions
replace_cell_source_by_prefix(nb_mousepads, 'def run_step(', run_step_code.split('\n'))
replace_cell_source_by_prefix(nb_mousepads, 'def list_designs(', list_designs_code.split('\n'))

with open('TipCat_Pipeline_Manager_mousepads.ipynb', 'w') as f:
    json.dump(nb_mousepads, f, indent=1)

print("✓ Created TipCat_Pipeline_Manager_phonecases.ipynb")
print("✓ Created TipCat_Pipeline_Manager_mousepads.ipynb")
