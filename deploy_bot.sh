#!/bin/bash
# Deploy the Telegram bot to Cloud Run and register the Telegram webhook.
# Usage: ./deploy_bot.sh [--skip-build]

set -e

PROJECT="tipcat-automation"
REGION="us-central1"
SERVICE="tipcat-bot"
IMAGE="us-central1-docker.pkg.dev/$PROJECT/pipeline/$SERVICE:latest"

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${BLUE}=== TipCat Bot Deploy ===${NC}\n"

# ── Step 1: Build & push image ───────────────────────────────────────────────
if [[ "$1" != "--skip-build" ]]; then
    echo -e "${YELLOW}Building bot image...${NC}"
    gcloud builds submit . \
        --project="$PROJECT" \
        --config=- <<EOF
steps:
- name: 'gcr.io/cloud-builders/docker'
  args: ['build', '-f', 'Dockerfile.bot', '-t', '$IMAGE', '.']
- name: 'gcr.io/cloud-builders/docker'
  args: ['push', '$IMAGE']
EOF
    echo -e "${GREEN}✓ Image pushed: $IMAGE${NC}\n"
else
    echo -e "${YELLOW}Skipping build (--skip-build)${NC}\n"
fi

# ── Step 2: Deploy to Cloud Run ──────────────────────────────────────────────
echo -e "${YELLOW}Deploying to Cloud Run...${NC}"
gcloud run services update "$SERVICE" \
    --image="$IMAGE" \
    --region="$REGION" \
    --project="$PROJECT"
echo -e "${GREEN}✓ Service deployed${NC}\n"

# ── Step 3: Register Telegram webhook ────────────────────────────────────────
echo -e "${YELLOW}Registering Telegram webhook...${NC}"

BOT_TOKEN=$(gcloud secrets versions access latest \
    --secret=TELEGRAM_BOT_TOKEN --project="$PROJECT" 2>/dev/null)
WEBHOOK_SECRET=$(gcloud secrets versions access latest \
    --secret=TELEGRAM_WEBHOOK_SECRET --project="$PROJECT" 2>/dev/null)
SERVICE_URL=$(gcloud run services describe "$SERVICE" \
    --region="$REGION" --project="$PROJECT" \
    --format="value(status.url)")
WEBHOOK_URL="${SERVICE_URL}/webhook"

if [[ -z "$BOT_TOKEN" ]]; then
    echo -e "${RED}ERROR: TELEGRAM_BOT_TOKEN secret not found${NC}"
    exit 1
fi

PAYLOAD="url=${WEBHOOK_URL}&allowed_updates=[\"message\",\"callback_query\"]"
if [[ -n "$WEBHOOK_SECRET" ]]; then
    PAYLOAD="${PAYLOAD}&secret_token=${WEBHOOK_SECRET}"
fi

RESULT=$(curl -s "https://api.telegram.org/bot${BOT_TOKEN}/setWebhook" -d "$PAYLOAD")
OK=$(echo "$RESULT" | python3 -c "import json,sys; print(json.load(sys.stdin).get('ok',''))" 2>/dev/null)

if [[ "$OK" == "True" ]]; then
    echo -e "${GREEN}✓ Webhook set: $WEBHOOK_URL${NC}"
else
    echo -e "${RED}ERROR setting webhook: $RESULT${NC}"
    exit 1
fi

# ── Done ─────────────────────────────────────────────────────────────────────
echo -e "\n${GREEN}=== Deploy complete ===${NC}"
echo "Service URL: $SERVICE_URL"
echo "Webhook:     $WEBHOOK_URL"

WEBHOOK_INFO=$(curl -s "https://api.telegram.org/bot${BOT_TOKEN}/getWebhookInfo")
PENDING=$(echo "$WEBHOOK_INFO" | python3 -c "import json,sys; print(json.load(sys.stdin).get('result',{}).get('pending_update_count',0))" 2>/dev/null)
echo "Pending updates: $PENDING"
