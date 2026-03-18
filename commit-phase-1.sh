#!/bin/bash

# Phase 1 Complete - Git Commit & Push Script
# Run this from project root: bash commit-phase-1.sh

set -e  # Exit on error

echo "=========================================="
echo "  Phase 1 Complete - Commit & Push"
echo "=========================================="
echo ""

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Verify we're in the right directory
if [ ! -d "Backend" ]; then
    echo "❌ Error: Backend directory not found. Are you in the project root?"
    exit 1
fi

echo -e "${BLUE}Current location:${NC}"
pwd
echo ""

# Step 1: Backend (Submodule)
echo -e "${YELLOW}=========================================="
echo "  STEP 1: Committing Backend (Submodule)"
echo -e "==========================================${NC}"
echo ""

cd Backend

echo -e "${BLUE}Backend status:${NC}"
git status
echo ""

echo -e "${BLUE}Staging Backend files...${NC}"
git add requirements.txt app.py blueprints/auth_api_bp.py

echo -e "${BLUE}Committing Backend changes...${NC}"
git commit -m "feat: Phase 1 - CORS + REST API authentication

- Add Flask-CORS==5.0.0 for frontend communication
- Create auth_api_bp.py with 5 REST endpoints
  * GET /api/auth/login - Initiate OAuth
  * GET /api/auth/callback - OAuth callback handler
  * GET /api/auth/me - Get user info (requires auth)
  * GET /api/auth/check - Check auth status
  * POST /api/auth/logout - Logout
- Update app.py to enable CORS and register blueprint
- Maintain backward compatibility with existing routes

Tested and verified:
- ✅ /api/auth/check returns correct JSON
- ✅ /api/auth/login returns OAuth URL
- ✅ /api/auth/me returns 401 when unauthenticated
- ✅ CORS configured for localhost:4200, localhost:8080
- ✅ Session management working

Ready for Angular frontend integration."

echo ""
echo -e "${BLUE}Pushing Backend to remote...${NC}"
git push origin refactor/modular-architecture

echo ""
echo -e "${GREEN}✅ Backend pushed successfully!${NC}"
echo ""
echo -e "${BLUE}Latest Backend commit:${NC}"
git log --oneline -1
echo ""

cd ..

# Step 2: Main Repo
echo -e "${YELLOW}=========================================="
echo "  STEP 2: Committing Main Repo"
echo -e "==========================================${NC}"
echo ""

echo -e "${BLUE}Main repo status:${NC}"
git status
echo ""

echo -e "${BLUE}Staging main repo files...${NC}"
git add PHASE_1_*.md GIT_*.md

echo -e "${BLUE}Committing main repo changes...${NC}"
git commit -m "docs: Phase 1 complete - Backend API authentication

Phase 1 Implementation Documentation:
- PHASE_1_DONE.md - Executive summary
- PHASE_1_COMPLETE.md - Full implementation guide
- PHASE_1_QUICK_START.md - Quick reference
- PHASE_1_ARCHITECTURE_DIAGRAM.md - System design
- PHASE_1_IMPLEMENTATION_SUMMARY.md - Change summary
- PHASE_1_VERIFICATION_CHECKLIST.md - Testing guide
- PHASE_1_DOCUMENTATION_INDEX.md - Documentation index
- PHASE_1_VISUAL_SUMMARY.md - Visual diagrams
- PHASE_1_TEST_RESULTS.md - Test verification ✅

Git Submodule Workflow Documentation:
- GIT_CHEAT_SHEET.md - Quick reference
- GIT_ANSWER_SUMMARY.md - Direct answer
- GIT_QUICK_REFERENCE.md - One-pager
- GIT_SUBMODULE_WORKFLOW.md - Comprehensive guide
- GIT_SUBMODULE_COMPLETE_ANSWER.md - Full explanation
- GIT_WORKFLOW_VISUAL.md - Visual diagrams
- GIT_COMMANDS_COPYPASTE.md - Copy-paste scripts
- GIT_SUBMODULE_COMPLETE_INDEX.md - Navigation guide
- GIT_GUIDES_README.md - Overview

Backend Changes (in submodule):
- Added Flask-CORS support
- Created 5 REST API authentication endpoints
- Enabled CORS for Angular integration
- All endpoints tested and verified ✅

Phase 1 Status: COMPLETE & VERIFIED ✅
Ready for Phase 2: Angular frontend integration"

echo ""
echo -e "${BLUE}Pushing main repo to remote...${NC}"
git push origin dev/front_back_split

echo ""
echo -e "${GREEN}✅ Main repo pushed successfully!${NC}"
echo ""
echo -e "${BLUE}Latest main repo commit:${NC}"
git log --oneline -1
echo ""

# Verification
echo -e "${YELLOW}=========================================="
echo "  VERIFICATION"
echo -e "==========================================${NC}"
echo ""

echo -e "${BLUE}Backend latest commits:${NC}"
git -C Backend log --oneline -3
echo ""

echo -e "${BLUE}Main repo latest commits:${NC}"
git log --oneline -3
echo ""

echo -e "${GREEN}=========================================="
echo "  ✅ ALL DONE!"
echo -e "==========================================${NC}"
echo ""
echo "Phase 1 has been committed and pushed to GitHub."
echo ""
echo "Next steps:"
echo "  1. Verify commits on GitHub"
echo "  2. Review PHASE_1_TEST_RESULTS.md"
echo "  3. Proceed to Phase 2 (Angular integration)"
echo ""
echo "🚀 Backend API authentication is ready!"
