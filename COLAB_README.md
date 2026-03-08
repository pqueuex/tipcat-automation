# 🎨 TipCat Pipeline Manager on Google Colab

**No setup required — run directly in your browser!**

Each product type has its own dedicated notebook with isolated GCS bucket, Cloud Run job, and configuration.

## 🚀 Quick Start

### Choose Your Product Type

#### 📱 Phone Cases
✅ **[OPEN PHONE CASES NOTEBOOK](https://colab.research.google.com/github/pqueuex/tipcat-automation/blob/main/TipCat_Pipeline_Manager_phonecases.ipynb)**

- **Bucket:** `tipcat-product-designs`
- **Config:** `tipcat-phonecases`
- **Products:** iPhone 11, 16, 16 Pro Max, 17 Air, 17 Pro Max
- **Job:** `tipcat-phonecases-pipeline`

```
https://colab.research.google.com/github/pqueuex/tipcat-automation/blob/main/TipCat_Pipeline_Manager_phonecases.ipynb
```

#### 🖱️ Mouse Pads
✅ **[OPEN MOUSE PADS NOTEBOOK](https://colab.research.google.com/github/pqueuex/tipcat-automation/blob/main/TipCat_Pipeline_Manager_mousepads.ipynb)**

- **Bucket:** `tipcat-mousepads`
- **Config:** `tipcat-mousepads`
- **Products:** Standard (250x210mm), Large (350x260mm), XL (400x300mm)
- **Job:** `tipcat-mousepads-pipeline`

```
https://colab.research.google.com/github/pqueuex/tipcat-automation/blob/main/TipCat_Pipeline_Manager_mousepads.ipynb
```

### Authenticate & Start Using

When notebook loads:
1. Run Cell 1 to initialize GCS connection
2. Cell 2 auto-scans for designs in your bucket
3. Cell 3 runs `refresh_inventory()` to update product list JSON
4. Cell 4 provides `run_step()` to execute pipeline steps
5. Cell 5 displays generated metadata

---

## 📋 What You Can Do

From the notebook:

## 📋 What You Can Do

From the notebook (product-specific):

✅ **List Designs**
- Scan GCS bucket for PNG files
- Auto-discover new designs
- Display file paths

✅ **Refresh Inventory**
- Generate `product_list.json` with all designs
- Auto-assign SKU numbers (1-N)
- Track design status (pending/processing/complete)

✅ **Run Pipeline Steps**
- Execute via Cloud Run job
- Config-driven (product type, store, variants)
- Limit designs for testing
- Real-time command output

✅ **View Generated Metadata**
- Display metadata from Step 1
- Show titles, tags, descriptions
- Verify Gemini output quality

---

## 🔗 Colab Links for Sharing

**Phone Cases:**
```
https://colab.research.google.com/github/pqueuex/tipcat-automation/blob/main/TipCat_Pipeline_Manager_phonecases.ipynb
```

**Mouse Pads:**
```
https://colab.research.google.com/github/pqueuex/tipcat-automation/blob/main/TipCat_Pipeline_Manager_mousepads.ipynb
```

Everyone with these links can:
- Run notebooks collaboratively
- Upload/manage designs to the correct bucket
- Execute pipeline steps for that product type
- View generated metadata

Each product type uses its own isolated GCS bucket and configuration.

---

## 🎯 Typical Workflow

**Phone Cases:**
```
1. Open phone cases Colab link
2. Run Cell 1 (initialize GCS connection)
3. Run Cell 2 (list designs from gs://tipcat-product-designs/)
4. Run Cell 3 (refresh product inventory)
5. Run Cell 4 (execute Step 1 with limit=5)
6. Run Cell 5 (view generated metadata)
7. Repeat Cell 4 for Steps 2-5
```

**Mouse Pads:**
```
1. Open mouse pads Colab link
2. Run Cell 1 (initialize GCS connection to gs://tipcat-mousepads/)
3. Follow same workflow as phone cases
4. Outputs stored in separate bucket
```

Takes ~30 seconds to initialize, then each step execution is a single cell run.

---

## 📊 Example Session (Phone Cases)

**Time: 0:00** - Open [Phone Cases Colab link](https://colab.research.google.com/github/pqueuex/tipcat-automation/blob/main/TipCat_Pipeline_Manager_phonecases.ipynb)
**Time: 0:30** - Run Cells 1-3 (initialize, list designs, refresh inventory)
**Time: 1:00** - Run Cell 4: `run_step(step=1, limit=5)` (metadata generation)
**Time: 5:00** - Step 1 completes, run Cell 5 to view metadata
**Time: 5:30** - Run Step 2 (Printify mockups): `run_step(step=2, limit=5)`
**Time: 35:00** - All 5 steps complete for 5 designs

Total workflow: ~35 minutes for full pipeline on 5 designs

---

## 🛠️ Requirements

✅ Google account (free)  
✅ Access to GCP project: `tipcat-automation`  
✅ IAM permissions:
   - `storage.objectViewer` on design buckets
   - `run.jobs.run` on Cloud Run jobs
   - `logging.viewer` for job logs

The notebooks handle GCS authentication automatically in Colab!

---

## 📞 Troubleshooting

**"Permission denied" on GCS bucket**
- Verify you have access to the correct bucket:
  - Phone cases: `gs://tipcat-product-designs/`
  - Mouse pads: `gs://tipcat-mousepads/`
- Ask project owner to grant you `storage.objectViewer` role

**"Can't see designs"**
- Upload designs to bucket first:
  ```bash
  gsutil cp designs/*.png gs://tipcat-product-designs/designs/
  ```
- Run Cell 2 to refresh design list

**"Cloud Run job not found"**
- Verify job exists:
  ```bash
  gcloud run jobs describe tipcat-phonecases-pipeline --region=us-central1
  ```
- Check you're using the correct notebook for your product type

**"Config not found"**
- Ensure config files are committed to GitHub:
  - `configs/tipcat-phonecases.json`
  - `configs/tipcat-mousepads.json`
- Pull latest from main branch

**"Step stuck/failed"**
- Check Cloud Run logs in notebook output
- Increase step timeout if needed
- Verify Gemini API has quota available

---

**Created:** March 8, 2026  
**Type:** Google Colab Notebook  
**Status:** ✅ Production Ready
