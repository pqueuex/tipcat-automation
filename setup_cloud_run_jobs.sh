#!/bin/bash
# Setup script to create Cloud Run jobs for TipCat pipeline

set -e

PROJECT="tipcat-automation"
REGION="us-central1"
IMAGE="us-central1-docker.pkg.dev/tipcat-automation/pipeline/tipcat-pipeline:latest"

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${BLUE}=== TipCat Cloud Run Job Setup ===${NC}\n"

# Function to create a Cloud Run job
create_job() {
    local job_name=$1
    local config_name=$2
    
    echo -e "${YELLOW}Creating Cloud Run job: $job_name${NC}"
    echo "  Config: $config_name"
    echo "  Image: $IMAGE"
    echo ""
    
    # Create the job
    gcloud run jobs create "$job_name" \
        --image "$IMAGE" \
        --memory=4Gi \
        --cpu=2 \
        --task-timeout=10800 \
        --region="$REGION" \
        --project="$PROJECT" \
        --set-env-vars="GOOGLE_CLOUD_PROJECT=$PROJECT" \
        2>&1 || {
            # If job already exists, update it instead
            echo "  Job exists, updating..."
            gcloud run jobs update "$job_name" \
                --image "$IMAGE" \
                --memory=4Gi \
                --cpu=2 \
                --task-timeout=10800 \
                --region="$REGION" \
                --project="$PROJECT"
        }
    
    # Bind secrets
    echo "  Binding secrets..."
    gcloud run jobs update "$job_name" \
        --update-secrets=GEMINI_API_KEY=gemini-api-key:latest \
        --update-secrets=PRINTIFY_API_KEY=printify-api-key:latest \
        --update-secrets=TIPCAT_SHOPIFY_CLIENT_ID=tipcat-shopify-client-id:latest \
        --update-secrets=TIPCAT_SHOPIFY_CLIENT_SECRET=tipcat-shopify-client-secret:latest \
        --region="$REGION" \
        --project="$PROJECT"
    
    echo -e "${GREEN}✓ Job ready: $job_name${NC}\n"
}

# Create phone cases job
create_job "tipcat-phonecases-pipeline" "tipcat-phonecases"

# Create mouse pads job (optional)
read -p "Also create tipcat-mousepads-pipeline? (y/n) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    create_job "tipcat-mousepads-pipeline" "tipcat-mousepads"
fi

echo -e "${GREEN}=== Setup Complete ===${NC}"
echo ""
echo "Test execution:"
echo "  gcloud run jobs execute tipcat-phonecases-pipeline \\"
echo "    --region=$REGION \\"
echo "    --project=$PROJECT \\"
echo "    --args=\"--config=tipcat-phonecases,--step=1,--limit=1,--verbose\""
