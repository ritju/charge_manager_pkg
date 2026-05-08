#!/bin/bash

set -e

SERVICE_NAME="bt_watcher"
SERVICE_FILE="${SERVICE_NAME}.service"
SERVICE_SOURCE="$(pwd)/${SERVICE_FILE}"
SERVICE_DEST="/etc/systemd/system/${SERVICE_FILE}"
RUN_USER="${SUDO_USER:-${USER:-$(whoami)}}"
RUN_HOME="$(eval echo ~${RUN_USER})"
LOG_DIR="${RUN_HOME}/.local/share/bt_watcher/logs"
PYTHON_SCRIPT="$(pwd)/bt_watcher/bt_watcher.py"

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}Installing ${SERVICE_NAME} service...${NC}"

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}Error: This script must be run as root (use sudo)${NC}"
    exit 1
fi

# Check if service file exists
if [ ! -f "${SERVICE_SOURCE}" ]; then
    echo -e "${RED}Error: ${SERVICE_FILE} not found in current directory${NC}"
    exit 1
fi

# Check if Python script exists
if [ ! -f "${PYTHON_SCRIPT}" ]; then
    echo -e "${RED}Error: bt_watcher.py not found at ${PYTHON_SCRIPT}${NC}"
    exit 1
fi

# Check if Python3 is installed
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}Error: python3 is not installed${NC}"
    exit 1
fi

# Check if required Python packages are installed (as target user)
echo -e "${YELLOW}Checking required Python packages...${NC}"
REQUIRED_PACKAGES=("aiomqtt" "bleak" "crcmod")
for pkg in "${REQUIRED_PACKAGES[@]}"; do
    if ! su - "${RUN_USER}" -c "python3 -c 'import ${pkg//-/_}'" 2>/dev/null; then
        echo -e "${YELLOW}Installing ${pkg} for ${RUN_USER}...${NC}"
        su - "${RUN_USER}" -c "pip3 install --user ${pkg}" || {
            echo -e "${RED}Failed to install ${pkg}. Try: su - ${RUN_USER} -c 'pip3 install --user ${pkg}'${NC}"
            exit 1
        }
    fi
done

# Create log directory (user-owned, no root needed)
echo -e "${YELLOW}Creating log directory...${NC}"
mkdir -p "${LOG_DIR}"
chown "${RUN_USER}:${RUN_USER}" "${LOG_DIR}"
chmod 755 "${LOG_DIR}"

# Install service file
echo -e "${YELLOW}Installing service file...${NC}"
cp "${SERVICE_SOURCE}" "${SERVICE_DEST}"
chmod 644 "${SERVICE_DEST}"

# Replace placeholders in service file
CURRENT_DIR="$(pwd)"
sed -i "s|__RUN_USER__|${RUN_USER}|g" "${SERVICE_DEST}"
sed -i "s|__RUN_HOME__|${RUN_HOME}|g" "${SERVICE_DEST}"
sed -i "s|__WORKING_DIR__|${CURRENT_DIR}|g" "${SERVICE_DEST}"

# Reload systemd daemon
echo -e "${YELLOW}Reloading systemd daemon...${NC}"
systemctl daemon-reload

# Enable service to start on boot
echo -e "${YELLOW}Enabling service...${NC}"
systemctl enable "${SERVICE_NAME}.service"

# Start the service
echo -e "${YELLOW}Starting service...${NC}"
systemctl start "${SERVICE_NAME}.service"

# Wait a moment and check status
sleep 2
if systemctl is-active --quiet "${SERVICE_NAME}.service"; then
    echo -e "${GREEN}✓ ${SERVICE_NAME} service installed and started successfully${NC}"
    echo -e "${GREEN}  User: ${RUN_USER}${NC}"
    echo -e "${GREEN}  Log file: ${LOG_DIR}/${SERVICE_NAME}.log${NC}"
    echo -e "${GREEN}  Service file: ${SERVICE_DEST}${NC}"
else
    echo -e "${RED}✗ Service failed to start. Check logs with:${NC}"
    echo -e "  journalctl -u ${SERVICE_NAME} -f"
    echo -e "  tail -f ${LOG_DIR}/${SERVICE_NAME}.log"
    exit 1
fi

echo -e ""
echo -e "${YELLOW}Useful commands:${NC}"
echo -e "  Check status:     systemctl status ${SERVICE_NAME}"
echo -e "  View logs:        journalctl -u ${SERVICE_NAME} -f"
echo -e "  View app logs:    tail -f ${LOG_DIR}/${SERVICE_NAME}.log"
echo -e "  Stop service:     systemctl stop ${SERVICE_NAME}"
echo -e "  Restart service:  systemctl restart ${SERVICE_NAME}"
echo -e "  Disable service:  systemctl disable ${SERVICE_NAME}"
echo -e "  Uninstall:        systemctl stop ${SERVICE_NAME} && systemctl disable ${SERVICE_NAME} && rm ${SERVICE_DEST}"
