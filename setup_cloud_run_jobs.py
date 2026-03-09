#!/usr/bin/env python3
"""
Setup script to create Cloud Run jobs for TipCat pipeline.
Requires: gcloud CLI installed and authenticated.
"""

import subprocess
import sys
from pathlib import Path


class CloudRunSetup:
    def __init__(self):
        self.project = "tipcat-automation"
        self.region = "us-central1"
        self.image = "us-central1-docker.pkg.dev/tipcat-automation/pipeline/tipcat-pipeline:latest"
        
    def run_command(self, cmd, description=""):
        """Execute a gcloud command."""
        if description:
            print(f"\n📋 {description}")
            print(f"   $ {' '.join(cmd)}\n")
        
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if result.returncode == 0:
                print(f"✅ Success")
                if result.stdout:
                    print(result.stdout[:500])
                return True
            else:
                print(f"❌ Error: {result.stderr[:500]}")
                return False
        except FileNotFoundError:
            print(f"\n❌ Error: 'gcloud' CLI not found.")
            print("   Install gcloud: https://cloud.google.com/sdk/docs/install")
            sys.exit(1)
    
    def create_phone_cases_job(self):
        """Create tipcat-phonecases-pipeline Cloud Run job."""
        job_name = "tipcat-phonecases-pipeline"
        config_name = "tipcat-phonecases"
        
        print(f"\n{'='*60}")
        print(f"Creating Cloud Run Job: {job_name}")
        print(f"{'='*60}")
        
        # Step 1: Create or update the job
        create_cmd = [
            "gcloud", "run", "jobs", "create", job_name,
            f"--image={self.image}",
            "--memory=4Gi",
            "--cpu=2",
            "--task-timeout=10800",
            f"--region={self.region}",
            f"--project={self.project}",
            f"--set-env-vars=GOOGLE_CLOUD_PROJECT={self.project}"
        ]
        
        success = self.run_command(
            create_cmd,
            f"Creating Cloud Run job '{job_name}'"
        )
        
        if not success:
            # Try updating if job already exists
            print("   Job might already exist, trying update...")
            update_cmd = [
                "gcloud", "run", "jobs", "update", job_name,
                f"--image={self.image}",
                "--memory=4Gi",
                "--cpu=2",
                "--task-timeout=10800",
                f"--region={self.region}",
                f"--project={self.project}"
            ]
            self.run_command(update_cmd, "Updating existing Cloud Run job")
        
        # Step 2: Bind secrets
        secrets_cmd = [
            "gcloud", "run", "jobs", "update", job_name,
            "--update-secrets=GEMINI_API_KEY=gemini-api-key:latest",
            "--update-secrets=PRINTIFY_API_KEY=printify-api-key:latest",
            "--update-secrets=TIPCAT_SHOPIFY_CLIENT_ID=tipcat-shopify-client-id:latest",
            "--update-secrets=TIPCAT_SHOPIFY_CLIENT_SECRET=tipcat-shopify-client-secret:latest",
            f"--region={self.region}",
            f"--project={self.project}"
        ]
        
        self.run_command(secrets_cmd, "Binding secrets to Cloud Run job")
        
        print(f"\n✅ Cloud Run job '{job_name}' is ready!")
        print(f"\nTest execution:")
        print(f"  gcloud run jobs execute {job_name} \\")
        print(f"    --region={self.region} \\")
        print(f"    --project={self.project} \\")
        print(f"    --args=\"--config={config_name},--step=1,--limit=1,--verbose\"")
    
    def create_mouse_pads_job(self):
        """Create tipcat-mousepads-pipeline Cloud Run job."""
        job_name = "tipcat-mousepads-pipeline"
        config_name = "tipcat-mousepads"
        
        print(f"\n{'='*60}")
        print(f"Creating Cloud Run Job: {job_name}")
        print(f"{'='*60}")
        
        # Create or update the job
        create_cmd = [
            "gcloud", "run", "jobs", "create", job_name,
            f"--image={self.image}",
            "--memory=4Gi",
            "--cpu=2",
            "--task-timeout=10800",
            f"--region={self.region}",
            f"--project={self.project}",
            f"--set-env-vars=GOOGLE_CLOUD_PROJECT={self.project}"
        ]
        
        success = self.run_command(
            create_cmd,
            f"Creating Cloud Run job '{job_name}'"
        )
        
        if not success:
            print("   Job might already exist, trying update...")
            update_cmd = [
                "gcloud", "run", "jobs", "update", job_name,
                f"--image={self.image}",
                "--memory=4Gi",
                "--cpu=2",
                "--task-timeout=10800",
                f"--region={self.region}",
                f"--project={self.project}"
            ]
            self.run_command(update_cmd, "Updating existing Cloud Run job")
        
        # Bind secrets
        secrets_cmd = [
            "gcloud", "run", "jobs", "update", job_name,
            "--update-secrets=GEMINI_API_KEY=gemini-api-key:latest",
            "--update-secrets=PRINTIFY_API_KEY=printify-api-key:latest",
            "--update-secrets=TIPCAT_SHOPIFY_CLIENT_ID=tipcat-shopify-client-id:latest",
            "--update-secrets=TIPCAT_SHOPIFY_CLIENT_SECRET=tipcat-shopify-client-secret:latest",
            f"--region={self.region}",
            f"--project={self.project}"
        ]
        
        self.run_command(secrets_cmd, "Binding secrets to Cloud Run job")
        
        print(f"\n✅ Cloud Run job '{job_name}' is ready!")
        print(f"\nTest execution:")
        print(f"  gcloud run jobs execute {job_name} \\")
        print(f"    --region={self.region} \\")
        print(f"    --project={self.project} \\")
        print(f"    --args=\"--config={config_name},--step=1,--limit=1,--verbose\"")


if __name__ == "__main__":
    setup = CloudRunSetup()
    
    print("\n🚀 TipCat Cloud Run Job Setup")
    print("=" * 60)
    
    # Create phone cases job
    setup.create_phone_cases_job()
    
    # Ask about mouse pads
    try:
        response = input("\n❓ Also create tipcat-mousepads-pipeline? (y/n): ").lower().strip()
        if response in ['y', 'yes']:
            setup.create_mouse_pads_job()
    except KeyboardInterrupt:
        print("\n\nSetup interrupted.")
        sys.exit(0)
    
    print("\n" + "=" * 60)
    print("✅ Cloud Run job setup complete!")
    print("\nNext steps:")
    print("  1. Test in Colab: https://colab.research.google.com/github/pqueuex/tipcat-automation/blob/main/TipCat_Pipeline_Manager_phonecases.ipynb")
    print("  2. Run Cell 1-2 to authenticate and list designs")
    print("  3. Run Cell 4 to execute the pipeline via Cloud Run")
