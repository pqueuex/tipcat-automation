#!/usr/bin/env python3
"""
Generate Colab link and setup instructions (no GCP auth needed locally)
"""
import json
from pathlib import Path

# Configuration
GCS_BUCKET = "tipcat-product-designs"
GCP_PROJECT = "tipcat-automation"
NOTEBOOK_PATH = "/Users/jj/Business/ECommerce/TipCat_Pipeline_Manager.ipynb"

# Generate Colab link
https_path = f"https://storage.googleapis.com/{GCS_BUCKET}/notebooks/TipCat_Pipeline_Manager.ipynb"
colab_link = f"https://colab.research.google.com/url={https_path}"

print("\n" + "="*70)
print("🎉 GOOGLE COLAB SETUP")
print("="*70)

print(f"\n📓 Notebook URL: {https_path}")
print(f"\n🔗 COLAB LINK:   {colab_link}")

print("\n" + "="*70)
print("NEXT STEPS:")
print("="*70)

instructions = f"""
1. 📤 Run from Cloud Shell to upload notebook to GCS:
   
   gcloud storage cp \\
     /Users/jj/Business/ECommerce/TipCat_Pipeline_Manager.ipynb \\
     gs://{GCS_BUCKET}/notebooks/TipCat_Pipeline_Manager.ipynb

2. 🔗 Copy the Colab link:
   
   {colab_link}

3. 🌐 Paste into your browser OR click:
   
   [CLICK HERE TO OPEN IN COLAB]({colab_link})

4. ✅ In Colab, run cells in order:
   - Cell 1: Setup & Configuration (authenticates with GCP)
   - Cell 2: Connect to GCS
   - Cell 3+: Use widgets to manage designs and run pipeline

5. 🚀 That's it! No local setup needed.

---

📝 SHARING WITH TEAM:

Send them the Colab link to collaborate in real-time:

{colab_link}

Everyone can:
✓ Upload designs
✓ Run pipeline steps  
✓ View results
✓ Monitor execution

All data stays in GCS (shared bucket)

---

💡 COLAB TIPS:

• Free GPU/TPU available if needed
• Cells run in shared kernel (state persists)
• Can take screenshots of widgets
• Download results locally if needed
• No credit card required for free tier

"""

print(instructions)

# Save Colab info
colab_info = {
    "colab_link": colab_link,
    "notebook_url": https_path,
    "bucket": GCS_BUCKET,
    "project": GCP_PROJECT,
    "instructions": f"""
1. From Cloud Shell (or local gcloud):
   gcloud storage cp TipCat_Pipeline_Manager.ipynb \\
     gs://{GCS_BUCKET}/notebooks/

2. Open Colab link:
   {colab_link}

3. Run all cells (authenticate on first cell)

4. Use interactive widgets to manage pipeline
"""
}

with open("/Users/jj/Business/ECommerce/COLAB_LINK.txt", "w") as f:
    f.write(f"Colab Link:\n{colab_link}\n\n")
    f.write(f"Notebook URL:\n{https_path}\n\n")
    f.write(f"Setup Command:\n")
    f.write(f"gcloud storage cp TipCat_Pipeline_Manager.ipynb gs://{GCS_BUCKET}/notebooks/\n")

print("\n✓ Colab link saved to: /Users/jj/Business/ECommerce/COLAB_LINK.txt")

# Create markdown README
readme = f"""# 🎨 TipCat Pipeline Manager on Google Colab

**No setup required — run directly in your browser!**

## 🚀 Quick Start

### 1. Upload Notebook to GCS

From Cloud Shell:
```bash
gcloud storage cp TipCat_Pipeline_Manager.ipynb \\
  gs://{GCS_BUCKET}/notebooks/TipCat_Pipeline_Manager.ipynb
```

### 2. Open in Google Colab

Click this link to open immediately:

✅ **[OPEN IN GOOGLE COLAB]({colab_link})**

Or copy-paste into browser:
```
{colab_link}
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
{colab_link}
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
✅ Access to `{GCS_BUCKET}` GCS bucket  
✅ GCP project: `{GCP_PROJECT}`  
✅ Service account with proper IAM roles  

The notebook handles everything else!

---

## 📞 Troubleshooting

**"Authentication failed"**
- Check your Google account has access to `{GCP_PROJECT}`
- Ask project owner to grant you Editor role

**"Can't see designs"**
- Run "Refresh Design List" cell
- Check GCS bucket: `gsutil ls gs://{GCS_BUCKET}/designs/`

**"Step stuck/failed"**
- Check Cloud Run logs in notebook output
- Increase step timeout if needed
- Verify Gemini API has quota available

---

**Created:** March 8, 2026  
**Type:** Google Colab Notebook  
**Status:** ✅ Production Ready
"""

with open("/Users/jj/Business/ECommerce/COLAB_README.md", "w") as f:
    f.write(readme)

print("✓ README saved to: /Users/jj/Business/ECommerce/COLAB_README.md")

print("\n" + "="*70)
print("🎉 ALL SET!")
print("="*70)
print("\nNext: Run this from Cloud Shell:")
print(f"gcloud storage cp TipCat_Pipeline_Manager.ipynb gs://{GCS_BUCKET}/notebooks/")
print(f"\nThen open: {colab_link}")

