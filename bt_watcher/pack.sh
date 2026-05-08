#!/bin/bash

set -e

VERSION="0.1.0"
PACKAGE_NAME="bt_watcher"
DIST_DIR="$(pwd)/dist"
BUILD_DIR="$(pwd)/build_standalone"
TARBALL="${DIST_DIR}/${PACKAGE_NAME}-${VERSION}.tar.gz"

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${GREEN}=== Packing ${PACKAGE_NAME} v${VERSION} ===${NC}"

# Clean previous builds
rm -rf "${BUILD_DIR}" "${DIST_DIR}"
mkdir -p "${BUILD_DIR}/${PACKAGE_NAME}" "${DIST_DIR}"

# Copy core files
echo -e "${YELLOW}Copying source files...${NC}"
cp "${PACKAGE_NAME}/bt_watcher.py" "${BUILD_DIR}/${PACKAGE_NAME}/"
cp "bt_watcher.service" "${BUILD_DIR}/"
cp "install_service.sh" "${BUILD_DIR}/"
cp "ReadMe.md" "${BUILD_DIR}/"

# Create requirements.txt (pure Python deps, no ROS)
cat > "${BUILD_DIR}/requirements.txt" << 'EOF'
aiomqtt>=2.0.0
bleak>=0.21.0
crcmod>=1.7
EOF

# Create a standalone setup.py (no ROS/ament)
cat > "${BUILD_DIR}/setup.py" << 'EOF'
from setuptools import setup, find_packages

setup(
    name='bt_watcher',
    version='0.1.0',
    packages=find_packages(),
    python_requires='>=3.10',
    install_requires=[
        'aiomqtt>=2.0.0',
        'bleak>=0.21.0',
        'crcmod>=1.7',
    ],
    zip_safe=True,
    entry_points={
        'console_scripts': [
            'bt_watcher = bt_watcher.bt_watcher:main',
        ],
    },
)
EOF

# Create MANIFEST.in to include all needed files
cat > "${BUILD_DIR}/MANIFEST.in" << 'EOF'
include requirements.txt
include ReadMe.md
include bt_watcher.service
include install_service.sh
recursive-include bt_watcher *.py
EOF

# Create an empty __init__.py if not present
if [ ! -f "${BUILD_DIR}/${PACKAGE_NAME}/__init__.py" ]; then
    touch "${BUILD_DIR}/${PACKAGE_NAME}/__init__.py"
fi

# Create tarball
echo -e "${YELLOW}Creating tarball...${NC}"
tar -czf "${TARBALL}" -C "${BUILD_DIR}" .

# Show result
SIZE=$(du -h "${TARBALL}" | cut -f1)
echo -e "${GREEN}✓ Package created: ${TARBALL} (${SIZE})${NC}"
echo -e ""
echo -e "${YELLOW}Package contents:${NC}"
tar -tzf "${TARBALL}" | sort

# Cleanup build dir
rm -rf "${BUILD_DIR}"

echo -e ""
echo -e "${YELLOW}Usage on target machine:${NC}"
echo -e "  1. tar xzf ${PACKAGE_NAME}-${VERSION}.tar.gz"
echo -e "  2. cd ${PACKAGE_NAME}-${VERSION}"
echo -e "  3. pip3 install -r requirements.txt"
echo -e "  4. sudo ./install_service.sh"
echo -e ""
echo -e "  Or run directly:"
echo -e "  python3 -m bt_watcher.bt_watcher"
