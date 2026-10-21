#!/bin/bash
# Setup supply chain security for local development
#
# This script configures authentication for:
#   1. Google Assured OSS (SLSA Level 3 packages)
#   2. Internal Artifact Registry (organization packages)
#   3. Infisical CLI (secrets management)
#
# Usage: ./scripts/setup-supply-chain.sh
#
set -e

# Constants
readonly SEPARATOR="=============================================="

echo "$SEPARATOR"
echo "  Supply Chain Security Setup"
echo "$SEPARATOR"
echo ""

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Check for gcloud CLI
echo "Checking prerequisites..."
if ! command -v gcloud &> /dev/null; then
    echo -e "${RED}ERROR: gcloud CLI not found${NC}" >&2
    echo "" >&2
    echo "Install the Google Cloud SDK from:" >&2
    echo "  https://cloud.google.com/sdk/docs/install" >&2
    echo "" >&2
    exit 1
fi
echo -e "  ${GREEN}✓${NC} gcloud CLI found"

# Check for uv
if ! command -v uv &> /dev/null; then
    echo -e "${YELLOW}WARNING: uv not found - install with: curl -LsSf https://astral.sh/uv/install.sh | sh${NC}"
else
    echo -e "  ${GREEN}✓${NC} uv found"
fi

echo ""
echo "Step 1: Authenticating to Google Cloud..."
echo "----------------------------------------"
echo "This will open a browser for authentication."
echo ""

# Check if already authenticated
if gcloud auth list --filter=status:ACTIVE --format="value(account)" 2>/dev/null | grep -q "@"; then
    CURRENT_ACCOUNT=$(gcloud auth list --filter=status:ACTIVE --format="value(account)" 2>/dev/null | head -1)
    echo -e "  ${GREEN}✓${NC} Already authenticated as: $CURRENT_ACCOUNT"
    read -p "  Re-authenticate? (y/N): " REAUTH
    if [[ "$REAUTH" =~ ^[Yy]$ ]]; then
        gcloud auth login
    fi
else
    gcloud auth login
fi

echo ""
echo "Step 2: Setting up Application Default Credentials..."
echo "-----------------------------------------------------"
echo "This allows UV to authenticate with Artifact Registry."
echo ""

# Check if ADC already exists
if [[ -f "$HOME/.config/gcloud/application_default_credentials.json" ]]; then
    echo -e "  ${GREEN}✓${NC} Application Default Credentials already exist"
    read -p "  Refresh credentials? (y/N): " REFRESH
    if [[ "$REFRESH" =~ ^[Yy]$ ]]; then
        gcloud auth application-default login
    fi
else
    gcloud auth application-default login
fi

echo ""
echo "Step 3: Installing Artifact Registry keyring..."
echo "------------------------------------------------"
pip install --quiet --only-binary :all: keyrings.google-artifactregistry-auth
echo -e "  ${GREEN}✓${NC} Keyring installed"

echo ""
echo "Step 4: Verifying access to package indexes..."
echo "-----------------------------------------------"

# Test Assured OSS access
echo -n "  Testing Assured OSS... "
if pip index versions numpy --index-url https://us-python.pkg.dev/cloud-aoss/cloud-aoss-python/simple 2>/dev/null | grep -q "numpy"; then
    echo -e "${GREEN}OK${NC}"
else
    echo -e "${YELLOW}UNAVAILABLE${NC} (may require GCP project setup)"
fi

# Test internal registry access (will fail if no packages exist yet)
echo -n "  Testing Internal Registry... "
INTERNAL_URL="https://us-central1-python.pkg.dev/assured-oss-457903/python-libs/simple"
if pip index versions test-package --index-url "$INTERNAL_URL" 2>/dev/null; then
    echo -e "${GREEN}OK${NC}"
else
    echo -e "${YELLOW}NO PACKAGES${NC} (registry may be empty)"
fi

echo ""
echo "Step 5: Infisical CLI (Optional)..."
echo "------------------------------------"

if command -v infisical &> /dev/null; then
    echo -e "  ${GREEN}✓${NC} Infisical CLI already installed"
    echo ""
    echo "  To connect to your project:"
    echo "    infisical login"
    echo "    infisical init"
else
    echo "  Infisical CLI not found."
    echo ""
    echo "  To install (optional - for local secrets management):"
    echo ""
    echo "  # macOS"
    echo "  brew install infisical/get-cli/infisical"
    echo ""
    echo "  # Linux (Debian/Ubuntu)"
    echo "  curl -1sLf 'https://dl.cloudsmith.io/public/infisical/infisical-cli/setup.deb.sh' | sudo -E bash"
    echo "  sudo apt-get install infisical"
    echo ""
    echo "  # Windows"
    echo "  scoop bucket add infisical https://github.com/Infisical/scoop-infisical.git"
    echo "  scoop install infisical"
fi

echo ""
echo "$SEPARATOR"
echo -e "  ${GREEN}Setup Complete!${NC}"
echo "$SEPARATOR"
echo ""
echo "Next steps:"
echo "  1. Run 'uv sync' to install dependencies from secure indexes"
echo "  2. For secrets, use 'infisical run -- <command>' or set up .env"
echo ""
echo "Package index priority:"
echo "  1. Assured OSS (Google-verified, SLSA Level 3)"
echo "  2. Internal Registry (python-libs)"
echo "  3. PyPI (fallback)"
echo ""
