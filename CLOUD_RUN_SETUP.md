# Creating Cloud Run Jobs for TipCat Pipeline

## Quick Start

If you have `gcloud` CLI installed and authenticated:

```bash
cd /Users/jj/Business/ECommerce/tipcat-pipeline
python3 setup_cloud_run_jobs.py
```

This will create both `tipcat-phonecases-pipeline` and `tipcat-mousepads-pipeline` Cloud Run jobs.

---

## Manual Setup (if gcloud is not available)

### Step 1: Install gcloud CLI

If you haven't installed gcloud, follow the [official guide](https://cloud.google.com/sdk/docs/install).

**On macOS:**
```bash
curl https://sdk.cloud.google.com | bash
exec -l $SHELL
gcloud init
```

**Verify installation:**
```bash
gcloud --version
gcloud auth list
```

### Step 2: Create Phone Cases Cloud Run Job

```bash
# Replace IMAGE with the actual Docker image URI
IMAGE="us-central1-docker.pkg.dev/tipcat-automation/pipeline/tipcat-pipeline:latest"

gcloud run jobs create tipcat-phonecases-pipeline \
  --image=$IMAGE \
  --memory=4Gi \
  --cpu=2 \
  --task-timeout=10800 \
  --region=us-central1 \
  --project=tipcat-automation \
  --set-env-vars="GOOGLE_CLOUD_PROJECT=tipcat-automation"
```

**If the job already exists, update it:**
```bash
gcloud run jobs update tipcat-phonecases-pipeline \
  --image=$IMAGE \
  --memory=4Gi \
  --cpu=2 \
  --task-timeout=10800 \
  --region=us-central1 \
  --project=tipcat-automation
```

### Step 3: Bind Secrets

The job needs access to API keys stored in Google Secret Manager:

```bash
gcloud run jobs update tipcat-phonecases-pipeline \
  --update-secrets=GEMINI_API_KEY=gemini-api-key:latest \
  --update-secrets=PRINTIFY_API_KEY=printify-api-key:latest \
  --update-secrets=TIPCAT_SHOPIFY_CLIENT_ID=tipcat-shopify-client-id:latest \
  --update-secrets=TIPCAT_SHOPIFY_CLIENT_SECRET=tipcat-shopify-client-secret:latest \
  --region=us-central1 \
  --project=tipcat-automation
```

### Step 4: Verify Installation

List your Cloud Run jobs:
```bash
gcloud run jobs list \
  --region=us-central1 \
  --project=tipcat-automation
```

You should see `tipcat-phonecases-pipeline` in the list.

### Step 5: Test Execution

Run a test job with a single design:

```bash
gcloud run jobs execute tipcat-phonecases-pipeline \
  --region=us-central1 \
  --project=tipcat-automation \
  --args="--config=tipcat-phonecases,--step=1,--limit=1,--verbose"
```

Monitor the job:
```bash
gcloud run jobs logs read tipcat-phonecases-pipeline \
  --region=us-central1 \
  --project=tipcat-automation \
  --limit=50
```

---

## Testing in Google Colab

Once the Cloud Run job is created, you can test it from Colab:

1. Open the notebook: [TipCat Pipeline Manager — Phone Cases](https://colab.research.google.com/github/pqueuex/tipcat-automation/blob/main/TipCat_Pipeline_Manager_phonecases.ipynb)

2. **Cell 1:** Authenticate with Google
   ```python
   # (auto-runs - just hit play)
   ```

3. **Cell 2:** List designs from GCS
   ```python
   designs = list_designs()
   ```

4. **Cell 3:** Refresh product inventory
   ```python
   product_list = refresh_inventory()
   ```

5. **Cell 4:** Run Step 1 via Cloud Run
   ```python
   result = run_step(step=1, limit=2, verbose=True)
   ```

6. **Cell 5:** Read generated metadata
   ```python
   metadata = read_generated_metadata()
   ```

---

## Optional: Create Mouse Pads Job

Follow the same steps above but replace:
- `tipcat-phonecases-pipeline` → `tipcat-mousepads-pipeline`
- `--config,tipcat-phonecases` → `--config,tipcat-mousepads`
- `GCS_BUCKET = "tipcat-product-designs"` → `GCS_BUCKET = "tipcat-mousepads"`

```bash
gcloud run jobs create tipcat-mousepads-pipeline \
  --image=$IMAGE \
  --memory=4Gi \
  --cpu=2 \
  --task-timeout=10800 \
  --region=us-central1 \
  --project=tipcat-automation \
  --set-env-vars="GOOGLE_CLOUD_PROJECT=tipcat-automation"

gcloud run jobs update tipcat-mousepads-pipeline \
  --update-secrets=GEMINI_API_KEY=gemini-api-key:latest \
  --update-secrets=PRINTIFY_API_KEY=printify-api-key:latest \
  --update-secrets=TIPCAT_SHOPIFY_CLIENT_ID=tipcat-shopify-client-id:latest \
  --update-secrets=TIPCAT_SHOPIFY_CLIENT_SECRET=tipcat-shopify-client-secret:latest \
  --region=us-central1 \
  --project=tipcat-automation
```

---

## Troubleshooting

### "gcloud: command not found"
Install the Google Cloud SDK: https://cloud.google.com/sdk/docs/install

### Job creation fails with "Image not found"
The Docker image might not be built. Ensure the Dockerfile is built and pushed to:
```
us-central1-docker.pkg.dev/tipcat-automation/pipeline/tipcat-pipeline:latest
```

Verify with:
```bash
gcloud container images list-tags \
  us-central1-docker.pkg.dev/tipcat-automation/pipeline/tipcat-pipeline \
  --project=tipcat-automation
```

### Job executes but fails with auth errors
Ensure these secrets exist in Google Secret Manager:
- `gemini-api-key`
- `printify-api-key`
- `tipcat-shopify-client-id`
- `tipcat-shopify-client-secret`

List secrets:
```bash
gcloud secrets list --project=tipcat-automation
```

Create a missing secret:
```bash
echo -n "YOUR_API_KEY_VALUE" | \
  gcloud secrets create gemini-api-key \
  --data-file=- \
  --project=tipcat-automation
```

---

## Architecture

Each Cloud Run job:
- ✅ Loads the config file (`configs/tipcat-phonecases.json`)
- ✅ Applies product-specific settings
- ✅ Reads designs from dedicated GCS bucket
- ✅ Executes the selected pipeline step
- ✅ Isolates state per product type

This allows running phone case and mouse pad pipelines in parallel without interference.
