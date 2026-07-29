# Engineering Inventory Management System (EIMS)

## 📌 Project Overview
The Engineering Inventory Management System (EIMS) is a hardware and software solution designed to track the borrowing and returning of registered workshop tools across multiple cabinets in real time. Powered by an ESP32 microcontroller and an MFRC522 RFID scanner, the system logs physical tag scans and syncs them to a locally hosted web dashboard for seamless monitoring, administration, and history tracking.

## ✨ Key Features
*   **Dual Operation Modes:** Hardware switch to toggle between User Mode (borrow/return) and Admin Mode (registration/management).
*   **RFID Integration:** Fast and secure tool tracking and engineer login using MFRC522.
*   **Standalone Wi-Fi Access Point:** Hosts its own network and web dashboard—no external internet connection required.
*   **Real-time Web Dashboard:** Live UI updates, tool search, dynamic cabinet carousels, and bulk management.
*   **Automated Logging & Export:** Tracks all transactions and sessions, automatically groups them weekly, and allows CSV exports.
*   **Failsafe Local Storage:** Uses JSON file storage on the ESP32 flash memory with automatic `.bak` backups to prevent data corruption.
*   **Visual & Audio Feedback:** Integrated NeoPixel LED and Buzzer for scan confirmations and error alerts.

---

## 🛠️ Hardware Specifications & Pinout

This project is built using an **ESP32-S3** (or standard ESP32) with the following pin configurations:

| Component | ESP32 Pin |
| :--- | :--- |
| **MFRC522 SCK** | GPIO 14 |
| **MFRC522 MOSI** | GPIO 13 |
| **MFRC522 MISO** | GPIO 12 |
| **MFRC522 RST** | GPIO 9 |
| **MFRC522 CS** | GPIO 10 |
| **NeoPixel LED** | GPIO 38 |
| **RGB RED** | GPIO 17 |
| **RGB BLUE** | GPIO 15 |
| **RGB GREEN** | GPIO 7 |
| **Buzzer** | GPIO 6 |
| **Admin/User Switch** | GPIO 4 (Uses internal Pull-Up) |

---

## 🚀 Network & Access

Upon powering the ESP32, it will broadcast its own Wi-Fi network.
*   **Network SSID:** `EIMS - ESP32`
*   **Password:** `12345678`
*   **Dashboard Access:** Open a web browser and navigate to the IP address assigned by the ESP32 (typically `http://192.168.4.1`).
*   **Default Admin Credentials:** Username: `admin` | Password: `123`

---

## 📖 How to Use

### 1. User Mode (Borrowing & Returning)
Ensure the physical hardware switch is flipped to **User Mode**.
*   **Login:** Tap a registered Engineer ID card on the scanner to start a session. 
*   **Borrow/Return:** Tap an available tool tag to borrow it. Tap a borrowed tool tag again to return it.
*   **Transfer:** If another engineer needs a tool you currently have, they can tap their ID card to take over the session, then tap the tool to transfer it into their name.
*   **Logout:** Tap your Engineer ID card again, or wait for the automatic inactivity timeout (default is 10 seconds).

### 2. Admin Mode (Registration & Management)
Flip the physical hardware switch to **Admin Mode** and log in to the web dashboard.
*   **Registration:** Enter the new Tool or Engineer details in the dashboard and click "Add". The system will enter standby mode. Tap a blank RFID tag on the physical scanner to burn the data and complete the registration.
*   **Bulk Management:** Use the dashboard to select multiple tools to move them between cabinets, change their storage layers, mark them inactive, or delete them.
*   **History & Reports:** Navigate to the "History" tab to view recent transactions and download complete weekly CSV logs for both tools and user sessions.

---

## ⚙️ System Settings & Data Management
The web dashboard includes a settings menu that allows the administrator to:
*   Update admin login credentials.
*   Adjust inactivity timeout durations for both users and admins.
*   Toggle physical buzzer feedback on or off.
*   Download full database backups or restore the system from a previous JSON backup file.
*   Purge old history logs to free up flash storage or perform a complete Factory Reset.S
