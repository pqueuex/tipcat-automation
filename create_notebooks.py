#!/usr/bin/env python3
"""Create instance-specific notebooks by copying and modifying the base template."""
import json

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
import subprocess
from google.cloud import storage
from datetime import datetime

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
# Update run_step function (index 3) to include config parameter
run_step_code = """def run_step(step=1, limit=None, verbose=True, config=CONFIG_NAME):
    \"\"\"Execute a single pipeline step via Cloud Run.\"\"\"
    args = [
        f"--config",
        config,
        f"--step",
        str(step)
    ]
    
    if limit and int(limit) > 0:
        args.extend(["--limit", str(int(limit))])
    
    if verbose:
        args.append("--verbose")
    
    cmd = [
        "gcloud", "run", "jobs", "execute", CLOUD_RUN_JOB,
        f"--region={CLOUD_RUN_REGION}",
        f"--project={GCP_PROJECT}"
    ]
    
    # Add args as comma-separated string
    cmd.append(f"--args={','.join(args)}")
    
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
nb_phonecases['cells'][3]['source'] = run_step_code.split('\n')

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
import subprocess
from google.cloud import storage
from datetime import datetime

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
# Update run_step function (index 3)
nb_mousepads['cells'][3]['source'] = run_step_code.split('\n')

with open('TipCat_Pipeline_Manager_mousepads.ipynb', 'w') as f:
    json.dump(nb_mousepads, f, indent=1)

print("✓ Created TipCat_Pipeline_Manager_phonecases.ipynb")
print("✓ Created TipCat_Pipeline_Manager_mousepads.ipynb")
