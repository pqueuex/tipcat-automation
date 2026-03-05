# Tip Cat Studios — Product Automation Pipeline

Automated pipeline to go from phone case design PNGs → published Shopify listings.

## Pipeline Steps

| Step | What | API |
|------|------|-----|
| 1 | Design PNG → metadata (title, desc, 13 tags) | Gemini 2.5 Flash |
| 2 | Design PNG → white-background iPhone mockups | Printify API |
| 3 | Printify mockup → lifestyle images (table + hand) | Gemini 3.1 Flash Image |
| 4 | Create Shopify products with variants & pricing | Shopify GraphQL |
| 5 | Upload 3 images per product to Shopify | Shopify GraphQL |

## Local Usage

```bash
# Install deps
pip install -r requirements.txt

# Set env vars (copy .env.example and fill in)
cp .env.example .env

# Run full pipeline
python product_automation_script.py

# Run single step
python product_automation_script.py --step 1

# Single product test
python product_automation_script.py --sku 1

# Wipe Shopify + rebuild
python product_automation_script.py --cleanup-shopify
```

## Cloud Run Deployment

```bash
# Build + push
gcloud builds submit \
  --tag us-central1-docker.pkg.dev/tipcat-automation/pipeline/tipcat-pipeline:latest

# Create job
gcloud run jobs create tipcat-pipeline \
  --image us-central1-docker.pkg.dev/tipcat-automation/pipeline/tipcat-pipeline:latest \
  --region us-central1 \
  --service-account tipcat-studios@tipcat-automation.iam.gserviceaccount.com \
  --set-secrets SHOPIFY_CLIENT_SECRET=shopify-secret:latest,SHOPIFY_CLIENT_ID=shopify-client-id:latest,PRINTIFY_API_KEY=printify-key:latest,GEMINI_API_KEY=gemini-key:latest \
  --set-env-vars SHOPIFY_STORE=tipcat-studios.myshopify.com,SHOPIFY_API_VERSION=2025-01,PRINTIFY_SHOP_ID=26630208,GCS_BUCKET=tipcat-product-designs,GOOGLE_CLOUD_PROJECT=tipcat-automation \
  --add-volume name=designs,type=cloud-storage,bucket=tipcat-product-designs \
  --add-volume-mount volume=designs,mount-path=/mnt/designs \
  --memory 2Gi \
  --task-timeout 3600s

# Run the job
gcloud run jobs execute tipcat-pipeline --region us-central1
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `GEMINI_API_KEY` | Google AI API key |
| `GEMINI_MODEL` | Metadata model (default: `gemini-2.5-flash`) |
| `GEMINI_IMAGE_MODEL` | Image gen model (default: `gemini-3.1-flash-image-preview`) |
| `PRINTIFY_API_KEY` | Printify JWT token |
| `PRINTIFY_SHOP_ID` | Printify shop (default: `26630208`) |
| `SHOPIFY_STORE` | Shopify store domain |
| `SHOPIFY_CLIENT_ID` | Shopify OAuth client ID |
| `SHOPIFY_CLIENT_SECRET` | Shopify OAuth client secret |
| `SHOPIFY_API_VERSION` | Shopify API version (default: `2025-01`) |
| `GCS_BUCKET` | GCS bucket for images (default: `tipcat-product-designs`) |
| `GOOGLE_CLOUD_PROJECT` | GCP project ID (default: `tipcat-automation`) |
| `CSV_PATH` | Path to product CSV (default: `tipcat_phonecase_sheet_with_images.csv`) |
| `DESIGNS_DIR` | Path to design PNGs (default: `phonecases`) |
