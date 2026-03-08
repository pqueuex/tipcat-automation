# Adding a New Product Type to TipCat Pipeline

This guide walks you through setting up a new product type (e.g., mouse pads, posters, stickers) with its own separate GCS bucket, Cloud Run job, Shopify store, and Jupyter notebook.

## Overview

The pipeline architecture separates concerns by product type and store:
- **One shared script** (`product_automation_script.py`) handles all product types
- **Separate config files** define product-specific settings (variants, Printify blueprint, prompts)
- **Separate GCS buckets** store designs and outputs per product type
- **Separate Cloud Run jobs** execute independently per product type
- **Separate notebooks** allow users to manage each product type in isolation

Example naming convention: `{store}-{product_type}`
- `tipcat-phonecases` → phone cases in Tip Cat Studios store
- `tipcat-mousepads` → mouse pads in Tip Cat Studios store
- `other-store-posters` → posters in another e-commerce store

---

## Step 1: Create a Config File

All product type settings are defined in a JSON config file. Copy the template and customize:

```bash
cd /Users/jj/Business/ECommerce/tipcat-pipeline
cp configs/template.json configs/my-store-new-product.json
```

Edit `configs/my-store-new-product.json`:

```json
{
  "name": "my-store-new-product",
  "description": "Your store + product type pipeline",
  
  "product": {
    "name": "Product Name",          // e.g., "Mouse Pad"
    "type": "Product Type",          // e.g., "Mouse Pad" (must match Shopify productType)
    "description": "product description"
  },
  
  "store": {
    "name": "Store Name",
    "url": "store.myshopify.com",
    "client_id_env": "MY_STORE_SHOPIFY_CLIENT_ID",
    "client_secret_env": "MY_STORE_SHOPIFY_CLIENT_SECRET",
    "api_version": "2025-01"
  },
  
  "gcs": {
    "bucket": "my-store-new-product",  // Must match GCS bucket name
    "designs_prefix": "designs/",
    "output_prefix": "output/",
    "mockups_prefix": "output/mockups/",
    "gemini_mockups_prefix": "output/gemini_mockups/"
  },
  
  "printify": {
    "api_key_env": "PRINTIFY_API_KEY",  // Can be shared across products
    "shop_id": "YOUR_SHOP_ID",          // From Printify dashboard
    "blueprint_id": 123,                // Product blueprint ID from Printify
    "provider_id": 1,                   // Usually 1 (SPOKE)
    "variants": {
      "Size 1": 12345,
      "Size 2": 12346,
      "Size 3": 12347
    },
    "price": "15.00"
  },
  
  "gemini": {
    "model": "gemini-2.5-flash",
    "image_model": "gemini-3.1-flash-image-preview",
    "api_key_env": "GEMINI_API_KEY"
  },
  
  "prompts": {
    "metadata": "Analyze this {product_name} design image and provide metadata...",
    "lifestyle_table": "A product photo of a {product_name} on a table...",
    "lifestyle_hand": "A {product_name} held in someone's hand..."
  },
  
  "shopify": {
    "product_type": "Product Type",
    "vendor": "Your Store Name",
    "title_template": "{design_name} {product_type}"
  }
}
```

### Key Settings to Find/Update:

- **`printify.shop_id`**: Login to Printify → Settings → Find your Shop ID
- **`printify.blueprint_id`**: Find the product you want to use: Printify → Products → Look for "Blueprint ID" in the product details
- **`printify.variants`**: Map variant names to Printify variant IDs (get from product details)
- **`store.client_id_env` / `client_secret_env`**: Environment variable names where Shopify OAuth credentials are stored (in Secret Manager on Cloud Run)
- **`gcs.bucket`**: Name of your GCS bucket (must be created next)

---

## Step 2: Create GCS Bucket

Each product type gets its own bucket to isolate designs and outputs:

```bash
# Create bucket (choose a unique name)
gsutil mb gs://my-store-new-product/

# Create folder structure
gsutil -m mkdir \
  gs://my-store-new-product/designs/ \
  gs://my-store-new-product/output/ \
  gs://my-store-new-product/output/mockups/ \
  gs://my-store-new-product/output/gemini_mockups/

# Verify
gsutil ls -r gs://my-store-new-product/
```

---

## Step 3: Set Up Cloud Run Job

Create a Cloud Run job that will execute the pipeline with your config. The job can be created via Terraform or gcloud CLI:

```bash
# Create Cloud Run job
gcloud run jobs create my-store-new-product-pipeline \
  --image us-central1-docker.pkg.dev/tipcat-automation/pipeline/tipcat-pipeline:latest \
  --memory=4Gi \
  --cpu=2 \
  --task-timeout=10800 \
  --region=us-central1 \
  --project=tipcat-automation \
  --set-env-vars="GOOGLE_CLOUD_PROJECT=tipcat-automation"

# Bind secrets from Secret Manager
gcloud run jobs update my-store-new-product-pipeline \
  --update-secrets=GEMINI_API_KEY=gemini-api-key:latest \
  --update-secrets=PRINTIFY_API_KEY=printify-api-key:latest \
  --update-secrets=MY_STORE_SHOPIFY_CLIENT_ID=my-store-shopify-client-id:latest \
  --update-secrets=MY_STORE_SHOPIFY_CLIENT_SECRET=my-store-shopify-client-secret:latest \
  --region=us-central1 \
  --project=tipcat-automation

# Test execution
gcloud run jobs execute my-store-new-product-pipeline \
  --region=us-central1 \
  --project=tipcat-automation \
  --args=--config,my-store-new-product,--step,1,--limit,1,--verbose
```

**Note**: Add Shopify secrets to Google Secret Manager before running:

```bash
echo -n "YOUR_SHOPIFY_CLIENT_ID" | gcloud secrets create my-store-shopify-client-id --data-file=-
echo -n "YOUR_SHOPIFY_CLIENT_SECRET" | gcloud secrets create my-store-shopify-client-secret --data-file=-
```

---

## Step 4: Create Jupyter Notebook

Create an instance-specific notebook for this product type. Copy and customize the template:

```bash
cd /Users/jj/Business/ECommerce/tipcat-pipeline

# Rename the phonecases notebook as a template
cp TipCat_Pipeline_Manager_phonecases.ipynb TipCat_Pipeline_Manager_my-store-new-product.ipynb
```

Edit the notebook and change these variables in **Cell 1**:

```python
CONFIG_NAME = "my-store-new-product"
GCS_BUCKET = "my-store-new-product"
CLOUD_RUN_JOB = "my-store-new-product-pipeline"
```

The notebook will now:
- Connect to your GCS bucket
- Auto-refresh the product list when new designs are added
- Allow running pipeline steps with the correct configuration

---

## Step 5: Upload Initial Designs

Upload PNG design images to your bucket. They can be organized in any folder structure under `designs/`:

```bash
# Upload a single design
gsutil cp design.png gs://my-store-new-product/designs/

# Upload a batch from a directory
gsutil -m cp -r local/designs/* gs://my-store-new-product/designs/

# Verify uploads
gsutil ls -h gs://my-store-new-product/designs/
```

---

## Step 6: Run a Test Pipeline

Test the configuration with a small batch:

### Option A: Via Notebook (Recommended for Testing)

1. Open the notebook in Colab:
   ```
   https://colab.research.google.com/github/pqueuex/tipcat-automation/blob/main/TipCat_Pipeline_Manager_my-store-new-product.ipynb
   ```

2. Cell 2: `list_designs()` → Verify designs are found
3. Cell 3: `refresh_inventory()` → Create `product_list.json`
4. Cell 5: `run_step(step=1, limit=2)` → Run Step 1 on 2 designs

### Option B: Via Cloud Run (Direct)

```bash
gcloud run jobs execute my-store-new-product-pipeline \
  --region=us-central1 \
  --project=tipcat-automation \
  --args=--config,my-store-new-product,--step,1,--limit,2,--verbose
```

### Option C: Local Testing (Dev)

```bash
cd /Users/jj/Business/ECommerce/tipcat-pipeline

# Load environment variables (locally)
export GEMINI_API_KEY="..."
export PRINTIFY_API_KEY="..."
export MY_STORE_SHOPIFY_CLIENT_ID="..."
export MY_STORE_SHOPIFY_CLIENT_SECRET="..."

# Run pipeline locally
python product_automation_script.py \
  --config my-store-new-product \
  --step 1 \
  --limit 2 \
  --verbose
```

---

## Step 7: Verify Output

After running Step 1, check that metadata was generated:

```bash
gsutil cat gs://my-store-new-product/output/generated_metadata.json | head -200
```

Expected output:
```json
[
  {
    "sku": "1",
    "gcs_path": "gs://my-store-new-product/designs/design1.png",
    "analysis": {
      "status": "success",
      "metadata": {
        "title": "Design Title",
        "teaser": "...",
        "full_description": "...",
        "tags": [...],
        ...
      }
    }
  }
]
```

---

## Step 8: Run Full Pipeline

Once testing passes, run all 5 steps:

### Via Notebook:
```python
for step in [1, 2, 3, 4, 5]:
    run_step(step=step, limit=None)
    # Wait for step to complete before running next
```

### Via Cloud Run:
```bash
for step in 1 2 3 4 5; do
  gcloud run jobs execute my-store-new-product-pipeline \
    --region=us-central1 \
    --project=tipcat-automation \
    --args=--config,my-store-new-product,--step,$step,--verbose
  
  # Poll for completion
  sleep 30
done
```

---

## Troubleshooting

### "Config file not found"
- Verify `configs/my-store-new-product.json` exists
- Check file is valid JSON: `python3 -m json.tool configs/my-store-new-product.json`

### "GCS bucket not found"
- Ensure bucket exists: `gsutil ls -b gs://my-store-new-product/`
- Job must have IAM permissions: `roles/storage.objectAdmin` on the bucket

### "Missing env vars"
- For local execution: Export in terminal before running script
- For Cloud Run: Verify secrets are bound via `gcloud run jobs describe my-store-new-product-pipeline`

### Gemini API errors
- Verify `GEMINI_API_KEY` is valid and has Vision API enabled
- Check quota limits: `gcloud compute project-info describe --project=tipcat-automation`

### Shopify authentication fails
- Verify `SHOPIFY_CLIENT_ID` and `SHOPIFY_CLIENT_SECRET` are correct
- Check store URL matches: `store.myshopify.com` (no https://)

---

## Next Steps

- **Monitor pipeline**: Check Cloud Run job logs via Google Cloud Console
- **Scale up**: Remove `--limit` flag to process all designs
- **Schedule**: Use Cloud Scheduler to run jobs on a schedule (e.g., hourly, daily)
- **Notifications**: Set up Pub/Sub or email alerts on job completion/failure

---

## Reference: Configuration Template Fields

| Field | Description | Example |
|-------|-------------|---------|
| `name` | Unique config identifier | `tipcat-phonecases` |
| `product.name` | Product display name | `Phone Case` |
| `product.type` | Shopify product type | `Phone Case` |
| `store.url` | Shopify store domain | `tipcat-studios.myshopify.com` |
| `gcs.bucket` | GCS bucket name | `tipcat-phonecases` |
| `printify.blueprint_id` | Printify product blueprint | `269` |
| `printify.variants` | Product size/model variants | `{"iPhone 16": 112813}` |
| `prompts.metadata` | Gemini prompt for metadata | Template with `{product_name}` substitution |
| `prompts.lifestyle_table` | Gemini prompt for table mockup | Template with `{product_type}` substitution |
| `prompts.lifestyle_hand` | Gemini prompt for hand mockup | Template with `{product_type}` substitution |
| `shopify.product_type` | Shopify product type label | `Phone Case` |
| `shopify.vendor` | Shopify vendor/brand | `Tip Cat Studios` |
| `shopify.title_template` | Product title format | `{design_name} iPhone Case` |

---

## Support

For issues or questions:
1. Check logs: `gcloud run jobs logs my-store-new-product-pipeline --limit=50`
2. Test locally: Run the script in development with full debugging
3. Review config: Ensure all required fields are present and valid
