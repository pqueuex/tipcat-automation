# 🎨 TipCat Pipeline Manager on Google Colab

**No setup required — run directly in your browser!**

## 🚀 Quick Start

### 1. Upload Notebook to GCS

From Cloud Shell:
```bash
gcloud storage cp TipCat_Pipeline_Manager.ipynb \
  gs://tipcat-product-designs/notebooks/TipCat_Pipeline_Manager.ipynb
```

### 2. Open in Google Colab

Click this link to open immediately:

✅ **[OPEN IN GOOGLE COLAB](https://colab.research.google.com/url=https://storage.googleapis.com/tipcat-product-designs/notebooks/TipCat_Pipeline_Manager.ipynb)**

Or copy-paste into browser:
```
https://colab.research.google.com/url=https://storage.googleapis.com/tipcat-product-designs/notebooks/TipCat_Pipeline_Manager.ipynb
```

### 3. Authenticate & Start Using

When notebook loads:
1. First code cell authenticates: `auth.authenticate_user()`
2. All subsequent cells have access to GCS
3. Use interactive widgets to manage pipeline

---

## 📋 What You Can Do

From the notebook:

✅ **Upload Designs**
- Drag-and-drop PNG files
- Auto-upload to GCS
- View upload progress

✅ **Browse Designs**  
- List all files in bucket
- See file sizes & dates
- One-click refresh

✅ **Generate Metadata**
- Run Gemini Vision API
- Pure image-based analysis
- Set limits for testing

✅ **Run Pipeline Steps**
- Execute on Cloud Run
- 4Gi memory, 2 CPU
- 3-hour timeout
- Real-time logs

✅ **View Results**
- Fetch metadata from GCS
- Preview generated titles, tags, descriptions
- Download to local

---

## 🔗 Colab Link for Sharing

Share this link with team members:

```
https://colab.research.google.com/url=https://storage.googleapis.com/tipcat-product-designs/notebooks/TipCat_Pipeline_Manager.ipynb
```

Everyone with this link can:
- Run notebooks collaboratively
- Upload/manage designs
- Execute pipeline steps
- View generated metadata

All data stored in shared GCS bucket.

---

## 🎯 Typical Workflow

```
1. Upload PNG → 2. Refresh → 3. Run Step 1 → 4. View Results → 5. Run Steps 2-5
```

Takes ~5 minutes for initial setup, then commands are just a few clicks.

---

## 📊 Example Session

**Time: 0:00** - Open Colab link
**Time: 0:30** - Upload 5 new designs 
**Time: 1:00** - Click "Run Step 1" with limit=5
**Time: 5:00** - Step 1 completes, view metadata
**Time: 5:30** - Run Step 2 (Printify mockups)
**Time: 30:00** - All steps complete

Total workflow: ~30 minutes for full pipeline on 5 designs

---

## 🛠️ Requirements

✅ Google account (free)  
✅ Access to `tipcat-product-designs` GCS bucket  
✅ GCP project: `tipcat-automation`  
✅ Service account with proper IAM roles  

The notebook handles everything else!

---

## 📞 Troubleshooting

**"Authentication failed"**
- Check your Google account has access to `tipcat-automation`
- Ask project owner to grant you Editor role

**"Can't see designs"**
- Run "Refresh Design List" cell
- Check GCS bucket: `gsutil ls gs://tipcat-product-designs/designs/`

**"Step stuck/failed"**
- Check Cloud Run logs in notebook output
- Increase step timeout if needed
- Verify Gemini API has quota available

---

**Created:** March 8, 2026  
**Type:** Google Colab Notebook  
**Status:** ✅ Production Ready
